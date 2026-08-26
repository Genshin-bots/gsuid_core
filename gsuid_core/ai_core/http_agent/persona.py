"""开流前解析人格：peek registry；冲突 409 不写 override。"""

from __future__ import annotations

from gsuid_core.ai_core.http_agent.types import HttpAgentKeyRecord
from gsuid_core.ai_core.http_agent.config import load_http_agent_settings


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
        from gsuid_core.buildin_plugins.core_command.core_ai_control.state import set_persona_override

        set_persona_override(session_id, candidate)
    return candidate
