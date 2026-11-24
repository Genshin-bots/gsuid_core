"""HTTP Agent 运行时：活跃 run 注册/取消、人格解析、入站桥接与路由挂载。"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Protocol
from dataclasses import dataclass

from fastapi import FastAPI

from gsuid_core.bot import _Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.handle_ai import PassiveChatResult, run_passive_interactive_chat
from gsuid_core.ai_core.http_agent.config import load_http_agent_settings
from gsuid_core.ai_core.http_agent.protocol import (
    HTTP_AGENT_WS_BOT_ID,
    HTTP_STATS_CHAT_TYPE,
    HttpAgentKeyRecord,
)
from gsuid_core.ai_core.http_agent.capture_bot import CaptureBot, CaptureItem

# ── 进行中的 HTTP Agent run（cancel / reset / 断连）─────────────


class CancelableAgent(Protocol):
    _cancel_generation: asyncio.Event


@dataclass
class ActiveRun:
    run_id: str
    key_id: str
    agent_session_id: str
    turn_task: asyncio.Task[object]
    agent: Optional[CancelableAgent] = None


_runs: Dict[str, ActiveRun] = {}
_by_session: Dict[str, List[str]] = {}


def register_run(run: ActiveRun) -> None:
    _runs[run.run_id] = run
    if run.agent_session_id not in _by_session:
        _by_session[run.agent_session_id] = []
    _by_session[run.agent_session_id].append(run.run_id)


def bind_agent(run_id: str, agent: CancelableAgent) -> None:
    if run_id in _runs:
        _runs[run_id].agent = agent


def get_run(run_id: str) -> ActiveRun | None:
    return _runs[run_id] if run_id in _runs else None


def discard_run(run_id: str) -> None:
    if run_id not in _runs:
        return
    run = _runs.pop(run_id)
    if run.agent_session_id in _by_session:
        ids = [i for i in _by_session[run.agent_session_id] if i != run_id]
        if ids:
            _by_session[run.agent_session_id] = ids
        else:
            del _by_session[run.agent_session_id]


def runs_for_session(agent_session_id: str) -> List[ActiveRun]:
    if agent_session_id not in _by_session:
        return []
    return [_runs[rid] for rid in list(_by_session[agent_session_id]) if rid in _runs]


async def cancel_run(run: ActiveRun) -> None:
    if run.agent is not None:
        run.agent._cancel_generation.set()
    run.turn_task.cancel()
    try:
        await run.turn_task
    except (asyncio.CancelledError, Exception):
        pass


async def cancel_session_runs(
    agent_session_id: str,
    *,
    except_run_id: str | None = None,
) -> None:
    for run in runs_for_session(agent_session_id):
        if except_run_id is not None and run.run_id == except_run_id:
            continue
        await cancel_run(run)
        discard_run(run.run_id)


# ── 人格钉选与解析（仅 HTTP_AGENT: 会话）────────────────────────

_HTTP_PERSONA_OVERRIDES: dict[str, str] = {}


def set_http_persona_override(session_id: str, persona_name: str) -> None:
    _HTTP_PERSONA_OVERRIDES[session_id] = persona_name


def get_http_persona_override(session_id: str) -> str | None:
    return _HTTP_PERSONA_OVERRIDES[session_id] if session_id in _HTTP_PERSONA_OVERRIDES else None


def clear_http_persona_override(session_id: str) -> bool:
    return _HTTP_PERSONA_OVERRIDES.pop(session_id, None) is not None


class PersonaResolveError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def peek_bound_persona(session_id: str) -> str | None:
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    session = get_ai_session_registry().get_ai_session(session_id)
    if session is None:
        return None
    name = session.persona_name
    return name if name else None


def _persona_exists(name: str) -> bool:
    from gsuid_core.ai_core.persona.persona import Persona

    try:
        return Persona(name).exists()
    except ValueError:
        return False


def resolve_http_persona(
    *,
    session_id: str,
    key: HttpAgentKeyRecord,
    requested: str | None,
) -> str:
    """返回将用于本 run 的人格名。成功且请求了人格时才写 override。"""
    existing = peek_bound_persona(session_id)
    req = requested.strip() if requested else ""
    key_persona = key["persona"].strip() if key["persona"] else ""
    default = load_http_agent_settings().default_persona.strip()

    if existing and req and req != existing:
        raise PersonaResolveError(409, "persona_pinned", "session already bound to another persona")

    if existing and not req:
        candidate = existing
    elif req:
        candidate = req
    elif key_persona:
        candidate = key_persona
    elif default:
        candidate = default
    else:
        from gsuid_core.ai_core.persona.config import persona_config_manager

        looked = persona_config_manager.get_persona_for_session(session_id)
        candidate = looked if looked else ""

    if not candidate:
        raise PersonaResolveError(422, "persona_unbound", "no persona bound for this session")
    if key_persona and candidate != key_persona:
        raise PersonaResolveError(403, "persona_forbidden", "api key is constrained to another persona")
    if not _persona_exists(candidate):
        raise PersonaResolveError(422, "persona_unbound", "persona does not exist")

    if req and (existing is None or existing == req):
        set_http_persona_override(session_id, candidate)
    return candidate


# ── 入站桥接（A 轨 + H00 + run_passive_interactive_chat）─────────


async def attach_inbound_tracks(bot: CaptureBot, event: Event) -> None:
    """A 轨（纯图也记）+ H00。禁止再手写 observe()。"""
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, fire_hooks, should_fire
    from gsuid_core.message_history import get_history_manager

    user_name = event.sender["nickname"] if event.sender and "nickname" in event.sender else None
    user_avatar = event.sender["avatar"] if event.sender and "avatar" in event.sender else None
    metadata: dict[str, object] = {
        "msg_id": event.msg_id,
        "bot_id": event.bot_id,
        "user_type": event.user_type,
    }
    if event.image_id_list:
        metadata["image_id_list"] = event.image_id_list
    elif event.image_id:
        metadata["image_id"] = event.image_id
    inbound_record = get_history_manager().add_message(
        event=event,
        role="user",
        content=event.raw_text.strip() if event.raw_text else "",
        user_name=user_name if isinstance(user_name, str) else None,
        user_avatar=user_avatar if isinstance(user_avatar, str) else None,
        metadata=metadata,
    )
    if should_fire(AgentHookPoint.ON_INBOUND):
        inbound_ctx = AgentHookContext(
            point=AgentHookPoint.ON_INBOUND,
            ev=event,
            session_id=event.session_id,
            create_by="Chat",
            inbound_link=inbound_record.link,
            query=event.raw_text or "",
        )
        bot.bot._add_bg_task(asyncio.create_task(fire_hooks(AgentHookPoint.ON_INBOUND, inbound_ctx)))


def make_capture_bot(event: Event, queue: asyncio.Queue[CaptureItem]) -> CaptureBot:
    return CaptureBot(_Bot(HTTP_AGENT_WS_BOT_ID), event, queue)


async def run_http_agent_turn(
    *,
    bot: CaptureBot,
    event: Event,
    wall_clock: int,
    run_id: str,
) -> PassiveChatResult:
    from gsuid_core.ai_core.ai_router import get_ai_session

    await attach_inbound_tracks(bot, event)
    # 首轮 registry 里还没有 session；先创建再 bind，cancel 才能 set _cancel_generation
    session = await get_ai_session(event)
    bind_agent(run_id, session)
    return await run_passive_interactive_chat(
        bot,
        event,
        enqueue_ts=None,
        budget_mode="skip",
        wall_clock_budget=float(wall_clock),
        return_mode="by_bot",
        deliver=True,
        outbound_stream=True,
        stats_chat_type=HTTP_STATS_CHAT_TYPE,
    )


# ── FastAPI 路由注册 ─────────────────────────────────────────────


def register_http_agent_routes(app: FastAPI) -> None:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        logger.info(t("log.ai.http_agent_skip_ai_disabled"))
        return
    from gsuid_core.ai_core.http_agent.routes import agent_router

    app.include_router(agent_router)
