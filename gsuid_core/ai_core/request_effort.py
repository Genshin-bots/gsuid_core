"""从已经发出的模型请求 JSON 里读取思考档位。

本地 ``model_effort`` 不参与。字段不在请求里时结果是 None。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import Token, ContextVar
from dataclasses import dataclass
from collections.abc import Iterator

import httpx
import httpx2


@dataclass
class EffortCapture:
    """一次模型调用期间，钩子写、调用方读。"""

    seen: bool = False
    effort: str | None = None
    taken: bool = False


_CURRENT: ContextVar[EffortCapture | None] = ContextVar("gscore_request_thinking_effort", default=None)


@contextmanager
def effort_capture() -> Iterator[EffortCapture]:
    """把当前任务的出站模型请求记到一个新槽里。"""
    slot = EffortCapture()
    token: Token[EffortCapture | None] = _CURRENT.set(slot)
    try:
        yield slot
    finally:
        _CURRENT.reset(token)


def take_request_effort(slot: EffortCapture) -> tuple[bool, str | None]:
    """返回 (是否读到生成请求, 档位)。同一槽只取一次。"""
    if slot.taken or not slot.seen:
        return False, None
    slot.taken = True
    return True, slot.effort


def wire_thinking_effort(body: dict[str, object]) -> str | None:
    """生成请求 JSON 上的思考档位。没有对应字段时返回 None。"""
    direct = _text(body, "reasoning_effort")
    if direct is not None:
        return direct
    reasoning = _child(body, "reasoning")
    if reasoning is not None:
        nested = _text(reasoning, "effort")
        if nested is not None:
            return nested
    for parent_key in ("output_config", "outputConfig"):
        parent = _child(body, parent_key)
        if parent is None:
            continue
        effort = _text(parent, "effort")
        if effort is not None:
            return effort
    gemini = _gemini_effort(body)
    if gemini is not None:
        return gemini
    return _anthropic_thinking(body)


def is_generation_body(body: dict[str, object]) -> bool:
    """聊天、Responses、Gemini 的生成请求。上传文件这类 JSON 不是。"""
    if "messages" in body and isinstance(body["messages"], list):
        return True
    if "contents" in body and isinstance(body["contents"], list):
        return True
    return "input" in body and "model" in body and isinstance(body["model"], str)


async def capture_model_request_effort(request: httpx.Request | httpx2.Request) -> None:
    """httpx 请求钩子。当前任务没有捕获槽时什么也不做。"""
    slot = _CURRENT.get()
    if slot is None or slot.taken:
        return
    body = _json_object(request)
    if body is None or not is_generation_body(body):
        return
    slot.seen = True
    slot.effort = wire_thinking_effort(body)


def _json_object(request: httpx.Request | httpx2.Request) -> dict[str, object] | None:
    # 流式 body 还没缓冲，读它会吃掉要发出的字节。
    if type(request.stream).__name__ != "ByteStream":
        return None
    raw = request.content
    if not raw.startswith(b"{"):
        return None
    # 钩子不能因为请求体不是 JSON 而打断发送。
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _mapping(parsed)


def _mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            out[key] = item
    return out


def _child(body: dict[str, object], key: str) -> dict[str, object] | None:
    if key not in body:
        return None
    return _mapping(body[key])


def _text(body: dict[str, object], key: str) -> str | None:
    if key not in body:
        return None
    value = body[key]
    if isinstance(value, str) and value:
        return value
    return None


def _integer(body: dict[str, object], key: str) -> int | None:
    if key not in body:
        return None
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _gemini_effort(body: dict[str, object]) -> str | None:
    config = _child(body, "generationConfig")
    if config is None:
        config = _child(body, "generation_config")
    if config is None:
        return None
    thinking = _child(config, "thinkingConfig")
    if thinking is None:
        thinking = _child(config, "thinking_config")
    if thinking is None:
        return None
    level = _text(thinking, "thinkingLevel")
    if level is None:
        level = _text(thinking, "thinking_level")
    if level is not None:
        return level
    budget = _integer(thinking, "thinkingBudget")
    if budget is None:
        budget = _integer(thinking, "thinking_budget")
    if budget is not None:
        return f"budget:{budget}"
    if "include_thoughts" in thinking and thinking["include_thoughts"] is True:
        return "include_thoughts"
    if "includeThoughts" in thinking and thinking["includeThoughts"] is True:
        return "include_thoughts"
    return None


def _anthropic_thinking(body: dict[str, object]) -> str | None:
    thinking = _child(body, "thinking")
    if thinking is None:
        return None
    kind = _text(thinking, "type")
    if kind is None:
        return None
    if kind == "enabled":
        budget = _integer(thinking, "budget_tokens")
        if budget is not None:
            return f"enabled:{budget}"
    return kind
