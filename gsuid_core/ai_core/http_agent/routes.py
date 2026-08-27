"""HTTP Agent 路由。``include_in_schema=False``；关闸 404。"""

from __future__ import annotations

import json
import time
import uuid
import asyncio
from typing import List, TypeVar, Optional, AsyncIterator

from fastapi import Request, APIRouter
from pydantic import Field, BaseModel, ConfigDict, ValidationError
from fastapi.responses import Response, JSONResponse, StreamingResponse

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.http_agent.auth import (
    ban_identity,
    is_ip_banned,
    authenticate_bearer,
    record_auth_failure,
    record_auth_success,
)
from gsuid_core.ai_core.http_agent.types import HTTP_AGENT_WS_BOT_ID, SseEventName, HttpAgentKeyRecord
from gsuid_core.ai_core.http_agent.config import load_http_agent_settings
from gsuid_core.ai_core.http_agent.errors import not_found, error_response
from gsuid_core.ai_core.http_agent.limiter import LimitExceeded, limiter
from gsuid_core.ai_core.http_agent.runtime import (
    ActiveRun,
    get_run,
    cancel_run,
    discard_run,
    register_run,
    cancel_session_runs,
)
from gsuid_core.ai_core.http_agent.protocol import SSE_HEADERS, encode_sse, encode_comment
from gsuid_core.ai_core.http_agent.session_id import (
    CLIENT_MSG_ID_RE,
    parse_group_id,
    session_id_for_key,
    normalize_client_session,
)
from gsuid_core.ai_core.http_agent.idempotency import IdempotencyConflict, idempotency_store

agent_router = APIRouter(prefix="/api/v1/agent", include_in_schema=False)
TBody = TypeVar("TBody", bound=BaseModel)


class ChatStreamRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = ""
    session_id: str = "default"
    client_msg_id: str
    persona: Optional[str] = None
    images: List[str] = Field(default_factory=list)
    group_id: Optional[str] = None


class SessionResetRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = "default"
    group_id: Optional[str] = None


def _cors_headers(request: Request) -> dict[str, str]:
    headers = dict(SSE_HEADERS)
    origins = load_http_agent_settings().cors_origins
    origin = request.headers.get("origin")
    if origin and origin in origins:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return headers


def _disabled() -> JSONResponse | None:
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.http_agent.config import is_http_agent_enabled

    if not is_http_agent_enabled() or not ai_config.get_config("enable").data:
        return not_found()
    return None


def _authorize(request: Request) -> tuple[HttpAgentKeyRecord | None, JSONResponse | None]:
    ident = ban_identity(request)
    if is_ip_banned(ident):
        return None, error_response(401, "unauthorized", "invalid api key")
    authorization = request.headers.get("authorization")
    key = authenticate_bearer(authorization)
    if key is None:
        record_auth_failure(ident)
        return None, error_response(401, "unauthorized", "invalid api key")
    record_auth_success(ident)
    return key, None


async def _read_capped_body(request: Request) -> bytes | JSONResponse:
    settings = load_http_agent_settings()
    cap = settings.max_body_bytes
    raw_len = request.headers.get("content-length")
    if raw_len is not None:
        try:
            n = int(raw_len)
        except ValueError:
            return error_response(400, "bad_request", "invalid content-length")
        if n > cap:
            return error_response(413, "payload_too_large", "request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            return error_response(413, "payload_too_large", "request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_json_model(raw: bytes, model: type[TBody]) -> TBody | JSONResponse:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return error_response(400, "bad_request", "invalid json")
    try:
        parsed: object = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return error_response(400, "bad_request", "invalid json")
    try:
        return model.model_validate(parsed)
    except ValidationError:
        return error_response(400, "bad_request", "invalid request body")


async def _gated_json(request: Request, model: type[TBody]) -> tuple[HttpAgentKeyRecord, TBody] | JSONResponse:
    closed = _disabled()
    if closed is not None:
        return closed
    key, err = _authorize(request)
    if err is not None or key is None:
        return err if err is not None else error_response(401, "unauthorized", "invalid api key")
    raw = await _read_capped_body(request)
    if isinstance(raw, JSONResponse):
        return raw
    body = _parse_json_model(raw, model)
    if isinstance(body, JSONResponse):
        return body
    return key, body


def _history_event_for_key(
    key: HttpAgentKeyRecord,
    client_session: str,
    group_id: str | None,
) -> Event:
    from gsuid_core.ai_core.http_agent.session_id import bot_self_id_for

    is_group = group_id is not None
    return Event(
        bot_id=key["bot_id"],
        bot_self_id=bot_self_id_for(key, client_session, group_id),
        user_type="group" if is_group else "direct",
        user_id=key["user_id"],
        group_id=group_id,
        WS_BOT_ID=HTTP_AGENT_WS_BOT_ID,
    )


@agent_router.get("/health")
async def agent_health() -> JSONResponse:
    closed = _disabled()
    if closed is not None:
        return closed
    return JSONResponse({"ok": True})


@agent_router.post("/sessions/reset")
async def agent_session_reset(request: Request) -> JSONResponse:
    gated = await _gated_json(request, SessionResetRequest)
    if isinstance(gated, JSONResponse):
        return gated
    key, body = gated
    client_session = normalize_client_session(body.session_id)
    if client_session is None:
        return error_response(400, "bad_session", "invalid session_id")
    parsed_group = parse_group_id(body.group_id)
    if not parsed_group.ok:
        return error_response(400, "bad_group", "invalid group_id")
    sid = session_id_for_key(key, client_session, parsed_group.value)
    await cancel_session_runs(sid)
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    get_ai_session_registry().remove_ai_session(sid)
    get_history_manager().delete_session(_history_event_for_key(key, client_session, parsed_group.value))
    logger.info(t("log.ai.http_agent_session_reset", session_id=sid))
    return JSONResponse({"ok": True})


@agent_router.post("/runs/{run_id}/cancel")
async def agent_run_cancel(run_id: str, request: Request) -> JSONResponse:
    closed = _disabled()
    if closed is not None:
        return closed
    key, err = _authorize(request)
    if err is not None or key is None:
        return err if err is not None else error_response(401, "unauthorized", "invalid api key")
    run = get_run(run_id)
    if run is None or run.key_id != key["key_id"]:
        return not_found()
    await cancel_run(run)
    discard_run(run_id)
    return JSONResponse({"ok": True})


@agent_router.post("/chat/stream", response_model=None)
async def agent_chat_stream(request: Request) -> Response:
    gated = await _gated_json(request, ChatStreamRequest)
    if isinstance(gated, JSONResponse):
        return gated
    key, body = gated
    parsed_group = parse_group_id(body.group_id)
    if not parsed_group.ok:
        return error_response(400, "bad_group", "invalid group_id")
    group_id = parsed_group.value
    client_session = normalize_client_session(body.session_id)
    if client_session is None:
        return error_response(400, "bad_session", "invalid session_id")
    if CLIENT_MSG_ID_RE.fullmatch(body.client_msg_id) is None:
        return error_response(400, "bad_client_msg_id", "invalid or missing client_msg_id")
    settings = load_http_agent_settings()
    if len(body.images) > settings.max_images:
        return error_response(413, "payload_too_large", "too many images")
    from gsuid_core.ai_core.http_agent.event_build import is_inline_agent_image

    for img in body.images:
        if not is_inline_agent_image(img):
            return error_response(400, "bad_image", "only data:image and base64:// images are allowed")
    if not body.text.strip() and not body.images:
        return error_response(400, "empty_message", "text or images required")

    sid = session_id_for_key(key, client_session, group_id)
    from gsuid_core.ai_core.http_agent.persona import PersonaResolveError, resolve_http_persona
    from gsuid_core.ai_core.http_agent.event_build import build_http_agent_event

    try:
        resolve_http_persona(session_id=sid, key=key, requested=body.persona)
    except PersonaResolveError as e:
        return error_response(e.status, e.code, e.message)

    from gsuid_core.ai_core.startup import is_ai_core_ready

    if not is_ai_core_ready():
        return error_response(503, "ai_unavailable", "ai core is not ready")

    event = await build_http_agent_event(
        key=key,
        client_session=client_session,
        text=body.text,
        images=body.images,
        group_id=group_id,
    )
    from gsuid_core.ai_core.turn_pipeline import evaluate_budget

    decision = await evaluate_budget(event)
    if decision is not None and not decision.allowed:
        msg = decision.message if decision.message else "budget exceeded"
        return error_response(429, "budget", msg)

    run_id = uuid.uuid4().hex
    try:
        await limiter.try_acquire(key["key_id"])
    except LimitExceeded as e:
        return error_response(429, e.code, e.message)

    try:
        idempotency_store.begin(key["key_id"], body.client_msg_id, run_id)
    except IdempotencyConflict:
        await limiter.release(key["key_id"])
        return error_response(409, "idempotency_conflict", "duplicate client_msg_id")

    async def _stream() -> AsyncIterator[str]:
        try:
            async for chunk in _sse_run(
                request=request,
                key=key,
                event=event,
                client_session=client_session,
                run_id=run_id,
                wall_clock=settings.wall_clock,
                hard_timeout=settings.hard_timeout,
                heartbeat_sec=settings.heartbeat_sec,
                queue_max=settings.queue_max,
            ):
                yield chunk
        finally:
            await limiter.release(key["key_id"])
            idempotency_store.complete(key["key_id"], body.client_msg_id)

    return StreamingResponse(_stream(), media_type="text/event-stream", headers=_cors_headers(request))


async def _sse_run(
    *,
    request: Request,
    key: HttpAgentKeyRecord,
    event: Event,
    client_session: str,
    run_id: str,
    wall_clock: int,
    hard_timeout: int,
    heartbeat_sec: int,
    queue_max: int,
) -> AsyncIterator[str]:
    from gsuid_core.ai_core.utils import sanitize_error_for_user
    from gsuid_core.ai_core.handle_ai import PassiveChatResult
    from gsuid_core.ai_core.http_agent.bridge import make_capture_bot, run_http_agent_turn
    from gsuid_core.ai_core.http_agent.capture_bot import CaptureItem

    queue: asyncio.Queue[CaptureItem] = asyncio.Queue(maxsize=queue_max)
    bot = make_capture_bot(event, queue)
    done = asyncio.Event()
    outcome: list[PassiveChatResult | BaseException] = []

    async def _turn() -> None:
        try:
            result = await asyncio.wait_for(
                run_http_agent_turn(bot=bot, event=event, wall_clock=wall_clock, run_id=run_id),
                timeout=float(hard_timeout),
            )
            outcome.append(result)
        except asyncio.CancelledError as e:
            outcome.append(e)
        except asyncio.TimeoutError as e:
            outcome.append(e)
        except Exception as e:
            outcome.append(e)
        finally:
            done.set()

    turn_task: asyncio.Task[object] = asyncio.create_task(_turn())
    register_run(
        ActiveRun(
            run_id=run_id,
            key_id=key["key_id"],
            agent_session_id=event.session_id,
            turn_task=turn_task,
        )
    )
    await cancel_session_runs(event.session_id, except_run_id=run_id)
    seq = 0
    terminal_sent = False

    def _next(event_name: SseEventName, data: dict[str, object]) -> str:
        nonlocal seq
        seq += 1
        payload = dict(data)
        payload["seq"] = seq
        return encode_sse(event_name, payload, seq)

    try:
        yield _next("run.start", {"run_id": run_id, "session_id": client_session})
        last_sent = time.monotonic()
        text_emitted = False
        held_atts: list[CaptureItem] = []

        def _att_data(item: CaptureItem) -> dict[str, object]:
            return {
                "kind": item.att_kind,
                "encoding": item.encoding,
                "mime": item.mime,
                "data": item.data,
                "nbytes": item.nbytes,
            }

        while True:
            if await request.is_disconnected():
                live = get_run(run_id)
                if live is not None:
                    await cancel_run(live)
                else:
                    turn_task.cancel()
                return
            if done.is_set() and queue.empty():
                break
            wait = max(0.05, float(heartbeat_sec) - (time.monotonic() - last_sent))
            try:
                item = await asyncio.wait_for(queue.get(), timeout=min(wait, 0.5))
            except asyncio.TimeoutError:
                if time.monotonic() - last_sent >= float(heartbeat_sec):
                    yield encode_comment("ping")
                    last_sent = time.monotonic()
                continue
            if item.kind == "text":
                yield _next("text", {"text": item.text})
                text_emitted = True
                last_sent = time.monotonic()
                for att in held_atts:
                    yield _next("attachment", _att_data(att))
                held_atts.clear()
            elif item.kind == "attachment":
                if text_emitted:
                    yield _next("attachment", _att_data(item))
                    last_sent = time.monotonic()
                else:
                    held_atts.append(item)
        while not queue.empty():
            item = queue.get_nowait()
            if item.kind == "text":
                yield _next("text", {"text": item.text})
                text_emitted = True
                for att in held_atts:
                    yield _next("attachment", _att_data(att))
                held_atts.clear()
            elif item.kind == "attachment":
                if text_emitted:
                    yield _next("attachment", _att_data(item))
                else:
                    held_atts.append(item)
        if held_atts and not text_emitted:
            from gsuid_core.ai_core.agent_run.loop import task_ack_phrase
            from gsuid_core.ai_core.http_agent.persona import peek_bound_persona

            ack = task_ack_phrase(peek_bound_persona(event.session_id))
            yield _next("text", {"text": ack})
            text_emitted = True
        for att in held_atts:
            yield _next("attachment", _att_data(att))
        held_atts.clear()
        raw_out: PassiveChatResult | BaseException | None = outcome[0] if outcome else None
        if isinstance(raw_out, asyncio.CancelledError) or isinstance(raw_out, asyncio.TimeoutError):
            code = "timeout" if isinstance(raw_out, asyncio.TimeoutError) else "cancelled"
            if code == "cancelled":
                yield _next("run.done", {"status": "cancelled"})
            else:
                yield _next("run.error", {"code": "timeout", "message": "run timed out"})
            terminal_sent = True
        elif isinstance(raw_out, BaseException):
            logger.exception(t("log.ai.http_agent_turn_fail", e=raw_out))
            yield _next(
                "run.error",
                {"code": "internal", "message": sanitize_error_for_user(str(raw_out))},
            )
            terminal_sent = True
        elif raw_out is None:
            yield _next("run.error", {"code": "internal", "message": "empty outcome"})
            terminal_sent = True
        elif raw_out.status == "silence":
            yield _next("run.done", {"status": "silence"})
            terminal_sent = True
        elif raw_out.turn is not None and raw_out.turn.is_error:
            text = sanitize_error_for_user(raw_out.turn.result_text)
            yield _next("run.error", {"code": "internal", "message": text})
            terminal_sent = True
        elif raw_out.status in ("disabled", "not_ready", "budget"):
            yield _next("run.error", {"code": "ai_unavailable", "message": raw_out.status})
            terminal_sent = True
        elif bot._overflow:
            yield _next("run.error", {"code": "output_truncated", "message": "output queue overflow"})
            terminal_sent = True
        else:
            yield _next("run.done", {"status": "ok"})
            terminal_sent = True
    finally:
        if not turn_task.done():
            live = get_run(run_id)
            await cancel_run(
                live
                if live is not None
                else ActiveRun(
                    run_id=run_id,
                    key_id=key["key_id"],
                    agent_session_id=event.session_id,
                    turn_task=turn_task,
                )
            )
        discard_run(run_id)
        if not terminal_sent and not await request.is_disconnected():
            yield _next("run.done", {"status": "cancelled"})
