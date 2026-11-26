"""长任务的开场句：模型在委派的同一次回复里写的那句话，派发成功后由工具代发。

loop 在 CallToolsNode 用 ``arm_opening_line`` 暂存台词（工具旁的字一律不直接发）；
``create_subagent`` 派发成功或直调慢工具 10s 超时后调 ``release_opening_line`` 过闸发送；
loop 在 ModelRequestNode / CallToolsNode / End 边界经 ``absorb_opening_line`` 记账。
一轮只开一次口：槽被第一个调用取走，并列的第二个调用拿不到。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import MutableMapping

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.output_gate import pre_send_gate
from gsuid_core.ai_core.control.directive import GateDecision
from gsuid_core.ai_core.agent_run.speech_policy import (
    SpeechContext,
    non_master_title,
)

OPENING_SLOT_KEY = "opening_line_slot"
OPENING_RESULT_KEY = "opening_line_result"
OPENING_TIMER_KEY = "opening_line_timer"
OPENING_LOCK_KEY = "opening_line_lock"
SOFT_LATENCY_ACK_SECONDS: float = 10.0

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
    source: str = "create_subagent"


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


def has_armed_opening_text(extra: MutableMapping[str, object]) -> bool:
    if OPENING_SLOT_KEY not in extra:
        return False
    slot = extra[OPENING_SLOT_KEY]
    return isinstance(slot, OpeningSlot) and bool(slot.text.strip())


def _opening_lock(extra: MutableMapping[str, object]) -> asyncio.Lock:
    raw = extra[OPENING_LOCK_KEY] if OPENING_LOCK_KEY in extra else None
    if isinstance(raw, asyncio.Lock):
        return raw
    lock = asyncio.Lock()
    extra[OPENING_LOCK_KEY] = lock
    return lock


def cancel_opening_timer(extra: MutableMapping[str, object]) -> None:
    """取消尚未开跑发送的直调工具延迟定时器；已进场发送的由锁同步等待收尾。"""
    if OPENING_TIMER_KEY not in extra:
        return
    task = extra[OPENING_TIMER_KEY]
    del extra[OPENING_TIMER_KEY]
    lock = extra[OPENING_LOCK_KEY] if OPENING_LOCK_KEY in extra else None
    if isinstance(lock, asyncio.Lock) and lock.locked():
        return
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()


async def wait_inflight_opening_line(extra: MutableMapping[str, object]) -> None:
    """若定时器已进场发送中，等待其落账后再继续下一跳，防止开场句与终稿乱序。"""
    raw = extra[OPENING_LOCK_KEY] if OPENING_LOCK_KEY in extra else None
    if isinstance(raw, asyncio.Lock) and raw.locked():
        async with raw:
            pass


def disarm_opening_line(extra: MutableMapping[str, object]) -> None:
    """每次新回复或进入终局前撤掉旧槽并取消延迟定时器。"""
    cancel_opening_timer(extra)
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


def _sent_order(extra: MutableMapping[str, object]) -> list[str] | None:
    raw = extra["run_sent_order"] if "run_sent_order" in extra else None
    if not isinstance(raw, list):
        return None
    order: list[str] = raw
    return order


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


def _gated_opening(text: str, tool_ctx: ToolContext, *, source: str = "create_subagent") -> str:
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
    user_text = ev.raw_text if ev is not None and ev.raw_text else ""
    from gsuid_core.ai_core.agent_node.registry import persona_allows_capability_agent

    _allow_render = persona_allows_capability_agent(persona or None, "render_agent")
    _is_group = bool(ev is not None and ev.group_id)
    is_async_src = source in ("create_subagent", "background_tool")
    speech_ctx = SpeechContext(
        policy="silence_only",
        pending_async=is_async_src,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=("create_subagent",) if source == "create_subagent" else (),
        speech_len_hard=resolve_chat_style(persona).hard if persona else 0,
        forbid_title=non_master_title(addr, persona or None),
        user_text=user_text,
        allow_render=_allow_render,
    )
    gr = pre_send_gate(body, extra, user_text=user_text, channel="tool", is_group=_is_group, speech_ctx=speech_ctx)
    if gr.decision is not GateDecision.ALLOW:
        why = gr.detail if gr.policy == "speech_policy" else "output_gate_tool"
        log_output_block_on_session(_opening_session_id(tool_ctx), why, body, policy="silence_only")
        if gr.policy == "speech_policy":
            logger.info(i18n_t("log.agent.silent_skipping_text", _text=f"[opening/{why}] {body[:60]}"))
        return ""
    return gr.send_text or body


async def _deliver(tool_ctx: ToolContext, text: str) -> bool:
    from gsuid_core.ai_core.delivery import send_chat_result
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


async def release_opening_line(
    tool_ctx: ToolContext,
    *,
    source: str = "create_subagent",
    cancel_ev: asyncio.Event | None = None,
    from_timer: bool = False,
) -> OpeningResult | None:
    """派发成功或延迟超时后发出暂存开场句。None = 本轮不该开口或槽已被取走。"""
    extra = tool_ctx.extra
    if not from_timer:
        cancel_opening_timer(extra)
    async with _opening_lock(extra):
        if cancel_ev is not None and cancel_ev.is_set():
            return None
        slot = _claim_slot(extra)
        if slot is None:
            return None
        text = _gated_opening(slot.text, tool_ctx, source=source) if slot.text else ""
        result = OpeningResult(text="", delivered=False, source=source)
        if text and (not slot.deliver or await _deliver(tool_ctx, text)):
            registry = _sent_registry(extra)
            if registry is not None:
                registry.add(text)
            order = _sent_order(extra)
            if order is not None and text not in order:
                order.append(text)
            result = OpeningResult(text=text, delivered=slot.deliver, source=source)
        extra[OPENING_RESULT_KEY] = result
        return result


def arm_soft_latency_ack(
    tool_ctx: ToolContext,
    *,
    delay_sec: float = SOFT_LATENCY_ACK_SECONDS,
    cancel_ev: asyncio.Event | None = None,
) -> asyncio.Task[None] | None:
    """直调工具超时仍未终局时，自动释放当前人格首跳已写好的开场句（严守无罐头文案）。"""
    extra = tool_ctx.extra
    if not has_armed_opening_text(extra):
        return None
    slot = extra[OPENING_SLOT_KEY]
    if not isinstance(slot, OpeningSlot) or not slot.deliver:
        return None
    cancel_opening_timer(extra)
    wait_s = max(0.0, delay_sec)

    async def _timer() -> None:
        cur_task = asyncio.current_task()
        try:
            await asyncio.sleep(wait_s)
            if cancel_ev is not None and cancel_ev.is_set():
                return
            if OPENING_SLOT_KEY not in tool_ctx.extra:
                return
            await release_opening_line(
                tool_ctx,
                source="soft_latency_ack",
                cancel_ev=cancel_ev,
                from_timer=True,
            )
        except asyncio.CancelledError:
            return
        finally:
            recorded = extra[OPENING_TIMER_KEY] if OPENING_TIMER_KEY in extra else None
            if cur_task is not None and recorded is cur_task:
                del extra[OPENING_TIMER_KEY]

    def _consume_timer_exc(done_task: asyncio.Task[None]) -> None:
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is not None:
            logger.debug(i18n_t("log.agent.text_send_fail_failed", _e=exc))

    task: asyncio.Task[None] = asyncio.create_task(_timer())
    task.add_done_callback(_consume_timer_exc)
    extra[OPENING_TIMER_KEY] = task
    return task
