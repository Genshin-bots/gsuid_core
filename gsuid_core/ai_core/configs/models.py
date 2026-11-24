"""AI模块共享适配器

提供LLM和嵌入的共享适配器，供mem、gs_agent等模块复用。

配置名称格式: "provider++config_name" (例如 "openai++MiniMAX")
- provider: "openai" / "anthropic" / "gemini"
- config_name: 配置文件名称
- 分隔符: "++"
- 兼容旧格式: 不含 "++" 的名称默认按 "openai" provider 处理

Gemini 说明: 走 pydantic_ai 的 GoogleModel(Google GenAI 原生格式), 依赖可选包
``google-genai``(pydantic-ai-slim 的 ``google`` extra)。缺依赖时仅 gemini 配置
不可用, 不影响 openai/anthropic —— 因此 GoogleModel 采用**延迟导入**。

pydantic-ai 升级: ModelProfile 在 2.0 起改 `TypedDict`, ``from_profile`` 和
``dataclasses.replace`` 都已移除; ``profile=`` 参数也由 ``(name) -> Profile``
改为 ``(default_profile) -> Profile``(框架把已解析的默认 profile 喂过来)。
"""

from __future__ import annotations

import ssl
import json
import asyncio
import hashlib
from typing import TYPE_CHECKING, Union, Literal, Protocol
from dataclasses import dataclass
from collections.abc import Callable
from typing_extensions import override

import httpx2
import httpcore2
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.settings import ModelSettings, ThinkingLevel
from pydantic_ai.profiles.openai import OpenAIModelProfile

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.path_safety import is_safe_filename
from gsuid_core.ai_core.request_effort import capture_model_request_effort
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .ai_config import ai_config
from .task_kinds import TaskLevel, parent_task_kind
from .provider_config_manager import ProviderType, is_provider_type, provider_manager

if TYPE_CHECKING:
    import httpx
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel, OpenAIChatModelSettings
    from pydantic_ai.models.anthropic import AnthropicModel

    from gsuid_core.ai_core.multimodal.openai_files import OpenAIVideoInputFormat

# 配置名称分隔符
PROVIDER_CONFIG_SEPARATOR = "++"

# 受支持的 provider 类型（webconsole API 与配置解析共用一份定义）
SUPPORTED_PROVIDERS: tuple[str, ...] = ("openai", "anthropic", "gemini")

# 任一 provider 构建出的模型对象（各 provider 均延迟导入，注解用前向引用）
AnyModel = Union["OpenAIChatModel", "OpenAIResponsesModel", "AnthropicModel", "GoogleModel"]

# OpenAI 请求方式：chat_completions 走 /v1/chat/completions，responses 走 /v1/responses。
RequestMethod = Literal["chat_completions", "responses"]

# OpenAI 模型对象（两种端点对 gs_agent 接口完全一致，仅底层请求路径不同）。
OpenAIModel = Union["OpenAIChatModel", "OpenAIResponsesModel"]

# openai 3.x / anthropic 1.x 的 HTTP 栈换成了 httpx2，与 httpx 不是同一套类、不可互传；
# 只有 google-genai 仍吃 httpx（在 _shared_model_http_client 内懒导入），同 §23 墙钟。
MODEL_REQUEST_TIMEOUT_HTTPX2 = httpx2.Timeout(connect=15.0, read=180.0, write=60.0, pool=30.0)
_MODEL_HTTPX2_LIMITS = httpx2.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)


class _SafeProxyConnection(httpcore2.AsyncConnectionInterface):
    """代理连接在握手完成前失败或取消时关闭底层连接，修复 httpcore2 隧道槽永久泄漏。"""

    def __init__(self, inner: httpcore2.AsyncConnectionInterface) -> None:
        self._inner = inner
        self._established = False

    @override
    async def handle_async_request(self, request: httpcore2.Request) -> httpcore2.Response:
        try:
            resp = await self._inner.handle_async_request(request)
            self._established = True
            return resp
        except BaseException:
            if not self._established:
                await asyncio.shield(self._inner.aclose())
            raise

    @override
    def can_handle_request(self, origin: httpcore2.Origin) -> bool:
        return self._inner.can_handle_request(origin)

    @override
    async def aclose(self) -> None:
        await self._inner.aclose()

    @override
    def is_available(self) -> bool:
        return self._inner.is_available()

    @override
    def has_expired(self) -> bool:
        return self._inner.has_expired()

    @override
    def is_idle(self) -> bool:
        return self._inner.is_idle()

    @override
    def is_closed(self) -> bool:
        return self._inner.is_closed()

    @override
    def is_connected(self) -> bool:
        return self._inner.is_connected()

    @override
    def can_multiplex(self) -> bool:
        return self._inner.can_multiplex()

    @override
    def info(self) -> str:
        return self._inner.info()


class _SafeAsyncHTTPProxy(httpcore2.AsyncHTTPProxy):
    @override
    def create_connection(self, origin: httpcore2.Origin) -> httpcore2.AsyncConnectionInterface:
        return _SafeProxyConnection(super().create_connection(origin))


class _SafeProxyAsyncClient(httpx2.AsyncClient):
    """httpx2.AsyncClient 变体：HTTP/HTTPS 代理传输改用 _SafeAsyncHTTPProxy 防隧道泄漏。"""

    @override
    def _init_proxy_transport(
        self,
        proxy: httpx2.Proxy,
        verify: ssl.SSLContext | str | bool = True,
        cert: str | tuple[str, str] | tuple[str, str, str] | None = None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: httpx2.Limits = _MODEL_HTTPX2_LIMITS,
    ) -> httpx2.AsyncBaseTransport:
        transport = super()._init_proxy_transport(
            proxy,
            verify=verify,
            cert=cert,
            trust_env=trust_env,
            http1=http1,
            http2=http2,
            limits=limits,
        )
        if isinstance(transport, httpx2.AsyncHTTPTransport) and proxy.url.scheme in ("http", "https"):
            # 若 httpx2 升级改名 _pool，此处显式断言失败而非静默回退到漏槽实现。
            assert isinstance(transport._pool, httpcore2.AsyncHTTPProxy)
            ssl_context = httpx2.create_ssl_context(verify=verify, cert=cert, trust_env=trust_env)
            transport._pool = _SafeAsyncHTTPProxy(
                proxy_url=httpcore2.URL(
                    scheme=proxy.url.raw_scheme,
                    host=proxy.url.raw_host,
                    port=proxy.url.port,
                    target=proxy.url.raw_path,
                ),
                proxy_auth=proxy.raw_auth,
                proxy_headers=proxy.headers.raw,
                proxy_ssl_context=proxy.ssl_context,
                ssl_context=ssl_context,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=http1,
                http2=http2,
            )
        return transport


_MODEL_HTTP_CLIENTS: dict[tuple[str, str], httpx.AsyncClient] = {}
_MODEL_HTTPX2_CLIENTS: dict[tuple[str, str], httpx2.AsyncClient] = {}
_MODEL_CLIENT_GENERATIONS: dict[tuple[str, str], int] = {}


def _shared_model_http_client(provider: str, config_name: str = "") -> httpx.AsyncClient:
    """进程级共享 httpx 客户端（仅 google-genai 需要，懒导入 httpx 避免非 Gemini 场景常驻）。"""
    import httpx

    key = (provider, config_name)
    client = _MODEL_HTTP_CLIENTS[key] if key in _MODEL_HTTP_CLIENTS else None
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=180.0, write=60.0, pool=30.0),
            event_hooks={"request": [capture_model_request_effort]},
        )
        _MODEL_HTTP_CLIENTS[key] = client
    return client


def _shared_model_httpx2_client(provider: str, config_name: str = "") -> httpx2.AsyncClient:
    """已迁到 httpx2 的 SDK（openai 3.x / anthropic 1.x）共享池（按配置名分池隔离）。"""
    key = (provider, config_name)
    client = _MODEL_HTTPX2_CLIENTS[key] if key in _MODEL_HTTPX2_CLIENTS else None
    if client is None or client.is_closed:
        # 钩子从发出的 JSON 读思考档位，不读本地 model_effort。
        client = _SafeProxyAsyncClient(
            timeout=MODEL_REQUEST_TIMEOUT_HTTPX2,
            limits=_MODEL_HTTPX2_LIMITS,
            event_hooks={"request": [capture_model_request_effort]},
        )
        _MODEL_HTTPX2_CLIENTS[key] = client
    return client


def reset_shared_model_http_client(provider: str, config_name: str = "") -> bool:
    """连接池耗尽时轮换对应 (provider, config_name) 共享池并递增代际触发会话重绑。"""
    key = (provider, config_name)
    reset = False
    # 仅移出缓存不主动 aclose：可能有健康在途流仍引用旧池，待流结束由 GC 回收。
    if key in _MODEL_HTTPX2_CLIENTS:
        _MODEL_HTTPX2_CLIENTS.pop(key)
        reset = True
    if key in _MODEL_HTTP_CLIENTS:
        _MODEL_HTTP_CLIENTS.pop(key)
        reset = True
    if reset:
        _MODEL_CLIENT_GENERATIONS[key] = (_MODEL_CLIENT_GENERATIONS[key] if key in _MODEL_CLIENT_GENERATIONS else 0) + 1
    return reset


def reset_shared_model_http_client_by_full_name(full_name: str) -> bool:
    """按 'provider++config_name' 重建对应配置的共享连接池。"""
    if not full_name:
        return False
    provider, config_name = parse_provider_config_name(full_name)
    return reset_shared_model_http_client(provider, config_name)


def parse_provider_config_name(full_name: str) -> tuple[str, str]:
    """
    解析 "provider++config_name" 格式的配置名称。

    Args:
        full_name: 完整配置名称，格式为 "provider++config_name"
                   兼容旧格式：不含 "++" 的名称默认按 "openai" provider 处理

    Returns:
        (provider, config_name) 元组
        - provider: "openai" / "anthropic" / "gemini"
        - config_name: 实际配置文件名称

    Examples:
        >>> parse_provider_config_name("openai++MiniMAX")
        ('openai', 'MiniMAX')
        >>> parse_provider_config_name("anthropic++Claude")
        ('anthropic', 'Claude')
        >>> parse_provider_config_name("gemini++Gemini")
        ('gemini', 'Gemini')
        >>> parse_provider_config_name("MiniMAX")  # 兼容旧格式
        ('openai', 'MiniMAX')
    """
    if PROVIDER_CONFIG_SEPARATOR in full_name:
        provider, config_name = full_name.split(PROVIDER_CONFIG_SEPARATOR, 1)
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                t(
                    "log.ai.gscore_ai_provider_supported",
                    provider=provider,
                    supported=" / ".join(SUPPORTED_PROVIDERS),
                )
            )
        return provider, config_name

    # 兼容旧格式：不含 "++" 的名称默认按 openai 处理
    return "openai", full_name


def format_provider_config_name(provider: str, config_name: str) -> str:
    """
    将 provider 和 config_name 格式化为 "provider++config_name" 格式。

    Args:
        provider: "openai" / "anthropic" / "gemini"
        config_name: 配置文件名称

    Returns:
        格式化后的完整配置名称
    """
    return f"{provider}{PROVIDER_CONFIG_SEPARATOR}{config_name}"


# model_effort 配置映射至 pydantic_ai ThinkingLevel，规范化 enable/disable 值为布尔值。
# adaptive 不在此表：不写 thinking，让模型用自己的默认。
THINKING_LEVEL_MAP: dict[str, ThinkingLevel] = {
    "enable": True,
    "disable": False,
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# 已保存的「自动」/ auto 仍省略 thinking，避免落到未知值后被当成 enable。
_OMIT_EFFORT = frozenset({"adaptive", "自动", "auto"})


def to_thinking_level(value: str) -> ThinkingLevel | None:
    """将配置中的 model_effort 字符串转换为 pydantic_ai 的 ThinkingLevel。

    adaptive 返回 None，调用方不要把 thinking 写进请求。
    """
    if value in _OMIT_EFFORT:
        return None
    if value not in THINKING_LEVEL_MAP:
        logger.warning(t("log.ai.gscore_effort_config_value_fell", value=repr(value)))
        return True
    return THINKING_LEVEL_MAP[value]


OpenAIConfiguredEffort = ThinkingLevel


def to_openai_effort(value: str) -> OpenAIConfiguredEffort | None:
    """OpenAI 兼容配置的思考档位。"""
    return to_thinking_level(value)


def _with_thinking(settings: ModelSettings, model_effort: ThinkingLevel | None) -> ModelSettings:
    if model_effort is None:
        return settings
    settings["thinking"] = model_effort
    return settings


def _with_openai_effort(
    settings: OpenAIChatModelSettings,
    model_effort: OpenAIConfiguredEffort | None,
) -> OpenAIChatModelSettings:
    if model_effort is not None:
        settings["thinking"] = model_effort
    return settings


def user_thinking_profile(
    model_effort: ThinkingLevel | None,
) -> Callable[[ModelProfile], ModelProfile]:
    """用户选了档位时让画像承认思考，否则 prepare_request 会丢掉 settings['thinking']。"""

    def _overlay(default: ModelProfile) -> ModelProfile:
        # adaptive 不传。画像已承认思考时不动，保留「不能关闭」的结论。
        if model_effort is None:
            return default
        if ("supports_thinking" in default and bool(default["supports_thinking"])) or (
            "thinking_always_enabled" in default and bool(default["thinking_always_enabled"])
        ):
            return default
        return merge_profile(default, ModelProfile(supports_thinking=True))

    return _overlay


def openai_user_thinking_profile(
    model_effort: OpenAIConfiguredEffort | None,
) -> Callable[[ModelProfile], ModelProfile]:
    """OpenAI 兼容配置声明过思考档位时开启 supports_thinking 与 max 映射，供 fast/thinking=False 生效。"""

    def _overlay(default: ModelProfile) -> ModelProfile:
        if model_effort is None:
            return default
        has_thinking = ("supports_thinking" in default and bool(default["supports_thinking"])) or (
            "thinking_always_enabled" in default and bool(default["thinking_always_enabled"])
        )
        patch = OpenAIModelProfile(
            openai_chat_supports_max_reasoning_effort=True,
            openai_responses_supports_max_reasoning_effort=True,
        )
        if not has_thinking:
            patch["supports_thinking"] = True
        return merge_profile(default, patch)

    return _overlay


def to_request_method(value: str) -> RequestMethod:
    """将配置中的 request_method 字符串归一为 RequestMethod，未知值回退 chat_completions。"""
    if value == "responses":
        return "responses"
    if value != "chat_completions":
        logger.warning(t("log.ai.gscore_request_method_config", value=repr(value)))
    return "chat_completions"


# 每 chunk 携带「累计」usage 的已知网关（vLLM/SGLang 系）：避免在此导入 openai_wire。
_CUMULATIVE_USAGE_URL_KEYWORDS = ("siliconflow",)


def _resolve_continuous_usage(base_url: str, mode: str) -> bool:
    """根据配置与 base_url 判定流式 usage 是否为累计语义（cumulative）。"""
    if mode == "cumulative":
        return True
    if mode == "incremental":
        return False
    if mode != "auto":
        logger.warning(t("log.ai.gscore_usage_stats_mode_config", mode=repr(mode)))
    import sys

    wire_mod = sys.modules.get("gsuid_core.ai_core.configs.openai_wire")
    if wire_mod is not None:
        from .openai_wire import DETECTED_CUMULATIVE_URLS

        if base_url in DETECTED_CUMULATIVE_URLS:
            return True
    return any(kw in base_url for kw in _CUMULATIVE_USAGE_URL_KEYWORDS)


def get_openai_config_by_name(
    config_name: str,
) -> tuple[str, str, str, OpenAIConfiguredEffort | None, RequestMethod, bool, str]:
    oconfig = provider_manager("openai").get_config(config_name)
    base_url, api_key, model_name, model_effort, request_method = (
        oconfig.get_config("base_url").data,
        oconfig.get_config("api_key").data[0],
        oconfig.get_config("model_name").data,
        to_openai_effort(oconfig.get_config("model_effort").data),
        to_request_method(oconfig.get_config("request_method").data),
    )
    # 旧配置文件缺该 key 时 get_config 会自动从模板补默认值 "auto", 不会抛异常
    usage_stats_mode = str(oconfig.get_config("usage_stats_mode").data)
    continuous_usage = _resolve_continuous_usage(base_url, usage_stats_mode)
    logger.info(
        t(
            "log.ai.gscore_openai_name_url_base",
            model_name=model_name,
            base_url=base_url,
            key_tail=api_key[-4:],
            request_method=request_method,
            usage_suffix=(", 流式usage: cumulative" if continuous_usage else ""),
        )
    )
    return base_url, api_key, model_name, model_effort, request_method, continuous_usage, usage_stats_mode


def get_anthropic_config_by_name(config_name: str) -> tuple[str, str, str, ThinkingLevel | None]:
    aconfig = provider_manager("anthropic").get_config(config_name)
    base_url, api_key, model_name, model_effort = (
        aconfig.get_config("base_url").data,
        aconfig.get_config("api_key").data[0],
        aconfig.get_config("model_name").data,
        to_thinking_level(aconfig.get_config("model_effort").data),
    )
    logger.info(
        t(
            "log.ai.gscore_anthropic_config_name_url",
            model_name=model_name,
            base_url=base_url,
            p0=api_key[-4:],
        )
    )
    return base_url, api_key, model_name, model_effort


def get_openai_model_by_name(config_name: str, *, fast: bool = False) -> OpenAIModel:
    """根据配置名获取 OpenAI 模型，按 request_method 选择端点。

    chat_completions → OpenAIChatModel(/v1/chat/completions)；
    responses → OpenAIResponsesModel(/v1/responses)。两者均接受同一 OpenAIProvider，
    且对 gs_agent 暴露相同接口（client/model_name/system/profile/request_stream）。

    ``fast=True`` 时不注入思考档位：隔离用户在配置文件里为聊天设置的 max/high，
    供前置路由与后台分诊使用。

    Args:
        config_name: 配置文件名（不含扩展名）
    """
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModelSettings
    from pydantic_ai.providers.openai import OpenAIProvider

    from gsuid_core.ai_core.multimodal.openai_files import parse_video_part_extra

    from .openai_wire import (
        AutoUsageOpenAIChatModel,
        RecoveringOpenAIChatModel,
        RecoveringOpenAIResponsesModel,
        openai_profile_for_model,
    )

    base_url, api_key, model_name, configured_effort, request_method, continuous_usage, usage_stats_mode = (
        get_openai_config_by_name(config_name)
    )
    settings_effort: OpenAIConfiguredEffort | None = None if fast else configured_effort
    if fast:
        usage_stats_mode = "incremental"

    # §23 墙钟 + 按配置分池；max_retries=0 禁 SDK 内建重试，避免与 _execute_run 叠乘。
    _client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=MODEL_REQUEST_TIMEOUT_HTTPX2,
        max_retries=0,
        http_client=_shared_model_httpx2_client("openai", config_name),
    )
    provider = OpenAIProvider(openai_client=_client)

    oconfig = provider_manager("openai").get_config(config_name)
    send_back_thinking = str(oconfig.get_config("send_back_thinking").data)
    video_input_format_val = str(oconfig.get_config("video_input_format").data)
    video_input_format: OpenAIVideoInputFormat = "video_url" if video_input_format_val == "video_url" else "file"
    video_fps_raw = oconfig.get_config("video_fps").data
    video_fps = float(video_fps_raw) if isinstance(video_fps_raw, (int, float)) else 0.0
    video_part_extra_raw = str(oconfig.get_config("video_part_extra").data).strip()
    video_part_extra = parse_video_part_extra(video_part_extra_raw) if video_part_extra_raw else {}
    video_inline_max_mb_raw = oconfig.get_config("video_inline_max_mb").data
    video_inline_max_mb = int(video_inline_max_mb_raw) if isinstance(video_inline_max_mb_raw, int) else 12
    video_url_passthrough = str(oconfig.get_config("video_url_passthrough").data)
    learned = openai_profile_for_model(base_url, model_name)
    keep_effort = openai_user_thinking_profile(configured_effort)

    def _responses_profile(default: ModelProfile) -> ModelProfile:
        return learned(keep_effort(default))

    def _chat_profile(default: ModelProfile) -> ModelProfile:
        profile = learned(keep_effort(default))
        if send_back_thinking != "off":
            return profile
        return merge_profile(profile, OpenAIModelProfile(openai_chat_send_back_thinking_parts=False))

    if request_method == "responses":
        # 视频走 /v1/files UploadedFile，不覆写 pydantic_ai 父类静态 mapper
        return RecoveringOpenAIResponsesModel(
            model_name=model_name,
            provider=provider,
            profile=_responses_profile,
            settings=_with_openai_effort(OpenAIChatModelSettings(), settings_effort),
        )

    # cumulative 语义网关须取「最后累计值」而非逐 chunk 累加, 否则统计膨胀数十倍;
    # auto 模式用探测子类兜底白名单外的网关（见 _AutoUsageStreamedResponse）
    model_cls = (
        RecoveringOpenAIChatModel if usage_stats_mode in ("incremental", "cumulative") else AutoUsageOpenAIChatModel
    )
    return model_cls(
        model_name=model_name,
        provider=provider,
        profile=_chat_profile,
        # 请求参数 stream_options.continuous_usage_stats 仅在部署者显式声明
        # cumulative 时发送; auto 的白名单/探测只作用于响应侧, 不改请求体
        settings=_with_openai_effort(
            OpenAIChatModelSettings(
                openai_continuous_usage_stats=usage_stats_mode == "cumulative",
            ),
            settings_effort,
        ),
        video_input_format=video_input_format,
        video_fps=video_fps,
        video_part_extra=video_part_extra,
        video_inline_max_mb=video_inline_max_mb,
        video_url_passthrough=video_url_passthrough,
    )


def make_anthropic_model(
    model_name: str,
    model_effort: ThinkingLevel | None,
    *,
    api_key: str,
    base_url: str,
    http_client: httpx2.AsyncClient,
    profile_effort: ThinkingLevel | None = None,
) -> AnthropicModel:
    """构造 Anthropic 模型。思考形状用本进程从 400 正文里学到的事实。"""
    from anthropic import AsyncAnthropic
    from pydantic_ai.providers.anthropic import AnthropicProvider

    from .anthropic_wire import RecoveringAnthropicModel, anthropic_profile_for_model

    client = AsyncAnthropic(
        api_key=api_key,
        base_url=base_url,
        timeout=MODEL_REQUEST_TIMEOUT_HTTPX2,
        max_retries=0,
        http_client=http_client,
    )
    learned = anthropic_profile_for_model(model_name)
    keep_effort = user_thinking_profile(profile_effort if profile_effort is not None else model_effort)

    def _profile(default: ModelProfile) -> ModelProfile:
        return keep_effort(learned(default))

    return RecoveringAnthropicModel(
        model_name=model_name,
        provider=AnthropicProvider(anthropic_client=client),
        profile=_profile,
        settings=_with_thinking(ModelSettings(), model_effort),
    )


def get_anthropic_chat_model_by_name(config_name: str, *, fast: bool = False) -> "AnthropicModel":
    """根据配置名获取Anthropic Chat Model
    Args:
        config_name: 配置文件名（不含扩展名）
    """
    base_url, api_key, model_name, configured_effort = get_anthropic_config_by_name(config_name)
    settings_effort = None if fast else configured_effort

    logger.info(
        t(
            "log.ai.gscore_anthropic_name_url_base",
            model_name=model_name,
            base_url=base_url,
            p0=api_key[-4:],
        )
    )

    # §23 墙钟 + 共享连接池：与 openai 工厂同一套超时/重试策略（评审修复 F8/E13）
    return make_anthropic_model(
        model_name,
        settings_effort,
        api_key=api_key,
        base_url=base_url,
        http_client=_shared_model_httpx2_client("anthropic", config_name),
        profile_effort=configured_effort,
    )


#: Gemini 官方 API 地址。base_url 等于它时不传给 SDK(用 SDK 内建默认,
#: 避免 URL 拼接/尾斜杠差异带来 404;浏览器直接访问根路径 404 是正常现象)
GEMINI_OFFICIAL_BASE_URL = "https://generativelanguage.googleapis.com"


def normalize_gemini_base_url(base_url: str) -> str:
    """官方默认地址归一为空串(SDK 用内建默认);中转地址原样返回。"""
    if base_url.strip().rstrip("/") == GEMINI_OFFICIAL_BASE_URL:
        return ""
    return base_url.strip()


def get_gemini_config_by_name(config_name: str) -> tuple[str, str, str, ThinkingLevel | None]:
    gconfig = provider_manager("gemini").get_config(config_name)
    api_keys = gconfig.get_config("api_key").data
    if not api_keys or not str(api_keys[0]).strip():
        raise ValueError(t("log.ai.gscore_gemini_config_name_api", config_name=config_name))
    base_url, api_key, model_name, model_effort = (
        gconfig.get_config("base_url").data,
        str(api_keys[0]).strip(),
        gconfig.get_config("model_name").data,
        to_thinking_level(gconfig.get_config("model_effort").data),
    )
    logger.info(
        t(
            "log.ai.gscore_gemini_config_name_url",
            model_name=model_name,
            base_url=base_url,
            p0=api_key[-4:],
        )
    )
    return base_url, api_key, model_name, model_effort


def get_gemini_model_by_name(config_name: str, *, fast: bool = False) -> "GoogleModel":
    """根据配置名获取 Gemini(Google GenAI 原生格式)模型。

    依赖可选包 ``google-genai``——延迟导入，缺依赖时只有 gemini 配置报错，
    不拖垮 openai/anthropic 的模型构建（本模块被 ai_core 启动链路 import）。

    Args:
        config_name: 配置文件名（不含扩展名）

    Raises:
        RuntimeError: 未安装 ``google-genai`` 依赖。
    """
    try:
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider
    except ImportError as e:
        raise RuntimeError(
            t(
                "log.ai.gscore_gemini_google_genai_pip",
                e=e,
            )
        ) from e

    base_url, api_key, model_name, configured_effort = get_gemini_config_by_name(config_name)
    settings_effort = None if fast else configured_effort

    # 官方地址走 SDK 内建默认;仅中转地址才显式传 base_url。
    # http_client 走共享池并携带 §23 墙钟超时（genai 内部转发给 httpx，评审修复 E13）
    normalized = normalize_gemini_base_url(base_url)
    _http_client = _shared_model_http_client("gemini", config_name)
    provider = (
        GoogleProvider(api_key=api_key, base_url=normalized, http_client=_http_client)
        if normalized
        else GoogleProvider(api_key=api_key, http_client=_http_client)
    )
    return GoogleModel(
        model_name=model_name,
        provider=provider,
        profile=user_thinking_profile(configured_effort),
        settings=_with_thinking(ModelSettings(), settings_effort),
    )


def get_high_level_config_name() -> str:
    """获取高级任务配置文件名（provider++name 格式）"""
    return ai_config.get_config("high_level_provider_config_name").data


def get_low_level_config_name() -> str:
    """获取低级任务配置文件名（provider++name 格式）"""
    return ai_config.get_config("low_level_provider_config_name").data


# 任务覆盖里「改用某档位」的记号；其余非空值视为 provider++name 配置全名
TASK_ROUTE_LEVEL_REFS: dict[str, TaskLevel] = {"@high": "high", "@low": "low"}
TaskRouteSource = Literal["default", "level", "config"]


@dataclass(frozen=True)
class TaskModelOverride:
    primary: str
    secondary: str


@dataclass(frozen=True)
class TaskModelRoute:
    """某任务实际生效的主/备配置。source=default 表示未覆盖，走调用方档位。"""

    primary: str
    secondary: str
    source: TaskRouteSource
    level: TaskLevel | None
    inherited_from: str | None = None


def _level_primary(task_level: TaskLevel) -> str:
    return get_high_level_config_name() if task_level == "high" else get_low_level_config_name()


def _level_secondary(task_level: TaskLevel) -> str:
    raw: object = ai_config.get_config(f"{task_level}_level_2nd_provider_config_name").data
    return raw if isinstance(raw, str) else ""


def normalize_provider_config_name(full_name: str) -> str:
    """规范化为 ``provider++config_name``；未知 provider 或空名时返回空串。"""
    if not full_name:
        return ""
    if PROVIDER_CONFIG_SEPARATOR in full_name:
        provider = full_name.split(PROVIDER_CONFIG_SEPARATOR, 1)[0]
        if provider not in SUPPORTED_PROVIDERS:
            return ""
    provider, config_name = parse_provider_config_name(full_name)
    if not config_name:
        return ""
    return format_provider_config_name(provider, config_name)


def provider_config_exists(full_name: str) -> bool:
    """配置全名能否落到磁盘上真实存在的配置文件（未知 provider 或非法名视为不存在）。"""
    if not full_name or full_name in TASK_ROUTE_LEVEL_REFS:
        return False
    if PROVIDER_CONFIG_SEPARATOR in full_name:
        provider = full_name.split(PROVIDER_CONFIG_SEPARATOR, 1)[0]
        if provider not in SUPPORTED_PROVIDERS:
            return False
    provider, config_name = parse_provider_config_name(full_name)
    if not config_name or not is_safe_filename(config_name):
        return False
    return provider_manager(provider).exists(config_name)


def get_task_model_overrides() -> dict[str, TaskModelOverride]:
    """读取全部任务覆盖；结构不合法的条目直接忽略（该字段允许用户手改 JSON）。"""
    raw: object = ai_config.get_config("task_model_overrides").data
    out: dict[str, TaskModelOverride] = {}
    if not isinstance(raw, dict):
        return out
    for key, entry in raw.items():
        if not isinstance(key, str) or not isinstance(entry, list) or not entry:
            continue
        primary = entry[0] if isinstance(entry[0], str) else ""
        secondary = entry[1] if len(entry) > 1 and isinstance(entry[1], str) else ""
        if primary.strip():
            out[key] = TaskModelOverride(primary.strip(), secondary.strip())
    return out


def save_task_model_overrides(overrides: dict[str, TaskModelOverride]) -> bool:
    """整表写回；仅「改档位」的条目不保存备用配置（备用随档位走）。"""
    data: dict[str, list[str]] = {}
    for key, ov in overrides.items():
        secondary = "" if ov.primary in TASK_ROUTE_LEVEL_REFS else ov.secondary
        data[key] = [ov.primary, secondary]
    return ai_config.set_config("task_model_overrides", data)


def resolve_task_route(task_level: TaskLevel, task_kind: str | None = None) -> TaskModelRoute:
    """解析任务实际使用的主/备配置（支持 ``parent:scope`` 层级回退）。

    覆盖指向的配置文件已被删除时依次回退父级覆盖、调用方档位，避免单点失效。
    """
    if task_kind:
        overrides = get_task_model_overrides()
        candidates: list[tuple[str, str | None]] = [(task_kind, None)]
        parent = parent_task_kind(task_kind)
        if parent is not None:
            candidates.append((parent, parent))
        for candidate, inherited_from in candidates:
            if candidate not in overrides:
                continue
            ov = overrides[candidate]
            if ov.primary in TASK_ROUTE_LEVEL_REFS:
                lvl = TASK_ROUTE_LEVEL_REFS[ov.primary]
                return TaskModelRoute(_level_primary(lvl), _level_secondary(lvl), "level", lvl, inherited_from)
            if provider_config_exists(ov.primary):
                secondary = ov.secondary if provider_config_exists(ov.secondary) else ""
                return TaskModelRoute(ov.primary, secondary, "config", None, inherited_from)
    return TaskModelRoute(_level_primary(task_level), _level_secondary(task_level), "default", task_level, None)


def get_config_name_for_task(task_level: TaskLevel, task_kind: str | None = None) -> str:
    """获取任务当前激活的配置全名（provider++name 格式），已计入按任务覆盖"""
    return resolve_task_route(task_level, task_kind).primary


def get_2nd_config_name_for_task(task_level: TaskLevel, task_kind: str | None = None) -> str:
    """获取任务的备用（兜底）配置全名（provider++name 格式），未配置返回空串"""
    return resolve_task_route(task_level, task_kind).secondary


def get_max_concurrency_for_config(full_name: str) -> int:
    """读取配置文件的允许并发数，缺失/异常回退 1，并 clamp 到 [1, 12]"""
    try:
        provider, config_name = parse_provider_config_name(full_name)
        cfg = _get_provider_string_config(provider, config_name)
        val = int(cfg.get_config("max_concurrency").data)
    except Exception:
        return 1
    return max(1, min(12, val))


def _get_provider_string_config(provider: str, config_name: str) -> StringConfig:
    """按 provider 取对应配置文件的 StringConfig（三 provider 唯一分派点）"""
    return provider_manager(provider).get_config(config_name)


class _ModelBuilder(Protocol):
    def __call__(self, config_name: str, *, fast: bool = False) -> AnyModel: ...


_MODEL_BUILDERS: dict[ProviderType, _ModelBuilder] = {
    "openai": get_openai_model_by_name,
    "anthropic": get_anthropic_chat_model_by_name,
    "gemini": get_gemini_model_by_name,
}


def _build_model_by_full_name(full_name: str, *, fast: bool) -> AnyModel:
    provider, config_name = parse_provider_config_name(full_name)
    if not is_provider_type(provider):
        raise ValueError(t("不支持的 provider 类型: {provider}", provider=provider))
    return _MODEL_BUILDERS[provider](config_name, fast=fast)


def get_model_by_full_name(full_name: str) -> AnyModel:
    """按 provider++name 全名直接构建模型（供 provider 路由按需构建主/备模型）"""
    return _build_model_by_full_name(full_name, fast=False)


def get_fast_model_by_full_name(full_name: str) -> AnyModel:
    """按全名构建**无思考**快模型：不注入 reasoning effort / thinking 档位。

    前置路由、后台分诊与两阶段摄入统一走这条通道，避免用户为聊天配置的
    ``model_effort="max"`` 让每次轻量调用吐出数千推理 Token。
    """
    return _build_model_by_full_name(full_name, fast=True)


def get_fast_model_for_task(task_level: TaskLevel = "low", task_kind: str | None = None) -> AnyModel:
    """按任务级别取无思考快模型（默认 low）。未配置时抛 ValueError。"""
    full_name = get_config_name_for_task(task_level, task_kind)
    if not full_name:
        raise ValueError(t("log.ai.gscore_ai_config_model_file"))
    return get_fast_model_by_full_name(full_name)


def get_model_config_by_full_name(full_name: str) -> StringConfig:
    """按 provider++name 取对应配置文件（failover 后读实际模型的 model_support）。"""
    provider, config_name = parse_provider_config_name(full_name)
    return _get_provider_string_config(provider, config_name)


def get_model_config_for_task(task_level: TaskLevel, task_kind: str | None = None) -> StringConfig:
    full_name = get_config_name_for_task(task_level, task_kind)
    if not full_name:
        raise ValueError(t("log.ai.gscore_ai_config_model_file"))

    provider, config_name = parse_provider_config_name(full_name)
    return _get_provider_string_config(provider, config_name)


def get_provider_for_task(task_level: TaskLevel, task_kind: str | None = None) -> str:
    """获取指定任务级别当前激活配置的 provider 类型（"openai"/"anthropic"/"gemini"）。

    未设置配置时返回空串——调用方（如多模态分支）应视为"非 gemini"处理。
    """
    full_name = get_config_name_for_task(task_level, task_kind)
    if not full_name:
        return ""
    provider, _ = parse_provider_config_name(full_name)
    return provider


def get_model_for_task(
    task_level: TaskLevel,
    task_kind: str | None = None,
) -> AnyModel:
    """根据任务级别获取对应的模型

    Args:
        task_level: 任务级别，"high"表示高级任务，"low"表示低级任务
        task_kind: 任务类型 ID（见 task_kinds），用于套用按任务覆盖

    Returns:
        对应的ChatModel实例
    """
    full_name = get_config_name_for_task(task_level, task_kind)

    if not full_name:
        raise ValueError(t("log.ai.gscore_ai_config_model_file"))

    return get_model_by_full_name(full_name)


def get_model_fingerprint_for_task(task_level: TaskLevel, task_kind: str | None = None) -> str:
    """激活配置的内容指纹（含 request_method）。

    全名相同但配置文件内字段被原地改动（如把 request_method 从 chat_completions 改为
    responses、或改 base_url/model_name）时指纹随之变化，供存活会话据此热替换模型。
    无配置时返回空串；取不到配置字典时退回全名，至少保住"切到别的配置文件"这条路径。
    """
    full_name = get_config_name_for_task(task_level, task_kind)
    if not full_name:
        return ""

    provider, config_name = parse_provider_config_name(full_name)
    config_dict = provider_manager(provider).get_config_dict(config_name)

    key = (provider, config_name)
    gen = _MODEL_CLIENT_GENERATIONS[key] if key in _MODEL_CLIENT_GENERATIONS else 0
    if not config_dict:
        return f"{full_name}:gen={gen}" if gen else full_name

    payload = json.dumps(config_dict, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{digest}:gen={gen}" if gen else digest
