"""长耗时工具的后台执行与主会话终态回灌。

声明 ``background=True`` 的工具在具备异步出站通道的交互式主会话中先等待
``background_grace`` 秒；宽限内完成（如参数/凭据校验快速报错、今日任务已完成）
则同步直返，超出宽限则不中断任务，转后台继续执行并立刻释放主会话锁，完成后回灌
主人格播报。
"""

from __future__ import annotations

import re
import time
import base64
import asyncio
import hashlib
from typing import TYPE_CHECKING, Callable, Optional, Sequence, Awaitable
from dataclasses import dataclass

from PIL import Image

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import Message
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import _drop_running_task
from gsuid_core.ai_core.tool_health import record_tool_failure, record_tool_success
from gsuid_core.ai_core.control.lines import (
    format_rm_resource_hint,
    background_tool_wake_line,
    background_tool_detached_ack,
    background_tool_failure_wake_line,
    background_tool_already_running_ack,
)
from gsuid_core.ai_core.async_delivery import (
    resolve_live_bot,
    build_delivery_hints,
    resolve_live_session,
    has_live_async_channel,
    sanitize_fallback_spoken,
    build_delivery_event_from,
    note_delivery_followup_if_sent,
)

if TYPE_CHECKING:
    from pydantic_ai import RunContext


@dataclass(slots=True)
class _RunningBgTool:
    key: str
    tool_name: str
    started_at: float
    task: asyncio.Task[object]


_INFLIGHT: dict[str, _RunningBgTool] = {}
_WATCHERS: set[asyncio.Task[None]] = set()


def _inflight_key(tool_name: str, deps: ToolContext, args_key: str = "") -> str:
    """按工具名 + 会话域 + 发起人 + 归一化参数指纹去重，防跨群/异参误吞。"""
    ev = deps.ev
    scope = (deps.parent_session_id or "").strip()
    if not scope and ev is not None:
        if ev.session_id:
            scope = str(ev.session_id)
        elif ev.group_id:
            scope = f"g:{ev.group_id}"
        elif ev.user_id:
            scope = f"d:{ev.user_id}"
    uid = str(ev.user_id).strip() if ev is not None and ev.user_id else "anon"
    norm_args = args_key.strip()
    arg_sig = hashlib.sha256(norm_args.encode("utf-8")).hexdigest()[:12] if norm_args else "0"
    return f"{tool_name}:s:{scope or 'global'}:u:{uid}:a:{arg_sig}"


def should_detach_background(deps: ToolContext) -> bool:
    """仅具备真实异步推送通道的交互式主会话可转后台；HTTP/评测/MCP/子代理保持同步。"""
    if not deps.allow_user_outbound:
        return False
    if deps.ev is None or deps.bot is None:
        return False
    extra = deps.extra
    if "parent_create_by" in extra and extra["parent_create_by"] == "TEST":
        return False
    if "source" in extra and extra["source"] == "mcp_server":
        return False
    if "return_mode" in extra and extra["return_mode"] == "return":
        return False
    return has_live_async_channel(deps.ev, deps.bot)


def _normalize_message_without_send(msg: Message) -> str:
    from gsuid_core.utils.resource_manager import RM

    data = msg.data
    if msg.type == "image" and isinstance(data, (str, bytes)):
        return format_rm_resource_hint("image", [RM.register(data)])
    if msg.type == "record" and isinstance(data, (str, bytes)):
        return format_rm_resource_hint("audio", [RM.register_audio(data)])
    if msg.type == "video" and isinstance(data, (str, bytes)):
        return format_rm_resource_hint("video", [RM.register_video(data)])
    if msg.type == "file" and isinstance(data, str):
        fid = RM.register_file(data)
        if fid is None:
            return "[文件为空或超过 10MB，未登记]"
        return format_rm_resource_hint("file", [fid])
    if msg.type == "text" and isinstance(data, str):
        return data
    return f"[{msg.type or 'unknown'}消息]"


def _register_binary_content(data: bytes, media_type: str) -> str:
    from gsuid_core.utils.resource_manager import RM

    mt = (media_type or "").lower()
    if mt.startswith("audio/"):
        return format_rm_resource_hint("audio", [RM.register_audio(data)])
    if mt.startswith("video/"):
        return format_rm_resource_hint("video", [RM.register_video(data)])
    if not mt or mt.startswith("image/"):
        return format_rm_resource_hint("image", [RM.register(data)])
    ext = mt.split("/", 1)[1].split(";", 1)[0].strip() or "bin"
    payload = f"attachment.{ext}|{base64.b64encode(data).decode('ascii')}"
    fid = RM.register_file(payload)
    if fid is None:
        return "[文件为空或超过 10MB，未登记]"
    return format_rm_resource_hint("file", [fid])


def _normalize_tool_return(raw_result: object) -> str:
    from pydantic_ai import ToolReturn
    from pydantic_ai.messages import BinaryContent

    assert isinstance(raw_result, ToolReturn)
    parts: list[str] = []
    val = raw_result.return_value
    if val is not None:
        val_str = val if isinstance(val, str) else str(val)
        if val_str.strip():
            parts.append(val_str.strip())
    content = raw_result.content
    if isinstance(content, str):
        if content.strip():
            parts.append(content.strip())
    elif isinstance(content, BinaryContent):
        parts.append(_register_binary_content(content.data, content.media_type))
    elif isinstance(content, Sequence):
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, BinaryContent):
                parts.append(_register_binary_content(item.data, item.media_type))
    return "\n".join(parts) if parts else "（工具已执行，无文本返回）"


_RM_IMAGE_ID_RE = re.compile(r"\bimg_[0-9a-fA-F]{3,}\b")


async def _normalize_bg_output(
    raw_result: object,
    *,
    raw_passthrough: bool,
) -> str:
    """后台终态纯归一化：仅登记 RM 资源 ID，绝不直接 bot.send 以防与主会话回灌重复发图。"""
    from pydantic_ai import ToolReturn

    from gsuid_core.ai_core.delivery import handle_tool_result
    from gsuid_core.utils.image.convert import convert_img
    from gsuid_core.utils.resource_manager import RM

    if raw_result is None:
        return "（工具已执行，无文本返回）"
    if isinstance(raw_result, ToolReturn):
        return _normalize_tool_return(raw_result)
    if isinstance(raw_result, bytes):
        return format_rm_resource_hint("image", [RM.register(raw_result)])
    if isinstance(raw_result, Image.Image):
        img_bytes = await convert_img(raw_result)
        return format_rm_resource_hint("image", [RM.register(img_bytes)])
    if isinstance(raw_result, Message):
        return _normalize_message_without_send(raw_result)
    if isinstance(raw_result, (list, tuple)) and any(
        isinstance(item, (Message, bytes, Image.Image)) for item in raw_result
    ):
        seq_parts: list[str] = []
        for item in raw_result:
            part_str = await _normalize_bg_output(item, raw_passthrough=raw_passthrough)
            if part_str.strip():
                seq_parts.append(part_str.strip())
        return "\n".join(seq_parts) if seq_parts else "（工具已执行，无文本返回）"
    if raw_passthrough:
        out_str = raw_result if isinstance(raw_result, str) else str(raw_result)
    else:
        out_str = await handle_tool_result(None, raw_result)
    stripped = out_str.strip()
    if stripped.startswith(("base64://", "link://", "data:image/")):
        return format_rm_resource_hint("image", [RM.register(stripped)])
    return out_str


async def _deliver_background_result(
    *,
    tool_name: str,
    tool_label: str,
    session_id: str,
    ev: Event,
    fallback_bot: Optional[Bot],
    result_text: str,
    is_failed: bool,
) -> None:
    live_bot = resolve_live_bot(ev.WS_BOT_ID or "", ev, fallback_bot)
    session = await resolve_live_session(session_id, ev, live_bot)
    owner = str(ev.user_id or "").strip()
    is_group = bool(ev.group_id)

    if session is None or live_bot is None:
        logger.warning(
            t(
                "log.register.tool_bg_delivery_fallback",
                p0=tool_name,
                reason=f"session={session is not None},bot={live_bot is not None}",
            )
        )
        if live_bot is not None and not is_failed:
            safe_spoken = sanitize_fallback_spoken(result_text, empty_fallback="")
            if safe_spoken:
                from gsuid_core.ai_core.proactive import emit_proactive_message

                await emit_proactive_message(
                    ev,
                    safe_spoken,
                    source="tool",
                    trigger_reason=f"bg_tool:{tool_name}",
                    bot=live_bot,
                    suppress_when_heartbeat_recent=False,
                    at_user_id=owner if is_group and owner else None,
                )
            from gsuid_core.utils.resource_manager import RM

            seen_imgs: set[str] = set()
            for img_id in _RM_IMAGE_ID_RE.findall(result_text):
                if img_id in seen_imgs or not RM.has(img_id):
                    continue
                seen_imgs.add(img_id)
                await live_bot.send(RM.peek(img_id))
        return

    at_hint, title_ban = build_delivery_hints(
        owner=owner,
        is_group=is_group,
        persona_name=session.persona_name,
    )
    if is_failed:
        frame_text = background_tool_failure_wake_line(
            tool_label=tool_label,
            reason=result_text,
            owner=owner,
            at_hint=at_hint,
            title_ban=title_ban,
        )
    else:
        frame_text = background_tool_wake_line(
            tool_label=tool_label,
            result_body=result_text,
            owner=owner,
            at_hint=at_hint,
            title_ban=title_ban,
        )

    from gsuid_core.ai_core.inbox.turn import detached_turn_scope

    with detached_turn_scope():
        await session.run(
            user_message=frame_text,
            bot=live_bot,
            ev=ev,
            return_mode="by_bot",
            is_framework_injection=True,
        )

    note_delivery_followup_if_sent(
        session,
        session_id=session_id,
        ev=ev,
        owner=owner,
        is_group=is_group,
    )


async def _watch_and_deliver(
    *,
    key: str,
    tool_name: str,
    tool_label: str,
    inner_task: asyncio.Task[object],
    remaining_timeout: Optional[float],
    total_timeout: Optional[float],
    session_id: str,
    delivery_ev: Event,
    fallback_bot: Optional[Bot],
    track_health: bool,
    raw_passthrough: bool,
) -> None:
    is_failed = False
    result_text = ""
    try:
        if remaining_timeout is None:
            raw_result = await inner_task
        else:
            raw_result = await asyncio.wait_for(inner_task, timeout=remaining_timeout)
        result_text = await _normalize_bg_output(raw_result, raw_passthrough=raw_passthrough)
        is_error_return = result_text.lstrip().startswith("❌")
        if is_error_return:
            is_failed = True
        if track_health:
            if is_error_return:
                record_tool_failure(tool_name, result_text)
            else:
                record_tool_success(tool_name)
    except asyncio.TimeoutError:
        _drop_running_task(inner_task)
        if track_health:
            record_tool_failure(tool_name, "timeout")
        is_failed = True
        if inner_task.done() and not inner_task.cancelled():
            inner_exc = inner_task.exception()
            logger.warning(t("log.register.tool_inner_timeout", p0=tool_name, e=inner_exc))
            err_type = type(inner_exc).__name__ if inner_exc else "TimeoutError"
            result_text = f"内部超时/失败（{err_type}: {inner_exc}）"
        else:
            timeout_sec = int(total_timeout if total_timeout is not None else (remaining_timeout or 0))
            logger.warning(t("log.register.timeout_sec_aborted", p0=tool_name, timeout_sec=timeout_sec))
            result_text = f"执行超时（超过 {timeout_sec} 秒）"
    except asyncio.CancelledError:
        _drop_running_task(inner_task)
        return
    except Exception as exc:
        if track_health:
            record_tool_failure(tool_name, f"{type(exc).__name__}: {exc}")
        logger.warning(t("log.register.tool_bg_failed", p0=tool_name, e=exc))
        is_failed = True
        result_text = f"{type(exc).__name__}: {exc}"
    finally:
        cur = _INFLIGHT[key] if key in _INFLIGHT else None
        if cur is not None and cur.task is inner_task:
            _INFLIGHT.pop(key, None)

    try:
        await _deliver_background_result(
            tool_name=tool_name,
            tool_label=tool_label,
            session_id=session_id,
            ev=delivery_ev,
            fallback_bot=fallback_bot,
            result_text=result_text,
            is_failed=is_failed,
        )
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning(t("log.register.tool_bg_failed", p0=tool_name, e=exc))


async def _release_or_peek_opening(deps: ToolContext) -> tuple[str, bool]:
    """释放开场句；若 soft_latency_ack 已抢先消费，则复用结果或在被拦时标 opening_missed。"""
    from gsuid_core.ai_core.agent_run.opening_line import (
        OPENING_RESULT_KEY,
        OpeningResult,
        release_opening_line,
    )

    opening = await release_opening_line(deps, source="background_tool")
    if opening is not None:
        return opening.text, not opening.text
    prev = deps.extra[OPENING_RESULT_KEY] if OPENING_RESULT_KEY in deps.extra else None
    if isinstance(prev, OpeningResult):
        if prev.text:
            return prev.text, False
        if prev.source == "soft_latency_ack":
            deps.extra[OPENING_RESULT_KEY] = OpeningResult(text="", delivered=False, source="background_tool")
            return "", True
    return "", False


async def run_with_background_grace(
    *,
    name: str,
    label: str,
    ctx: "RunContext[ToolContext]",
    call: Callable[[], Awaitable[object]],
    timeout: Optional[float],
    grace_seconds: float,
    track_health: bool,
    raw_passthrough: bool,
    args_key: str = "",
) -> tuple[bool, object]:
    """短宽限同步等待，超时则转后台继续执行。返回 ``(finished_sync, result_or_ack)``。"""
    deps = ctx.deps
    key = _inflight_key(name, deps, args_key)
    existing = _INFLIGHT[key] if key in _INFLIGHT else None
    if existing is not None and not existing.task.done():
        elapsed = max(1, int(time.monotonic() - existing.started_at))
        spoken, opening_missed = await _release_or_peek_opening(deps)
        return False, background_tool_already_running_ack(
            elapsed_sec=elapsed,
            spoken=spoken,
            opening_missed=opening_missed,
        )

    async def _run_call() -> object:
        return await call()

    inner_task: asyncio.Task[object] = asyncio.create_task(_run_call())
    _INFLIGHT[key] = _RunningBgTool(key=key, tool_name=name, started_at=time.monotonic(), task=inner_task)

    grace = max(0.0, grace_seconds)
    try:
        done, _pending = await asyncio.wait({inner_task}, timeout=grace)
    except asyncio.CancelledError:
        _drop_running_task(inner_task)
        cur = _INFLIGHT[key] if key in _INFLIGHT else None
        if cur is not None and cur.task is inner_task:
            _INFLIGHT.pop(key, None)
        raise

    if inner_task in done:
        cur = _INFLIGHT[key] if key in _INFLIGHT else None
        if cur is not None and cur.task is inner_task:
            _INFLIGHT.pop(key, None)
        if inner_task.cancelled():
            return True, f"⚠️ 工具 {name} 已被中断取消，请稍后重试"
        return True, inner_task.result()

    # 超出宽限仍未完成：用干净回灌 Event 保留 inner_task 在后台跑，立刻释放主会话锁
    ev = deps.ev
    assert ev is not None
    delivery_ev = build_delivery_event_from(ev)
    session_id = deps.parent_session_id or delivery_ev.session_id
    remaining = None if timeout is None else max(0.05, timeout - grace)

    watcher: asyncio.Task[None] = asyncio.create_task(
        _watch_and_deliver(
            key=key,
            tool_name=name,
            tool_label=label or name,
            inner_task=inner_task,
            remaining_timeout=remaining,
            total_timeout=timeout,
            session_id=session_id,
            delivery_ev=delivery_ev,
            fallback_bot=deps.bot,
            track_health=track_health,
            raw_passthrough=raw_passthrough,
        )
    )
    _WATCHERS.add(watcher)
    watcher.add_done_callback(_WATCHERS.discard)

    spoken, opening_missed = await _release_or_peek_opening(deps)
    logger.info(t("log.register.tool_detached_background", p0=name, grace=round(grace, 1)))
    return False, background_tool_detached_ack(
        spoken=spoken,
        opening_missed=opening_missed,
    )


def cancel_background_tools_for_names(tool_names: Sequence[str]) -> int:
    """插件热重载卸载工具时取消该插件仍在途的后台任务，避免旧模块代码继续执行。"""
    targets = set(tool_names)
    if not targets:
        return 0
    cancelled = 0
    for key, entry in list(_INFLIGHT.items()):
        if entry.tool_name in targets:
            _drop_running_task(entry.task)
            _INFLIGHT.pop(key, None)
            cancelled += 1
    return cancelled


async def shutdown_background_tools(*, timeout: float = 5.0) -> int:
    """关机钩子：取消所有在途后台工具及监控协程，带超时上限防不配合任务挂起关机。"""
    tasks: list[asyncio.Task[object]] = []
    for entry in list(_INFLIGHT.values()):
        if not entry.task.done():
            _drop_running_task(entry.task)
            tasks.append(entry.task)
    watchers: list[asyncio.Task[None]] = []
    for w in list(_WATCHERS):
        if not w.done():
            w.cancel()
            watchers.append(w)
    wait_budget = max(0.01, timeout)
    if tasks:
        await asyncio.wait(tasks, timeout=wait_budget)
    if watchers:
        await asyncio.wait(watchers, timeout=wait_budget)
    _INFLIGHT.clear()
    _WATCHERS.clear()
    cancelled = len(tasks)
    if cancelled:
        logger.info(t("log.register.tool_bg_shutdown_cancelled", count=cancelled))
    return cancelled
