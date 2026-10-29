"""长任务的开场句：模型在委派的同一次回复里写的那句话，派发成功后由工具代发。

loop 在 CallToolsNode 用 ``arm_opening_line`` 暂存台词（工具旁的字一律不直接发）；
``create_subagent`` 派发成功、后台开工后调 ``release_opening_line`` 过闸发送；
loop 在下一次 ModelRequestNode 用 ``take_opening_result`` 记账。
一轮只开一次口：槽被第一个调用取走，并列的第二个调用拿不到。
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import MutableMapping

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.output_gate import tool_gate_feedback
from gsuid_core.ai_core.agent_run.speech_policy import (
    non_master_title,
    should_block_user_visible_text,
)

OPENING_SLOT_KEY = "opening_line_slot"
OPENING_RESULT_KEY = "opening_line_result"

_OPENING_CREATE_BY = frozenset({"Chat", "Agent", "TEST"})


@dataclass(frozen=True)
class OpeningSlot:
    """本轮还欠用户一句开场。text 为空 = 模型没在同一次回复里写台词。"""

    text: str
    # False：return 模式（网页对话 / 评测），只记账不经 bot 发送
    deliver: bool


@dataclass(frozen=True)
class OpeningResult:
    """工具取走槽之后的结果。text 为空 = 该开口但没说出去，回执要允许补一句。"""

    text: str
    delivered: bool


def turn_expects_opening(
    *,
    create_by: str,
    is_subagent: bool,
    is_framework: bool,
    is_status_inquiry: bool,
) -> bool:
    """真人发起的交互主人格轮才有开场句。框架回灌、追问进度、子 Agent 都没有。"""
    if is_subagent or is_framework or is_status_inquiry:
        return False
    return create_by in _OPENING_CREATE_BY


def arm_opening_line(extra: MutableMapping[str, object], *, text: str, deliver: bool) -> None:
    extra[OPENING_SLOT_KEY] = OpeningSlot(text=text, deliver=deliver)


def disarm_opening_line(extra: MutableMapping[str, object]) -> None:
    """每次新回复先撤掉旧槽：上一次回复的台词不能跨回复代发。"""
    if OPENING_SLOT_KEY in extra:
        del extra[OPENING_SLOT_KEY]


def take_opening_result(extra: MutableMapping[str, object]) -> OpeningResult | None:
    if OPENING_RESULT_KEY not in extra:
        return None
    result = extra[OPENING_RESULT_KEY]
    del extra[OPENING_RESULT_KEY]
    return result if isinstance(result, OpeningResult) else None


def _claim_slot(extra: MutableMapping[str, object]) -> OpeningSlot | None:
    if OPENING_SLOT_KEY not in extra:
        return None
    slot = extra[OPENING_SLOT_KEY]
    del extra[OPENING_SLOT_KEY]
    return slot if isinstance(slot, OpeningSlot) else None


def _extra_str(extra: MutableMapping[str, object], key: str) -> str:
    raw = extra[key] if key in extra else ""
    return raw if isinstance(raw, str) else ""


def _sent_registry(extra: MutableMapping[str, object]) -> set[str] | None:
    raw = extra["run_sent_texts"] if "run_sent_texts" in extra else None
    if not isinstance(raw, set):
        return None
    registry: set[str] = raw
    return registry


def _mention_names(extra: MutableMapping[str, object]) -> dict[str, str]:
    raw = extra["mention_names"] if "mention_names" in extra else None
    names: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str) and key and value:
                names[key] = value
    return names


def _opening_session_id(tool_ctx: ToolContext) -> str:
    if tool_ctx.parent_session_id:
        return tool_ctx.parent_session_id
    ev = tool_ctx.ev
    if ev is not None and ev.session_id:
        return str(ev.session_id)
    return ""


def _gated_opening(text: str, tool_ctx: ToolContext) -> str:
    """过主通道同一套话术闸和出站闸。不合格返回空串，不替模型改写成别的话。"""
    from gsuid_core.ai_core.agent_run.support import log_output_block_on_session
    from gsuid_core.ai_core.persona.chat_style import resolve_chat_style

    body = text.strip()
    if not body:
        return ""
    extra = tool_ctx.extra
    registry = _sent_registry(extra)
    if registry is not None and body in registry:
        return ""
    persona = _extra_str(extra, "persona_name")
    ev = tool_ctx.ev
    addr = _extra_str(extra, "at_user_id")
    if not addr and ev is not None and ev.user_id:
        addr = str(ev.user_id)
    blocked, why = should_block_user_visible_text(
        "silence_only",
        body,
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        speech_len_hard=resolve_chat_style(persona).hard if persona else 0,
        forbid_title=non_master_title(addr, persona or None),
    )
    if blocked:
        log_output_block_on_session(_opening_session_id(tool_ctx), why, body, policy="silence_only")
        logger.info(i18n_t("log.agent.silent_skipping_text", _text=f"[opening/{why}] {body[:60]}"))
        return ""
    user_text = ev.raw_text if ev is not None and ev.raw_text else ""
    if tool_gate_feedback(body, extra, user_text=user_text) is not None:
        log_output_block_on_session(_opening_session_id(tool_ctx), "output_gate_tool", body, policy="silence_only")
        return ""
    return body


async def _deliver(tool_ctx: ToolContext, text: str) -> bool:
    from gsuid_core.ai_core.utils import send_chat_result
    from gsuid_core.ai_core.agent_run.support import turn_reply_metadata

    bot = tool_ctx.bot
    if bot is None or not tool_ctx.allow_user_outbound:
        return False
    at_uid = _extra_str(tool_ctx.extra, "at_user_id")
    # Why: 任务已在后台开工，发送失败不能把派发结果也变成工具报错
    try:
        await send_chat_result(
            bot,
            text,
            ev=tool_ctx.ev,
            ooc_check=False,
            at_user_id=at_uid or None,
            mention_names=_mention_names(tool_ctx.extra),
            extra_metadata=turn_reply_metadata(tool_ctx.ev),
        )
    except Exception as e:
        logger.debug(i18n_t("log.agent.text_send_fail_failed", _e=e))
        return False
    return True


async def release_opening_line(tool_ctx: ToolContext) -> OpeningResult | None:
    """派发成功后发出暂存的开场句。None = 本轮不该开口或槽已被别的调用取走。"""
    extra = tool_ctx.extra
    slot = _claim_slot(extra)
    if slot is None:
        return None
    text = _gated_opening(slot.text, tool_ctx) if slot.text else ""
    result = OpeningResult(text="", delivered=False)
    if text and (not slot.deliver or await _deliver(tool_ctx, text)):
        registry = _sent_registry(extra)
        if registry is not None:
            registry.add(text)
        result = OpeningResult(text=text, delivered=slot.deliver)
    extra[OPENING_RESULT_KEY] = result
    return result
