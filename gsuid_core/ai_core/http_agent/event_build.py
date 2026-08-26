"""从 HTTP 请求装配 Event：走 ``msg_process``，不进 ``handle_event``。"""

from __future__ import annotations

from typing import List

from gsuid_core.models import Event, Message, MessageReceive
from gsuid_core.ai_core.http_agent.types import HTTP_AGENT_WS_BOT_ID, HttpAgentKeyRecord
from gsuid_core.ai_core.http_agent.session_id import bot_self_id_for


def is_inline_agent_image(raw: str) -> bool:
    # v1 不拉远程 URL / 不读本地路径，避免 Bearer 面变成 SSRF
    return raw.startswith("base64://") or raw.startswith("data:image/")


async def build_http_agent_event(
    *,
    key: HttpAgentKeyRecord,
    client_session: str,
    text: str,
    images: List[str],
    group_id: str | None = None,
) -> Event:
    from gsuid_core.handler import msg_process

    content: List[Message] = []
    if text:
        content.append(Message(type="text", data=text))
    for img in images:
        content.append(Message(type="image", data=img))
    is_group = group_id is not None
    msg = MessageReceive(
        bot_id=key["bot_id"],
        bot_self_id=bot_self_id_for(key, client_session, group_id),
        user_type="group" if is_group else "direct",
        group_id=group_id,
        user_id=key["user_id"],
        user_pm=key["user_pm"],
        content=content,
    )
    event = await msg_process(msg)
    event.WS_BOT_ID = HTTP_AGENT_WS_BOT_ID
    event.is_tome = True
    return event
