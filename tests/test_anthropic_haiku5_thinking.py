"""Anthropic 的 400 正文决定下一次请求怎么写思考参数。

没出现过这类 400 的名字仍用 budget_tokens。事实只留在本进程。
"""

from __future__ import annotations

import json
import asyncio
import warnings
from dataclasses import dataclass
from collections.abc import Iterator

import httpx2
import pytest
from anthropic import Omit, APIStatusError
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.settings import ModelSettings, ThinkingLevel
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.anthropic import (
    AnthropicModel,
    AnthropicModelSettings,
    _is_stale_thinking_block_error,
)

from gsuid_core.ai_core.configs.models import to_thinking_level, make_anthropic_model
from gsuid_core.ai_core.configs.anthropic_wire import revise_anthropic_attempt, clear_anthropic_wire_facts

_ENABLED_MESSAGE = (
    '"thinking.type.enabled" is not supported for this model. '
    'Use "thinking.type.adaptive" and "output_config.effort" to control thinking behavior.'
)
_SIGNATURE_MESSAGE = (
    "messages.1.content.0: Invalid `signature` in `thinking` block. "
    "The block is bound to a different conversation. "
    'Remove the block, or set `thinking.block_binding.prefix_mismatch_behavior` to "drop_block". '
    "That setting requires the `thinking-binding-controls-2026-08-01` value in the `anthropic-beta` header."
)
_FUTURE = "claude-future-9"


@dataclass
class _Posted:
    payload: dict[str, object]
    beta: str | None


@pytest.fixture(autouse=True)
def _reset_anthropic_wire_facts() -> Iterator[None]:
    clear_anthropic_wire_facts()
    yield
    clear_anthropic_wire_facts()


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


def _plain_body(message: str) -> dict[str, object]:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


def _proxy_body(message: str) -> dict[str, object]:
    # 中转把 Anthropic 的 JSON 再塞进 message 字符串。扫描要认这种正文。
    return _plain_body(json.dumps(_plain_body(message), ensure_ascii=False))


def _model_error(body: dict[str, object], status: int = 400) -> ModelHTTPError:
    return ModelHTTPError(status, _FUTURE, body)


def _api_error(body: dict[str, object]) -> APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return APIStatusError("bad", response=httpx2.Response(400, request=request), body=body)


def _object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        out[key] = item
    return out


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


def _teach_adaptive() -> None:
    revised = revise_anthropic_attempt(_FUTURE, _model_error(_proxy_body(_ENABLED_MESSAGE)), None)
    assert revised is not None
    assert "anthropic_thinking" not in revised


def _settings_dict(settings: ModelSettings) -> dict[str, object]:
    copied: dict[str, object] = {}
    for name, value in settings.items():
        copied[name] = value
    return copied


def _assert_effort(output: object, out_effort: str | None) -> None:
    if out_effort is None:
        assert output is None
        return
    assert isinstance(output, dict)
    assert "effort" in output
    assert output["effort"] == out_effort


@pytest.mark.parametrize(
    ("model_name", "effort", "kind", "budget", "out_effort"),
    [
        (_FUTURE, None, "omit", None, None),
        (_FUTURE, True, "adaptive", None, None),
        (_FUTURE, False, "disabled", None, None),
        (_FUTURE, "minimal", "adaptive", None, "low"),
        (_FUTURE, "low", "adaptive", None, "low"),
        (_FUTURE, "medium", "adaptive", None, "medium"),
        (_FUTURE, "high", "adaptive", None, "high"),
        (_FUTURE, "xhigh", "adaptive", None, "max"),
        (f"anthropic.{_FUTURE}", "high", "adaptive", None, "high"),
        ("claude-haiku-4-5", True, "enabled", 10000, None),
        ("claude-haiku-4-5", "high", "enabled", 16384, None),
        ("claude-haiku-4-5", False, "omit", None, None),
        ("claude-haiku-4-5", None, "omit", None, None),
    ],
)
def test_thinking_wire_follows_learned_facts(
    model_name: str,
    effort: ThinkingLevel | None,
    kind: str,
    budget: int | None,
    out_effort: str | None,
) -> None:
    if _FUTURE in model_name:
        _teach_adaptive()
    thinking, output = _wire(model_name, effort)
    if kind == "omit":
        assert isinstance(thinking, Omit)
    else:
        got_kind, got_budget = _as_thinking(thinking)
        assert got_kind == kind
        assert got_budget == budget
    _assert_effort(output, out_effort)


@pytest.mark.parametrize("body", [_plain_body(_ENABLED_MESSAGE), _proxy_body(_ENABLED_MESSAGE)])
def test_enabled_rejection_teaches_adaptive_and_stops(body: dict[str, object]) -> None:
    first = revise_anthropic_attempt(_FUTURE, _model_error(body), None)
    assert first is not None
    assert revise_anthropic_attempt(_FUTURE, _model_error(body), None) is None

    thinking, output = _wire(_FUTURE, "high")
    got_kind, got_budget = _as_thinking(thinking)
    assert got_kind == "adaptive"
    assert got_budget is None
    _assert_effort(output, "high")

    kept, http = _open(_FUTURE, "high")
    haiku4, http4 = _open("claude-haiku-4-5", True)
    try:
        prepared, _params = kept.prepare_request(ModelSettings(temperature=0.0), ModelRequestParameters())
        assert prepared is not None
        assert _settings_dict(prepared)["temperature"] == 0.0
        old_kind, old_budget = _as_thinking(haiku4._translate_thinking(AnthropicModelSettings(), _params_of(haiku4)))
        assert old_kind == "enabled"
        assert old_budget == 10000
    finally:
        _close(kept, http)
        _close(haiku4, http4)


def _params_of(model: AnthropicModel) -> ModelRequestParameters:
    _prepared, params = model.prepare_request(None, ModelRequestParameters())
    return params


@pytest.mark.parametrize("body", [_plain_body(_SIGNATURE_MESSAGE), _proxy_body(_SIGNATURE_MESSAGE)])
def test_signature_rejection_retries_with_drop_block(body: dict[str, object]) -> None:
    revised = revise_anthropic_attempt(_FUTURE, _model_error(body), ModelSettings())
    assert revised is not None
    thinking = revised["anthropic_thinking"] if "anthropic_thinking" in revised else None
    assert isinstance(thinking, dict)
    assert thinking["type"] == "adaptive"
    binding = thinking["block_binding"] if "block_binding" in thinking else None
    assert isinstance(binding, dict)
    assert binding["prefix_mismatch_behavior"] == "drop_block"
    assert revise_anthropic_attempt(_FUTURE, _model_error(body), ModelSettings()) is None

    taught, http_taught = _open(_FUTURE, None)
    haiku4, http4 = _open("claude-haiku-4-5", None)
    try:
        error = _api_error(body)
        assert _is_stale_thinking_block_error(taught.profile, Omit(), error) is True
        assert _is_stale_thinking_block_error(haiku4.profile, Omit(), error) is False
    finally:
        _close(taught, http_taught)
        _close(haiku4, http4)


@pytest.mark.parametrize(
    "message",
    [
        "'temperature' is deprecated for this model.",
        "top_k is not supported for this model.",
    ],
)
def test_sampling_rejection_drops_params_on_the_next_request(message: str) -> None:
    revised = revise_anthropic_attempt(_FUTURE, _model_error(_plain_body(message)), None)
    assert revised is not None
    taught, http_taught = _open(_FUTURE, "high")
    haiku4, http4 = _open("claude-haiku-4-5", "high")
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            prepared, _params = taught.prepare_request(
                ModelSettings(temperature=0.0, top_p=1.0),
                ModelRequestParameters(),
            )
        assert prepared is None or ("temperature" not in prepared and "top_p" not in prepared)
        assert any(issubclass(item.category, UserWarning) and _FUTURE in str(item.message) for item in caught)

        with warnings.catch_warnings(record=True) as kept:
            warnings.simplefilter("always")
            prepared4, _params4 = haiku4.prepare_request(
                ModelSettings(temperature=0.0, top_p=1.0),
                ModelRequestParameters(),
            )
        assert prepared4 is not None
        kept_settings = _settings_dict(prepared4)
        assert kept_settings["temperature"] == 0.0
        assert kept_settings["top_p"] == 1.0
        assert not any("Sampling parameters" in str(item.message) for item in kept)
    finally:
        _close(taught, http_taught)
        _close(haiku4, http4)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "Your credit balance is too low to access the Anthropic API."),
        (500, _ENABLED_MESSAGE),
    ],
)
def test_unrelated_error_keeps_budget(status: int, message: str) -> None:
    assert revise_anthropic_attempt(_FUTURE, _model_error(_plain_body(message), status), None) is None
    thinking, output = _wire(_FUTURE, True)
    kind, budget = _as_thinking(thinking)
    assert kind == "enabled"
    assert budget == 10000
    assert output is None


def test_adaptive_effort_omits_thinking_level() -> None:
    assert to_thinking_level("adaptive") is None
    assert to_thinking_level("自动") is None
    assert to_thinking_level("auto") is None
    assert to_thinking_level("enable") is True
    assert to_thinking_level("high") == "high"


def _posts(model_name: str, first_body: dict[str, object], *, stream: bool) -> list[_Posted]:
    posted: list[_Posted] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        parsed: object = json.loads(request.content.decode("utf-8"))
        payload = _object_dict(parsed)
        assert payload is not None
        beta = request.headers.get("anthropic-beta")
        posted.append(_Posted(payload, beta if isinstance(beta, str) else None))
        # 第二次起不再提供新事实，外层重试应停住并把这个 400 抛出去。
        body = first_body if len(posted) == 1 else _plain_body("Your credit balance is too low.")
        return httpx2.Response(400, json=body)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    model = make_anthropic_model(
        model_name,
        True,
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com",
        http_client=http,
    )

    async def _run() -> None:
        messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hi")])]
        try:
            if stream:
                async with model.request_stream(messages, None, ModelRequestParameters()):
                    return
            await model.request(messages, None, ModelRequestParameters())
        except ModelHTTPError:
            return
        finally:
            await model.client.close()
            await http.aclose()

    asyncio.run(_run())
    return posted


def _thinking_type(posted: _Posted) -> str | None:
    thinking = _object_dict(posted.payload["thinking"]) if "thinking" in posted.payload else None
    if thinking is None or "type" not in thinking:
        return None
    kind = thinking["type"]
    return kind if isinstance(kind, str) else None


@pytest.mark.parametrize("stream", [False, True])
def test_enabled_400_is_retried_as_adaptive(stream: bool) -> None:
    posted = _posts(_FUTURE, _proxy_body(_ENABLED_MESSAGE), stream=stream)
    assert len(posted) == 2
    assert _thinking_type(posted[0]) == "enabled"
    assert _thinking_type(posted[1]) == "adaptive"


def test_signature_400_is_retried_with_drop_block_and_beta() -> None:
    posted = _posts("claude-haiku-5-5", _proxy_body(_SIGNATURE_MESSAGE), stream=False)
    assert len(posted) == 2
    assert _thinking_type(posted[0]) == "enabled"
    second = _object_dict(posted[1].payload["thinking"]) if "thinking" in posted[1].payload else None
    assert second is not None
    assert second["type"] == "adaptive"
    binding = _object_dict(second["block_binding"]) if "block_binding" in second else None
    assert binding is not None
    assert binding["prefix_mismatch_behavior"] == "drop_block"
    assert posted[1].beta is not None
    assert "thinking-binding-controls-2026-08-01" in posted[1].beta
