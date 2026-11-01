"""会话管理（消息记录）API。

控制台的「会话管理」页要像 QQ 客户端那样看消息：按段渲染、能实时收到新消息、能引用 / @ /
置顶 / 备注 / 拉黑。这里只做 HTTP + WS 的搬运，数据仍由 ``message_history`` 提供：

- 列表与消息默认来自 ``HistoryManager`` 的内存窗口（12 小时）；
  开了「消息缓存」后还会读 ``coremessagecache`` 表，补上重启后丢掉的那段；
- 置顶 / 备注是面板自己的状态，落在 ``data/webconsole_chat_prefs.json``；
  拉黑直接写框架级全局黑名单 ``sp_config.BlackList``（与「拉黑」命令同源），真的会拦消息；
- 新消息由 ``message_history.hub`` 推给 WS 订阅者，前端不用轮询。

路由统一挂在 ``/api/history/chat`` 下，避开 ``/api/history/{session_id}`` 的单段匹配。
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, Set, Dict, List, Tuple, Optional, Sequence

from fastapi import Body, File, Form, Depends, WebSocket, UploadFile, WebSocketDisconnect

from gsuid_core.models import Event
from gsuid_core.message_history import segments_preview, get_history_manager
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, livechat_ws_authorized
from gsuid_core.message_history.hub import get_history_hub
from gsuid_core.message_history.cache import (
    cache_size,
    cache_hours,
    cache_enabled,
    purge_expired,
    query_messages,
    query_sessions,
)
from gsuid_core.webconsole.chat_prefs import (
    set_note,
    snapshot,
    set_pinned,
)
from gsuid_core.webconsole.history_api import _parse_session_id
from gsuid_core.message_history.segments import RecordLike, chat_message_from_record

from ._api_tags import SESSION_CHAT

# WS 心跳间隔：没有新消息时也发一帧，既保活也顺便发现掉线的客户端
_WS_PING_SECONDS = 25.0
# 一次最多回多少条历史，够翻完 12 小时窗口
_MESSAGE_LIMIT_MAX = 500


def _fail(msg: str) -> Dict[str, Any]:
    return {"status": 1, "msg": msg, "data": None}


def _ok(data: Any) -> Dict[str, Any]:
    return {"status": 0, "msg": "ok", "data": data}


_SEND_ERROR_PREFIX = "__send_error__:"


def _send_error(receipts: Optional[List[str]], bot: Any) -> str:
    """从 ``wait_recall`` 回执里读发送结果，空串表示成功。"""
    if receipts is None:
        return ""
    for item in receipts:
        text = str(item or "")
        if text.startswith(_SEND_ERROR_PREFIX):
            return text[len(_SEND_ERROR_PREFIX) :] or "发送失败"
    if receipts:
        return ""
    if getattr(bot, "_supports_recall", None) is False:
        return ""
    return "适配器没有返回回执，消息可能没有发出去"


def _event_of(
    ws_bot_id: str,
    bot_id: str,
    bot_self_id: str,
    group_id: Optional[str],
    user_id: str,
) -> Event:
    return Event(
        bot_id=bot_id,
        real_bot_id=bot_id,
        bot_self_id=bot_self_id,
        user_id=user_id,
        group_id=group_id,
        user_type="group" if group_id else "direct",
        WS_BOT_ID=ws_bot_id,
    )


def _resolve(session_id: str) -> Optional[Event]:
    """session_id → Event。格式非法返回 None。"""
    parsed = _parse_session_id(session_id)
    if parsed is None:
        return None
    ws_bot_id, bot_id, bot_self_id, group_id, user_id = parsed
    return _event_of(ws_bot_id, bot_id, bot_self_id, group_id, user_id)


async def _group_meta(group_id: str) -> Dict[str, Any]:
    """群名 / 群头像 / 群人数。库里没有就留空，前端自己退化成群号。"""
    try:
        from gsuid_core.utils.database.models import CoreGroup

        row = await CoreGroup.base_select_data(group_id=group_id)
    except Exception:
        return {}
    if row is None:
        return {}

    def _pick(value: Any) -> str:
        text = str(value or "").strip()
        return "" if text in ("", "1") else text

    try:
        count = int(row.group_count or 0)
    except (TypeError, ValueError):
        count = 0

    return {
        "group_name": _pick(row.group_name),
        "group_icon": _pick(row.group_icon),
        "group_count": count,
    }


async def _user_meta(user_id: str, bot_id: str) -> Dict[str, str]:
    """早柚用户库里的昵称 / 头像。给 @ 补昵称、会话列表补标题读的都是这一份。"""
    query: Dict[str, str] = {"user_id": user_id}
    if bot_id:
        query["bot_id"] = bot_id
    try:
        from gsuid_core.utils.database.models import CoreUser

        row = await CoreUser.base_select_data(**query)
    except Exception:
        return {}
    if row is None:
        return {}

    def _pick(value: Any) -> str:
        text = str(value or "").strip()
        return "" if text in ("", "1") else text

    return {
        "user_name": _pick(row.user_name),
        "user_icon": _pick(row.user_icon),
    }


def _last_sender(records: Sequence[RecordLike], user_id: str = "", need: str = "") -> Dict[str, str]:
    """倒着找最近一条 sender 快照；给了 user_id 就只认这个人，need 要求带上某个键。"""
    for record in reversed(records):
        meta = record.metadata if isinstance(record.metadata, dict) else {}
        sender = meta.get("sender")
        if not isinstance(sender, dict):
            continue
        if user_id and str(sender.get("user_id") or "") != str(user_id):
            continue
        if need and not sender.get(need):
            continue
        return {str(k): str(v) for k, v in sender.items()}
    return {}


async def _cached_sender(session_id: str, user_id: str = "") -> Dict[str, str]:
    """内存历史里没有就翻消息缓存。

    消息只在内存里留 12 小时，进程一重启就空了；缓存里存了完整的 sender 快照，
    否则刚重启那会儿点开头像只能看到一串 ID。
    """
    from gsuid_core.message_history.cache import CoreMessageCache

    try:
        rows = await CoreMessageCache.select_recent(session_id, 50)
    except Exception:
        return {}
    for row in reversed(rows):
        try:
            sender = json.loads(row.sender or "{}")
        except ValueError:
            continue
        if not isinstance(sender, dict):
            continue
        if user_id and str(sender.get("user_id") or "") != str(user_id):
            continue
        return {str(k): str(v) for k, v in sender.items()}
    return {}


# ---------------------------------------------------------------------------
# @ 昵称
# ---------------------------------------------------------------------------


async def _fill_at_names(messages: List[Dict[str, Any]], bot_id: str) -> None:
    """给 at 段补昵称。"""
    candidates: List[str] = []
    for item in messages:
        for seg in item.get("segments") or []:
            if seg.get("type") != "at" or seg.get("name"):
                continue
            value = str(seg.get("qq") or "")
            if value and value not in candidates:
                candidates.append(value)
    if not candidates:
        return

    from gsuid_core.utils.database.models import CoreUser

    try:
        names = await CoreUser.get_names_by_ids(candidates[:200], bot_id)
    except Exception:
        return
    if not names:
        return

    for item in messages:
        for seg in item.get("segments") or []:
            if seg.get("type") != "at" or seg.get("name"):
                continue
            value = str(seg.get("qq") or "")
            if value in names:
                seg["name"] = names[value]


# ---------------------------------------------------------------------------
# 全局黑名单
# ---------------------------------------------------------------------------


def _blacklist_ids() -> List[str]:
    """框架级全局黑名单，和「拉黑」命令读的是同一份 ``sp_config.BlackList``。"""
    try:
        from gsuid_core.utils.plugins_config.gs_config import sp_config

        return [str(item) for item in sp_config.get_config("BlackList").data]
    except Exception:
        return []


def _is_blacklisted(target_id: str) -> bool:
    return bool(target_id) and target_id in _blacklist_ids()


def _set_blacklisted(target_id: str, on: bool) -> None:
    from gsuid_core.utils.plugins_config.gs_config import sp_config

    current = _blacklist_ids()
    if on:
        if target_id in current:
            return
        current.append(target_id)
    else:
        if target_id not in current:
            return
        current = [item for item in current if item != target_id]
    sp_config.set_config("BlackList", current)


# ---------------------------------------------------------------------------
# 会话列表
# ---------------------------------------------------------------------------


async def _title_avatar(
    is_group: bool,
    group_id: Any,
    user_id: Any,
    sender: Dict[str, str],
) -> Tuple[str, str]:
    """会话标题与头像。"""
    if is_group:
        name = str(sender.get("group_name") or "")
        icon = str(sender.get("group_icon") or "")
        if name:
            return name, icon
        meta = await _group_meta(str(group_id))
        return meta.get("group_name", "") or str(group_id), icon or meta.get("group_icon", "")

    return (
        str(sender.get("card") or sender.get("nickname") or user_id or "未知"),
        str(sender.get("avatar") or ""),
    )


async def _session_item(
    session_id: str,
    parsed: Tuple[str, str, str, Optional[str], str],
    *,
    message_count: int,
    last_segments: List[Any],
    last_content: str = "",
    last_role: str,
    last_time: float,
    last_access: Any,
    created_at: Any,
    sender: Dict[str, str],
    prefs: Dict[str, Any],
    blacklist: Set[str],
    from_cache: bool = False,
) -> Dict[str, Any]:
    """拼一条会话列表项。内存与缓存两条来源共用，避免两份字段各写一遍。"""
    ws_bot_id, bot_id, bot_self_id, group_id, user_id = parsed
    is_group = bool(group_id)
    title, avatar = await _title_avatar(is_group, group_id, user_id, sender)
    item: Dict[str, Any] = {
        "session_id": session_id,
        "type": "group" if is_group else "private",
        "WS_BOT_ID": ws_bot_id,
        "bot_id": bot_id,
        "bot_self_id": bot_self_id,
        "group_id": group_id,
        "user_id": user_id,
        "title": title,
        "avatar": avatar,
        "message_count": message_count,
        "last_access": last_access,
        "created_at": created_at,
        "last_preview": segments_preview(last_segments) if last_segments else last_content,
        "last_role": last_role,
        "last_time": last_time,
        "pinned": session_id in prefs["pins"],
        "blacklisted": (str(group_id) if is_group else str(user_id)) in blacklist,
        "note": prefs["notes"].get(session_id, ""),
    }
    if from_cache:
        item["from_cache"] = True
    return item


@app.get("/api/history/chat/sessions", summary="会话列表（带置顶 / 备注 / 最后一条）", tags=SESSION_CHAT)
async def list_chat_sessions(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """会话管理页左侧列表。"""
    try:
        manager = get_history_manager()
        prefs = snapshot()
        # 全局黑名单一次取全，别在循环里反复读配置
        blacklist = set(_blacklist_ids())
        all_sessions = manager.get_all_sessions_info()

        sessions: List[Dict[str, Any]] = []
        for session_id, info in all_sessions.items():
            group_id = info.get("group_id")
            user_id = info.get("user_id") or ""
            parsed = (
                str(info.get("WS_BOT_ID") or ""),
                str(info.get("bot_id") or ""),
                str(info.get("bot_self_id") or ""),
                group_id,
                user_id,
            )
            ev = _event_of(parsed[0], parsed[1], parsed[2], group_id, user_id)
            records = manager.get_history(ev)
            last = records[-1] if records else None
            last_content = str(last.content or "") if last is not None else ""
            sender = _last_sender(records, "" if group_id else user_id)
            if group_id and not sender.get("group_name"):
                named = _last_sender(records, "", "group_name")
                if named:
                    sender = {**sender, "group_name": named["group_name"]}

            sessions.append(
                await _session_item(
                    session_id,
                    parsed,
                    message_count=len(records),
                    last_segments=[],
                    last_content=last_content,
                    last_role=last.role if last is not None else "",
                    last_time=last.timestamp if last is not None else 0.0,
                    last_access=info.get("last_access"),
                    created_at=info.get("created_at"),
                    sender=sender,
                    prefs=prefs,
                    blacklist=blacklist,
                )
            )

        if cache_enabled():
            cached_sessions = await query_sessions()
            known = {item["session_id"] for item in sessions}
            for session_id, cached in cached_sessions.items():
                if session_id in known:
                    continue
                parsed = _parse_session_id(session_id)
                if parsed is None:
                    continue
                message = cached["row"].to_chat_message()
                timestamp = message.get("timestamp", 0.0)
                sessions.append(
                    await _session_item(
                        session_id,
                        parsed,
                        message_count=cached["count"],
                        last_segments=message.get("segments") or [],
                        last_role=message.get("role", ""),
                        last_time=timestamp,
                        last_access=timestamp,
                        created_at=timestamp,
                        sender=message.get("sender") or {},
                        prefs=prefs,
                        blacklist=blacklist,
                        from_cache=True,
                    )
                )

        sessions.sort(
            key=lambda item: (1 if item["pinned"] else 0, item["last_time"] or 0.0),
            reverse=True,
        )
        return _ok({"sessions": sessions, "count": len(sessions)})
    except Exception as e:
        return _fail(f"获取会话列表失败: {e}")


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


@app.get("/api/history/chat/{session_id}/messages", summary="会话消息（按段返回）", tags=SESSION_CHAT)
async def get_chat_messages(
    session_id: str,
    limit: int = _MESSAGE_LIMIT_MAX,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """返回会话内最近的 N 条消息，每条带可渲染的 ``segments``。"""
    ev = _resolve(session_id)
    if ev is None:
        return _fail("无效的 session_id 格式")
    try:
        capped = max(1, min(int(limit or _MESSAGE_LIMIT_MAX), _MESSAGE_LIMIT_MAX))
        records = get_history_manager().get_history(ev)
        # 内存里没有段数组（原始数据只落库），所以只能按纯文本还原
        messages: List[Dict[str, Any]] = [chat_message_from_record(record) for record in records[-capped:]]

        # 缓存开着就以库为准：库里是完整段数组，内存那份只补还没落库的
        from_cache = 0
        if cache_enabled():
            cached = await query_messages(session_id, capped)
            if cached:
                seen = {(item["role"], round(item["timestamp"], 3), item["content"]) for item in cached}
                extra = [
                    item
                    for item in messages
                    if (item["role"], round(item["timestamp"], 3), item["content"]) not in seen
                ]
                from_cache = len(cached)
                messages = sorted(cached + extra, key=lambda item: item["timestamp"])[-capped:]

        # 官机的 at 只有 openid，用 coreuser 里的昵称补上（查不到就留空，前端显示 id）
        await _fill_at_names(messages, str(ev.bot_id or ""))

        for item in messages:
            item["self"] = item["role"] == "assistant"
        return _ok(
            {
                "session_id": session_id,
                "messages": messages,
                "count": len(messages),
                "from_cache": from_cache,
            }
        )
    except Exception as e:
        return _fail(f"获取消息失败: {e}")


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------


# 图文卡片（tuwen）
_CARD_FIELDS = ("title", "description", "pic_url", "url")


def _card_segments(raw: str) -> List[Any]:
    """Card 段：前端传 ``{type, content}``，``type`` 缺省 tuwen。"""
    from gsuid_core.segment import MessageSegment

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Card 内容必须是一个 JSON 对象")
    raw_content = payload.get("content")
    if not isinstance(raw_content, dict) or not raw_content:
        raise ValueError("Card 至少要填一个字段")

    content: Dict[str, str] = {}
    for key, value in raw_content.items():
        name = str(key).strip()
        text = str(value or "").strip()
        if name in _CARD_FIELDS and text:
            content[name] = text
    if not content:
        raise ValueError("Card 至少要填一个字段")

    card_type = str(payload.get("type") or "tuwen")
    return MessageSegment.card(content, card_type)


@app.post("/api/history/chat/{session_id}/send", summary="向会话发送消息", tags=SESSION_CHAT)
async def send_chat_message(
    session_id: str,
    mode: str = Form("plain"),
    text: str = Form(""),
    markdown: str = Form(""),
    card: str = Form(""),
    at: str = Form(""),
    reply: str = Form(""),
    images: List[UploadFile] = File(default=[]),
    records: List[UploadFile] = File(default=[]),
    videos: List[UploadFile] = File(default=[]),
    files: List[UploadFile] = File(default=[]),
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """四种输入方式（对应面板上的四个页签）：

    - ``plain``：普通文本，可带 @ / 引用 / 图片
    - ``markdown``：markdown 正文，平台不支持时适配器自己会降级
    - ``card``：图文卡片（tuwen），传 ``{type, content}``
    - ``rich``：富媒体，图片 / 语音 / 视频 / 文件上传

    引用（``reply``）放在最前面，@ 放在正文之后，跟 QQ 客户端的段顺序一致。
    """
    ev = _resolve(session_id)
    if ev is None:
        return _fail("无效的 session_id 格式")

    from gsuid_core.bot import Bot
    from gsuid_core.gss import gss
    from gsuid_core.segment import MessageSegment

    target_id = ev.group_id if ev.group_id else ev.user_id
    if not target_id:
        return _fail("session_id 中缺少有效的发送目标")

    ws_bot_id = str(ev.WS_BOT_ID or "")
    if not gss.active_bot:
        return _fail("当前没有任何已连接的 Bot")
    ws_bot = gss.active_bot.get(ws_bot_id)
    if ws_bot is None:
        return _fail(f"Bot WS '{ws_bot_id}' 当前未连接，无法发送消息")

    segments: List[Any] = []
    try:
        if reply.strip():
            from gsuid_core.models import Message

            segments.append(Message(type="reply", data=reply.strip()))

        if mode == "card":
            if not card.strip():
                return _fail("Card 内容不能为空")
            segments.extend(_card_segments(card))
        elif mode == "markdown":
            if markdown.strip():
                segments.extend(MessageSegment.markdown(markdown))
        elif text.strip():
            segments.append(MessageSegment.text(text))

        for uid in [item.strip() for item in at.split(",") if item.strip()]:
            segments.append(MessageSegment.at(uid))

        for upload in images:
            raw = await upload.read()
            if raw:
                segments.append(MessageSegment.image(raw))
        for upload in records:
            raw = await upload.read()
            if raw:
                segments.append(MessageSegment.record(raw))
        for upload in videos:
            raw = await upload.read()
            if raw:
                segments.append(MessageSegment.video(raw))
        for upload in files:
            raw = await upload.read()
            if raw:
                segments.append(MessageSegment.file(raw, upload.filename or "file"))
    except ValueError as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(f"组装消息失败: {e}")

    if not segments:
        return _fail("消息内容不能为空")

    try:
        # 旧的空 bot_self_id 会话并到真实会话，避免发完出现两个 session
        get_history_manager().merge_session(
            _event_of(
                ws_bot_id,
                ev.bot_id,
                "",
                ev.group_id,
                ev.user_id,
            ),
            ev,
        )
        receipts = await Bot(ws_bot, ev).send(segments, wait_recall=True)
        error = _send_error(receipts, ws_bot)
        if error:
            return _fail(f"发送消息失败: {error}")
        return _ok({"session_id": session_id, "segments": len(segments)})
    except Exception as e:
        return _fail(f"发送消息失败: {e}")


# ---------------------------------------------------------------------------
# 会话偏好：置顶 / 备注 / 拉黑
# ---------------------------------------------------------------------------


@app.post("/api/history/chat/{session_id}/prefs", summary="置顶 / 备注 / 拉黑", tags=SESSION_CHAT)
async def update_chat_prefs(
    session_id: str,
    payload: Dict[str, Any] = Body(default={}),
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """三个开关可以一次改，也可以只改一个。

    请求体：``{"pinned": true, "note": "自家小群", "blacklisted": false}``。
    ``note`` 传空串就是清除备注。

    ``blacklisted`` 写的是**框架级全局黑名单**（``sp_config.BlackList``，与「拉黑」命令同源）：
    群聊记群号、私聊记 QQ 号，命中后该会话不再触发任何命令 / AI / 插件。
    """
    body = payload or {}
    ev = _resolve(session_id)
    if ev is None:
        return _fail("无效的 session_id 格式")

    # 黑名单存的是群号 / QQ 号，不带 session 前缀，这样才能和命令侧对上
    target = str(ev.group_id or ev.user_id or "")
    try:
        if "pinned" in body:
            set_pinned(session_id, bool(body["pinned"]))
        if "note" in body:
            set_note(session_id, body["note"])
        if "blacklisted" in body:
            if not target:
                return _fail("session_id 中缺少群号 / QQ 号，无法拉黑")
            _set_blacklisted(target, bool(body["blacklisted"]))
    except Exception as e:
        return _fail(f"保存会话偏好失败: {e}")

    prefs = snapshot()
    return _ok(
        {
            "session_id": session_id,
            "pinned": session_id in prefs["pins"],
            "blacklisted": _is_blacklisted(target),
            "note": prefs["notes"].get(session_id, ""),
        }
    )


# ---------------------------------------------------------------------------
# 群 / 用户信息卡
# ---------------------------------------------------------------------------


@app.get("/api/history/chat/{session_id}/info", summary="群 / 用户信息卡", tags=SESSION_CHAT)
async def get_chat_target_info(
    session_id: str,
    user_id: str = "",
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """点头像弹出的那张卡。

    群聊返回群名 / 人数 / 群号；私聊返回昵称 / 群名片 / 群身份 / openid / 账号 / unionid。
    昵称头像优先用消息里的 sender 快照，没有就查早柚用户库（和 @ 补昵称同一份数据）。
    """
    ev = _resolve(session_id)
    if ev is None:
        return _fail("无效的 session_id 格式")

    try:
        manager = get_history_manager()
        records = manager.get_history(ev)
        bot_id = str(ev.bot_id or "")

        if ev.group_id:
            meta = await _group_meta(str(ev.group_id))
            return _ok(
                {
                    "type": "group",
                    "key": session_id,
                    "id": ev.group_id,
                    "name": str(meta.get("group_name") or ev.group_id),
                    "avatar": str(meta.get("group_icon") or ""),
                    "member_count": meta.get("group_count") or 0,
                }
            )

        target = user_id or ev.user_id
        sender = _last_sender(records, target) or await _cached_sender(session_id, target)
        meta = await _user_meta(target, bot_id)
        return _ok(
            {
                "type": "user",
                "key": session_id,
                "id": target,
                "name": str(sender.get("card") or sender.get("nickname") or meta.get("user_name") or target),
                "avatar": str(sender.get("avatar") or meta.get("user_icon") or ""),
                "card": str(sender.get("card") or ""),
                "role": str(sender.get("role") or ""),
                "title": str(sender.get("title") or ""),
                "openid": str(sender.get("openid") or ""),
                "unionid": str(sender.get("unionid") or ""),
            }
        )
    except Exception as e:
        return _fail(f"获取信息失败: {e}")


# ---------------------------------------------------------------------------
# 消息缓存配置
# ---------------------------------------------------------------------------


def _apply_cache_hours(hours: int) -> Dict[str, Any]:
    from gsuid_core.utils.plugins_config.gs_config import message_cache_config
    from gsuid_core.utils.plugins_config.message_cache_config import MAX_CACHE_HOURS

    value = max(0, min(int(hours), MAX_CACHE_HOURS))
    message_cache_config.set_config("CacheHours", value)
    return {"hours": value, "enabled": value > 0}


@app.get("/api/history/chat/cache", summary="消息缓存配置与占用", tags=SESSION_CHAT)
async def get_chat_cache_config(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """当前缓存时长（小时）、是否开启、库里已存多少条。

    0 表示不缓存。数据表是 ``coremessagecache``，只在开启后才会有行。
    """
    try:
        return _ok(
            {
                "hours": cache_hours(),
                "enabled": cache_enabled(),
                "count": await cache_size(),
            }
        )
    except Exception as e:
        return _fail(f"获取缓存配置失败: {e}")


@app.post("/api/history/chat/cache", summary="设置消息缓存时长", tags=SESSION_CHAT)
async def set_chat_cache_config(
    payload: Dict[str, Any] = Body(default={}),
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """请求体 ``{"hours": 24}``，单位小时；传 0 就是关掉缓存。

    改小之后立刻清一次超期数据，不用等下一次定时清理。
    """
    body = payload or {}
    try:
        hours = int(body.get("hours") or 0)
    except (TypeError, ValueError):
        return _fail("hours 必须是整数")
    try:
        result = _apply_cache_hours(hours)
    except Exception as e:
        return _fail(f"保存缓存配置失败: {e}")

    removed = await purge_expired() if result["enabled"] else 0
    result["count"] = await cache_size()
    result["removed"] = removed
    return _ok(result)


@app.post("/api/history/chat/cache/clear", summary="清空消息缓存", tags=SESSION_CHAT)
async def clear_chat_cache(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """把已经缓存的消息全部删掉，不影响缓存开关本身。"""
    from gsuid_core.message_history.cache import CoreMessageCache

    try:
        removed = await CoreMessageCache.clear_all()
    except Exception as e:
        return _fail(f"清空缓存失败: {e}")
    return _ok({"removed": removed})


# ---------------------------------------------------------------------------
# 实时推送
# ---------------------------------------------------------------------------


@app.websocket("/api/history/chat/ws")
async def chat_ws(websocket: WebSocket) -> None:
    """新消息实时推送。帧格式：

    ``{"type": "message", "session_id": "...", "message": {...}}``
    ``{"type": "cleared" | "deleted", "session_id": "..."}``
    ``{"type": "ping"}``
    """
    token = websocket.query_params.get("token")
    if not livechat_ws_authorized(token):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    hub = get_history_hub()
    sub_id, queue = hub.subscribe()

    async def _pump() -> None:
        while True:
            payload: Dict[str, Any]
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=_WS_PING_SECONDS)
            except asyncio.TimeoutError:
                payload = {"type": "ping"}
            if payload.get("type") == "message":
                # 和 HTTP 路径对齐：补 self 标记与 at 昵称，否则实时推来的这条
                # 会把 openid 原样显示出来，刷新一下才对
                msg_val = payload.get("message")
                if isinstance(msg_val, dict):
                    message = dict(msg_val)
                    message["self"] = message.get("role") == "assistant"
                    ev = _resolve(str(payload.get("session_id") or ""))
                    if ev is not None:
                        await _fill_at_names([message], str(ev.bot_id or ""))
                    payload = dict(payload)
                    payload["message"] = message
            await websocket.send_text(json.dumps(payload, ensure_ascii=False, default=str))

    async def _watch() -> None:
        # 客户端不会主动发东西，这里只为尽早发现断开
        while True:
            await websocket.receive_text()

    pump_task = asyncio.create_task(_pump())
    watch_task = asyncio.create_task(_watch())
    try:
        # 任一结束就收工：发送端报错说明对端没了，接收端返回说明对端主动断开
        _, pending = await asyncio.wait(
            {pump_task, watch_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        hub.unsubscribe(sub_id)
        for task in (pump_task, watch_task):
            if not task.done():
                task.cancel()
