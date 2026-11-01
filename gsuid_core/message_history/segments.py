"""把 Message 段序列化成前端可直接渲染的段数组。

控制台「会话管理」要像 QQ 客户端那样还原一条消息：文字、@、引用、图片、语音、视频、
文件、表情、合并转发、markdown、按钮、ark 卡片各归各位。HistoryManager 存的 content
是给 AI 看的纯文本，不足以还原一条消息。

段数组属于「原始数据」：只随 `coremessagecache` 落库给控制台看，**不进内存里的
`MessageRecord`**。内存那份是给 AI 的，AI 只读 content，留着纯属白占。

约定（与前端 `src/lib/sessionChat/index.ts` 的 `MsgSeg` 一致）：

    {"type": "text",     "text": "..."}
    {"type": "at",       "qq": "123", "name": ""}
    {"type": "reply",    "id": "..."}
    {"type": "image",    "url": "...", "name": "", "error": "图片已过期"}
    {"type": "record" | "video", "url": "...", "error": "..."}
    {"type": "file",     "url": "...", "name": "", "error": "..."}
    {"type": "face",     "id": "..."}
    {"type": "forward",  "count": 2, "items": [[seg, ...], ...]}
    {"type": "markdown", "content": "...", "template": {...}, "raw": "..."}
    {"type": "button",   "rows": [[btn, ...]], "raw": "..."}
    {"type": "ark",      "templateId": 0, "kv": [{"key": "", "value": ""}], "raw": "..."}
    {"type": "unknown",  "kind": "...", "text": "[kind]"}

`url` 拿不到时才会出现 `error`，前端优先显示它而不是通用的「加载失败」。

媒体一律不留内容：能拿到 http(s) 直链就用直链，本地路径原样交给前端显示，
base64 只留类型占位 —— 控制台知道「发过这个」就够了。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Protocol

from gsuid_core.models import Event, Message

# 合并转发最多展开几层；再深就只留计数，免得一条消息把内存撑爆
_MAX_NODE_DEPTH = 3
# 合并转发单层最多展开多少条子消息
_MAX_NODE_ITEMS = 100

# 只有 record / video 会用它拼「已过期」文案，别的类型走自己的分支
_MEDIA_LABEL = {"record": "[语音]", "video": "[视频]"}

# markdown 段里的图片语法；只对真正的 markdown 段用，普通文本不解析
_INLINE_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(([^)\n]+)\)")


def _md_plain(text: str) -> str:
    """markdown 去语法，给会话列表预览用。"""
    text = _INLINE_IMAGE.sub("", text)
    text = re.sub(r"\[([^\]\n]+)\]\([^)\n]*\)", r"\1", text)
    text = re.sub(r"^[ \t]*>[ \t]?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]+", "", text, flags=re.MULTILINE)
    return re.sub(r"[`*_~]+", "", text)


def _text_seg(text: str) -> Dict[str, Any]:
    """普通文本原样保留，不当 markdown 解析。"""
    return {"type": "text", "text": text}


def _strip_link(data: str) -> str:
    """`link://http://...` 是 MessageSegment.image 对直链的包装，取回真身。"""
    if data.startswith("link://"):
        return data[len("link://") :]
    return data


def _media_ref(data: Any) -> str:
    """媒体段能直接用的地址：只认 http(s) 直链和 data:image。

    本地路径、base64 一律不转换、不读盘、不进内存 —— 控制台拿不到就不显示，
    由调用方把原路径或类型占位交给前端。
    """
    if not isinstance(data, str):
        return ""
    raw = _strip_link(data.strip())
    if raw.startswith(("http://", "https://", "data:image/")):
        return raw
    return ""


def _image_seg(data: Any) -> Dict[str, Any]:
    """图片段。直链直接用；本地路径原样显示路径文本，内容不进内存。"""
    url = _media_ref(data)
    if url:
        return {"type": "image", "url": url}
    # 非直链：本地路径原样交给前端显示；base64 太长，只留占位
    if isinstance(data, str):
        raw = _strip_link(data.strip())
        if raw and not raw.startswith("base64://"):
            return {"type": "image", "url": "", "name": raw}
    return {"type": "image", "url": "", "error": "图片已过期"}


def _file_seg(data: Any) -> Dict[str, Any]:
    """文件段的数据是 `文件名|link://url` 或 `文件名|base64`。

    只留文件名和直链。收到的文件本来就是 url，那份 base64 内容没有保留价值，
    不进内存 —— 前端看到文件名就知道「发过这个文件」。
    """
    name = ""
    payload = data
    if isinstance(data, str) and "|" in data:
        name, payload = data.split("|", 1)
    return {"type": "file", "name": name, "url": _media_ref(payload)}


def _buttons_seg(data: Any) -> Dict[str, Any]:
    """按钮段：data 可能是单层列表，也可能是按行分组的嵌套列表。"""
    rows: List[List[Dict[str, Any]]] = []
    source = data if isinstance(data, list) else []
    for row in source:
        if isinstance(row, list):
            rows.append([_one_button(btn) for btn in row])
        else:
            rows.append([_one_button(row)])
    return {"type": "button", "rows": rows, "raw": _json_preview(data)}


_BUTTON_STR_KEYS = (
    "text",
    "data",
    "callback",
    "input",
    "link",
    "prefix",
    "pressed_text",
    "content",
    "confirm_text",
    "cancel_text",
    "type",
)
# style / action / permission 都是整数，别只收字符串 —— 丢了 style 前端就只能一种颜色
_BUTTON_INT_KEYS = ("style", "action", "permission", "permisson")


def _one_button(btn: Any) -> Dict[str, Any]:
    if not isinstance(btn, dict):
        return {"text": str(btn)}
    out: Dict[str, Any] = {}
    for key in _BUTTON_STR_KEYS:
        value = btn.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    for key in _BUTTON_INT_KEYS:
        value = btn.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            # 模型里那个字段名少个 s（permisson），对外统一成 permission
            out["permission" if key == "permisson" else key] = int(value)
        except (TypeError, ValueError):
            continue
    if "limited" in btn:
        out["limited"] = bool(btn["limited"])
    if btn.get("enter") is True:
        out["enter"] = True
    return out


def _at_seg(data: Any) -> Dict[str, Any]:
    """at 段。id 原样保留（官机是裸 openid，其他平台是 QQ 号）。"""
    return {"type": "at", "qq": "" if data is None else str(data).strip(), "name": ""}


def _ark_seg(data: Any) -> Dict[str, Any]:
    """ark / card 卡片：尽力拆出模板 id 与键值对，拆不出就只留原文。"""
    seg: Dict[str, Any] = {"type": "ark", "raw": _json_preview(data)}
    if isinstance(data, dict):
        template_id = data.get("template_id") or data.get("templateId")
        if isinstance(template_id, int):
            seg["templateId"] = template_id
        elif isinstance(template_id, str) and template_id.isdigit():
            seg["templateId"] = int(template_id)
        kv_source = data.get("kv") or data.get("fields")
        # 图文卡片（tuwen）是 `{type, content}`，字段都在 content 里
        if kv_source is None and isinstance(data.get("content"), dict):
            kv_source = data["content"]
        if isinstance(kv_source, dict):
            kv_source = [{"key": key, "value": value} for key, value in kv_source.items()]
        if isinstance(kv_source, list):
            kv: List[Dict[str, str]] = []
            for item in kv_source:
                if isinstance(item, dict) and "key" in item:
                    kv.append({"key": str(item["key"]), "value": str(item.get("value", ""))})
            if kv:
                seg["kv"] = kv
        if isinstance(data.get("type"), str):
            seg["kind"] = data["type"]
    return seg


def _json_preview(data: Any) -> str:
    import json

    try:
        text = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)
    return text[:4000]


def _node_seg(messages: List[Message], depth: int) -> Dict[str, Any]:
    items: List[List[Dict[str, Any]]] = []
    for item in messages[:_MAX_NODE_ITEMS]:
        items.append(segments_from_messages([item], depth=depth + 1))
    return {"type": "forward", "count": len(messages), "items": items}


def segments_from_messages(messages: Optional[List[Message]], *, depth: int = 0) -> List[Dict[str, Any]]:
    """把一段 Message 列表压成前端段数组。深度超过上限时转发只留计数。"""
    result: List[Dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, Message):
            if isinstance(msg, str) and msg:
                result.append({"type": "text", "text": msg})
            continue
        msg_type = msg.type or "unknown"
        data = msg.data

        if msg_type == "text":
            if data:
                result.append(_text_seg(str(data)))
        elif msg_type == "at":
            result.append(_at_seg(data))
        elif msg_type == "reply":
            result.append({"type": "reply", "id": "" if data is None else str(data)})
        elif msg_type == "image":
            result.append(_image_seg(data))
        elif msg_type in ("record", "video"):
            url = _media_ref(data)
            seg = {"type": msg_type, "url": url}
            if not url and data is None:
                seg["error"] = f"{_MEDIA_LABEL[msg_type]}已过期"
            result.append(seg)
        elif msg_type == "file":
            result.append(_file_seg(data))
        elif msg_type == "face":
            result.append({"type": "face", "id": "" if data is None else str(data)})
        elif msg_type == "node":
            if depth >= _MAX_NODE_DEPTH:
                count = len(data) if isinstance(data, list) else 0
                result.append({"type": "forward", "count": count, "items": []})
            elif isinstance(data, list):
                result.append(_node_seg(data, depth))
        elif msg_type == "markdown":
            content = data.get("content") if isinstance(data, dict) else data
            result.append({"type": "markdown", "content": content if isinstance(content, str) else ""})
        elif msg_type == "template_markdown":
            seg = {"type": "markdown", "template": data if isinstance(data, dict) else {}, "raw": _json_preview(data)}
            result.append(seg)
        elif msg_type in ("buttons", "template_buttons"):
            if msg_type == "template_buttons":
                result.append({"type": "button", "rows": [], "raw": _json_preview(data)})
            else:
                result.append(_buttons_seg(data))
        elif msg_type in ("ark", "card"):
            result.append(_ark_seg(data))
        elif msg_type == "image_size":
            # 只影响出图排版，不该占一条可见消息
            continue
        elif msg_type == "group":
            # 出站帧尾的目标群号，纯协议用
            continue
        else:
            result.append({"type": "unknown", "kind": msg_type, "text": f"[{msg_type}]"})
    return result


def segments_from_event(event: Event) -> List[Dict[str, Any]]:
    """入站消息的段数组。优先用 event.content，缺失时按字段兜底。"""
    segments = segments_from_messages(event.content)
    if segments:
        return segments

    if event.raw_text:
        segments.append({"type": "text", "text": event.raw_text})
    for at in event.at_list or []:
        segments.append(_at_seg(at))
    if event.image:
        segments.append(_image_seg(event.image))
    return segments


class RecordLike(Protocol):
    @property
    def role(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def user_id(self) -> Optional[str]: ...

    @property
    def user_name(self) -> Optional[str]: ...

    @property
    def user_avatar(self) -> Optional[str]: ...

    @property
    def timestamp(self) -> float: ...

    @property
    def metadata(self) -> Dict[str, Any]: ...


def chat_message_from_record(record: RecordLike, segments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """MessageRecord → 控制台消息对象。

    段数组是「原始数据」，只进 `coremessagecache` 给人看，不进内存里的 `MessageRecord`。
    所以这里优先用调用方递进来的那一份（刚处理完的消息手上就有），拿不到再按 content
    兜底成一条文本段。
    """
    meta = record.metadata if isinstance(record.metadata, dict) else {}
    if not isinstance(segments, list) or not segments:
        content = str(record.content or "")
        segments = [{"type": "text", "text": content}] if content else []
    sender = meta.get("sender")
    sender_dict = sender if isinstance(sender, dict) else {}
    role = record.role or "user"
    user_id = record.user_id or ""
    # 机器人自己发的那条，record.user_id 存的是提问的人，换回 bot 自己
    if role == "assistant" and sender_dict.get("user_id"):
        user_id = str(sender_dict["user_id"])
    return {
        "role": role,
        "user_id": user_id,
        "user_name": record.user_name,
        "user_avatar": record.user_avatar,
        "timestamp": record.timestamp or 0.0,
        "content": record.content or "",
        "segments": segments,
        "sender": sender_dict,
        "msg_id": str(meta.get("msg_id") or ""),
    }


def segments_preview(segments: List[Dict[str, Any]], limit: int = 80) -> str:
    """段数组压成一行摘要，给会话列表用。"""
    parts: List[str] = []
    for seg in segments:
        kind = seg.get("type")
        if kind == "text":
            parts.append(str(seg.get("text", "")))
        elif kind == "at":
            parts.append(f"@{seg.get('name') or seg.get('qq') or ''}")
        elif kind == "reply":
            parts.append("[引用]")
        elif kind == "image":
            parts.append("[图片]")
        elif kind == "record":
            parts.append("[语音]")
        elif kind == "video":
            parts.append("[视频]")
        elif kind == "file":
            parts.append("[文件]")
        elif kind == "face":
            parts.append("[表情]")
        elif kind == "forward":
            parts.append("[合并转发]")
        elif kind == "markdown":
            parts.append(_md_plain(str(seg.get("content") or "")) or "[Markdown]")
        elif kind == "button":
            parts.append("[按钮]")
        elif kind == "ark":
            parts.append("[卡片]")
    text = " ".join(part for part in parts if part).strip()
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text
