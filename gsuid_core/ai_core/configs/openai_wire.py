"""OpenAI 兼容接口 400 的通用改写。

按上游错误正文自动学习思考下限（如拒绝 reasoning_effort=none 时退到 low、
不支持思考字段时剥离）与 max_completion_tokens 兼容，进程内记忆复用。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace, dataclass
from collections.abc import Callable, AsyncIterator
from typing_extensions import override

from openai import AsyncOpenAI
from pydantic_ai import RunContext
from pydantic_ai.models import StreamedResponse, ModelRequestParameters
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.settings import ModelSettings
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.multimodal.openai_files import OpenAIChatModelWithVideo

_EFFORT_KEYS = ("reasoning_effort", "reasoning.effort", "thinking")
_UNKNOWN_HINTS = (
    "unknown parameter",
    "unknown field",
    "unrecognized",
    "extra_forbidden",
    "extra inputs are not permitted",
    "not support",
    "unsupported parameter",
)


@dataclass(frozen=True)
class OpenAIWireFacts:
    """从 OpenAI 兼容网关 400 正文确认过的请求形状。"""

    always_thinking: bool = False
    drop_effort: bool = False
    legacy_max_tokens: bool = False
    drop_max_tokens: bool = False


_FACTS: dict[tuple[str, str], OpenAIWireFacts] = {}


def clear_openai_wire_facts() -> None:
    """清空本进程学到的事实，供测试隔离。"""
    _FACTS.clear()


def openai_wire_key(base_url: str, model_name: str) -> tuple[str, str]:
    return (base_url.strip().rstrip("/").lower(), model_name.strip().lower())


def get_openai_wire_facts(base_url: str, model_name: str) -> OpenAIWireFacts:
    key = openai_wire_key(base_url, model_name)
    return _FACTS[key] if key in _FACTS else OpenAIWireFacts()


def openai_profile_for_model(base_url: str, model_name: str) -> Callable[[ModelProfile], ModelProfile]:
    """把本进程学到的 OpenAI 兼容事实叠到画像上。"""

    def _overlay(default: ModelProfile) -> ModelProfile:
        key = openai_wire_key(base_url, model_name)
        if key not in _FACTS:
            return default
        facts = _FACTS[key]
        parts: list[OpenAIModelProfile] = []
        if facts.drop_effort:
            parts.append(OpenAIModelProfile(supports_thinking=False, thinking_always_enabled=False))
        elif facts.always_thinking:
            parts.append(
                OpenAIModelProfile(
                    supports_thinking=True,
                    thinking_always_enabled=True,
                    openai_reasoning_efforts=frozenset({"low", "medium", "high", "max"}),
                )
            )
        if facts.legacy_max_tokens:
            parts.append(OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False))
        if not parts:
            return default
        return merge_profile(default, *parts)

    return _overlay


def _error_text(body: object) -> str:
    parts: list[str] = []
    _collect_text(body, parts, 0)
    return "\n".join(parts)


def _collect_text(body: object, parts: list[str], depth: int) -> None:
    if depth > 6:
        return
    if isinstance(body, str):
        parts.append(body)
        return
    if isinstance(body, dict):
        for value in body.values():
            _collect_text(value, parts, depth + 1)
        return
    if isinstance(body, list):
        for value in body:
            _collect_text(value, parts, depth + 1)


def _mentions_effort(text: str) -> bool:
    return any(k in text for k in _EFFORT_KEYS)


def _looks_unsupported_field(text: str) -> bool:
    return any(h in text for h in _UNKNOWN_HINTS)


def _strip_effort_settings(settings: ModelSettings | None) -> ModelSettings | None:
    if settings is None:
        return None
    cleaned = OpenAIChatModelSettings(**settings)
    if "thinking" in cleaned:
        del cleaned["thinking"]
    if "openai_reasoning_effort" in cleaned:
        del cleaned["openai_reasoning_effort"]
    return cleaned if cleaned else None


def _strip_max_tokens_settings(settings: ModelSettings | None) -> ModelSettings | None:
    if settings is None:
        return None
    cleaned = OpenAIChatModelSettings(**settings)
    if "max_tokens" in cleaned:
        del cleaned["max_tokens"]
    return cleaned if cleaned else None


def revise_openai_attempt(
    base_url: str,
    model_name: str,
    error: ModelHTTPError,
    settings: ModelSettings | None,
    params: ModelRequestParameters,
) -> tuple[ModelSettings | None, ModelRequestParameters] | None:
    """400 正文可归因到思考档位或 max_tokens 字段时，更新事实并返回重试入参。"""
    if error.status_code != 400:
        return None
    text = _error_text(error.body).lower()
    if not text:
        return None

    key = openai_wire_key(base_url, model_name)
    prev = _FACTS[key] if key in _FACTS else OpenAIWireFacts()
    always_thinking = prev.always_thinking
    drop_effort = prev.drop_effort
    legacy_max = prev.legacy_max_tokens
    drop_max = prev.drop_max_tokens
    reasons: list[str] = []

    if _mentions_effort(text):
        if params.thinking is False and not prev.always_thinking and not _looks_unsupported_field(text):
            always_thinking = True
            reasons.append("min_effort_low")
        elif not prev.drop_effort:
            drop_effort = True
            reasons.append("drop_effort")

    if "max_completion_tokens" in text and not prev.legacy_max_tokens:
        legacy_max = True
        reasons.append("legacy_max_tokens")
    elif ("max_tokens" in text or "max_completion_tokens" in text) and not prev.drop_max_tokens:
        drop_max = True
        reasons.append("drop_max_tokens")

    updated = OpenAIWireFacts(
        always_thinking=always_thinking,
        drop_effort=drop_effort,
        legacy_max_tokens=legacy_max,
        drop_max_tokens=drop_max,
    )
    if updated == prev or not reasons:
        return None

    _FACTS[key] = updated
    logger.warning(t("log.ai.gscore_openai_wire_retry", model_name=model_name, reason=",".join(reasons)))

    next_settings = settings
    next_params = params
    if updated.drop_effort:
        next_settings = _strip_effort_settings(next_settings)
        next_params = replace(next_params, thinking=None)
    if updated.drop_max_tokens:
        next_settings = _strip_max_tokens_settings(next_settings)
    return next_settings, next_params


class RecoveringOpenAIChatModel(OpenAIChatModelWithVideo):
    """OpenAI 兼容 ChatModel：遇思考档位或 max_tokens 400 时自愈重试。"""

    def _provider_base_url(self) -> str:
        client = self.client
        if isinstance(client, AsyncOpenAI):
            return str(client.base_url)
        return ""

    @override
    def prepare_request(
        self,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[ModelSettings | None, ModelRequestParameters]:
        facts = get_openai_wire_facts(self._provider_base_url(), self.model_name)
        merged_settings, params = super().prepare_request(model_settings, model_request_parameters)
        if facts.drop_effort:
            merged_settings = _strip_effort_settings(merged_settings)
            params = replace(params, thinking=None)
        if facts.drop_max_tokens:
            merged_settings = _strip_max_tokens_settings(merged_settings)
        return merged_settings, params

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        settings = model_settings
        params = model_request_parameters
        while True:
            try:
                return await super().request(messages, settings, params)
            except ModelHTTPError as error:
                recovered = self._recover(error, settings, params)
                if recovered is None:
                    raise
                settings, params = recovered

    @override
    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: object | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        settings = model_settings
        params = model_request_parameters
        while True:
            context = run_context if isinstance(run_context, RunContext) else None
            stream_cm = super().request_stream(messages, settings, params, context)
            try:
                response = await stream_cm.__aenter__()
            except ModelHTTPError as error:
                recovered = self._recover(error, settings, params)
                if recovered is None:
                    raise
                settings, params = recovered
                continue
            try:
                yield response
            finally:
                await stream_cm.__aexit__(None, None, None)
            return

    def _recover(
        self,
        error: ModelHTTPError,
        settings: ModelSettings | None,
        params: ModelRequestParameters,
    ) -> tuple[ModelSettings | None, ModelRequestParameters] | None:
        revised = revise_openai_attempt(self._provider_base_url(), self.model_name, error, settings, params)
        if revised is None:
            return None
        if "profile" in self.__dict__:
            delattr(self, "profile")
        return revised
