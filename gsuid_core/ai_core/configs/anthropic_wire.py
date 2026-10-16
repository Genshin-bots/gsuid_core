"""Anthropic 400 的通用改写。

新模型不必写进代码。接口在错误正文里说明拒绝原因时，按正文改一次请求，
并把学到的事实留在本进程，后面的请求直接套用。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections.abc import Callable, AsyncIterator
from typing_extensions import override

from pydantic_ai import RunContext
from pydantic_ai.models import StreamedResponse, ModelRequestParameters
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.settings import ModelSettings
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.profiles.anthropic import AnthropicModelProfile
from anthropic.types.beta.beta_thinking_config_adaptive_param import BetaThinkingConfigAdaptiveParam

from gsuid_core.i18n import t
from gsuid_core.logger import logger

_ENABLED_REJECTED = "thinking.type.enabled"
_ADAPTIVE_HINT = "adaptive"
_STALE_BLOCK = "bound to a different conversation"
_DROP_BLOCK = "prefix_mismatch_behavior"
_SAMPLING_NAMES = ("temperature", "top_p", "top_k")


@dataclass(frozen=True)
class AnthropicWireFacts:
    """从一个模型的 400 正文里确认过的请求形状。"""

    adaptive: bool = False
    binds_blocks: bool = False
    drop_sampling: bool = False


_FACTS: dict[str, AnthropicWireFacts] = {}


def clear_anthropic_wire_facts() -> None:
    """清空本进程学到的事实。测试用来隔离用例。"""
    _FACTS.clear()


def anthropic_wire_key(model_name: str) -> str:
    """同一模型的 `anthropic.` 前缀与原名共用一份事实。"""
    name = model_name.lower()
    prefix = "anthropic."
    if name.startswith(prefix):
        return name[len(prefix) :]
    return name


def anthropic_profile_for_model(model_name: str) -> Callable[[ModelProfile], ModelProfile]:
    """把本进程学到的事实叠到 pydantic-ai 画像上。没学过则原样返回。"""

    def _overlay(default: ModelProfile) -> ModelProfile:
        facts = _FACTS.get(anthropic_wire_key(model_name))
        if facts is None:
            return default
        parts: list[AnthropicModelProfile] = []
        if facts.adaptive:
            parts.append(
                AnthropicModelProfile(
                    anthropic_supports_adaptive_thinking=True,
                    anthropic_supports_effort=True,
                    anthropic_disallows_budget_thinking=True,
                    thinking_enabled_by_default=True,
                )
            )
        if facts.binds_blocks:
            parts.append(AnthropicModelProfile(anthropic_binds_thinking_blocks=True))
        if facts.drop_sampling:
            parts.append(AnthropicModelProfile(anthropic_disallows_sampling_settings=True))
        if not parts:
            return default
        return merge_profile(default, *parts)

    return _overlay


def revise_anthropic_attempt(
    model_name: str,
    error: ModelHTTPError,
    settings: ModelSettings | None,
) -> ModelSettings | None:
    """400 正文能对应到已知改写时，记下事实并返回下一次请求的设置。

    正文没有新事实时返回 None，调用方应把原错误抛出去。
    """
    if error.status_code != 400:
        return None
    text = _error_text(error.body).lower()
    if not text:
        return None
    previous = _FACTS.get(anthropic_wire_key(model_name), AnthropicWireFacts())
    adaptive = previous.adaptive or (_ENABLED_REJECTED in text and _ADAPTIVE_HINT in text)
    binds = previous.binds_blocks or (_STALE_BLOCK in text or _DROP_BLOCK in text)
    drop_sampling = previous.drop_sampling or _rejects_sampling(text)
    updated = AnthropicWireFacts(adaptive=adaptive, binds_blocks=binds, drop_sampling=drop_sampling)
    if updated == previous:
        return None
    _FACTS[anthropic_wire_key(model_name)] = updated
    added = _added_reasons(previous, updated)
    logger.warning(t("log.ai.gscore_anthropic_wire_retry", model_name=model_name, reason=",".join(added)))
    revised: AnthropicModelSettings = {}
    if settings is not None:
        revised = {**settings}
    if binds and not previous.binds_blocks:
        drop_block: BetaThinkingConfigAdaptiveParam = {
            "type": "adaptive",
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        }
        revised["anthropic_thinking"] = drop_block
    return revised


def _added_reasons(previous: AnthropicWireFacts, updated: AnthropicWireFacts) -> list[str]:
    added: list[str] = []
    if updated.adaptive and not previous.adaptive:
        added.append("adaptive")
    if updated.binds_blocks and not previous.binds_blocks:
        added.append("drop_block")
    if updated.drop_sampling and not previous.drop_sampling:
        added.append("sampling")
    return added


def _rejects_sampling(text: str) -> bool:
    if "deprecated for this model" not in text and "not support" not in text:
        return False
    return any(name in text for name in _SAMPLING_NAMES)


def _error_text(body: object) -> str:
    parts: list[str] = []
    _collect_text(body, parts, 0)
    return "\n".join(parts)


def _collect_text(body: object, parts: list[str], depth: int) -> None:
    # 中转会把 Anthropic 的 JSON 再嵌进 message 字符串，扫到字符串即可，不必再解析。
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


class RecoveringAnthropicModel(AnthropicModel):
    """请求被 400 拒绝时按正文改写后再发一次。"""

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        settings = model_settings
        while True:
            try:
                return await super().request(messages, settings, model_request_parameters)
            except ModelHTTPError as error:
                revised = self._recover(error, settings)
                if revised is None:
                    raise
                settings = revised

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
        while True:
            # 父类签名是 RunContext。这里只转发真正的 RunContext，避免把 object 传进去。
            context = run_context if isinstance(run_context, RunContext) else None
            stream_cm = super().request_stream(messages, settings, model_request_parameters, context)
            try:
                response = await stream_cm.__aenter__()
            except ModelHTTPError as error:
                revised = self._recover(error, settings)
                if revised is None:
                    raise
                settings = revised
                continue
            try:
                yield response
            finally:
                await stream_cm.__aexit__(None, None, None)
            return

    def _recover(self, error: ModelHTTPError, settings: ModelSettings | None) -> ModelSettings | None:
        revised = revise_anthropic_attempt(self.model_name, error, settings)
        if revised is None:
            return None
        # profile 是 cached_property，清掉后下一次请求才看得到刚学到的事实。
        if "profile" in self.__dict__:
            delattr(self, "profile")
        return revised
