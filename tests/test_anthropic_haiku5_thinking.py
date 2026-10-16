"""Haiku 5.5 拒绝 thinking.type=enabled。思考档位必须走 adaptive + effort。"""

from __future__ import annotations

import asyncio
import warnings

import httpx2
import pytest
from anthropic import Omit
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.settings import ModelSettings, ThinkingLevel
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings

from gsuid_core.ai_core.configs.models import to_thinking_level, make_anthropic_model


def _close(model: AnthropicModel, http: httpx2.AsyncClient) -> None:
    async def _run() -> None:
        await model.client.close()
        await http.aclose()

    asyncio.run(_run())


def _open(model_name: str, effort: ThinkingLevel | None) -> tuple[AnthropicModel, httpx2.AsyncClient]:
    http = httpx2.AsyncClient()
    model = make_anthropic_model(
        model_name,
        effort,
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com",
        http_client=http,
    )
    return model, http


def _wire(model_name: str, effort: ThinkingLevel | None) -> tuple[object, object]:
    model, http = _open(model_name, effort)
    try:
        _prepared, params = model.prepare_request(None, ModelRequestParameters())
        settings = AnthropicModelSettings()
        return (
            model._translate_thinking(settings, params),
            model._build_output_config(params, settings),
        )
    finally:
        _close(model, http)


def _as_thinking(body: object) -> tuple[str, int | None]:
    assert isinstance(body, dict)
    kind = body["type"] if "type" in body else None
    assert isinstance(kind, str)
    if "budget_tokens" not in body:
        return kind, None
    budget = body["budget_tokens"]
    assert isinstance(budget, int)
    return kind, budget


@pytest.mark.parametrize(
    ("model_name", "effort", "kind", "budget", "out_effort"),
    [
        ("claude-haiku-5-5", None, "omit", None, None),
        ("claude-haiku-5-5", True, "adaptive", None, None),
        ("claude-haiku-5-5", False, "disabled", None, None),
        ("claude-haiku-5-5", "minimal", "adaptive", None, "low"),
        ("claude-haiku-5-5", "low", "adaptive", None, "low"),
        ("claude-haiku-5-5", "medium", "adaptive", None, "medium"),
        ("claude-haiku-5-5", "high", "adaptive", None, "high"),
        ("claude-haiku-5-5", "xhigh", "adaptive", None, "xhigh"),
        ("anthropic.claude-haiku-5-5", "high", "adaptive", None, "high"),
        ("claude-haiku-4-5", True, "enabled", 10000, None),
        ("claude-haiku-4-5", "high", "enabled", 16384, None),
        ("claude-haiku-4-5", False, "omit", None, None),
        ("claude-haiku-4-5", None, "omit", None, None),
    ],
)
def test_haiku_thinking_wire(
    model_name: str,
    effort: ThinkingLevel | None,
    kind: str,
    budget: int | None,
    out_effort: str | None,
) -> None:
    thinking, output = _wire(model_name, effort)
    if kind == "omit":
        assert isinstance(thinking, Omit)
    else:
        got_kind, got_budget = _as_thinking(thinking)
        assert got_kind == kind
        assert got_budget == budget
    if out_effort is None:
        assert output is None
    else:
        assert isinstance(output, dict)
        assert output["effort"] == out_effort


def test_adaptive_effort_omits_thinking_level() -> None:
    assert to_thinking_level("adaptive") is None
    assert to_thinking_level("自动") is None
    assert to_thinking_level("auto") is None
    assert to_thinking_level("enable") is True
    assert to_thinking_level("high") == "high"


def test_haiku5_drops_sampling_params_haiku4_keeps_them() -> None:
    haiku5, http5 = _open("claude-haiku-5-5", "high")
    haiku4, http4 = _open("claude-haiku-4-5", "high")
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            prepared5, _params5 = haiku5.prepare_request(
                ModelSettings(temperature=0.0, top_p=1.0),
                ModelRequestParameters(),
            )
        assert prepared5 is None or ("temperature" not in prepared5 and "top_p" not in prepared5)
        assert any(
            issubclass(item.category, UserWarning) and "claude-haiku-5-5" in str(item.message) for item in caught
        )

        with warnings.catch_warnings(record=True) as kept:
            warnings.simplefilter("always")
            prepared4, _params4 = haiku4.prepare_request(
                ModelSettings(temperature=0.0, top_p=1.0),
                ModelRequestParameters(),
            )
        assert prepared4 is not None
        assert "temperature" in prepared4
        assert "top_p" in prepared4
        temperature = prepared4["temperature"]
        top_p = prepared4["top_p"]
        assert temperature == 0.0
        assert top_p == 1.0
        assert not any("Sampling parameters" in str(item.message) for item in kept)
    finally:
        _close(haiku5, http5)
        _close(haiku4, http4)
