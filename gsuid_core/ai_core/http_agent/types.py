"""HTTP Agent API 的 TypedDict / 字面量。禁止 Any。"""

from __future__ import annotations

from typing import Literal, TypedDict

HTTP_AGENT_WS_BOT_ID = "HTTP_AGENT"
TOKEN_PREFIX = "gsk_"
KEY_ID_LEN = 8

SseEventName = Literal["run.start", "text", "attachment", "run.done", "run.error"]
RunDoneStatus = Literal["ok", "silence", "cancelled"]
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


class RunStartPayload(TypedDict):
    run_id: str
    session_id: str
    seq: int


class TextPayload(TypedDict):
    text: str
    seq: int


class AttachmentPayload(TypedDict):
    kind: AttachmentKind
    encoding: AttachmentEncoding
    mime: str
    data: str
    nbytes: int
    seq: int


class RunDonePayload(TypedDict):
    status: RunDoneStatus
    seq: int


class RunErrorPayload(TypedDict):
    code: str
    message: str
    seq: int


class ErrorBody(TypedDict):
    code: str
    message: str
