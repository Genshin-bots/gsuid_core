"""HTTP Agent API 的 TypedDict / 字面量。禁止 Any。"""

from __future__ import annotations

from typing import Literal, TypedDict

HTTP_AGENT_WS_BOT_ID = "HTTP_AGENT"
TOKEN_PREFIX = "gsk_"
KEY_ID_LEN = 8
#: Token 用量分类（与 IM 的 Chat 分开）。settle 按此写入 token_by_type。
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
