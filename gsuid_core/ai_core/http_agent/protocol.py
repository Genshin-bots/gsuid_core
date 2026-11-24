"""HTTP Agent 协议层：SSE v1 编解码、类型定义、错误响应、Session ID 与 Event 装配。"""

from __future__ import annotations

import re
import json
from typing import Dict, List, Literal, Mapping, TypedDict
from dataclasses import dataclass

from fastapi.responses import JSONResponse

from gsuid_core.models import Event, Message, MessageReceive

HTTP_AGENT_WS_BOT_ID = "HTTP_AGENT"
TOKEN_PREFIX = "gsk_"
KEY_ID_LEN = 8
HTTP_STATS_CHAT_TYPE = "Http_Chat"

SseEventName = Literal["run.start", "text", "attachment", "run.done", "run.error"]
AttachmentEncoding = Literal["base64", "omitted", "url"]
AttachmentKind = Literal["image", "file"]
BudgetMode = Literal["gate", "decision", "skip"]
PassiveChatStatus = Literal["ok", "silence", "disabled", "not_ready", "budget"]


class HttpAgentKeyRecord(TypedDict):
    key_id: str
    token_hash: str
    user_id: str
    bot_id: str
    user_pm: int
    persona: str
    label: str
    created_at: float
    revoked: bool


class HttpAgentKeyPublic(TypedDict):
    key_id: str
    user_id: str
    bot_id: str
    user_pm: int
    persona: str
    label: str
    created_at: float
    revoked: bool


class ErrorBody(TypedDict):
    code: str
    message: str


SSE_HEADERS: Dict[str, str] = {
    # no-transform：按 RFC 7234 要求中间链路不得压缩/改写正文，nginx 等反代据此关掉 gzip。
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


def encode_sse(event: SseEventName, data: Mapping[str, object], event_id: int) -> str:
    payload = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n"


def encode_comment(text: str) -> str:
    return f": {text}\n\n"


@dataclass(frozen=True)
class SseFrame:
    event: str
    data: Dict[str, object]
    id: int | None


def parse_sse_chunk(raw: str) -> List[SseFrame]:
    """解析一块 SSE 文本。comment 行忽略。"""
    frames: List[SseFrame] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_lines: List[str] = []
        event_id: int | None = None
        is_comment_only = True
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            is_comment_only = False
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line.startswith("id:"):
                raw_id = line[3:].strip()
                if raw_id.isdigit():
                    event_id = int(raw_id)
        if is_comment_only or not event_name:
            continue
        data_obj: Dict[str, object] = {}
        blob = "\n".join(data_lines)
        if blob:
            parsed: object = json.loads(blob)
            if isinstance(parsed, dict):
                data_obj = parsed
        frames.append(SseFrame(event=event_name, data=data_obj, id=event_id))
    return frames


def error_response(status: int, code: str, message: str) -> JSONResponse:
    body: ErrorBody = {"code": code, "message": message}
    return JSONResponse(status_code=status, content=body)


def not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


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
