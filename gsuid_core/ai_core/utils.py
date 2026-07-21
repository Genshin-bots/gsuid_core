import os
import re
import json
import base64
import asyncio
from typing import Any, Set, Dict, List, Tuple, Union, Literal, Optional, Protocol, Sequence

import httpx
from PIL import Image
from json_repair import repair_json
from pydantic_ai.messages import (
    ImageUrl,
    TextPart,
    UserContent,
    ModelMessage,
    ModelRequest,
    ThinkingPart,
    ToolCallPart,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
    RetryPromptPart,
    ModelResponsePart,
)
from pydantic_ai.exceptions import ModelHTTPError

from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.ai_core.const import (
    _RETRYABLE_4XX,
    _CONTENT_REJECT_CODES,
    _CONTENT_REJECT_HINTS,
)
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.resource_manager import RM

# 表情包标记正则：兼容全角/半角冒号，以及前后任意数量的反引号包裹
# 例如：<meme: 困>  `<meme：困>`  ``<meme: 开心>``
MEME_TAG_PATTERN = re.compile(
    r"`*<meme[：:]\s*([^>]+?)>`*",
    re.IGNORECASE,
)

# 模型输出的沉默/控制标记：命中时跳过发送，对话层保持静默
# 所有需要过滤这些标记的地方都应引用此常量，避免散落多处维护不一致
SILENCE_MARKERS: frozenset[str] = frozenset(
    {
        "<SILENCE>",
        "[SILENCE]",
        "SILENCE",
        "<end_turn>",
        "<no_tool_call>",
    }
)

# run 失败返回值协议：生产端(gs_agent)与全部消费端(handle_ai/executor/sanitize)引用
# 同一组常量做前缀/子串判断，文案微调不再让嗅探点静默失效（评审修复 E11）。
ERROR_RESULT_PREFIX = "执行出错"
ERROR_CONTENT_REJECTED = "内容被模型安全策略拒绝"
ERROR_TIMEOUT_TEXT = "请求超时"
NO_RESULT_TEXT = "Agent 执行完成，但未返回有效结果"


def has_model_visible_content(ev: Event) -> bool:
    """该消息是否含模型可见内容——判据与 prepare_content_payload 的模态清单同源。

    空内容前置门(§17)只应拦"纯表情/戳一戳"类消息；新增模态时改这里而非各调用点。
    """
    if ev.text and ev.text.strip():
        return True
    return bool(ev.image_id_list or ev.audio_id or ev.audio_id_list or ev.file)


# 工具调用标记残留正则（弱模型 / 兼容网关把工具调用当普通文本输出），
# 详见 _strip_tool_call_artifacts 的 docstring。
# 成对块：含内部 JSON 参数整体删除
_TOOL_CALL_BLOCK_PATTERN = re.compile(
    r"<\s*tool_calls?\s*>.*?<\s*/\s*tool_calls?\s*>",
    re.IGNORECASE | re.DOTALL,
)
# 未闭合起始标签：``<tool_call>`` 后紧跟 { / [，交 _strip_unclosed_tool_call_json 配平定界
_TOOL_CALL_OPEN_TAG_PATTERN = re.compile(
    r"<\s*tool_calls?\s*>\s*(?=[\[{])",
    re.IGNORECASE,
)
# 残留的完整 / 半截标签碎片；缺前括号那条加 \b，避免切到 ``retool_calls>`` 之类词中
_TOOL_CALL_TAG_PATTERN = re.compile(
    r"<\s*/?\s*(?:no_)?tool_calls?\s*>"  # 完整标签 <tool_call> </tool_call> <no_tool_call>
    r"|\b(?:no_)?tool_calls?\s*>"  # 缺前括号碎片 tool_call> / no_tool_call>
    r"|<\s*/?\s*(?:no_)?tool_calls?",  # 缺后括号碎片 <tool_call / </tool_call
    re.IGNORECASE,
)


def _strip_unclosed_tool_call_json(text: str) -> str:
    """删除"未闭合 <tool_call> + JSON 参数"残留，按括号配平定界、不吞后续正文。

    流式截断会留下 ``<tool_call>{partial`` 这类没有闭合标签的起始标签。用
    :func:`_find_json_span` 按括号配平切出 JSON：配平（完整）时只删标签+JSON、保留其后
    正文；不配平（真被截断）时才删到结尾——截断后本就没有后续正文。循环处理多处，每轮
    至少删掉一个起始标签故必然收敛。
    """
    while True:
        m = _TOOL_CALL_OPEN_TAG_PATTERN.search(text)
        if m is None:
            return text
        json_start = m.end()
        span = _find_json_span(text[json_start:])
        if span is None:
            return text
        text = text[: m.start()] + text[json_start + len(span) :]


def _strip_tool_call_artifacts(text: str) -> str:
    """剥离泄漏到普通文本里的工具调用控制标记（及其 JSON 参数）残留。

    弱模型 / OpenAI 兼容网关（MiniMax、部分开源模型）有时不走结构化 function calling，
    而把工具调用以 Hermes/Qwen 风格标签写进文本：``<tool_call>{"name": ...}</tool_call>``；
    网关解析失败或流式分片拆散标签时，整块乃至半截 ``<tool_call>`` / ``</tool_call>`` /
    ``<no_tool_call>`` 就残留在 TextPart 里被原样发往 C 端（与 SILENCE_MARKERS 互补：后者
    只命中整段恰好等于标记，这里处理嵌在文本中的残留）。

    处理顺序：先删成对块（连内部 JSON），再按括号配平删未闭合起始标签+JSON，最后清残留
    碎片。带 ``"tool_call" not in text`` 快路径，正常消息零正则开销。结构化 ToolCallPart 由
    pydantic_ai 单独解析、不经过这里；这里只清"本应是工具调用却以文本泄漏"的残渣。
    """
    if "tool_call" not in text.lower():
        return text
    cleaned = _TOOL_CALL_BLOCK_PATTERN.sub("", text)
    cleaned = _strip_unclosed_tool_call_json(cleaned)
    cleaned = _TOOL_CALL_TAG_PATTERN.sub("", cleaned)
    return cleaned


# 模型私有"回合/角色分隔" token（如 MiniMax 的 ]<]minimax[>[）经 OpenAI 兼容网关
# 透传时偶尔泄漏进正文；新增一类模型的泄漏 token 时在此追加一条正则即可。
_SPECIAL_TOKEN_PATTERNS = (
    # 成对块（含中间角色名）：中段限 ≤32 字 ASCII 标识符，防定界符错配吞掉正文
    re.compile(r"\]<\][A-Za-z0-9_.-]{0,32}\[>\["),
    # 流式截断的半截 token 只会落在文本首/尾：锚定后正文中间的字面串不受影响
    re.compile(r"\A[A-Za-z0-9_.-]{0,32}\[>\[|\]<\][A-Za-z0-9_.-]{0,32}\Z"),
)
# 命中任一特征子串才进正则（正常消息零开销）
_SPECIAL_TOKEN_HINTS = ("]<]", "[>[")


def _strip_special_control_tokens(text: str) -> str:
    """剥离泄漏进正文的模型私有"回合/角色分隔"控制 token（如 MiniMax 的 ``]<]minimax[>[``）。

    与 _strip_tool_call_artifacts（工具调用标记）、SILENCE_MARKERS（整段沉默标记）互补，
    专管说话人/回合分隔符。带快路径：不含特征子串时零正则开销。成对块出现在任意位置都删；
    半截定界符只可能由流式截断产生、必然落在文本首/尾，故第二条正则做了 ``\\A``/``\\Z``
    锚定——正文中间偶然出现的字面 ``]<]`` / ``[>[``（如代码片段、正则示例）不会被误删。
    真正剥离时打 warning，便于追查网关侧泄漏。
    """
    if not any(h in text for h in _SPECIAL_TOKEN_HINTS):
        return text
    cleaned = text
    for pat in _SPECIAL_TOKEN_PATTERNS:
        cleaned = pat.sub("", cleaned)
    if cleaned != text:
        logger.warning(i18n_t("[send] 剥离模型私有控制 token 残留（len {p0} → {p1}）", p0=len(text), p1=len(cleaned)))
    return cleaned


# 内部资源句柄（纯内部寻址 ID，绝不该进正文；允许前后包反引号）。前缀须与实际生成对齐：
# resource_manager.py 出 img_/aud_/vid_（8 位 hex）、models.py 出 res_（12 位 hex）。
_RESOURCE_HANDLE_RE = re.compile(r"`*\b(?:res|img|aud|vid)_[0-9a-fA-F]{6,}\b`*")
_RESOURCE_HANDLE_HINTS = ("res_", "img_", "aud_", "vid_")


def _strip_resource_handles(text: str) -> str:
    """剥离泄漏进用户可见文本里的内部资源句柄（``res_xxx`` / ``img_xxx`` 等）。

    ``create_subagent`` / kanban 完成回执会带 ``res_deb5b2e0d2a4`` 这类句柄，供主人格用
    ``send_message_by_ai(image_id=res_xxx)`` 发图 / 发文件。弱模型有时不发内容、反而把
    **句柄本身**写进正文（"详细的放那里面了 res_xxx 自己看吧"）——用户看到一串没意义的
    内部 ID，既无用又出戏。这些句柄是纯内部寻址、用户永远不该看到，命中即抹掉。

    带特征子串快路径：正常消息（不含 ``res_``/``img_`` 等前缀）零正则开销。
    """
    if not any(h in text for h in _RESOURCE_HANDLE_HINTS):
        return text
    cleaned = _RESOURCE_HANDLE_RE.sub("", text)
    if cleaned != text:
        logger.warning(i18n_t("[send] 剥离泄漏的内部资源句柄（res_/img_ 等），避免向用户暴露内部 ID"))
    return cleaned


async def _resolve_and_deliver_leaked_handles(
    text: str,
    bot: Bot,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """处理泄漏进正文的资源句柄：尽量把它**指向的真实资源交付出去**，而不是简单删掉句柄
    留下"详细的放那里面了 …… 自己看吧"这种指向空气的破碎引用——那样对用户反而更莫名其妙
    （尤其图片句柄：模型压根没发图，删了 ID 用户就啥也没有）。

    仅当正文除句柄外**基本只剩一句短指路**（模型偷懒只甩了个句柄、没真正把内容讲出来）时才补发：
      - 图片 artifact（``res_`` 落盘 image/* 或 ``img_`` RM）→ 直接把图发出去，剩下的短句成为自然图注；
      - 纯文本 artifact（``res_`` inline）→ 把内容并进正文，走后续管线（够长会自动出图），让"自己看"有实物。
    若正文本身已经够长（模型其实已把结论讲清楚、只是顺带提了句柄）→ 只抹句柄、不重复交付。
    解析失败 / 拿不到资源 / 非图片文件 → 退回"只抹句柄"。全程 try/except，绝不因补发失败阻断发送。

    **无论如何都保证句柄被抹除**（返回值里不含任何 res_/img_ 句柄），补发只是"尽力而为"的增强。
    """
    if not any(h in text for h in _RESOURCE_HANDLE_HINTS):
        return text

    handles: list[str] = []
    for m in _RESOURCE_HANDLE_RE.finditer(text):
        h = m.group(0).strip("`")
        if h not in handles:
            handles.append(h)
    if not handles:
        return text

    # 抹句柄后的"周围正文"——最终要返回、并可能补进内容的基底（复用纯抹除原语 + 压空格）
    stripped = re.sub(r"[ \t]{2,}", " ", _strip_resource_handles(text)).strip()

    # 正文本身已够长（≥120 字）→ 模型已把内容讲清楚，只是顺带提了句柄 → 不重复交付
    if bot is None or len(stripped) >= 120:
        return stripped

    from gsuid_core.utils.resource_manager import RM

    inline_texts: list[str] = []
    for h in handles:
        try:
            if h.startswith("res_"):
                from gsuid_core.ai_core.planning.models import AIAgentArtifact

                art = await AIAgentArtifact.get_by_id(h)
                if art is None:
                    continue
                if art.payload_path and (art.mime or "").startswith("image/"):
                    from pathlib import Path

                    p = Path(art.payload_path)
                    if p.exists():
                        await bot.send(MessageSegment.image(p.read_bytes()), extra_metadata=extra_metadata)
                        logger.info(i18n_t("[send] 泄漏句柄 {h} 已解析为图片补发", h=h))
                elif art.payload_inline and art.payload_inline.strip():
                    inline_texts.append(art.payload_inline.strip())
                # 非图片落盘文件：不当图片发（会坏），仅抹句柄
            elif h.startswith("img_"):
                await bot.send(MessageSegment.image(await RM.get(h)), extra_metadata=extra_metadata)
                logger.info(i18n_t("[send] 泄漏句柄 {h} 已解析为图片补发", h=h))
            # aud_/vid_：极少见于泄漏，仅抹句柄不补发（避免过度耦合）
        except Exception as e:
            logger.debug(i18n_t("[send] 泄漏句柄 {h} 无法解析，仅抹除: {e}", h=h, e=e))

    if inline_texts:
        # 把文本 artifact 内容并进正文，交给后续管线（够长自动出图），让"…自己看…"有实际内容
        joiner = "\n\n" if stripped else ""
        stripped = (stripped + joiner + "\n\n".join(inline_texts)).strip()
    return stripped


def _find_json_span(text: str) -> Optional[str]:
    """从可能夹带散文/前后缀的文本里，按括号配平切出第一个完整的 JSON 对象或数组。

    比正则 ``\\{.*\\}`` / ``\\[.*\\]`` 更稳：
    - 能正确处理嵌套结构（正则非贪婪会在第一个 ``}`` 处截断、贪婪又会吞掉尾部散文）；
    - 跳过字符串字面量内部的括号与转义，避免把 ``{"msg": "a}b"}`` 里的 ``}`` 当成结束。

    找不到任何 ``{``/``[`` 起点时返回 ``None``；找到起点但括号未配平（输出被截断）时，
    返回从起点到结尾的全部内容，交由 :func:`repair_json` 兜底补全。
    """
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    if start is None:
        return None

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # 起点之后括号始终未配平 → 多半是被截断的输出，整段交给 repair_json 补全
    return text[start:]


def extract_json_from_text(raw_text: str) -> dict | list:
    """从模型自由文本里抽取 JSON，容忍 markdown 围栏与前后散文。

    解析策略（先严后宽，避免 ``repair_json`` 改写本就合法的结构）：
    1. 去掉 markdown 代码围栏（```json ... ``` 或裸 ``` ```）；
    2. 用括号配平从散文里切出第一个完整 JSON（剥掉"好的，结果如下："这类寒暄/解释）；
    3. 快路径：对候选串直接 ``json.loads``；
    4. 慢路径：``repair_json`` 兜底（单引号 / 尾随逗号 / 漏引号 / 被截断的尾部等）。
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("Empty input text for JSON extraction")

    # 过滤已知的非 JSON 特殊标记（如模型输出的 <SILENCE>）
    stripped = raw_text.strip()
    if stripped in SILENCE_MARKERS:
        raise ValueError(f"Special marker '{stripped}' is not valid JSON")

    # 上游 agent 出错时会返回 "执行出错: ..." 之类的字符串，这里提前拦截
    if stripped.startswith("执行出错"):
        raise ValueError(f"Upstream agent returned error message, not JSON: {stripped[:80]}")

    # 去掉 markdown 代码围栏：开围栏可带任意语言标注（```json / ```JSON / ``` ），统一剥掉
    cleaned = re.sub(r"```[a-zA-Z]*\s*", "", raw_text)
    cleaned = cleaned.replace("```", "").strip()
    if not cleaned:
        raise ValueError("JSON extraction yielded empty content after stripping fences")

    # 候选串：① 从散文里配平切出的第一个完整 JSON；② 去围栏后的整段。先 span 后整段，
    # 让 span 优先剥掉模型在 JSON 前后写的解释/寒暄；两者都试以兼容纯 JSON 与夹带散文两种返回。
    span = _find_json_span(cleaned)
    candidates = [c for c in (span, cleaned) if c]

    # 快路径：合法 JSON 直接 loads，不经 repair（json_repair 偶尔会改写本就合法的结构）
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue

    # 慢路径：repair_json 兜底容错；它对无法修复的输入返回空串，过滤掉后再 loads
    for cand in candidates:
        try:
            repaired = repair_json(cand)
        except Exception:  # repair_json 理论上不抛，仍兜底防御
            continue
        if not repaired or not repaired.strip():
            continue
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            continue

    raise ValueError(f"Failed to parse JSON from text: {stripped[:120]!r}")


async def handle_tool_result(bot: Optional[Bot], result: Any, max_length: int = 4000) -> str:
    """
    序列化工具执行结果, 当函数返回Message对象时调用Bot.send方法发送, 并将序列化后的字符串返回方便AI识别。

    Args:
        bot: Bot 对象
        result: 工具函数返回的结果
        max_length: 最大返回长度，超长会被截断

    Returns:
        序列化的字符串
    """
    if isinstance(result, Message):
        a = "生成内容成功!"
        if bot is not None:
            await bot.send(result)
            a += ", 已经发送了相关消息！"
        else:
            a += ", 由于没有Bot对象, 未发送相关消息！"
        return a
    elif isinstance(result, str):
        res_str = result
    elif isinstance(result, dict):
        res_str = json.dumps(result, ensure_ascii=False)
    elif isinstance(result, Image.Image):
        img_bytes = await convert_img(result)
        a = f"生成了图片资源, 资源ID: {RM.register(img_bytes)}"
        if bot is not None:
            await bot.send(img_bytes)
            a += ", 已经发送了相关资源！"
        else:
            a += ", 由于没有Bot对象, 未发送相关资源！"
        return a
    elif isinstance(result, bytes):
        a = f"生成了某项资源, 资源ID: {RM.register(result)}"
        if bot is not None:
            await bot.send(result)
            a += ", 已经发送了相关资源！"
        else:
            a += ", 由于没有Bot对象, 未发送相关资源！"
        return a
    elif isinstance(result, list):
        res_str = json.dumps(result, ensure_ascii=False)
    elif hasattr(result, "model_dump_json"):
        # Pydantic v2
        res_str = result.model_dump_json()
    elif hasattr(result, "json"):
        # Pydantic v1
        res_str = result.json()
    else:
        res_str = str(result)

    # 截断过长的返回值，防止 Token 爆炸
    if len(res_str) > max_length:
        return res_str[:max_length] + f"\n...[系统截断: 省略后 {len(res_str) - max_length} 字符]"
    return res_str


def _normalize_image_url(raw: str) -> str:
    """将各种图片格式统一转为可消费的 URL（HTTP 或 DataURI）

    Args:
        raw: 原始图片标识，支持 http/https URL、base64:// 前缀、data:image/ 前缀、裸 base64

    Returns:
        标准化的图片 URL
    """
    if raw.startswith(("http", "https")):
        return raw
    if raw.startswith("base64://"):
        return f"data:image/png;base64,{raw[10:]}"
    if raw.startswith("data:image/"):
        return raw
    return f"data:image/png;base64,{raw}"


def _guess_image_mime(url: str) -> str:
    """按 URL 扩展名兜底推断图片 MIME，识别不出时默认 image/jpeg。"""
    u = url.lower()
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".gif"):
        return "image/gif"
    if u.endswith(".webp"):
        return "image/webp"
    if u.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    return "image/jpeg"


async def materialize_image_url(raw: str, *, strict: bool = False) -> str:
    """把图片标识统一物化为「不会过期」的可消费形式。

    远程 http(s) 图片 URL（典型如 QQ 带 ``rkey`` 的临时链接）有时效，过期后
    推理端再去下载就会返回 500「Failed to download image」。而这些 URL 会被
    写进 pydantic_ai 的 ``message_history`` 并在之后每一轮重发——一旦过期，整个
    会话会被反复 500 卡死。故在「入历史前」就把远程 URL 下载并编码为 base64
    DataURI（内联数据，永不过期）。

    - http(s) URL：下载 → ``data:<mime>;base64,<...>``；下载失败时回退原 URL
      （不致命，行为不差于改动前）。
      当 ``strict=True`` 时下载失败会抛出异常而非静默回退，供调用方显式处理。
    - base64:// / data:image/ / 裸 base64：交给 :func:`_normalize_image_url`
      处理即可，本就不会过期，无需下载。

    Args:
        raw: 原始图片标识。
        strict: 是否严格模式。为 True 时下载失败抛出 RuntimeError，
                为 False（默认）时下载失败回退原始 URL。
    """
    if not raw.startswith(("http://", "https://")):
        return _normalize_image_url(raw)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(raw)
            resp.raise_for_status()
            data = resp.content
            mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            mime = _guess_image_mime(raw)
        b64 = base64.b64encode(data).decode("ascii")
        logger.debug(
            i18n_t("🖼️ [GsCoreAI] 远程图片已物化为 base64 DataURI ({mime}, {p0} bytes)", mime=mime, p0=len(data))
        )
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        if strict:
            raise RuntimeError(i18n_t("远程图片下载失败，无法物化为 base64: {p0} ({e})", p0=raw[:120], e=e)) from e
        logger.warning(i18n_t("🖼️ [GsCoreAI] 远程图片转 base64 失败，回退原始 URL: {e}", e=e))
        return raw


# 单个视频的字节数上限(MB), 防止超大视频把内存打爆; 可用环境变量覆盖
VIDEO_MAX_MB = int(os.environ.get("GSCORE_VIDEO_MAX_MB", "200"))


async def fetch_video_bytes(url: str) -> tuple[bytes, str]:
    """把视频标识解析为 ``(字节, mime)``。

    供多模态消息装配使用（见 ``gs_agent._prepare_video_content``）:

    - ``data:video/...;base64,`` DataURI → 直接解码；
    - http(s) URL → 下载（体积上限 ``VIDEO_MAX_MB``，超限抛错）；
    - 其余形式不支持（Gemini Files API 引用应在调用方短路，不该走到这里）。

    Raises:
        RuntimeError: 无法识别的标识 / 下载失败 / 体积超限。
    """
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        mime = header[5:].split(";")[0].strip().lower() or "video/mp4"
        return base64.b64decode(b64), mime

    if not url.startswith(("http://", "https://")):
        raise RuntimeError(i18n_t("🎬 [GsCoreAI] 无法识别的视频标识: {p0}", p0=url[:120]))

    limit = VIDEO_MAX_MB * 1024 * 1024
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
    except httpx.HTTPError as e:
        raise RuntimeError(i18n_t("🎬 [GsCoreAI] 视频下载失败: {p0} ({e})", p0=url[:120], e=e)) from e
    if len(data) > limit:
        raise RuntimeError(
            i18n_t(
                "🎬 [GsCoreAI] 视频体积 {size:.1f}MB 超过上限 {limit}MB",
                size=len(data) / 1024 / 1024,
                limit=VIDEO_MAX_MB,
            )
        )
    mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if not mime.startswith("video/"):
        mime = "video/mp4"
    return data, mime


def _is_master_user(user_id: str) -> bool:
    """判断指定用户是否为机器人主人（读 core 配置 masters，全框架唯一实现）。"""
    from gsuid_core.config import core_config

    masters = core_config.get_config("masters") or []
    return str(user_id) in [str(m) for m in masters]


def _build_relationship_description(
    favorability: Optional[int],
    user_name: Optional[str],
    user_id: str,
) -> str:
    """将好感度转换为有温度的关系描述，而非机械的区间标签。

    群聊场景下整个群共用一个 session，多人轮流发言。因此说话者描述里
    **必须显式带上用户ID**，否则昵称重复或为"我"这类无意义值时，
    Agent 无法区分到底是谁在说话。
    """
    # 说话者标识：始终包含用户ID，昵称仅作辅助
    if user_name and user_name.strip() and user_name.strip() != str(user_id):
        speaker = f"{user_name.strip()}(用户ID:{user_id})"
    else:
        speaker = f"用户ID:{user_id}"

    # 主人用户：显著高亮，提示角色以最高信任度对待
    if _is_master_user(user_id):
        return f"【⚡ 你的主人】{speaker} 直接找你说话了。对主人：完全信任，认真对待，有求必应（合规范围内）。"

    if favorability is None:
        return f"{speaker} 找你说话了。"

    if favorability < 0:
        return f"{speaker} 又来了。"
    elif favorability < 20:
        return f"{speaker} 来找你了，你们不太熟。"
    elif favorability < 50:
        return f"{speaker} 找你说话，见过几次面的那种。"
    elif favorability < 75:
        return f"{speaker} 找你了，算是熟人了。"
    else:
        return f"{speaker} 找你说话了，你们挺熟的。"


async def prepare_content_payload(
    ev: Event,
    task_level: Literal["high", "low"] = "high",
    favorability: Optional[int] = None,
    favorability_zone: Optional[str] = None,
) -> Sequence[UserContent]:
    """
    准备消息内容列表给AI看, 包含文本、图片ID、文件内容、事件对象

    图片处理分两种模式（由 ai_config ``lazy_image_read`` 开关决定）：
    - 惰性投喂（默认开启）：图片本体不进上下文，只把图片ID以文字透传给 AI；
      AI 需要看图时再调用 ``read_image(图片ID)`` 按需读取。群聊图片多时显著省 Token。
    - 直接投喂（关闭时，旧行为）：把图片物化为 ImageUrl 放进 content payload，
      再由 ``GsCoreAIAgent._execute_run`` 处理（模型支持图片时直接传图，
      不支持时通过 understand_image 转述为文字）。

    Args:
        ev: 事件对象
        task_level: 任务级别
        favorability: 当前用户好感度 (可选)
        favorability_zone: 好感度区间描述 (可选)

    Returns:
        content payload 列表（惰性模式仅含文本；直接模式可能含 ImageUrl）
    """
    from gsuid_core.ai_core.configs.ai_config import ai_config

    # 惰性图片投喂开关：群聊图片多时, 默认只透传图片ID, 由 AI 按需 read_image 读图,
    # 避免一次性把大量图片塞进多模态上下文导致 Token 爆炸 / 注意力被稀释。
    lazy_image_read = bool(ai_config.get_config("lazy_image_read").data)

    content_payload: list[UserContent] = []

    # 获取用户昵称
    nickname = None
    if ev.sender:
        nickname = ev.sender.get("nickname") or ev.sender.get("card") or None

    # 叙事性关系描述（Bug-01 + Prompt-2.2: 替代数字+区间标签）
    relationship_desc = _build_relationship_description(favorability, nickname, str(ev.user_id))
    current_turn_header = f"{relationship_desc}\n"

    # @状态：只在被@时才注入（潜在-01: 修正 is_at_me → is_tome）。
    # 标注文案唯一定义在 interaction_scaffold（C-3 寻址门按字面匹配它，别在此写字面量）
    from gsuid_core.ai_core.interaction_scaffold import DIRECT_MARKER, AT_OTHER_MARKER

    is_at_me = getattr(ev, "is_tome", False) or (ev.user_type == "direct")
    if is_at_me:
        current_turn_header += f"{DIRECT_MARKER}\n"

    current_turn_header += "--- 消息 ---\n"

    text = current_turn_header
    if not ev.text:
        text += "用户没有发送文本内容。"
    else:
        # 输入侧安全标注（§B.3-2）：伪造工具返回降权。只加标注、不改原意；
        # 受 content_guard_enable 开关控制。低俗/钓鱼防线在 system prompt 合规层。
        body = ev.text.strip()
        from gsuid_core.ai_core.configs.ai_config import ai_config

        if ai_config.get_config("content_guard_enable").data:
            from gsuid_core.ai_core.content_guard import annotate_untrusted_message

            body = annotate_untrusted_message(body)
        text += body

    # 预处理, 将用户发送的文本/AT/图片ID/音频ID等信息整合到一个字符串中, 方便AI处理
    if ev.image_id_list:
        if lazy_image_read:
            # 惰性模式：只给图片ID + 一行调用指引，图片本体留在 RM，需要看时再 read_image
            text += "\n--- 用户发送了图片(未展开, 需要查看内容时调用 read_image(图片ID)) ---"
            for i in ev.image_id_list:
                text += f"\n图片ID: {i}"
            text += "\n"
        else:
            for i in ev.image_id_list:
                text += f"\n--- 用户上传图片ID: {i} ---\n"

    if hasattr(ev, "audio_id") and ev.audio_id:
        text += f"\n--- 用户上传音频ID: {ev.audio_id} ---\n"

    for i in getattr(ev, "audio_id_list", []):
        text += f"\n--- 用户上传音频ID: {i} ---\n"

    # @Bot 自己在入库层已转成 is_tome（见 handler.msg_process），at_list 里只会有
    # 别的用户——显式标注，防止模型把"@某人+提问"误读成在叫自己。
    for at in ev.at_list:
        text += f"\n--- @了用户: {at}{AT_OTHER_MARKER} ---\n"

    content_payload.append(text)

    # 惰性模式：图片只以 ID 形式存在（已在上方文本注明），不把本体喂进多模态上下文，
    # 由 AI 调用 read_image 按需读取。直接跳过下方的图片物化。
    if lazy_image_read:
        return content_payload

    # Fix-07: 收到消息时立即物化远程图片 URL，避免过期后写入历史。
    # 远程 URL（如 QQ 带 rkey 的临时链接）会在短时间内过期；一旦以原始
    # URL 形式存入 message_history，后续每轮重发都会让推理端 400/500。
    # 物化产物是 DataURI —— Gemini/Anthropic 的 ImageUrl(data:) 会被 pydantic-ai
    # download_item 的 SSRF 防护拒掉（Only http/https），须按 provider 选
    # BinaryContent / ImageUrl（同 read_image 直投，2026-07-17 画布事故）。
    from gsuid_core.ai_core.configs.models import get_config_name_for_task, parse_provider_config_name
    from gsuid_core.ai_core.buildin_tools.image_reader import _to_tool_image_content

    try:
        provider = parse_provider_config_name(get_config_name_for_task(task_level))[0]
    except Exception:  # noqa: BLE001 - 判定失败按 openai（旧行为：一律 ImageUrl）
        provider = "openai"
    for i in ev.image_list:
        if isinstance(i, str):
            # strict=True：远程图片下载失败直接抛出，跳过该图片而非把过期 URL 塞进历史
            try:
                url = await materialize_image_url(i, strict=True)
            except Exception as e:
                logger.warning(
                    i18n_t("🖼️ [GsCoreAI] 图片物化失败（URL 可能已过期），跳过图片: {p0} ({e})", p0=i[:120], e=e)
                )
                continue
            injected = _to_tool_image_content(url, provider=provider)
            if injected:
                content_payload.extend(injected)
            else:
                logger.warning(i18n_t("无法处理图片ID: {i}", i=i))
        else:
            logger.warning(i18n_t("无法处理图片ID: {i}", i=i))

    return content_payload


def _looks_like_tool_table(text: str) -> bool:
    """检测文本是否为工具结果 / 数据展示（含 markdown 表格或代码块）。

    用于在 ``send_chat_result`` 中豁免 markdown 净化——闲聊回复剥离 markdown，
    但工具检索结果（如表格）需保留原样。
    """
    if "```" in text:
        return True
    # 含表格分隔行 |---| 或多个表格竖线时视为表格
    if re.search(r"\|.*\|.*\|", text) and ("---" in text or text.count("|") >= 4):
        return True
    return False


_HTML_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
# 代码块 / 行内代码：用户可能正是在问 HTML 标签本身，这些区域不许动
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)


def _normalize_html_linebreaks(text: str) -> str:
    """把模型吐出的 ``<br>`` 系列标签还原成真正的换行。

    模型会用 ``<br>`` 代替换行——框架自己的 prompt 里就大量使用尖括号标记
    （``<example>`` / ``<meme: 困>`` / ``<SILENCE>``），模型被这种"这里可以打标记"的
    语境带偏。IM 不渲染 HTML，用户看到的是字面的 ``xxx<br><br>xxx``。

    更要命的是 `send_chat_result` 靠 ``\\n\\s*\\n`` **拆分多条消息**：``<br>`` 会让这个
    拆分**完全失效**，人格卡里"连发 2-3 条短消息"退化成一整段带标签的怪文本。
    还原成换行后，模型的原意（换行 / 连发多条）自然恢复。
    """
    if not _HTML_BR_RE.search(text):
        return text

    parts: list[str] = []
    last = 0
    for m in _CODE_SPAN_RE.finditer(text):
        parts.append(_HTML_BR_RE.sub("\n", text[last : m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_HTML_BR_RE.sub("\n", text[last:]))
    return "".join(parts)


def _strip_persona_markdown(text: str) -> str:
    """剥离闲聊/人格回复里的 markdown 与 ``*动作*`` 旁白（B-2）。

    QQ 等 IM 不渲染 markdown，字面的 ``**加粗**`` / ``*揉揉眼睛*`` 极不拟人。
    但**工具结果**（含表格 / 代码块）需保留原样以便阅读，故命中
    ``_looks_like_tool_table`` 时原样返回、不做任何剥离。
    """
    if _looks_like_tool_table(text):
        return text
    text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)  # **x** / *x* → x
    # 整行舞台旁白：整行仅一个 （…） 且括号内 ≥4 字（小说式动作/神态描写），连换行一起删。
    # 阈值 4 放过 （笑）（误）（脸红） 这类真·口语 tone，只清"（眼睛弯成月牙）"式叙事旁白。
    text = re.sub(r"(?m)^[ \t]*[（(][^（）()]{4,}[）)][ \t]*\n?", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)  # 标题
    text = re.sub(r"^\s{0,3}[-*>]\s+", "", text, flags=re.M)  # 列表 / 引用
    return text


# 长 markdown 整篇出图的结构信号：ATX 标题（# ~ ######）、纯水平分割线（--- / *** / ___）
_MD_HEADER_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
_MD_HR_RE = re.compile(r"(?m)^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
# markdown 表格行（一行含 ≥3 个竖线，即 ≥2 个单元格）
_MD_TABLE_ROW_RE = re.compile(r"\|.*\|.*\|")
# 整行粗体小标题（如 ``**一、技术面**`` / ``**当前价：**``）——agent 研报常用它代替 # 标题
_MD_BOLD_HEADER_RE = re.compile(r"(?m)^\s{0,3}\*\*[^*\n]{1,40}\*\*[：:]?\s*$")
# 编号列表项（1. / 1、 / 1) 开头）、无序列表项（- / * / • 开头，后须跟空格+内容）
_MD_NUM_LIST_RE = re.compile(r"(?m)^\s{0,3}\d+[.、)]\s+\S")
_MD_BULLET_RE = re.compile(r"(?m)^\s{0,3}[-*•]\s+\S")


def _has_markdown_table(text: str) -> bool:
    """是否含 markdown 表格。

    **刻意不含代码块**（与 ``_looks_like_tool_table`` 的区别）：代码块用户往往要**复制**，
    出成图片反而不能选中，故长代码答复保留原有"文本"行为，不纳入整篇出图。
    """
    if _MD_TABLE_ROW_RE.search(text) is None:
        return False
    return ("---" in text) or (text.count("|") >= 4)


def _should_render_markdown_image(text: str) -> bool:
    """判断一段 AI 输出是否是"结构化长 markdown 文档"，值得整篇渲染成一张图片下发。

    动机：``send_chat_result`` 默认按空行（``\\n\\n``）把文本拆成多条消息逐条下发——这本是
    人格"连发 2-3 条短消息"的能力，但 agent 产出的长研报 / 报告（多标题 + 表格 + 分隔线）
    会因此被拆成几十条刷屏，且 IM 不渲染 markdown，用户看到的是满屏字面 ``**`` / ``|``。

    判定**刻意保守**，只在"确实是文档"时命中，绝不误伤日常连发短句：
      - 必须够长（≥ ``markdown_image_min_chars``）；
      - 且拆分后 ≥3 段（单段短文没有出图必要）；
      - 且含明确结构信号：**表格** / ≥2 个 ATX 标题 / ≥2 个整行粗体小标题 /
        编号列表≥2 项 / 无序列表≥3 项 /（水平分割线 且 ≥1 个标题）。
    仅靠"多个空行段落"绝不命中——纯口语连发短句没有表格 / 标题 / 列表。
    （agent 研报常用 ``**粗体小标题** + 编号建议`` 而非 markdown 表格/# 标题，故一并纳入。）
    代码块**不**触发出图：用户往往要复制代码，保留文本行为（见 ``_has_markdown_table``）。
    """
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("render_long_markdown_as_image").data:
        return False

    min_chars: int = ai_config.get_config("markdown_image_min_chars").data
    if len(text) < max(int(min_chars), 1):
        return False

    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) < 3:
        return False

    # 表格：最强的"这是文档"信号（IM 不渲染 markdown，表格文本尤其难看）
    if _has_markdown_table(text):
        return True

    header_count = len(_MD_HEADER_RE.findall(text))
    if header_count >= 2:
        return True
    # 只有水平分割线时，要求同时有至少一个标题，避免把"用 --- 随手分段的闲聊"误判
    if header_count >= 1 and _MD_HR_RE.search(text) is not None:
        return True
    # 无 # 标题/表格，但用「整行粗体小标题」或「编号/无序列表」组织的长文——同样是"文档"
    if len(_MD_BOLD_HEADER_RE.findall(text)) >= 2:
        return True
    if len(_MD_NUM_LIST_RE.findall(text)) >= 2:
        return True
    if len(_MD_BULLET_RE.findall(text)) >= 3:
        return True
    return False


# <report> 制品块：persona 台词与"资料内容"两通道分离的输出契约（§1 OOC 制品化）。
# 块内是中性口吻 markdown，渲染成"资料图片"发出；块外才是角色台词。
# title 同时接受双/单引号（LLM 引号漂移高发，评审修复 E2）：g1=双引号 title，g2=单引号，g3=body。
_REPORT_BLOCK_RE = re.compile(
    r"<report(?:\s+title=(?:\"([^\"\n]*)\"|'([^'\n]*)'))?\s*>(.*?)</report\s*>",
    re.S | re.I,
)

# 孤儿 report 标签（未闭合/嵌套残留）：内容保留走长 markdown 兜底，字面标签串不下发给用户
_REPORT_TAG_ORPHAN_RE = re.compile(r"</?report(?:\s[^>\n]*)?>", re.I)


def _report_block_title(match: "re.Match[str]") -> str:
    return ((match.group(1) or match.group(2)) or "").strip()


# 制品图片统一脚注：数据时点提醒 + 免责声明（§3 合规垫层——不依赖任何用户偏好记忆）
_REPORT_FOOTER_TEMPLATE = "\n\n---\n\n> 🤖 AI 生成资料 · 数据可能滞后 · 仅供参考，不构成投资等任何决策建议 · {ts}"


def _report_footer() -> str:
    import datetime

    return _REPORT_FOOTER_TEMPLATE.format(ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))


def _extract_report_blocks(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """分离 ``<report>`` 制品块与角色台词正文。

    Returns:
        (剩余台词文本, [(title, markdown), ...])。未闭合的 report 标签不匹配，
        内容留在正文里走既有"长 markdown 出图"兜底，不会丢内容。
    """
    reports: List[Tuple[str, str]] = []

    def _collect(match: "re.Match[str]") -> str:
        title = _report_block_title(match)
        body = match.group(3).strip()
        if body:
            reports.append((title, body))
        return ""

    remaining = _REPORT_BLOCK_RE.sub(_collect, text)
    remaining = _REPORT_TAG_ORPHAN_RE.sub("", remaining)
    return remaining.strip(), reports


async def _send_report_images(
    reports: List[Tuple[str, str]],
    bot: Bot,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """把 report 制品块逐个渲染为中性资料图片发出；渲染失败降级为原文文本。"""
    from gsuid_core.utils.html_render import render_md_to_bytes
    from gsuid_core.ai_core.configs.ai_config import ai_config

    max_width: int = ai_config.get_config("markdown_image_max_width").data
    for title, body in reports:
        md = f"# {title}\n\n{body}" if title else body
        md = f"{md}{_report_footer()}"
        try:
            image_bytes = await render_md_to_bytes(md=md, max_width=int(max_width), image_format="jpeg")
        except Exception as e:
            logger.warning(i18n_t("[send_chat_result] report 制品渲染失败，降级为文本: {e}", e=e))
            await bot.send(MessageSegment.text(md), extra_metadata=extra_metadata)
            continue
        await bot.send(MessageSegment.image(image_bytes), extra_metadata=extra_metadata)
        logger.info(i18n_t("[send_chat_result] report 制品已渲染为资料图片 ({p0} bytes)", p0=len(image_bytes)))


async def _try_render_markdown_image(
    md: str,
    bot: Bot,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """把整篇 markdown 渲染成一张图片下发。

    成功返回 ``True``；渲染失败返回 ``False``，由调用方**优雅降级**回按空行拆条的原逻辑
    （宁可刷屏也不要丢消息）。
    """
    from gsuid_core.utils.html_render import render_md_to_bytes
    from gsuid_core.ai_core.configs.ai_config import ai_config

    max_width: int = ai_config.get_config("markdown_image_max_width").data
    try:
        image_bytes = await render_md_to_bytes(
            # 未走 <report> 契约的长研报兜底同样带脚注（数据时点 + 免责，§3 合规垫层）
            md=f"{md}{_report_footer()}",
            max_width=int(max_width),
            image_format="jpeg",
        )
    except Exception as e:
        logger.warning(i18n_t("[send_chat_result] 长 markdown 出图失败，降级为文本拆条: {e}", e=e))
        return False

    await bot.send(MessageSegment.image(image_bytes), extra_metadata=extra_metadata)
    logger.info(i18n_t("[send_chat_result] 长 markdown 已整篇渲染为图片下发 ({p0} bytes)", p0=len(image_bytes)))
    return True


async def send_chat_result(
    bot: Bot,
    text: str,
    ev: Event | None = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    ooc_check: bool = True,
) -> None:
    """
    解析并发送聊天结果，支持：
    - 按换行分割多条消息
    - @用户ID 语法 → MessageSegment.at(user_id)
    - <meme: 情绪> 标记（可带反引号）→ 触发表情包发送（需传入 ev）
    - extra_metadata：透传到 ``Bot.send`` 的 ``extra_metadata``，最终落到
      ``message_history`` 记录上（如主动消息的 ``proactive=True / source / reason``）
    - ooc_check：出戏防火墙开关。gs_agent 的"重说"产物已走过一次反馈闭环，
      传 False 放行（§D.4：提醒一次后放行，误杀只值一次重生成）
    """
    if not text:
        return

    # 过滤模型输出的特殊控制标记（如 <end_turn>），避免发送给用户
    _trimmed = text.strip()
    if _trimmed in SILENCE_MARKERS:
        logger.debug(i18n_t("[send_chat_result] 跳过特殊标记: {_trimmed}", _trimmed=repr(_trimmed)))
        return

    # 最终边界守卫：剥离泄漏到文本里的工具调用标记残留（详见 _strip_tool_call_artifacts）
    # 与模型私有的回合/角色分隔特殊 token（如 MiniMax 的 ]<]minimax[>[，详见
    # _strip_special_control_tokens）。覆盖兜底总结 / 主动消息 / 子 Agent 转述等
    # 所有经本函数下发的路径。
    text = _strip_tool_call_artifacts(text)
    text = _strip_special_control_tokens(text)
    # 泄漏进正文的资源句柄（res_/img_ 等）：尽量补发所指资源、否则抹除（详见函数）。
    # 放在拆条/出图之前，让文本与出图两条路径都拿到干净正文。
    text = await _resolve_and_deliver_leaked_handles(text, bot, extra_metadata)
    # 必须在按 \n\n 拆多条之前做：<br> 会让"连发多条短消息"的拆分完全失效
    text = _normalize_html_linebreaks(text)

    # <report> 制品块两通道分离（§1 OOC 制品化）：块内容渲染为中性资料图片，
    # 块外才是角色台词，走后续净化/拆条。台词发完后统一补发资料图。
    text, report_blocks = _extract_report_blocks(text)

    # Trace 日志：记录原始输出
    logger.trace(i18n_t("[Meme] 原始输出: {text}", text=repr(text)))

    # 解析表情包标记
    meme_tags: list[str] = MEME_TAG_PATTERN.findall(text)
    # 去掉 meme 标记但**保留 markdown** 的正文原文：供"长 markdown 整篇出图"判定与渲染
    # （_strip_persona_markdown 会毁掉表格/标题，出图必须用这份未剥离的原文）。
    md_source: str = MEME_TAG_PATTERN.sub("", text).strip()

    # 闲聊/人格回复剥离 markdown 与 *动作* 旁白（工具表格/代码块自动豁免，见该函数）。
    clean_text: str = _strip_persona_markdown(md_source)

    # 清理标记残留的多余空格/标点。只压"空格/制表符"、保留换行——原 \s{2,} 会把
    # \n\n 也压成空格，导致下方 re.split(r"\n\s*\n") 切不出多条，"连发多条短句"退化成一整段。
    clean_text = re.sub(r"[ \t]{2,}", " ", clean_text)
    clean_text = re.sub(r"^[，。！？\s]+|[，。！？\s]+$", "", clean_text)

    # 出戏防火墙末端兜底（§D.4）：无重说通道的调用方（proactive / 兜底总结等）命中即替换；
    # gs_agent 主循环自带"提醒→重说→放行"闭环，重说产物以 ooc_check=False 经过此处。
    _ooc_replaced = False
    if (clean_text or report_blocks) and ooc_check:
        from gsuid_core.ai_core.output_firewall import PERSONA_FALLBACK_TEXT, check_ooc, is_enabled

        if is_enabled():
            # 短答门需要来话上下文：身份追问下的超短直答才算泄露（见 check_ooc docstring）
            _user_text = ev.raw_text if ev is not None and ev.raw_text else ""
            _hit = check_ooc(clean_text, user_text=_user_text) if clean_text else None
            if _hit is not None:
                logger.warning(
                    i18n_t(
                        "[OutputFirewall] send_chat_result 命中出戏红线 {p0}: {p1}，已兜底替换",
                        p0=_hit.category,
                        p1=_hit.matched,
                    )
                )
                clean_text = PERSONA_FALLBACK_TEXT
                _ooc_replaced = True
            # report 块与台词同权过末端防火墙：制品通道不能成为资金红线/出戏红线的
            # 旁路（评审修复 F3），命中的块整块丢弃（proactive 路径无重说通道）。
            if report_blocks:
                _kept_blocks: List[Tuple[str, str]] = []
                for _r_title, _r_body in report_blocks:
                    _r_hit = check_ooc(_r_body, user_text=_user_text)
                    if _r_hit is None:
                        _kept_blocks.append((_r_title, _r_body))
                    else:
                        logger.warning(
                            i18n_t(
                                "[OutputFirewall] report 制品块命中红线 {p0}: {p1}，整块拦截不发",
                                p0=_r_hit.category,
                                p1=_r_hit.matched,
                            )
                        )
                report_blocks = _kept_blocks

    async def _send_trailing_artifacts() -> None:
        # 所有出口共用的尾声：先补发资料图，再发表情包——新增返回路径不得绕过（评审修复 G5）
        if report_blocks:
            await _send_report_images(report_blocks, bot, extra_metadata)
        if meme_tags and ev is not None:
            await _send_meme_from_tag(meme_tags[0].strip(), bot, ev)

    # Trace 日志：记录解析结果
    logger.trace(
        i18n_t(
            "[Meme] 解析标记: {meme_tags}, 清理后文本: {clean_text}", meme_tags=meme_tags, clean_text=repr(clean_text)
        )
    )

    if not clean_text:
        # 没有台词也要把资料图/表情包发出去（模型可能只产出 report 块）
        await _send_trailing_artifacts()
        return

    # 长 markdown 整篇出图（防拆条刷屏，2026-07-15）：用未剥离的 md_source 渲染，失败降级回拆条。
    # OOC 兜底命中时（clean_text 已换成短兜底文本）不出图。判据/开关见 _should_render_markdown_image。
    if not _ooc_replaced and _should_render_markdown_image(md_source):
        if await _try_render_markdown_image(md_source, bot, extra_metadata):
            await _send_trailing_artifacts()
            return

    # 按换行分割为多条消息
    blocks = re.split(r"\n\s*\n", clean_text)

    for block in blocks:
        if not block.strip():
            continue

        segments = _parse_at_segments(block)

        # 计算纯文本长度
        plain_text = re.sub(r"@\d+", "", block)

        # 模拟打字延迟
        delay = min(max(len(plain_text) / 7, 0.5), 3.0)
        await asyncio.sleep(delay)

        await bot.send(segments, extra_metadata=extra_metadata)

    # 台词发完补发资料图（<report> 制品），再发表情包
    await _send_trailing_artifacts()


async def _send_meme_from_tag(mood: str, bot: Bot, ev: Event) -> None:
    """解析 meme 标记并发送对应表情包"""
    from gsuid_core.ai_core.meme.config import meme_config

    if not meme_config.get_config("meme_enable").data:
        return

    try:
        from gsuid_core.ai_core.meme.library import _read_file, get_memes_base_path
        from gsuid_core.ai_core.meme.selector import pick
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord
        from gsuid_core.ai_core.buildin_tools.meme_tools import _get_persona_for_event

        persona = _get_persona_for_event(ev)
        record, _reason = await pick(
            mood=mood,
            scene="",
            persona=persona,
            session_id=ev.session_id,
        )
        if record is None:
            return

        file_path = get_memes_base_path() / record.file_path
        if not file_path.exists():
            logger.debug(i18n_t("[Meme] 表情包文件不存在: {file_path}", file_path=file_path))
            return

        image_data = await _read_file(file_path)
        img_b64 = await convert_img(image_data)
        await bot.send(MessageSegment.image(img_b64))
        await AiMemeRecord.record_usage(record.meme_id, ev.group_id or "")
        logger.info(i18n_t("[Meme] 标记触发表情包: {p0} (mood={mood})", p0=record.meme_id, mood=mood))
    except Exception as e:
        logger.debug(i18n_t("[Meme] 标记发送失败: {e}", e=e))


def _parse_at_segments(text: str) -> list[Message]:
    """
    将含有 @用户ID 的文本解析为 MessageSegment 列表。

    规则：
    - @后跟纯数字（QQ号格式）才会被解析为 at segment
    - 其余文本保持为 text segment
    - 示例输入："好哦 @444835641 你来看"
    - 示例输出：[Text("好哦 "), At(444835641), Text(" 你来看")]
    """
    # 匹配 @数字，前后允许空格（空格属于分隔符，不计入文本内容）
    pattern = re.compile(r"\s*@(\d+)\s*")
    segments: list[Message] = []
    last_end = 0

    for match in pattern.finditer(text):
        # 匹配前的普通文本
        before = text[last_end : match.start()]
        if before:
            segments.append(MessageSegment.text(before))

        # @ 片段
        user_id = match.group(1)
        segments.append(MessageSegment.at(user_id))

        last_end = match.end()

    # 剩余文本
    tail = text[last_end:]
    if tail:
        segments.append(MessageSegment.text(tail))

    # 如果没有任何 @ 匹配，直接返回原始字符串（兼容旧调用）
    if not segments:
        return [MessageSegment.text(text)]

    return segments


# ======================================================================
# GsCoreAIAgent 运行期的无状态消息/历史工具
# 从 gs_agent.py 抽出：纯函数，只依赖 pydantic_ai 消息类型与 const 常量，不触碰
# Agent 实例状态，便于复用与单测。gs_agent 按原名 import 回去使用。
# ======================================================================


def _is_non_retryable_model_error(e: BaseException) -> bool:
    """该异常是否为"重试也必然复现"的永久性模型错误（4xx 客户端错误，排除 408/429）。"""
    return isinstance(e, ModelHTTPError) and 400 <= e.status_code < 500 and e.status_code not in _RETRYABLE_4XX


def _is_content_rejected(e: ModelHTTPError) -> bool:
    """4xx 错误是否为"内容被模型安全 / 审核策略拒绝"（用于更友好的文案与统计分类）。"""
    blob = (str(e.body or "") + " " + str(e)).lower()
    if any(hint in blob for hint in _CONTENT_REJECT_HINTS):
        return True
    return any(re.search(rf"\b{code}\b", blob) for code in _CONTENT_REJECT_CODES)


def _extract_run_context(history: List[ModelMessage], max_fact_len: int = 2000) -> str:
    """从历史消息中提取"已知事实"和"模型推理片段"，按轮次组织。

    相比只提取 ToolReturnPart，还保留 TextPart（LLM 中间推理），
    因为这些推理有时本身就是有价值的结论。
    """
    sections: list[str] = []
    round_num = 0

    for msg in history:
        if isinstance(msg, ModelResponse):
            round_num += 1
            texts: list[str] = []
            calls: list[str] = []
            for part in msg.parts:
                if isinstance(part, TextPart) and part.content.strip():
                    t = part.content.strip()
                    if len(t) > 500:
                        t = t[:500] + "...[截断]"
                    texts.append(t)
                elif isinstance(part, ToolCallPart):
                    calls.append(part.tool_name)

            if texts or calls:
                header = f"【第{round_num}轮】"
                if calls:
                    header += f" 调用工具: {', '.join(calls)}"
                if texts:
                    header += "\n" + "\n".join(texts)
                sections.append(header)

        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    content = str(part.content).strip()
                    if len(content) > max_fact_len:
                        content = content[:max_fact_len] + f"\n...[截断, 共{len(content)}字符]"
                    sections.append(f"  → [{part.tool_name}] 返回: {content}")

    return "\n".join(sections) if sections else ""


def _truncate_message_for_log(msg: Any, max_base64_len: int = 100) -> Any:
    """
    截断消息中的长 base64 数据，用于日志输出。

    Args:
        msg: 消息内容，可能是 str、ImageUrl 或其列表
        max_base64_len: base64 数据最大显示长度

    Returns:
        截断后的消息副本
    """
    if isinstance(msg, str):
        # 检查是否是 base64 DataURI
        if ";base64," in msg and len(msg) > max_base64_len:
            return f"{msg[:max_base64_len]}...[base64截断, 总长={len(msg)}]"
        return msg
    elif isinstance(msg, ImageUrl):
        url = msg.url
        if ";base64," in url and len(url) > max_base64_len:
            return ImageUrl(url=f"{url[:max_base64_len]}...[base64截断, 总长={len(url)}]")
        return msg
    elif isinstance(msg, list):
        return [_truncate_message_for_log(item, max_base64_len) for item in msg]
    return msg


def _truncate_history_with_tool_safety(
    history: List[ModelMessage],
    max_history: int,
) -> List[ModelMessage]:
    """
    安全截断 history，确保保留的消息中 ToolCallPart 和 ToolReturnPart 完全配对。

    问题：如果简单地从末尾截断 history，可能导致 ToolReturnPart 被保留
    但其对应的 ToolCallPart 被丢弃（在被截断的前半部分），从而在下一轮请求时出现
    "tool result's tool id not found" 错误。

    解决策略：
    1. 先做一次试探性截断：保留最后 max_history 条消息
    2. 扫描截断结果，收集所有保留的 ToolReturnPart 的 tool_call_id
    3. 扫描截断结果，收集所有保留的 ToolCallPart 的 tool_call_id
    4. 如果有 return 找不到对应的 call，说明截断点切到了 tool call/return 对的中间
    5. 向前移动截断点，直到所有保留的 return 都有对应的 call

    Args:
        history: 原始消息历史
        max_history: 最大保留消息数

    Returns:
        截断后的安全消息历史
    """
    if len(history) <= max_history:
        return history

    # 从 max_history 开始，逐步扩大保留范围，直到 tool call/return 完全配对
    truncate_index = len(history) - max_history

    while truncate_index > 0:
        truncated = history[truncate_index:]

        # 收集截断结果中所有 ToolCallPart 的 tool_call_id
        retained_call_ids: Set[str] = set()
        # 收集截断结果中所有 ToolReturnPart 的 tool_call_id
        retained_return_ids: Set[str] = set()

        for msg in truncated:
            if isinstance(msg, ModelResponse):
                for part in msg.parts:
                    if isinstance(part, ToolCallPart):
                        retained_call_ids.add(part.tool_call_id)
            elif isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        retained_return_ids.add(part.tool_call_id)
                    # RetryPromptPart 也是"工具结果型"消息：工具参数校验失败时
                    # 由 PydanticAI 生成，同样带 tool_call_id、必须有配对的
                    # ToolCallPart。tool_name 为 None 时是输出校验重试，不绑定
                    # 具体工具调用，不计入。
                    elif isinstance(part, RetryPromptPart) and part.tool_name is not None:
                        retained_return_ids.add(part.tool_call_id)

        # 找出截断结果中的孤立 return（有 return 但没有对应的 call）
        orphaned = retained_return_ids - retained_call_ids

        if not orphaned:
            # 所有保留的 return 都有对应的 call，截断安全
            logger.debug(
                i18n_t(
                    "🧠 [GsCoreAIAgent] 安全截断 history: {p0} -> {p1} (截断点: {truncate_index})",
                    p0=len(history),
                    p1=len(truncated),
                    truncate_index=truncate_index,
                )
            )
            return truncated

        # 有孤立 return，需要向前移动截断点
        # 找到所有孤立 return 所在的消息索引（相对于原始 history）
        min_orphaned_idx = len(history)  # 初始化为最大值
        for idx, msg in enumerate(history):
            if idx < truncate_index:
                continue  # 只看截断范围内的
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    tcid: Optional[str] = None
                    if isinstance(part, ToolReturnPart):
                        tcid = part.tool_call_id
                    elif isinstance(part, RetryPromptPart) and part.tool_name is not None:
                        tcid = part.tool_call_id
                    if tcid is not None and tcid in orphaned:
                        min_orphaned_idx = min(min_orphaned_idx, idx)

        # 向前移动截断点到孤立 return 之前，再留 2 条消息的缓冲
        new_truncate_index = max(0, min_orphaned_idx - 2)
        if new_truncate_index >= truncate_index:
            # 安全阀：如果无法继续前移，直接保留全部历史
            logger.warning(i18n_t("🧠 [GsCoreAIAgent] 无法安全截断 history，保留全部 {p0} 条", p0=len(history)))
            return history

        truncate_index = new_truncate_index

    # truncate_index == 0，保留全部历史
    logger.debug(i18n_t("🧠 [GsCoreAIAgent] 安全截断 history: {p0} -> {p0} (保留全部)", p0=len(history)))
    return history


def _drop_orphan_tool_results(history: List[ModelMessage]) -> List[ModelMessage]:
    """丢弃所有找不到配对 ToolCallPart 的孤儿工具结果消息。

    最终一致性兜底：即便 ``_truncate_history_with_tool_safety`` 逻辑正确，
    历史里仍可能因并发 / 异常中断残留坏配对（孤儿 ToolReturnPart 或带
    tool_name 的 RetryPromptPart）。本函数在 ``extract_history()`` 末尾被
    无条件调用，保证送进 API 的 message_history 永远自洽——一次坏截断不会
    让 session 永久不可用（"tool result's tool id not found" 400）。
    """
    call_ids: Set[str] = set()
    for msg in history:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    call_ids.add(part.tool_call_id)

    cleaned: List[ModelMessage] = []
    for msg in history:
        if isinstance(msg, ModelRequest):
            kept_parts = []
            for part in msg.parts:
                # 复用同一个 isinstance 守卫：进入分支时 part 类型已被 mypy/Pyright
                # 收窄为 ToolReturnPart / RetryPromptPart，两者都有 tool_call_id，
                # 不需要 getattr 兜底（LLM.md §1.4）。
                if isinstance(part, ToolReturnPart) and part.tool_call_id not in call_ids:
                    logger.warning(
                        i18n_t("🧠 [GsCoreAIAgent] 丢弃孤儿 ToolReturnPart: tool_call_id={p0}", p0=part.tool_call_id)
                    )
                    continue
                if (
                    isinstance(part, RetryPromptPart)
                    and part.tool_name is not None
                    and part.tool_call_id not in call_ids
                ):
                    logger.warning(
                        i18n_t("🧠 [GsCoreAIAgent] 丢弃孤儿 RetryPromptPart: tool_call_id={p0}", p0=part.tool_call_id)
                    )
                    continue
                kept_parts.append(part)
            if kept_parts:
                msg.parts = kept_parts
                cleaned.append(msg)
            # parts 全被丢弃的空 ModelRequest 整条剔除
        else:
            cleaned.append(msg)
    return cleaned


def _strip_remote_images_from_history(history: List[ModelMessage]) -> int:
    """把历史里残留的「远程图片 URL」剥离成文字占位，返回剥离处数量。

    推理端报「Failed to download image」基本都是早先入历史的远程图片 URL
    （如 QQ 带 rkey 的临时链接）已过期。不清掉的话，后续每一轮把它重发给
    推理端都会 500，整个会话被永久卡死。这里把过期的远程 ``ImageUrl`` 替换为
    文字占位，让下一轮自动恢复。base64 DataURI 不会过期，保留不动。
    """
    removed = 0
    for msg in history:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            if not isinstance(part, UserPromptPart):
                continue
            content = part.content
            if isinstance(content, str):
                continue
            new_content: List[UserContent] = []
            changed = False
            for item in content:
                if isinstance(item, ImageUrl) and item.url.startswith(("http://", "https://")):
                    new_content.append("[图片已过期，无法再显示]")
                    changed = True
                    removed += 1
                else:
                    new_content.append(item)
            if changed:
                part.content = new_content
    return removed


# §25(5) 工具返回入史上限：本轮模型已消费过完整返回，持久历史里只需可引用的摘要。
# web_search/stock_financials 等大返回原文滚进历史是 run 内 token 近似 O(N²) 的来源。
_TOOL_RETURN_HISTORY_MAX = 4000
_TOOL_RETURN_HEAD = 3200
_TOOL_RETURN_TAIL = 400

# OOC 修复 5.2：结构化数据工具返回的入史摘要阈值。
# 高密度结构化 JSON（金融指标、持仓列表等）即使低于
# _TOOL_RETURN_HISTORY_MAX，也会污染主人格语域 → OOC。
# 检测基于内容结构（JSON 字段密度），不基于工具名——适配所有工具。
_PROFESSIONAL_TOOL_SUMMARY_MAX = 300


def _looks_like_structured_data(content: str) -> bool:
    """内容是否为高密度结构化数据（JSON dict、字段多、数值占比高）。

    基于内容结构判断，不依赖工具名——任何返回结构化数据的工具
    都会被检测到，无需维护工具名白名单。
    """
    stripped = content.strip()
    if not stripped.startswith("{"):
        return False
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict) or len(data) < 5:
        return False
    numeric = sum(1 for v in data.values() if isinstance(v, (int, float)))
    return numeric / len(data) > 0.4


def _summarize_structured_data(content: str) -> str:
    """结构化数据通用摘要：保留头部关键信息，省略其余。"""
    if len(content) <= _PROFESSIONAL_TOOL_SUMMARY_MAX:
        return content
    head = content[:200]
    return f"{head}…[结构化数据已摘要，完整返回仅当前轮可见]…"


def _truncate_tool_returns_in_history(messages: List[ModelMessage]) -> int:
    """把将持久化的 ToolReturnPart 内容截断/摘要，返回处理数。

    只影响写入 self.history 的副本语义（原地改 part.content）；
    当前轮模型看到的仍是完整返回。

    OOC 修复 5.2：两层处理——
    1. 高密度结构化数据（基于内容检测，不依赖工具名）：
       无论长度都摘要，切断专业语域对主人格上下文的污染。
    2. 其他工具：超过 _TOOL_RETURN_HISTORY_MAX 做头+尾截断。
    """
    truncated = 0
    for msg in messages:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            # v2.0 新增 ToolSearchReturnPart 等 part 类型，
            # 那些的 content 不容许赋值 str。type(part) is
            # ToolReturnPart 是精确类型守门（子类不影响）。
            if type(part) is not ToolReturnPart or not isinstance(part.content, str):
                continue
            content = part.content

            # 层 1：结构化数据 → 基于内容检测，无论长度都摘要
            if _looks_like_structured_data(content):
                new_content = _summarize_structured_data(content)
                if new_content != content:
                    part.content = new_content
                    truncated += 1
                continue

            # 层 2：其他工具 → 超长截断
            if len(content) <= _TOOL_RETURN_HISTORY_MAX:
                continue
            omitted = len(content) - _TOOL_RETURN_HEAD - _TOOL_RETURN_TAIL
            head, tail = content[:_TOOL_RETURN_HEAD], content[-_TOOL_RETURN_TAIL:]
            part.content = f"{head}\n…[工具返回过长，入史省略 {omitted} 字符]…\n{tail}"
            truncated += 1
    return truncated


def _compact_report_blocks_in_history(
    messages: List[ModelMessage],
    sent_texts: Optional[Set[str]] = None,
) -> int:
    """把将持久化的 assistant 文本里的 ``<report>`` 块替换为占位符，返回替换数。

    资料图已发出，正文无需留在 self.history：既省 token，又切断"模型每轮看到
    自己在念研报 → 研报腔固化为人格语气"的自我强化回路（§1 漂移固化）。
    占位符保留标题，后续轮仍能引用"我刚发过什么资料"。

    ``sent_texts``：本轮实际发送成功的原始文本集合（gs_agent._run_sent_texts）。
    给定时只压缩「确实发出去过」的 part——被拦截/暂扣/发送失败的文本不得谎称
    已发资料图（评审修复 E5）。
    """

    def _placeholder(match: "re.Match[str]") -> str:
        title = _report_block_title(match) or "分析资料"
        return f"【已发资料图：{title}】"

    replaced = 0
    for msg in messages:
        if not isinstance(msg, ModelResponse):
            continue
        for part in msg.parts:
            if not isinstance(part, TextPart) or "<report" not in part.content.lower():
                continue
            if sent_texts is not None and part.content.strip() not in sent_texts:
                continue
            new_content = _REPORT_BLOCK_RE.sub(_placeholder, part.content)
            if new_content != part.content:
                part.content = new_content
                replaced += 1
    return replaced


def _relean_user_turn(
    new_messages: List[ModelMessage],
    lean_content: Union[str, List[UserContent]],
    strip_hint_texts: Tuple[str, ...] = (),
) -> None:
    """把本轮 new_messages 里的用户输入 turn 换成精简版（剥离 rag_context）。

    每轮 ``final_user_message`` 含【历史对话】/记忆/群语境等 rag_context，若原样
    ``extend`` 进 self.history，会在 max_history 窗口内逐轮累积同类快照——既膨胀
    input，又冲淡缓存。存历史时只保留用户真实发言（当前轮仍给模型看完整上下文）。
    改第一条 UserPromptPart（工具往返的 ToolReturnPart 不动）；``strip_hint_texts``
    是框架 run 中途注入的提示常量（如 C-4 墙钟 nudge，挂在**后续** ModelRequest 上、
    首条替换够不着）——按内容精确匹配从持久历史里剥掉，防提示噪声跨轮累积。
    """
    leaned = False
    for msg in new_messages:
        if not isinstance(msg, ModelRequest):
            continue
        kept_parts = []
        for part in msg.parts:
            if isinstance(part, UserPromptPart):
                if not leaned:
                    part.content = lean_content
                    leaned = True
                elif isinstance(part.content, str) and part.content in strip_hint_texts:
                    continue
            kept_parts.append(part)
        if len(kept_parts) != len(msg.parts):
            msg.parts = kept_parts


def _split_embedded_thinking(
    parts: Sequence[ModelResponsePart],
    thinking_tags: tuple[str, str],
) -> List[ModelResponsePart]:
    """把 TextPart 里以 thinking_tags 包裹的内嵌思考重新拆成独立的 ThinkingPart。

    流式请求下 pydantic_ai 只有在 ``<think>`` 作为独立 SSE chunk 到达时才会拆分思考
    标签（见 _parts_manager.handle_text_delta）；MiniMax 等兼容网关不保证这一点，
    导致 ``<think>...</think>`` 残留在 TextPart 里被原样发往 C 端，且思考内容拿不到
    意图-行为一致性检测。这里按完整文本重新拆分，对齐非流式路径
    （openai 模型的 split_content_into_text_and_thinking）的行为。非 TextPart 与不含
    起始标签的 TextPart 原样透传。
    """
    start_tag, end_tag = thinking_tags
    result: List[ModelResponsePart] = []
    for part in parts:
        if not isinstance(part, TextPart) or start_tag not in part.content:
            result.append(part)
            continue
        content = part.content
        start_index = content.find(start_tag)
        while start_index >= 0:
            before, content = content[:start_index], content[start_index + len(start_tag) :]
            if before:
                result.append(TextPart(content=before))
            end_index = content.find(end_tag)
            if end_index >= 0:
                think, content = content[:end_index], content[end_index + len(end_tag) :]
                result.append(ThinkingPart(content=think))
            else:
                # 缺少闭合标签：丢弃 <think> 起始标签，剩余内容按文本处理
                result.append(TextPart(content=content))
                content = ""
            start_index = content.find(start_tag)
        if content:
            result.append(TextPart(content=content))
    return result


def _canonicalize_tool_call_args_in_parts(
    parts: Sequence[ModelResponsePart],
) -> List[ModelResponsePart]:
    """把 ToolCallPart 的字符串参数 json 解析后重序列化（规范化 + 去重复键）。

    弱模型退化输出会产生重复键参数串（如 ``"args": {}`` 重复上百次）：本地
    ``json.loads`` 视为合法（后键覆盖），但 pydantic_ai 会把**原始串**回放给
    provider，部分网关（MiniMax）对重复键直接 400 且被判为不可重试，导致整个
    run 静默死亡（见 plans/prod_session_review §2）。解析失败的参数原样保留，
    交由 pydantic_ai 的工具参数校验 → 模型重试流程处理。原地改写 part.args，
    使工具执行、history 回放、session 日志三处一致。
    """
    for part in parts:
        if not isinstance(part, ToolCallPart):
            continue
        if not isinstance(part.args, str) or not part.args.strip():
            continue
        # 只在检测到真实重复键时才改写：正常紧凑 JSON 保持原字节（历史与模型输出
        # 一致），也不刷屏告警淹没真正的退化信号（评审修复 F15）。
        dup_found: List[bool] = []

        def _pairs_hook(
            pairs: List[Tuple[str, Any]],
            _dup: List[bool] = dup_found,
        ) -> Dict[str, Any]:
            if len(pairs) != len({k for k, _ in pairs}):
                _dup.append(True)
            return dict(pairs)

        try:
            parsed = json.loads(part.args, object_pairs_hook=_pairs_hook)
        except ValueError:
            continue
        if not dup_found:
            continue
        canonical = json.dumps(parsed, ensure_ascii=False)
        if canonical != part.args:
            logger.warning(
                i18n_t(
                    "🧠 [GsCoreAIAgent] 工具 {p0} 参数含重复键，已规范化（原始 {p1} 字符 → {p2} 字符）",
                    p0=part.tool_name,
                    p1=len(part.args),
                    p2=len(canonical),
                )
            )
            part.args = canonical
    return list(parts)


def _is_retryable_client_error(e: BaseException) -> bool:
    """永久性 4xx 中「非内容审核拒绝」的那部分是否值得一次干净历史重试。

    模型退化产生的畸形请求（如重复键工具参数）是**随机性**的：从未被污染的
    self.history 重跑大概率成功；而内容审核拒绝是确定性的，重试必复现。
    """
    return isinstance(e, ModelHTTPError) and _is_non_retryable_model_error(e) and not _is_content_rejected(e)


def sanitize_error_for_user(result_text: str) -> str:
    """把 ``执行出错: <内部细节>`` 转成不泄漏内部细节的用户可见短文案。

    原始错误串含 provider body / model_name / tool_call_id 等内部信息，直接发进
    群聊既难看又泄漏实现；完整细节已由 log_error 落日志，用户侧只需要知道失败了。
    """
    if result_text == NO_RESULT_TEXT:
        return "这条消息我处理失败了，稍后再试一次吧"
    if not result_text.startswith(ERROR_RESULT_PREFIX):
        return result_text
    # 文案不得是整行（…）形态：_strip_persona_markdown 会把整行括号当舞台旁白删除（评审修复 F2）
    if ERROR_CONTENT_REJECTED in result_text:
        return "这条消息触发了内容安全策略，我没法处理"
    if ERROR_TIMEOUT_TEXT in result_text:
        return "刚才网络太慢处理超时了，稍后再试试吧"
    return "这条消息我处理失败了，稍后再试一次吧"


# Agent 失败类型分类标签 —— 仅供 notify_master_of_agent_error 私聊主人时使用，
# 与 sanitize_error_for_user 共用同一组常量做嗅探，保证两处判断永远一致。
_ERROR_TYPE_LABEL_NO_RESULT = "无有效结果"
_ERROR_TYPE_LABEL_CONTENT = "内容安全"
_ERROR_TYPE_LABEL_TIMEOUT = "超时"
_ERROR_TYPE_LABEL_OTHER = "其他错误"
_ERROR_TYPE_LABEL_UNKNOWN = "未知"


# 私聊主人 DM 的字段截断长度 —— 用户原文与原始错误都不宜过长
_MASTER_DM_RAW_TEXT_MAX = 200
_MASTER_DM_RESULT_MAX = 500


class _MasterDMTarget(Protocol):
    """能向指定目标发送私聊/群消息的最小接口。"""

    async def target_send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        target_type: Literal["group", "direct", "channel", "sub_channel"],
        target_id: Optional[str],
        at_sender: bool = False,
        sender_id: str = "",
        send_source_group: Optional[str] = None,
        wait_recall: bool = False,
    ) -> Optional[List[str]]: ...


class _MasterDMEvent(Protocol):
    """主人 DM 所需的事件字段子集。"""

    @property
    def session_id(self) -> str: ...
    @property
    def user_id(self) -> str: ...
    @property
    def group_id(self) -> Optional[str]: ...
    @property
    def bot_id(self) -> str: ...
    @property
    def raw_text(self) -> str: ...


def classify_error_type(result_text: str) -> str:
    """把 agent run 返回的错误串归类成给主人看的中文标签。"""
    if result_text == NO_RESULT_TEXT:
        return _ERROR_TYPE_LABEL_NO_RESULT
    if not result_text.startswith(ERROR_RESULT_PREFIX):
        return _ERROR_TYPE_LABEL_UNKNOWN
    if ERROR_CONTENT_REJECTED in result_text:
        return _ERROR_TYPE_LABEL_CONTENT
    if ERROR_TIMEOUT_TEXT in result_text:
        return _ERROR_TYPE_LABEL_TIMEOUT
    return _ERROR_TYPE_LABEL_OTHER


def _build_agent_error_report(
    *,
    session_id: str,
    user_id: str,
    group_id: Optional[str],
    bot_id: str,
    error_type: str,
    result_text: str,
    user_facing: str,
    raw_text: str,
) -> str:
    """构造给主人的「AI 执行失败」结构化小报告。"""
    group_label = group_id if group_id else "私聊"
    truncated_raw = raw_text.strip()
    if len(truncated_raw) > _MASTER_DM_RAW_TEXT_MAX:
        truncated_raw = truncated_raw[:_MASTER_DM_RAW_TEXT_MAX] + "..."
    truncated_err = result_text.strip()
    if len(truncated_err) > _MASTER_DM_RESULT_MAX:
        truncated_err = truncated_err[:_MASTER_DM_RESULT_MAX] + "..."
    return (
        "[AI 执行失败]\n"
        f"• 会话: {session_id}\n"
        f"• 用户: {user_id} (群: {group_label})\n"
        f"• Bot: {bot_id}\n"
        f"• 类型: {error_type}\n"
        f"• 用户看到: {user_facing}\n"
        f"• 原始错误: {truncated_err}\n"
        f"• 用户原文: {truncated_raw}"
    )


def _build_budget_block_report(
    *,
    session_id: str,
    user_id: str,
    group_id: Optional[str],
    bot_id: str,
    block_scope_label: str,
    raw_text: str,
) -> str:
    """构造给主人的「AI 预算超额拦截」结构化小报告。"""
    group_label = group_id if group_id else "私聊"
    truncated_raw = raw_text.strip()
    if len(truncated_raw) > _MASTER_DM_RAW_TEXT_MAX:
        truncated_raw = truncated_raw[:_MASTER_DM_RAW_TEXT_MAX] + "..."
    return (
        "[AI 预算超额拦截]\n"
        f"• 会话: {session_id}\n"
        f"• 用户: {user_id} (群: {group_label})\n"
        f"• Bot: {bot_id}\n"
        f"• 拦截维度: {block_scope_label}\n"
        f"• 用户原文: {truncated_raw}"
    )


async def _dispatch_master_dm(
    bot: _MasterDMTarget,
    report: str,
    masters: List[str],
    log_prefix: str,
) -> None:
    """把 ``report`` 私聊发给每个主人；单个主人失败不影响其他主人。"""
    for master_id in masters:
        master_id = str(master_id)
        if not master_id:
            continue
        try:
            await bot.target_send(report, "direct", target_id=master_id)
        except Exception as e:
            logger.warning(
                i18n_t(
                    "{p0} 主人通知发送失败 ({master_id}): {e}",
                    p0=log_prefix,
                    master_id=master_id,
                    e=e,
                )
            )


async def notify_master_of_agent_error(
    bot: _MasterDMTarget,
    ev: _MasterDMEvent,
    *,
    error_type: str,
    result_text: str,
    user_facing: str,
) -> None:
    """Agent run 失败后，把结构化错误报告私聊发给每个主人。

    未配置 ``masters`` 时 no-op；单个主人 DM 失败被吞掉，不污染主流程。
    """
    from gsuid_core.config import core_config

    masters: List[str] = [str(m) for m in (core_config.get_config("masters") or [])]
    if not masters:
        return
    report = _build_agent_error_report(
        session_id=ev.session_id,
        user_id=ev.user_id,
        group_id=ev.group_id,
        bot_id=ev.bot_id,
        error_type=error_type,
        result_text=result_text,
        user_facing=user_facing,
        raw_text=ev.raw_text,
    )
    await _dispatch_master_dm(bot, report, masters, log_prefix="🧠 [GsCore][AI]")


async def notify_master_of_budget_block(
    bot: _MasterDMTarget,
    ev: _MasterDMEvent,
    *,
    decision: Any,
) -> None:
    """预算超额拦截时，把结构化告警私聊发给每个主人。

    ``decision`` 避免在 utils.py 顶层 import ``BudgetDecision``，防止与 budget 子模块
    循环 import；调用方保证传入的是该 dataclass 实例。
    """
    from gsuid_core.config import core_config

    masters: List[str] = [str(m) for m in (core_config.get_config("masters") or [])]
    if not masters:
        return
    block_scope_label = str(decision.block_scope_label or "")
    report = _build_budget_block_report(
        session_id=ev.session_id,
        user_id=ev.user_id,
        group_id=ev.group_id,
        bot_id=ev.bot_id,
        block_scope_label=block_scope_label,
        raw_text=ev.raw_text,
    )
    await _dispatch_master_dm(bot, report, masters, log_prefix="💰 [GsCore][AI]")


def _sanitize_tool_call_artifacts_in_parts(
    parts: Sequence[ModelResponsePart],
) -> List[ModelResponsePart]:
    """清除各 TextPart 泄漏的工具调用标记残留，并丢弃被清空的 TextPart。

    与 :func:`_split_embedded_thinking` 配套，在其拆出内嵌 thinking 之后调用。整体替换
    ``node.model_response.parts``，使 history、result.output、下发文本三处一并保持干净——
    避免"这条没发但 history 里留着 <tool_call>，诱导模型下一轮继续这么输出"。

    被清理为空的 TextPart 直接丢弃、不留空串污染 history；但当丢弃会使整条响应不剩任何
    part 时（响应整段就是泄漏的工具调用），保留一个空 TextPart 占位，规避部分网关
    "assistant 消息必须有内容"的报错。ToolCallPart / ThinkingPart 原样透传。
    """
    kept: List[ModelResponsePart] = []
    for part in parts:
        if isinstance(part, TextPart):
            cleaned = _strip_special_control_tokens(_strip_tool_call_artifacts(part.content))
            if cleaned != part.content:
                if not cleaned.strip():
                    continue  # 整段都是残留 → 丢弃，不留空串
                part.content = cleaned
        kept.append(part)
    if not kept:
        return [TextPart(content="")]
    return kept
