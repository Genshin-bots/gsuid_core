"""请求 JSON 上的思考档位进入会话日志。不读本地 model_effort。"""

from __future__ import annotations

import httpx
import httpx2
import pytest

from gsuid_core.ai_core.request_effort import (
    effort_capture,
    is_generation_body,
    take_request_effort,
    wire_thinking_effort,
    capture_model_request_effort,
)
from gsuid_core.ai_core.session_logger import AISessionLogger


def test_chat_effort_is_the_wire_field() -> None:
    body: dict[str, object] = {
        "model": "deepseek/deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "max",
    }
    assert is_generation_body(body)
    assert wire_thinking_effort(body) == "max"


def test_missing_effort_field_is_none() -> None:
    body: dict[str, object] = {
        "model": "deepseek/deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert wire_thinking_effort(body) is None


def test_disable_none_stays_distinct_from_omitted() -> None:
    body: dict[str, object] = {"model": "m", "messages": [], "reasoning_effort": "none"}
    assert wire_thinking_effort(body) == "none"


def test_responses_and_anthropic_and_gemini_fields() -> None:
    responses: dict[str, object] = {
        "model": "deepseek-v4-pro",
        "input": "hi",
        "reasoning": {"effort": "max"},
    }
    anthropic: dict[str, object] = {
        "model": "claude",
        "messages": [],
        "output_config": {"effort": "max"},
        "thinking": {"type": "adaptive"},
    }
    gemini: dict[str, object] = {
        "contents": [],
        "generationConfig": {"thinkingConfig": {"thinking_level": "HIGH"}},
    }
    budget: dict[str, object] = {
        "contents": [],
        "generationConfig": {"thinkingConfig": {"thinking_budget": 0}},
    }
    assert wire_thinking_effort(responses) == "max"
    assert wire_thinking_effort(anthropic) == "max"
    assert wire_thinking_effort(gemini) == "HIGH"
    assert wire_thinking_effort(budget) == "budget:0"


def test_enabled_budget_and_upload_are_not_confused() -> None:
    enabled: dict[str, object] = {
        "model": "claude",
        "messages": [],
        "thinking": {"type": "enabled", "budget_tokens": 1024},
    }
    upload: dict[str, object] = {"purpose": "assistants", "file": "abc"}
    assert wire_thinking_effort(enabled) == "enabled:1024"
    assert not is_generation_body(upload)


@pytest.mark.anyio
async def test_http_hook_reads_the_buffered_json() -> None:
    chat = httpx.Request(
        "POST",
        "https://api.example/v1/chat/completions",
        json={"model": "deepseek-v4", "messages": [], "reasoning_effort": "max"},
    )
    with effort_capture() as slot:
        await capture_model_request_effort(chat)
        assert take_request_effort(slot) == (True, "max")
        assert take_request_effort(slot) == (False, None)


@pytest.mark.anyio
async def test_later_generation_request_replaces_the_effort() -> None:
    first = httpx.Request(
        "POST",
        "https://api.example/v1/chat/completions",
        json={"model": "deepseek-v4", "messages": [], "reasoning_effort": "max"},
    )
    omitted = httpx2.Request(
        "POST",
        "https://api.example/v1/chat/completions",
        json={"model": "deepseek-v4", "messages": []},
    )
    with effort_capture() as slot:
        await capture_model_request_effort(first)
        await capture_model_request_effort(omitted)
        assert take_request_effort(slot) == (True, None)


@pytest.mark.anyio
async def test_hook_ignores_non_generation_json() -> None:
    upload = httpx.Request(
        "POST",
        "https://api.example/v1/files",
        json={"purpose": "assistants"},
    )
    with effort_capture() as slot:
        await capture_model_request_effort(upload)
        assert take_request_effort(slot) == (False, None)


def test_session_log_records_null_and_max() -> None:
    slog = AISessionLogger("test:thinking:effort", is_subagent=True)
    try:
        slog.log_request_thinking_effort(None)
        slog.log_request_thinking_effort("max")
        rows = [e for e in slog.entries if e["type"] == "thinking_effort"]
        assert [e["data"]["effort"] for e in rows] == [None, "max"]
    finally:
        slog._closed = True


def test_shared_model_clients_install_the_hook() -> None:
    from gsuid_core.ai_core.configs.models import _shared_model_http_client, _shared_model_httpx2_client

    http = _shared_model_http_client("effort-probe-http")
    http2 = _shared_model_httpx2_client("effort-probe-httpx2")
    assert capture_model_request_effort in http.event_hooks["request"]
    assert capture_model_request_effort in http2.event_hooks["request"]
