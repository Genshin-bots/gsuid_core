"""HTTP Agent Session ID。格式复用 Event.session_id 五段。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gsuid_core.ai_core.http_agent.types import HTTP_AGENT_WS_BOT_ID, HttpAgentKeyRecord

CLIENT_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CLIENT_MSG_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class ParsedGroupId:
    ok: bool
    value: str | None


def normalize_client_session(raw: str | None) -> str | None:
    value = raw if raw else "default"
    if CLIENT_SESSION_RE.fullmatch(value) is None:
        return None
    return value


def parse_group_id(raw: str | None) -> ParsedGroupId:
    """None / 空串 = 私聊。非法字符 ok=False。"""
    if raw is None or raw == "":
        return ParsedGroupId(ok=True, value=None)
    if CLIENT_SESSION_RE.fullmatch(raw) is None:
        return ParsedGroupId(ok=False, value=None)
    return ParsedGroupId(ok=True, value=raw)


def bot_self_id_for(
    key: HttpAgentKeyRecord,
    client_session: str,
    group_id: str | None = None,
) -> str:
    # 群聊 bot_self_id 不含 key_id，同 bot_id + session + group 才共享 Agent
    if group_id is not None:
        return f"g_{client_session}"
    return f"{key['key_id']}_{client_session}"


def session_id_for_key(
    key: HttpAgentKeyRecord,
    client_session: str,
    group_id: str | None = None,
) -> str:
    self_id = bot_self_id_for(key, client_session, group_id)
    if group_id is not None:
        return f"{HTTP_AGENT_WS_BOT_ID}:{key['bot_id']}:{self_id}:group:{group_id}"
    return f"{HTTP_AGENT_WS_BOT_ID}:{key['bot_id']}:{self_id}:private:{key['user_id']}"
