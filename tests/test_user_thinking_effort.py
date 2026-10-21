"""用户选择的思考档位进入请求。模型名不在 pydantic-ai 名单里时也不丢掉。"""

from __future__ import annotations

import asyncio
from typing import Literal

import httpx2
from openai import AsyncOpenAI, omit
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIResponsesModel,
    OpenAIChatModelSettings,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.providers.openai import OpenAIProvider

from gsuid_core.ai_core.configs.models import OpenAIConfiguredEffort, _with_openai_effort, user_thinking_profile
from gsuid_core.utils.plugins_config.models import GsStrConfig
from gsuid_core.ai_core.configs.openai_config.config_manager import OPENAI_CONFIG_TEMPLATE


def _kept_max(prepared: ModelSettings | None) -> Literal["max"] | None:
    # prepare_request 的标注丢掉 openai_*，运行时字典仍带着协议档。
    if not isinstance(prepared, dict) or "openai_reasoning_effort" not in prepared:
        return None
    if prepared["openai_reasoning_effort"] == "max":
        return "max"
    return None


def _close(model: OpenAIChatModel | OpenAIResponsesModel, http: httpx2.AsyncClient) -> None:
    async def _run() -> None:
        await model.client.close()
        await http.aclose()

    asyncio.run(_run())


def _wire(model_name: str, effort: OpenAIConfiguredEffort | None, *, responses: bool) -> object:
    http = httpx2.AsyncClient()
    client = AsyncOpenAI(api_key="sk-test", base_url="https://example.invalid/v1", http_client=http)
    provider = OpenAIProvider(openai_client=client)
    settings = _with_openai_effort(OpenAIChatModelSettings(), effort)
    profile = user_thinking_profile(None if effort == "max" else effort)
    model: OpenAIChatModel | OpenAIResponsesModel
    if responses:
        model = OpenAIResponsesModel(model_name, provider=provider, profile=profile, settings=settings)
    else:
        model = OpenAIChatModel(model_name, provider=provider, profile=profile, settings=settings)
    try:
        prepared, params = model.prepare_request(None, ModelRequestParameters())
        kept_max = _kept_max(prepared)
        if responses:
            assert isinstance(model, OpenAIResponsesModel)
            response_settings = OpenAIResponsesModelSettings()
            if kept_max is not None:
                response_settings["openai_reasoning_effort"] = kept_max
            reasoning = model._translate_thinking(response_settings, params)
            if reasoning is omit:
                return omit
            assert isinstance(reasoning, dict)
            return reasoning["effort"] if "effort" in reasoning else None
        assert isinstance(model, OpenAIChatModel)
        chat_settings = OpenAIChatModelSettings()
        if kept_max is not None:
            chat_settings["openai_reasoning_effort"] = kept_max
        return model._translate_thinking(chat_settings, params)
    finally:
        _close(model, http)


def test_unlisted_name_sends_high_and_xhigh() -> None:
    assert _wire("gemini-3.1-pro", "high", responses=False) == "high"
    assert _wire("gemini-3.1-pro", "xhigh", responses=False) == "xhigh"
    assert _wire("gpt-4o-mini", "high", responses=True) == "high"
    assert _wire("MiniMax-M3", "xhigh", responses=True) == "xhigh"


def test_enable_and_disable_are_sent() -> None:
    assert _wire("deepseek-chat", True, responses=False) == "medium"
    assert _wire("deepseek-chat", False, responses=False) == "none"


def test_known_reasoning_model_keeps_high() -> None:
    assert _wire("gpt-5.4", "high", responses=False) == "high"
    assert _wire("gpt-5.4", "xhigh", responses=True) == "xhigh"


def test_adaptive_omits_effort() -> None:
    assert _wire("gemini-3.1-pro", None, responses=False) is omit
    assert _wire("gemini-3.1-pro", None, responses=True) is omit


def test_max_is_sent_as_protocol_effort() -> None:
    assert _wire("deepseek-chat", "max", responses=False) == "max"
    assert _wire("deepseek-v4-pro", "max", responses=True) == "max"
    assert _wire("gpt-4o-mini", "max", responses=False) == "max"
    effort_cfg = OPENAI_CONFIG_TEMPLATE["model_effort"]
    assert isinstance(effort_cfg, GsStrConfig)
    assert "max" in effort_cfg.options
