"""会话历史压缩、工具返回瘦身与入史清理。"""

from __future__ import annotations

import re
import json
from typing import Set, Dict, List, Tuple, Union, Optional, Sequence
from dataclasses import replace as _dc_replace

from pydantic_ai.messages import (
    ImageUrl,
    TextPart,
    UserContent,
    ModelMessage,
    ModelRequest,
    ToolCallPart,
    BinaryContent,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
    RetryPromptPart,
    ModelResponsePart,
)

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.protocol_text import (
    _strip_tool_call_artifacts,
    _strip_special_control_tokens,
    normalize_escaped_control_tags,
)


def _truncate_message_for_log(msg: object, max_base64_len: int = 100) -> object:
    """
    截断消息中的长 base64 数据，用于日志输出。

    Args:
        msg: 消息内容，可能是 str、ImageUrl 或其列表
        max_base64_len: base64 数据最大显示长度

    Returns:
        截断后的消息副本
    """
    if isinstance(msg, str):
        if ";base64," in msg and len(msg) > max_base64_len:
            return f"{msg[:max_base64_len]}...[base64截断, 总长={len(msg)}]"
        return msg
    elif isinstance(msg, ImageUrl):
        url = msg.url
        if ";base64," in url and len(url) > max_base64_len:
            return ImageUrl(url=f"{url[:max_base64_len]}...[base64截断, 总长={len(url)}]")
        return msg
    elif isinstance(msg, BinaryContent):
        return f"BinaryContent(media_type={msg.media_type!r}, bytes={len(msg.data)})"
    elif isinstance(msg, list):
        return [_truncate_message_for_log(item, max_base64_len) for item in msg]
    return msg


def _collect_tool_call_return_ids(messages: Sequence[ModelMessage]) -> tuple[Set[str], Set[str]]:
    """扫描消息序列，收集 ToolCall / ToolReturn(含 Retry) 的 tool_call_id 集合。"""
    call_ids: Set[str] = set()
    return_ids: Set[str] = set()
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    call_ids.add(part.tool_call_id)
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    return_ids.add(part.tool_call_id)
                elif isinstance(part, RetryPromptPart) and part.tool_name is not None:
                    return_ids.add(part.tool_call_id)
    return call_ids, return_ids


def _has_orphan_tool_returns(messages: Sequence[ModelMessage]) -> bool:
    """保留集里是否存在断裂的工具调用或返回配对。"""
    call_ids, return_ids = _collect_tool_call_return_ids(messages)
    return bool((return_ids - call_ids) or (call_ids - return_ids))


_LEAN_TOOL_RETURN_NAMES: frozenset[str] = frozenset(
    {
        "search_cognition",
        "read_handle",
        "find_tools",
        "web_search_tool",
        "web_fetch_tool",
        "read_chat_history",
        "read_life_journal",
        "capability_map",
    }
)
_LEAN_TOOL_RETURN_THRESHOLD = 900
_LEAN_TOOL_RETURN_HEAD = 360


def _call_tool_inner_name_from_part(part: ToolCallPart) -> str:
    if part.tool_name != "call_tool":
        return ""
    args_dict = part.args_as_dict()
    if "tool_name" in args_dict and isinstance(args_dict["tool_name"], str):
        return args_dict["tool_name"].strip()
    return ""


def _lean_oversized_tool_returns(messages: List[ModelMessage]) -> None:
    """compact 尾段瘦身：仅折叠只读/检索类超大回执，保留写操作/任务类 call_tool 完整 ID。"""
    inner_by_cid: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart) and part.tool_call_id and part.tool_name == "call_tool":
                    inner = _call_tool_inner_name_from_part(part)
                    if inner:
                        inner_by_cid[part.tool_call_id] = inner

    for msg_idx, msg in enumerate(messages):
        if not isinstance(msg, ModelRequest):
            continue
        new_parts = list(msg.parts)
        changed = False
        for idx, part in enumerate(new_parts):
            if not isinstance(part, ToolReturnPart):
                continue
            eff_name = (
                inner_by_cid[part.tool_call_id]
                if part.tool_name == "call_tool" and part.tool_call_id and part.tool_call_id in inner_by_cid
                else (part.tool_name or "")
            )
            if eff_name not in _LEAN_TOOL_RETURN_NAMES:
                continue
            if not isinstance(part.content, str) or len(part.content) <= _LEAN_TOOL_RETURN_THRESHOLD:
                continue
            orig_len = len(part.content)
            folded = (
                part.content[:_LEAN_TOOL_RETURN_HEAD].rstrip()
                + f"\n…（已入史折叠，原长 {orig_len} 字；需复查原文请重调对应句柄或检索）"
            )
            new_parts[idx] = _dc_replace(part, content=folded)
            changed = True
        if changed:
            messages[msg_idx] = _dc_replace(msg, parts=new_parts)


def _truncate_history_keep_prefix_with_split(
    history: List[ModelMessage],
    max_keep: int,
    *,
    prefix_ratio: float = 0.55,
) -> tuple[List[ModelMessage], int]:
    """前缀稳定截断：永不丢头部，只裁中段，保留近期尾部，同时返回保留前缀长度。"""
    if len(history) <= max_keep:
        return history, len(history)
    if max_keep <= 1:
        return list(history[-1:]), 0

    prefix_n = max(2, int(max_keep * prefix_ratio))
    prefix_n = min(prefix_n, max_keep - 1)
    # 若 prefix_n 切在未闭合 tool call，向前收缩至完整轮次（防悬空 call 引发 400）
    while prefix_n > 1:
        p_calls, p_rets = _collect_tool_call_return_ids(history[:prefix_n])
        if not (p_calls - p_rets):
            break
        prefix_n -= 1

    tail_budget = max_keep - prefix_n
    tail_start = len(history) - tail_budget

    while tail_start > prefix_n:
        retained = history[:prefix_n] + history[tail_start:]
        if not _has_orphan_tool_returns(retained):
            logger.debug(
                i18n_t(
                    "log.ai.safe_truncation_history_cutoff",
                    p0=len(history),
                    p1=len(retained),
                    truncate_index=tail_start,
                )
            )
            return retained, prefix_n
        tail_start -= 1

    if not _has_orphan_tool_returns(history):
        logger.warning(i18n_t("log.ai.cannot_safely_truncate_history", p0=len(history)))
    return history, len(history)


def _extractive_middle_summary(dropped: Sequence[ModelMessage]) -> str:
    """被裁中段的抽取摘要。LLM 蒸馏失败时的摊还重写材料。"""
    bits: list[str] = []
    used = 0
    for msg in dropped:
        chunk = ""
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart) and part.content.strip():
                    chunk = part.content.strip()[:80]
                    break
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    body = part.content.strip()
                    if _is_framework_prompt_content(body):
                        continue
                    chunk = body[:80]
                    break
        if not chunk:
            continue
        bits.append(chunk)
        used += len(chunk)
        if used >= 400:
            break
    if not bits:
        return ""
    body = " / ".join(bits)[:400]
    return f"（更早对话摘要：{body}）"


def compact_session_history(
    history: List[ModelMessage],
    max_history: int,
    *,
    trim_ratio: float = 0.6,
    prefix_ratio: float = 0.55,
) -> tuple[List[ModelMessage], bool]:
    """会话 history 高低水位 compact（保头裁中段 + 仅在压缩时折叠尾段超大回执）。

    Returns:
        (new_history, did_truncate) — did_truncate 表示因超水位主动丢过中段。
    """
    if max_history <= 0:
        return [], True
    before = len(history)
    if before <= max_history:
        return _drop_orphan_tool_results(history), False
    low_target = max(1, int(max_history * trim_ratio))
    trimmed, prefix_n = _truncate_history_keep_prefix_with_split(
        history,
        low_target,
        prefix_ratio=prefix_ratio,
    )
    # 仅折叠截断点之后的旧超大回执，保留 prefix_n 前缀字节完全不变以保 KV 缓存
    if prefix_n < len(trimmed):
        tail = trimmed[prefix_n:]
        _lean_oversized_tool_returns(tail)
        trimmed = [*trimmed[:prefix_n], *tail]
    cleaned = _drop_orphan_tool_results(trimmed)
    return cleaned, len(cleaned) < before


def _truncate_history_with_tool_safety(
    history: List[ModelMessage],
    max_history: int,
) -> List[ModelMessage]:
    """
    安全截断 history，确保保留的消息中 ToolCallPart 和 ToolReturnPart 完全配对。

    .. deprecated::
        主会话请用 ``compact_session_history``（保头裁中段，前缀缓存友好）。
        本函数仍为「保留尾部」语义，供需要旧行为的调用方。

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

    truncate_index = len(history) - max_history

    while truncate_index > 0:
        truncated = history[truncate_index:]

        call_ids, return_ids = _collect_tool_call_return_ids(truncated)
        orphaned = return_ids - call_ids

        if not orphaned:
            logger.debug(
                i18n_t(
                    "log.ai.safe_truncation_history_cutoff",
                    p0=len(history),
                    p1=len(truncated),
                    truncate_index=truncate_index,
                )
            )
            return truncated

        min_orphaned_idx = len(history)
        for idx, msg in enumerate(history):
            if idx < truncate_index:
                continue
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    tcid: Optional[str] = None
                    if isinstance(part, ToolReturnPart):
                        tcid = part.tool_call_id
                    elif isinstance(part, RetryPromptPart) and part.tool_name is not None:
                        tcid = part.tool_call_id
                    if tcid is not None and tcid in orphaned:
                        min_orphaned_idx = min(min_orphaned_idx, idx)

        new_truncate_index = max(0, min_orphaned_idx - 2)
        if new_truncate_index >= truncate_index:
            logger.warning(i18n_t("log.ai.cannot_safely_truncate_history", p0=len(history)))
            return history

        truncate_index = new_truncate_index

    logger.debug(i18n_t("log.ai.safe_truncation_history_kept", p0=len(history)))
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
                if isinstance(part, ToolReturnPart) and part.tool_call_id not in call_ids:
                    logger.warning(i18n_t("log.ai.dropping_orphan_toolreturnpart_call", p0=part.tool_call_id))
                    continue
                if (
                    isinstance(part, RetryPromptPart)
                    and part.tool_name is not None
                    and part.tool_call_id not in call_ids
                ):
                    logger.warning(i18n_t("log.ai.dropping_orphan_retrypromptpart_call", p0=part.tool_call_id))
                    continue
                kept_parts.append(part)
            if kept_parts:
                msg.parts = kept_parts
                cleaned.append(msg)
        else:
            cleaned.append(msg)
    return cleaned


_IMAGE_ERROR_HINTS: tuple[str, ...] = (
    "image_url",
    "unsupported image",
    "invalid image",
    "download image",
    "image format",
    "decode image",
    'url protocol "data"',
)
_MESSAGES_IMAGE_ERR_RE = re.compile(r"\.messages\[\d+\]\.image", re.IGNORECASE)


def _is_image_payload_error(err_str: str) -> bool:
    """异常信息是否指向图片载荷异常（过期外链 / 损坏 base64 / 不支持的图片格式）。"""
    low = (err_str or "").lower()
    if any(h in low for h in _IMAGE_ERROR_HINTS):
        return True
    return _MESSAGES_IMAGE_ERR_RE.search(low) is not None


def _strip_remote_images_from_history(
    history: List[ModelMessage],
    *,
    include_inline: bool = False,
) -> int:
    """剥离 history 中已过期的远程图片 URL（或被上游拒绝的内联图片）。"""
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
                if isinstance(item, ImageUrl) and (include_inline or item.url.startswith(("http://", "https://"))):
                    new_content.append("[图片已失效，无法再显示]")
                    changed = True
                    removed += 1
                elif (
                    include_inline
                    and isinstance(item, BinaryContent)
                    and str(item.media_type or "").startswith("image/")
                ):
                    new_content.append("[图片已失效，无法再显示]")
                    changed = True
                    removed += 1
                else:
                    new_content.append(item)
            if changed:
                part.content = new_content
    return removed


_TOOL_RETURN_HISTORY_MAX = 12_000
_TOOL_RETURN_HEAD = 9_000
_TOOL_RETURN_TAIL = 1_500
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


_DELIVERY_INSTR = ("create_subagent", "send_message_by_ai", "禁止", "你是主人格", "长文尚未", "出图委派")


def lean_delivery_frame(full: str) -> str:
    """交付包入史瘦身：任务号 + 句柄 + 主题。不取说明书行。"""
    s = (full or "").strip()
    if not s:
        return ""
    ordinal = "?"
    m_ord = re.search(r"任务#(\d+)", s)
    if m_ord is not None:
        ordinal = m_ord.group(1)
    handles = re.findall(r"res_[0-9a-fA-F]{3,}", s)
    handle = handles[0] if handles else "-"
    topic = ""
    m_title = re.search(r"任务#\d+「([^」]+)」", s)
    if m_title is not None:
        topic = m_title.group(1).strip()[:40]
    if not topic:
        for line in s.splitlines():
            t = line.strip()
            if re.match(r"res_[0-9a-fA-F]{3,}\s*\|", t):
                bits = [p.strip() for p in t.split("|")]
                if len(bits) >= 3 and bits[-1]:
                    topic = bits[-1][:40]
                    break
    if not topic:
        for line in s.splitlines():
            t = line.strip()
            if not t or t.startswith("[框架") or t.startswith("【子任务"):
                continue
            if t.startswith("产物") or t.startswith("💡") or t.startswith("你是主人格"):
                continue
            if any(h in t for h in _DELIVERY_INSTR):
                continue
            topic = t[:40]
            break
    tail = f"，{topic}" if topic else ""
    return f"[框架·任务完成] 任务#{ordinal} 完成，句柄 {handle}{tail}"


def _is_delivery_frame(content: str) -> bool:
    s = content.lstrip()
    return s.startswith("[框架·任务完成]") or s.startswith("【子任务交付")


_EPHEMERAL_SYSTEM_PREFIXES: tuple[str, ...] = (
    "（系统：",
    "（系统提示：",
    "（系统校验",
)


def _peel_ephemeral_system_lines(content: str) -> str:
    """入史前剥（系统：）类 per-turn 提示，避免污染后续轮。"""
    kept: list[str] = []
    for ln in content.splitlines():
        s = ln.strip()
        if s.startswith(_EPHEMERAL_SYSTEM_PREFIXES):
            continue
        kept.append(ln)
    return "\n".join(kept).rstrip("\n")


def _hint_matches_strip(content: str, strip_hint_texts: Tuple[str, ...]) -> bool:
    return any(content == h or (bool(h) and content.startswith(h)) for h in strip_hint_texts)


def _relean_user_prompt_part(
    part: UserPromptPart,
    lean_content: Union[str, List[UserContent]],
    strip_hint_texts: Tuple[str, ...],
) -> tuple[UserPromptPart | None, bool]:
    content = part.content
    if isinstance(content, str):
        if _is_framework_prompt_content(content):
            if _is_delivery_frame(content):
                lean = lean_content if isinstance(lean_content, str) and lean_content else lean_delivery_frame(content)
                if lean:
                    return UserPromptPart(content=lean), True
            return None, False
        if _hint_matches_strip(content, strip_hint_texts):
            return None, False
        did_lean = isinstance(lean_content, str) and bool(lean_content)
        target = lean_content if isinstance(lean_content, str) and lean_content else content
        peeled = _peel_ephemeral_system_lines(target)
        if not peeled.strip():
            return None, did_lean
        if peeled != content:
            return UserPromptPart(content=peeled), did_lean
        return part, did_lean
    if isinstance(lean_content, list) and bool(lean_content):
        did_lean = True
        source_items: Sequence[UserContent] = lean_content
    elif isinstance(lean_content, str) and bool(lean_content):
        did_lean = True
        replaced_str = False
        rebuilt: list[UserContent] = []
        for item in content:
            if isinstance(item, str) and not replaced_str:
                rebuilt.append(lean_content)
                replaced_str = True
            else:
                rebuilt.append(item)
        source_items = rebuilt
    else:
        did_lean = False
        source_items = content
    new_items: list[UserContent] = []
    changed = did_lean
    for item in source_items:
        if not isinstance(item, str):
            new_items.append(item)
            continue
        if _is_framework_prompt_content(item) and not _is_delivery_frame(item):
            changed = True
            continue
        if _hint_matches_strip(item, strip_hint_texts):
            changed = True
            continue
        peeled = _peel_ephemeral_system_lines(item)
        if not peeled.strip():
            changed = True
            continue
        if peeled != item:
            changed = True
        new_items.append(peeled)
    if not new_items:
        return None, did_lean
    if changed:
        return UserPromptPart(content=new_items), did_lean
    return part, did_lean


def _relean_user_turn(
    new_messages: Sequence[ModelMessage],
    lean_content: Union[str, List[UserContent]],
    strip_hint_texts: Tuple[str, ...] = (),
) -> None:
    """首条真人 UserPromptPart 替换为 lean_content（剥离 rag_context），并剥校验注入与框架块。"""
    leaned = False
    for msg in new_messages:
        if not isinstance(msg, ModelRequest):
            continue
        kept_parts = []
        for part in msg.parts:
            if isinstance(part, UserPromptPart):
                kept, did_lean = _relean_user_prompt_part(
                    part,
                    lean_content if not leaned else "",
                    strip_hint_texts,
                )
                if did_lean:
                    leaned = True
                if kept is not None:
                    kept_parts.append(kept)
                continue
            kept_parts.append(part)
        msg.parts = kept_parts


_LEGACY_FRAMEWORK_PREFIXES: tuple[str, ...] = (
    "[框架·",
    "[系统·",
    "（系统校验",
    "（系统：",
    "（系统提示：",
    "<control",
)

_USER_SPEECH_SHELL = "[用户发言]"


def _is_framework_prompt_content(content: str) -> bool:
    """框架/校验注入：不得当作真人发言进入 B 轨。"""
    s = content.lstrip()
    if s.startswith(_USER_SPEECH_SHELL):
        s = s[len(_USER_SPEECH_SHELL) :].lstrip()
    return s.startswith(_LEGACY_FRAMEWORK_PREFIXES)


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
        dup_found: List[bool] = []

        def _pairs_hook(
            pairs: List[Tuple[str, object]],
            _dup: List[bool] = dup_found,
        ) -> Dict[str, object]:
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
                    "log.ai.args_duplicate_keys_normalized",
                    p0=part.tool_name,
                    p1=len(part.args),
                    p2=len(canonical),
                )
            )
            part.args = canonical
    return list(parts)


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
                    continue
                part.content = cleaned
        kept.append(part)
    if not kept:
        return [TextPart(content="")]
    return kept


def normalize_escaped_control_tags_in_parts(
    parts: Sequence[ModelResponsePart],
) -> List[ModelResponsePart]:
    """规范化 TextPart 内泄漏的转义控制标签（如 &lt;SILENCE&gt; / &lt;think&gt;）。"""
    result: List[ModelResponsePart] = []
    for part in parts:
        if not isinstance(part, TextPart):
            result.append(part)
            continue
        normalized = normalize_escaped_control_tags(part.content)
        if normalized == part.content:
            result.append(part)
        else:
            part.content = normalized
            result.append(part)
    return result
