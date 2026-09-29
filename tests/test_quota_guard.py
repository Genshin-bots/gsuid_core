"""配额熔断（``quota_guard``）回归锁 + 错误文案分层。

背景：provider 把「套餐用量打满」（MiniMax 2056）也塞进 429，而 429 在
``const._RETRYABLE_4XX`` 里被当成可重试 —— 于是同一条失败在群里连喷 N 次兜底文案，
且每次都烧完整套重试预算。本文件锁住「配额类 fail-fast、限流/过载仍退避重试」。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError

import gsuid_core.ai_core.gs_agent as ga
from gsuid_core.ai_core.const import ERROR_QUOTA_EXHAUSTED
from gsuid_core.ai_core.utils import (
    ERROR_TIMEOUT_TEXT,
    ERROR_RESULT_PREFIX,
    classify_error_type,
    sanitize_error_for_user,
)
from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
from gsuid_core.ai_core.quota_guard import (
    QuotaBreaker,
    quota_breaker,
    classify_provider_error,
)
from gsuid_core.ai_core.session_logger import AISessionLogger
from gsuid_core.ai_core.persona.settings import get_persona_setting


def _http(status_code: int, message: str) -> ModelHTTPError:
    return ModelHTTPError(
        status_code=status_code,
        model_name="MiniMax-M3.1",
        body={"type": "x", "message": message},
    )


def test_quota_exhausted_is_not_a_generic_rate_limit() -> None:
    """用量上限 / 配额耗尽必须归到 quota，与 429 速率限制分开。"""
    assert classify_provider_error(_http(429, "已达到 Token Plan 用量上限 (2056)")) == "quota"
    assert classify_provider_error(_http(429, "insufficient_quota")) == "quota"


def test_rate_limit_stays_retryable() -> None:
    """速率限制是真瞬时，仍走原有退避重试，不能被熔断吃掉。"""
    assert classify_provider_error(_http(429, "已达到 Token Plan 速率限制 (2062)")) == "rate_limit"


def test_overloaded_is_retryable() -> None:
    """集群过载（529）同样是瞬时故障。"""
    assert classify_provider_error(_http(529, "当前服务集群负载较高 (2064)")) == "overloaded"


def test_unknown_http_error_is_other() -> None:
    """没见过的错误不该被误判成配额，否则会无谓熔断。"""
    assert classify_provider_error(_http(400, "bad request")) == "other"


def test_non_http_exception_is_other() -> None:
    assert classify_provider_error(ValueError("boom")) == "other"


def test_breaker_opens_on_quota_and_closes_after_window() -> None:
    """命中配额立刻开闸，窗口过后自动闭合。"""
    br = QuotaBreaker()
    br.configure(break_seconds=900.0, notify_seconds=300.0)
    assert br.is_open("minimax") is False
    br.note_hit("minimax", "quota")
    assert br.is_open("minimax") is True
    assert br.remaining_break_seconds("minimax") > 0

    br.configure(break_seconds=0.0, notify_seconds=300.0)
    assert br.is_open("minimax") is False


def test_rate_limit_does_not_open_breaker() -> None:
    """限流/过载只记账不开闸，否则会把真瞬时故障误熔断。"""
    br = QuotaBreaker()
    br.note_hit("minimax", "rate_limit")
    br.note_hit("minimax", "overloaded")
    assert br.is_open("minimax") is False


def test_breaker_is_per_provider() -> None:
    """一个 provider 配额打满不该牵连另一个。"""
    br = QuotaBreaker()
    br.note_hit("minimax", "quota")
    assert br.is_open("openai") is False


def test_repeat_notify_is_suppressed_inside_window() -> None:
    """熔断期内同一会话只放行第一句兜底。"""
    br = QuotaBreaker()
    br.configure(break_seconds=900.0, notify_seconds=300.0)
    assert br.should_notify("notify:s1") is True
    assert br.should_notify("notify:s1") is False
    assert br.should_notify("notify:s2") is True

    br.configure(break_seconds=900.0, notify_seconds=0.0)
    assert br.should_notify("notify:s1") is True


def test_reset_clears_notify_so_next_real_error_announces() -> None:
    """恢复正常后要清掉去重窗口，否则后续真实失败也被静默。"""
    br = QuotaBreaker()
    br.configure(break_seconds=900.0, notify_seconds=300.0)
    assert br.should_notify("notify:s1") is True
    br.reset("notify:s1")
    assert br.should_notify("notify:s1") is True


def test_expired_notify_keys_are_dropped_on_read() -> None:
    """窗口过期后去重表要丢掉别的 session，不能按会话只增不减。"""
    br = QuotaBreaker()
    br.configure(break_seconds=900.0, notify_seconds=300.0)
    assert br.should_notify("notify:s1") is True
    assert br.should_notify("notify:s2") is True
    br.configure(break_seconds=900.0, notify_seconds=0.0)
    assert "notify:s1" not in br._notified_at
    assert "notify:s2" not in br._notified_at
    assert br.should_notify("notify:s3") is True
    assert list(br._notified_at) == ["notify:s3"]


def test_expired_open_keys_are_dropped_on_read() -> None:
    """过期闸要在下次读取时整表清掉，不能只删被问到的那一把。"""
    br = QuotaBreaker()
    br.configure(break_seconds=900.0, notify_seconds=300.0)
    br.note_hit("minimax", "quota")
    br.note_hit("openai", "quota")
    br.configure(break_seconds=0.0, notify_seconds=300.0)
    assert br._opened_at == {}
    assert br.is_open("minimax") is False
    assert br.is_open("openai") is False


def test_quota_error_maps_to_dedicated_persona_phrase() -> None:
    """配额耗尽必须走独立短句，不能复用通用失败兜底。"""
    text = f"{ERROR_RESULT_PREFIX}: {ERROR_QUOTA_EXHAUSTED}"
    assert sanitize_error_for_user(text) == get_persona_setting(None, "error_quota")


def test_quota_error_label_for_master_dm() -> None:
    """私聊主人的失败分类要能区分配额打满。"""
    assert classify_error_type(f"{ERROR_RESULT_PREFIX}: {ERROR_QUOTA_EXHAUSTED}") == "套餐用量打满"


def _bare_execute_run_agent(monkeypatch: pytest.MonkeyPatch) -> tuple[GsCoreAIAgent, dict[str, int]]:
    """与 ``test_tool_safety`` 同形的裸 ``_execute_run`` 桩：不走 ``__init__``。"""

    class _Log(AISessionLogger):
        def __init__(self) -> None:
            super().__init__(session_id="test", is_subagent=True)

    original_get = ga.ai_config.get_config

    def fake_get(key: str) -> SimpleNamespace:
        if key == "agent_max_run_attempts":
            return SimpleNamespace(data=1)
        if key == "agent_run_retry_delay":
            return SimpleNamespace(data=0.0)
        return original_get(key)

    monkeypatch.setattr(ga.ai_config, "get_config", fake_get)
    agent = object.__new__(ga.GsCoreAIAgent)
    agent._last_attempt_tool_calls = []
    agent._active_config_name = "minimax:test"
    agent.model_config_name = "minimax:test"
    agent._session_logger = _Log()
    calls = {"n": 0}
    return agent, calls


def test_open_breaker_skips_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """闸已开时下一轮不得再打上游。"""
    agent, calls = _bare_execute_run_agent(monkeypatch)
    quota_breaker.clear()
    quota_breaker.note_hit("minimax:test", "quota")

    async def fake_once(*_a: object, **_k: object) -> str:
        calls["n"] += 1
        return "should-not-run"

    agent._execute_run_once = fake_once
    try:
        result = asyncio.run(agent._execute_run(user_message="hi"))
    finally:
        quota_breaker.clear()
    assert calls["n"] == 0
    assert ERROR_QUOTA_EXHAUSTED in str(result)
    assert str(result).startswith(ERROR_RESULT_PREFIX)


def test_timeout_after_breaker_opens_stays_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求途中闸被打开时，本次超时仍走超时文案，不改写成套餐打满。"""
    agent, calls = _bare_execute_run_agent(monkeypatch)
    quota_breaker.clear()

    async def fake_once(*_a: object, **_k: object) -> str:
        calls["n"] += 1
        quota_breaker.note_hit("minimax:test", "quota")
        raise httpx.TimeoutException("late")

    agent._execute_run_once = fake_once
    try:
        result = asyncio.run(agent._execute_run(user_message="hi"))
    finally:
        quota_breaker.clear()
    assert calls["n"] == 1
    assert ERROR_TIMEOUT_TEXT in str(result)
    assert ERROR_QUOTA_EXHAUSTED not in str(result)
    assert str(result).startswith(ERROR_RESULT_PREFIX)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
