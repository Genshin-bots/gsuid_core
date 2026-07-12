"""AI模块共享适配器

提供LLM和嵌入的共享适配器，供mem、gs_agent等模块复用。

配置名称格式: "provider++config_name" (例如 "openai++MiniMAX")
- provider: "openai" 或 "anthropic"
- config_name: 配置文件名称
- 分隔符: "++"
- 兼容旧格式: 不含 "++" 的名称默认按 "openai" provider 处理
"""

import json
import hashlib
from typing import Union, Literal, final
from collections.abc import AsyncIterator
from typing_extensions import override

from openai.types.chat import ChatCompletionChunk
from pydantic_ai.usage import RequestUsage
from pydantic_ai.settings import ModelSettings, ThinkingLevel
from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIResponsesModel,
    OpenAIStreamedResponse,
    OpenAIChatModelSettings,
)
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.anthropic import AnthropicProvider

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .ai_config import ai_config
from .openai_config import get_openai_config, get_openai_config_dict
from .anthropic_config import get_anthropic_config, get_anthropic_config_dict

# 配置名称分隔符
PROVIDER_CONFIG_SEPARATOR = "++"

# OpenAI 请求方式：chat_completions 走 /v1/chat/completions，responses 走 /v1/responses。
RequestMethod = Literal["chat_completions", "responses"]

# OpenAI 模型对象（两种端点对 gs_agent 接口完全一致，仅底层请求路径不同）。
OpenAIModel = Union[OpenAIChatModel, OpenAIResponsesModel]


def parse_provider_config_name(full_name: str) -> tuple[str, str]:
    """
    解析 "provider++config_name" 格式的配置名称。

    Args:
        full_name: 完整配置名称，格式为 "provider++config_name"
                   兼容旧格式：不含 "++" 的名称默认按 "openai" provider 处理

    Returns:
        (provider, config_name) 元组
        - provider: "openai" 或 "anthropic"
        - config_name: 实际配置文件名称

    Examples:
        >>> parse_provider_config_name("openai++MiniMAX")
        ('openai', 'MiniMAX')
        >>> parse_provider_config_name("anthropic++Claude")
        ('anthropic', 'Claude')
        >>> parse_provider_config_name("MiniMAX")  # 兼容旧格式
        ('openai', 'MiniMAX')
    """
    if PROVIDER_CONFIG_SEPARATOR in full_name:
        provider, config_name = full_name.split(PROVIDER_CONFIG_SEPARATOR, 1)
        if provider not in ("openai", "anthropic"):
            raise ValueError(
                t(
                    "🧠 [GsCore][AI] 不支持的 provider 类型: '{provider}'，仅支持 'openai' 或 'anthropic'",
                    provider=provider,
                )
            )
        return provider, config_name

    # 兼容旧格式：不含 "++" 的名称默认按 openai 处理
    return "openai", full_name


def format_provider_config_name(provider: str, config_name: str) -> str:
    """
    将 provider 和 config_name 格式化为 "provider++config_name" 格式。

    Args:
        provider: "openai" 或 "anthropic"
        config_name: 配置文件名称

    Returns:
        格式化后的完整配置名称
    """
    return f"{provider}{PROVIDER_CONFIG_SEPARATOR}{config_name}"


# 配置项 model_effort 字符串 → pydantic_ai ThinkingLevel 的映射。
# ThinkingLevel = bool | Literal["minimal","low","medium","high","xhigh"]，
# "enable"/"disable" 必须映射为 True/False，原样透传会在 provider 的
# REASONING_EFFORT/THINKING_BUDGET 映射表中触发 KeyError。
THINKING_LEVEL_MAP: dict[str, ThinkingLevel] = {
    "enable": True,
    "disable": False,
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
}


def to_thinking_level(value: str) -> ThinkingLevel:
    """将配置中的 model_effort 字符串转换为 pydantic_ai 的 ThinkingLevel"""
    if value not in THINKING_LEVEL_MAP:
        logger.warning(t("🧠 [GsCore] 未知的 model_effort 配置: {value}, 已回退为 enable", value=repr(value)))
        return True
    return THINKING_LEVEL_MAP[value]


def to_request_method(value: str) -> RequestMethod:
    """将配置中的 request_method 字符串归一为 RequestMethod，未知值回退 chat_completions。"""
    if value == "responses":
        return "responses"
    if value != "chat_completions":
        logger.warning(
            t("🧠 [GsCore] 未知的 request_method 配置: {value}, 已回退为 chat_completions", value=repr(value))
        )
    return "chat_completions"


# 每 chunk 携带「累计」usage 的已知网关（vLLM/SGLang 系）：pydantic_ai 默认
# 逐 chunk 累加会令 token 统计膨胀约 chunk 数倍，必须改为「取最后一个值」。
_CUMULATIVE_USAGE_URL_KEYWORDS = ("siliconflow",)

# 运行时探测到累计语义的 base_url（进程内记忆），后续 auto 建模直接继承。
_detected_cumulative_urls: set[str] = set()


def _resolve_continuous_usage(base_url: str, mode: str) -> bool:
    """根据配置与 base_url 判定流式 usage 是否为累计语义（cumulative）。

    True → 传给 pydantic_ai 的 openai_continuous_usage_stats，使其对每个
    chunk 的 usage 取「替换」而非「累加」，避免 token 统计成倍膨胀。
    """
    if mode == "cumulative":
        return True
    if mode == "incremental":
        return False
    if mode != "auto":
        logger.warning(t("🧠 [GsCore] 未知的 usage_stats_mode 配置: {mode}, 已回退为 auto", mode=repr(mode)))
    if base_url in _detected_cumulative_urls:
        return True
    return any(kw in base_url for kw in _CUMULATIVE_USAGE_URL_KEYWORDS)


class _AutoUsageStreamedResponse(OpenAIStreamedResponse):
    """流式 usage 语义在线探测（auto 模式专用）。

    标准 OpenAI 语义下整条流只有最后一个 chunk 携带 usage；vLLM 系累计语义
    则每个 chunk 都携带「运行总量」（prompt_tokens 恒定、completion_tokens
    单调不减）。探测分两层：

    1. 即时翻转：观测到第 2 个带 usage 的 chunk 且符合累计特征时，原地翻转
       openai_continuous_usage_stats。pydantic_ai 的 _get_event_iterator 在流
       循环内逐 chunk 读取该设置，翻转后改为「替换」语义，后续累计值会覆盖
       之前误加的和。
    2. 终局对账：全程持续校验每个 usage chunk 的累计特征（prompt 恒定 +
       completion 单调不减），并同时维护一份「增量语义影子和」。流结束时：
       证据链完整 → usage 直接定格为最后一个 chunk 的精确累计值；中途翻转
       后证据链断裂（罕见的非标准语义）→ 用影子和整体回退到增量解释，
       误判也不丢数。

    _validate_response 是 pydantic_ai 文档标明供子类覆写的 chunk 校验钩子
    （官方 openrouter 模型同样以 async generator 形式覆写）。
    """

    def _enable_replace_semantics(self) -> None:
        # 拷贝后再改: merge 出的 settings 可能被别处持有,
        # 避免把本次判定结果泄漏到其他请求对象上
        updated = (
            OpenAIChatModelSettings(**self._model_settings)
            if self._model_settings is not None
            else OpenAIChatModelSettings()
        )
        updated["openai_continuous_usage_stats"] = True
        self._model_settings: OpenAIChatModelSettings | None = updated

    @override
    async def _validate_response(self) -> AsyncIterator[ChatCompletionChunk]:
        usage_seen = 0
        first_prompt_tokens = 0
        prev_completion_tokens = -1
        monotone = True  # 累计特征是否始终成立
        last_usage_chunk: ChatCompletionChunk | None = None
        shadow_sum = RequestUsage()  # 按增量语义累加的影子和, 供误判回退

        # 白名单/已探测网关直接预置「替换」语义。只改响应对象、不影响请求体
        # （请求此刻已发出）, 探测校验仍全程进行, 预置错了也会被终局对账纠正
        flipped = self._provider_url in _detected_cumulative_urls or any(
            kw in self._provider_url for kw in _CUMULATIVE_USAGE_URL_KEYWORDS
        )
        if flipped:
            self._enable_replace_semantics()

        async for chunk in self._response:
            if chunk.usage is not None:
                usage_seen += 1
                last_usage_chunk = chunk
                shadow_sum += self._map_usage(chunk)
                prompt_tokens = chunk.usage.prompt_tokens or 0
                completion_tokens = chunk.usage.completion_tokens or 0
                if usage_seen == 1:
                    first_prompt_tokens = prompt_tokens
                else:
                    monotone = monotone and (
                        first_prompt_tokens > 0
                        and prompt_tokens == first_prompt_tokens
                        and completion_tokens >= prev_completion_tokens
                    )
                    if monotone and not flipped:
                        flipped = True
                        self._enable_replace_semantics()
                prev_completion_tokens = completion_tokens
            yield chunk

        # 终局对账（流被中途取消时不执行, 维持 pydantic_ai 的 best-effort 语义）
        if not flipped or last_usage_chunk is None:
            return
        if monotone:
            # 证据链完整: 定格为最后一个累计值, 翻转前误加的部分一并修正
            self._usage: RequestUsage = self._map_usage(last_usage_chunk)
            # 仅 1 个 usage chunk 无法证明累计语义(预置命中的标准网关即如此), 不入registry
            if usage_seen >= 2 and self._provider_url not in _detected_cumulative_urls:
                _detected_cumulative_urls.add(self._provider_url)
                logger.warning(
                    t(
                        "🧠 [GsCore] 探测并确认网关 {provider_url} 流式 usage 为累计语义 "
                        "(全流 {usage_seen} 个 usage chunk 均符合 prompt 恒定 + completion 单调), "
                        "已按「取最后值」结算防止 token 统计膨胀。"
                        "可在该 OpenAI 配置中将 usage_stats_mode 显式设为 cumulative 固化此结果",
                        provider_url=self._provider_url,
                        usage_seen=usage_seen,
                    )
                )
        else:
            # 翻转后证据链断裂(非累计语义): 用影子和回退增量解释, 误判不丢数
            self._usage = shadow_sum
            logger.warning(
                t(
                    "🧠 [GsCore] 网关 {provider_url} 出现多个 usage chunk 但不符合累计特征, "
                    "已按增量语义回退结算 (共 {usage_seen} 个 usage chunk)。"
                    "若统计仍异常, 请显式设置该配置的 usage_stats_mode",
                    provider_url=self._provider_url,
                    usage_seen=usage_seen,
                )
            )


@final
class AutoUsageOpenAIChatModel(OpenAIChatModel):
    """auto 模式下使用的 ChatModel：流式响应走 usage 语义在线探测。"""

    @property
    @override
    def _streamed_response_cls(self) -> type[OpenAIStreamedResponse]:
        return _AutoUsageStreamedResponse


def get_openai_config_by_name(config_name: str) -> tuple[str, str, str, ThinkingLevel, RequestMethod, bool, str]:
    oconfig = get_openai_config(config_name)
    base_url, api_key, model_name, model_effort, request_method = (
        oconfig.get_config("base_url").data,
        oconfig.get_config("api_key").data[0],
        oconfig.get_config("model_name").data,
        to_thinking_level(oconfig.get_config("model_effort").data),
        to_request_method(oconfig.get_config("request_method").data),
    )
    # 旧配置文件缺该 key 时 get_config 会自动从模板补默认值 "auto", 不会抛异常
    usage_stats_mode = str(oconfig.get_config("usage_stats_mode").data)
    continuous_usage = _resolve_continuous_usage(base_url, usage_stats_mode)
    logger.info(
        t(
            "🧠 [GsCore] 加载 OpenAI 配置: Name: {model_name}, URL: {base_url}, "
            "Key: ...{key_tail}, 请求方式: {request_method}{usage_suffix}",
            model_name=model_name,
            base_url=base_url,
            key_tail=api_key[-4:],
            request_method=request_method,
            usage_suffix=(", 流式usage: cumulative" if continuous_usage else ""),
        )
    )
    return base_url, api_key, model_name, model_effort, request_method, continuous_usage, usage_stats_mode


def get_anthropic_config_by_name(config_name: str) -> tuple[str, str, str, ThinkingLevel]:
    aconfig = get_anthropic_config(config_name)
    base_url, api_key, model_name, model_effort = (
        aconfig.get_config("base_url").data,
        aconfig.get_config("api_key").data[0],
        aconfig.get_config("model_name").data,
        to_thinking_level(aconfig.get_config("model_effort").data),
    )
    logger.info(
        t(
            "🧠 [GsCore] 加载 Anthropic 配置: Name: {model_name}, URL: {base_url}, Key: ...{p0}",
            model_name=model_name,
            base_url=base_url,
            p0=api_key[-4:],
        )
    )
    return base_url, api_key, model_name, model_effort


def get_openai_model_by_name(config_name: str) -> OpenAIModel:
    """根据配置名获取 OpenAI 模型，按 request_method 选择端点。

    chat_completions → OpenAIChatModel(/v1/chat/completions)；
    responses → OpenAIResponsesModel(/v1/responses)。两者均接受同一 OpenAIProvider，
    且对 gs_agent 暴露相同接口（client/model_name/system/profile/request_stream）。

    Args:
        config_name: 配置文件名（不含扩展名）
    """
    base_url, api_key, model_name, model_effort, request_method, continuous_usage, usage_stats_mode = (
        get_openai_config_by_name(config_name)
    )

    provider = OpenAIProvider(api_key=api_key, base_url=base_url)

    if request_method == "responses":
        return OpenAIResponsesModel(
            model_name=model_name,
            provider=provider,
            settings=ModelSettings(thinking=model_effort),
        )

    # cumulative 语义网关须取「最后累计值」而非逐 chunk 累加, 否则统计膨胀数十倍;
    # auto 模式用探测子类兜底白名单外的网关（见 _AutoUsageStreamedResponse）
    model_cls = OpenAIChatModel if usage_stats_mode in ("incremental", "cumulative") else AutoUsageOpenAIChatModel
    return model_cls(
        model_name=model_name,
        provider=provider,
        # 请求参数 stream_options.continuous_usage_stats 仅在部署者显式声明
        # cumulative 时发送; auto 的白名单/探测只作用于响应侧, 不改请求体
        settings=OpenAIChatModelSettings(
            thinking=model_effort,
            openai_continuous_usage_stats=usage_stats_mode == "cumulative",
        ),
    )


def get_anthropic_chat_model_by_name(config_name: str) -> "AnthropicModel":
    """根据配置名获取Anthropic Chat Model
    Args:
        config_name: 配置文件名（不含扩展名）
    """
    base_url, api_key, model_name, model_effort = get_anthropic_config_by_name(config_name)

    logger.info(
        t(
            "🧠 [GsCore] 加载 Anthropic 模型: Name: {model_name}, URL: {base_url}, Key: ...{p0}",
            model_name=model_name,
            base_url=base_url,
            p0=api_key[-4:],
        )
    )

    return AnthropicModel(
        model_name=model_name,
        provider=AnthropicProvider(
            api_key=api_key,
            base_url=base_url,
        ),
        settings=ModelSettings(thinking=model_effort),
    )


def get_high_level_config_name() -> str:
    """获取高级任务配置文件名（provider++name 格式）"""
    return ai_config.get_config("high_level_provider_config_name").data


def get_low_level_config_name() -> str:
    """获取低级任务配置文件名（provider++name 格式）"""
    return ai_config.get_config("low_level_provider_config_name").data


def get_config_name_for_task(task_level: Literal["high", "low"]) -> str:
    """获取指定任务级别当前激活的配置全名（provider++name 格式）"""
    return get_high_level_config_name() if task_level == "high" else get_low_level_config_name()


def get_2nd_config_name_for_task(task_level: Literal["high", "low"]) -> str:
    """获取指定任务级别的备用（兜底）配置全名（provider++name 格式），未配置返回空串"""
    key = f"{task_level}_level_2nd_provider_config_name"
    try:
        return ai_config.get_config(key).data
    except Exception:
        return ""


def get_max_concurrency_for_config(full_name: str) -> int:
    """读取配置文件的允许并发数，缺失/异常回退 1，并 clamp 到 [1, 10]"""
    try:
        provider, config_name = parse_provider_config_name(full_name)
        cfg = get_openai_config(config_name) if provider == "openai" else get_anthropic_config(config_name)
        val = int(cfg.get_config("max_concurrency").data)
    except Exception:
        return 1
    return max(1, min(10, val))


def get_model_by_full_name(full_name: str) -> Union[OpenAIChatModel, OpenAIResponsesModel, AnthropicModel]:
    """按 provider++name 全名直接构建模型（供 provider 路由按需构建主/备模型）"""
    provider, config_name = parse_provider_config_name(full_name)
    if provider == "openai":
        return get_openai_model_by_name(config_name)
    return get_anthropic_chat_model_by_name(config_name)


def get_model_config_for_task(task_level: Literal["high", "low"]) -> StringConfig:
    full_name = get_config_name_for_task(task_level)
    if not full_name:
        raise ValueError(t("🧠 [GsCore][AI] 未设置AI模型配置文件，请先前往网页控制台设置配置文件！"))

    provider, config_name = parse_provider_config_name(full_name)

    if provider == "openai":
        return get_openai_config(config_name)
    else:
        return get_anthropic_config(config_name)


def get_model_for_task(
    task_level: Literal["high", "low"],
) -> Union[OpenAIChatModel, OpenAIResponsesModel, AnthropicModel]:
    """根据任务级别获取对应的模型

    Args:
        task_level: 任务级别，"high"表示高级任务，"low"表示低级任务

    Returns:
        对应的ChatModel实例
    """
    full_name = get_config_name_for_task(task_level)

    if not full_name:
        raise ValueError(t("🧠 [GsCore][AI] 未设置AI模型配置文件，请先前往网页控制台设置配置文件！"))

    provider, config_name = parse_provider_config_name(full_name)

    if provider == "openai":
        return get_openai_model_by_name(config_name)
    else:
        return get_anthropic_chat_model_by_name(config_name)


def get_model_fingerprint_for_task(task_level: Literal["high", "low"]) -> str:
    """激活配置的内容指纹（含 request_method）。

    全名相同但配置文件内字段被原地改动（如把 request_method 从 chat_completions 改为
    responses、或改 base_url/model_name）时指纹随之变化，供存活会话据此热替换模型。
    无配置时返回空串；取不到配置字典时退回全名，至少保住"切到别的配置文件"这条路径。
    """
    full_name = get_config_name_for_task(task_level)
    if not full_name:
        return ""

    provider, config_name = parse_provider_config_name(full_name)
    if provider == "openai":
        config_dict = get_openai_config_dict(config_name)
    else:
        config_dict = get_anthropic_config_dict(config_name)

    if not isinstance(config_dict, dict):
        return full_name

    payload = json.dumps(config_dict, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
