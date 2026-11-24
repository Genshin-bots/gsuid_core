"""
Provider 配置管理器模块

统一管理 OpenAI / Anthropic / Gemini 配置模板与多配置文件读取、写入、热切换。
"""

from typing import Literal, TypeGuard

from gsuid_core.i18n import t
from gsuid_core.ai_core.resource import (
    GEMINI_CONFIGS_PATH,
    OPENAI_CONFIGS_PATH,
    ANTHROPIC_CONFIGS_PATH,
)
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsFloatConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig, ConfigSetManager

ProviderType = Literal["openai", "anthropic", "gemini"]

OPENAI_CONFIG_TEMPLATE: dict[str, GSC] = {
    "base_url": GsStrConfig(
        title="OpenAI API基础URL",
        desc="指定OpenAI API的基础URL, 注意一般是以 /v1 结尾",
        data="https://api.openai.com/v1",
        options=[
            # 默认及原版配置
            "https://api.openai.com/v1",
            "https://api.bltcy.ai/v1",
            "https://api.minimaxi.com/v1",
            # 2026 国内主流大模型厂商官方接口
            "https://api.xiaomimimo.com/v1",  # 小米 MiMo
            "https://dashscope.aliyuncs.com/compatible-mode/v1",  # 阿里通义千问 (DashScope)
            "https://api.deepseek.com",  # DeepSeek 深度求索
            "https://open.bigmodel.cn/api/paas/v4",  # 智谱AI (GLM)
            "https://api.moonshot.cn/v1",  # 月之暗面 (Kimi)
            "https://api.lingyiwanwu.com/v1",  # 零一万物 (Yi)
            "https://ark.cn-beijing.volces.com/api/v3",  # 字节跳动火山引擎 (豆包)
            # 国内外知名API聚合/中转平台
            "https://api.siliconflow.cn/v1",  # 硅基流动 (SiliconFlow)
            "https://openrouter.ai/api/v1",  # OpenRouter
            "https://api.aimlapi.com/v1",  # AI/ML API
            # 国外其他主流大模型厂商
            "https://api.x.ai/v1",  # xAI (Grok)
            "https://api.anthropic.com/v1",  # Anthropic (Claude)
            # 开发者本地常用的 OneAPI / NewAPI 中转系统默认地址
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
    ),
    "api_key": GsListStrConfig(
        title="OpenAI API密钥",
        desc="指定OpenAI API的密钥, 注意是以 sk- 开头, 不要泄露, 支持添加多个",
        data=["sk-"],
        options=["sk-"],
    ),
    "model_name": GsStrConfig(
        title="调用模型名称",
        desc="指定OpenAI API的模型, 该模型将会用于处理大部分任务",
        data="gpt-4o-mini",
        options=[
            # 小米 (MiMo)
            "mimo-v2.6-pro",
            # OpenAI (2026 最新主推)
            "gpt-5.5",
            "gpt-5",
            "o4",
            "o4-mini",
            "o3",
            "gpt-4o",
            "gpt-4o-mini",  # 兼容保留
            # xAI (Grok 4 最前沿家族)
            "grok-4.3",
            "grok-4.20",
            "grok-4.20-multi-agent",
            "grok-4.1-fast",
            # Anthropic (Claude 4.7 / 4.6 家族)
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5",
            # 阿里通义千问 (Qwen 3.6 最新矩阵)
            "qwen3.6-plus-preview",
            "qwen3.6-35b-a3b",
            "qwen3.6-flash",
            "qwen3.5-plus",
            "qwen-turbo",
            # DeepSeek
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-reasoner",
            "deepseek-chat",
            # 智谱 AI (GLM-5 家族)
            "glm-5.1",
            "glm-5-turbo",
            "glm-4-flash",
            # 月之暗面 Kimi
            "kimi-k2.6",
            "moonshot-v1-auto",
            # Google (Gemini)
            "gemini-3.1-pro",
            "gemini-3.1-flash",
            "gemini-2.5-flash",
            # 零一万物 (Yi)
            "yi-lightning",
            # MiniMax
            "MiniMax-M2.7",
            "MiniMax-M2.7-highspeed",
            "MiniMax-M3",
            "MiniMax-M3.1-Flash",
        ],
    ),
    "model_support": GsListStrConfig(
        title="模型支持能力",
        desc="显式指定模型支持能力，如是否能看图、能处理文件/音频/视频等",
        data=["text"],
        options=["text", "image", "audio", "video"],
    ),
    "model_effort": GsStrConfig(
        title="模型性能",
        desc=(
            "思考档位。adaptive 不传 reasoning_effort。"
            "enable 发送 medium，disable 发送 none。"
            "minimal / low / medium / high / xhigh 按原值发送。"
            "max 发送 reasoning_effort=max。"
            "模型名不在思考名单里也发送。上游拒绝时，错误原样返回"
        ),
        data="enable",
        options=["adaptive", "enable", "disable", "minimal", "low", "medium", "high", "xhigh", "max"],
    ),
    "max_concurrency": GsIntConfig(
        title="允许并发数",
        desc=(
            "该配置允许同时进行的LLM请求数(1~12)。并发占满后，新请求自动切换到"
            "AI配置中的备用(2nd)配置，实现多 provider 同时工作与负载均衡"
        ),
        data=1,
        max_value=12,
        options=[1, 2, 3, 4, 5, 6, 8, 10, 11, 12],
    ),
    "usage_stats_mode": GsStrConfig(
        title="流式Usage统计模式",
        desc=(
            "网关在流式响应中返回usage的方式: incremental(仅最后一个chunk带usage, OpenAI/Moonshot等标准行为) 或 "
            "cumulative(每个chunk带累计usage, 如SiliconFlow/vLLM网关, 若按incremental累加会导致token统计膨胀数十倍)。"
            "auto=已知网关按base_url识别+未知网关流式响应在线探测(推荐)"
        ),
        data="auto",
        options=["auto", "incremental", "cumulative"],
    ),
    "request_method": GsStrConfig(
        title="API请求方式",
        desc=(
            "OpenAI 接口风格: chat_completions(/v1/chat/completions, 通用兼容) 或 "
            "responses(/v1/responses, 仅 OpenAI 官方及实现该端点的网关支持)"
        ),
        data="chat_completions",
        options=["chat_completions", "responses"],
    ),
    "remote_web_search": GsStrConfig(
        title="远端 Web Search",
        desc=(
            "默认开启。请求方式为 responses 时用上游内置 web_search，"
            "不再把本地 web_search_tool 发给模型；"
            "chat_completions 无论本开关开或关都走本地 Tavily/Jina/Exa/AnySearch。"
            "中转的 responses 若不支持 hosted 工具会 400，那时再关掉即可"
        ),
        data="on",
        options=["off", "on"],
    ),
    "forward_end_user_id": GsStrConfig(
        title="透传终端用户标识",
        desc=(
            "是否在请求中携带 OpenAI 标准的 user 字段(终端用户标识), 供上游网关按调用方聚合用量、"
            "日志与滥用监控: off=不携带(默认); hashed=携带加盐摘要, 不泄露原始标识; "
            "raw=携带原始标识(仅建议对自建网关开启)"
        ),
        data="off",
        options=["off", "hashed", "raw"],
    ),
    "end_user_id_salt": GsStrConfig(
        title="终端用户标识盐值",
        desc=(
            "hashed 模式计算摘要用的盐值。留空则为无密钥摘要, 标识空间小(纯数字账号)时可被枚举"
            "反查, 只起混淆作用; 需要抗反查请填一段随机字符串"
        ),
        data="",
        secret=True,
    ),
    "send_back_thinking": GsStrConfig(
        title="思考回传",
        desc=(
            "多轮对话时是否把历史里的模型思考内容回传给模型(仅 chat_completions 生效): "
            "auto=pydantic_ai 默认(思考以 <think></think> 标签或厂商字段回发, 部分中转网关会因此报错); "
            "off=完全不回传思考内容(遇网关兼容问题时选它, 对话质量几乎无影响)"
        ),
        data="auto",
        options=["auto", "off"],
    ),
    "video_input_format": GsStrConfig(
        title="视频输入格式",
        desc=(
            "OpenAI 兼容接口的视频输入格式: file(兼容 LiteLLM / Gemini 中转) 或 "
            "video_url(适配 MiMo / 阿里百炼 / OpenRouter 等全模态模型, 仅 chat_completions 生效)"
        ),
        data="video_url",
        options=["video_url", "file"],
    ),
    "video_fps": GsFloatConfig(
        title="视频采样帧率",
        desc="video_url 模式下的抽样帧率 (FPS)。大于 0 时写入同级 fps 字段 (如 2 或 0.5), 0 表示不发送",
        data=0.0,
    ),
    "video_part_extra": GsStrConfig(
        title="视频块额外参数 (JSON)",
        desc=(
            'video_url 模式下合并进视频块的 JSON 对象。同级合并如 {"media_resolution":"default"}; '
            "若含 video_url 键且为对象则合并至内部 video_url"
        ),
        data="",
    ),
    "video_inline_max_mb": GsIntConfig(
        title="视频内联大小上限 (MB)",
        desc="video_url 模式下转为 DataURI 发送的视频原始大小上限 (MB)。超过时自动降级为抽帧或占位",
        data=12,
    ),
    "video_url_passthrough": GsStrConfig(
        title="视频公网URL直通",
        desc=(
            "是否将原始 http(s) URL 直接填入 video_url.url: off=先下载转 DataURI(默认, "
            "避免临时链接过期导致会话卡死); on=直接透传 URL"
        ),
        data="off",
        options=["off", "on"],
    ),
}

ANTHROPIC_CONFIG_TEMPLATE: dict[str, GSC] = {
    "base_url": GsStrConfig(
        title="Anthropic API基础URL",
        desc="指定Anthropic API的基础URL",
        data="https://api.anthropic.com",
        options=[
            # 官方默认地址
            "https://api.anthropic.com",
            # 原配置中的第三方兼容/代理地址
            "https://api.minimaxi.com/anthropic",
            "https://api.deepseek.com/anthropic",
            "https://api.bltcy.ai",  # 常见的高级中转
            # 国际知名的 API 聚合器 (支持 Anthropic SDK 原生调用)
            "https://openrouter.ai/api",
            "https://api.aimlapi.com",
            # 开发者本地常用的 OneAPI / NewAPI 中转系统默认地址
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
    ),
    "api_key": GsListStrConfig(
        title="Anthropic API密钥",
        desc="指定Anthropic API的密钥, 注意是以 sk-ant- 开头, 不要泄露, 支持添加多个",
        data=["sk-ant-"],
        options=["sk-ant-"],
    ),
    "model_name": GsStrConfig(
        title="调用模型名称",
        desc="指定Anthropic API的模型, 该模型将会用于处理大部分任务",
        data="claude-sonnet-4-20250514",
        options=[
            # 2026 年最新 4.7 / 4.6 家族 (性能/成本最优组合)
            "claude-opus-4-7",  # 4月最新，最强推理与长上下文复杂代码
            "claude-opus-4-6",
            "claude-sonnet-4-6",  # 综合性价比与速度首选
            "claude-haiku-5-5",  # 只接受 adaptive thinking
            "claude-haiku-4-5",  # 极速轻量模型
            # 最新滚动标签 (官方推荐的面向未来的调用方式)
            "claude-opus-latest",
            "claude-sonnet-latest",
            "claude-haiku-latest",
            # 2025 年中期主推版本 (原配置保留)
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-haiku-4-20250514",
        ],
    ),
    "max_tokens": GsStrConfig(
        title="最大输出Token",
        desc="指定最大输出Token数",
        data="8192",
        options=["4096", "8192", "16384", "32768"],
    ),
    "model_support": GsListStrConfig(
        title="模型支持能力",
        desc="显式指定模型支持能力，如是否能看图、能处理文件/音频/视频等",
        data=["text"],
        options=["text", "image"],
    ),
    "model_effort": GsStrConfig(
        title="模型性能",
        desc=(
            "思考档位。adaptive 不传 thinking，由模型自己决定"
            "（claude-haiku-5-5 上这就是 adaptive，默认 effort 为 medium）。"
            "enable 显式打开；minimal / low / medium / high / xhigh 再写 output_config.effort；"
            "disable 发送 thinking.type=disabled。claude-haiku-4-5 及更早的 enable 仍用 budget_tokens"
        ),
        data="enable",
        options=["adaptive", "enable", "disable", "minimal", "low", "medium", "high", "xhigh"],
    ),
    "max_concurrency": GsIntConfig(
        title="允许并发数",
        desc=(
            "该配置允许同时进行的LLM请求数(1~10)。并发占满后，新请求自动切换到"
            "AI配置中的备用(2nd)配置，实现多 provider 同时工作与负载均衡"
        ),
        data=1,
        max_value=10,
        options=[1, 2, 3, 4, 5, 6, 8, 10],
    ),
    "remote_web_search": GsStrConfig(
        title="远端 Web Search",
        desc=(
            "默认开启。Anthropic Messages API 直接用上游内置 web_search，"
            "不再把本地 web_search_tool 发给模型；关闭则走本地 Tavily/Jina/Exa/AnySearch。"
            "中转若不支持 hosted 工具会 400，那时再关掉即可"
        ),
        data="on",
        options=["off", "on"],
    ),
}

GEMINI_CONFIG_TEMPLATE: dict[str, GSC] = {
    "base_url": GsStrConfig(
        title="Gemini API基础URL",
        desc="指定Gemini API的基础URL, 官方为 https://generativelanguage.googleapis.com",
        data="https://generativelanguage.googleapis.com",
        options=[
            # 官方默认地址 (Google AI Studio / Generative Language API)
            "https://generativelanguage.googleapis.com",
            # 国内外常见的 Gemini 原生格式中转
            "https://api.bltcy.ai",
            # 开发者本地常用的 OneAPI / NewAPI 中转系统默认地址
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
    ),
    "api_key": GsListStrConfig(
        title="Gemini API密钥",
        desc="指定Gemini API的密钥, 一般以 AIza 开头, 不要泄露, 支持添加多个",
        # data 必须默认为空:占位串会被当成真实 key 发出去(options 仅作前缀提示)
        data=[],
        options=["AIza"],
    ),
    "model_name": GsStrConfig(
        title="调用模型名称",
        desc="指定Gemini API的模型, 该模型将会用于处理大部分任务",
        data="gemini-2.5-flash",
        options=[
            # 2026 最新 Gemini 3 家族
            "gemini-3.1-pro",
            "gemini-3.1-flash",
            "gemini-3-pro-preview",
            # 2.5 家族 (稳定主力)
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
    ),
    "model_support": GsListStrConfig(
        title="模型支持能力",
        desc="显式指定模型支持能力，如是否能看图、能处理文件/音频/视频等",
        data=["text", "image", "video"],
        options=["text", "image", "audio", "video"],
    ),
    "model_effort": GsStrConfig(
        title="模型性能",
        desc=(
            "思考档位。adaptive 不传思考参数。"
            "enable 打开，disable 关闭；minimal / low / medium / high / xhigh 按该档发送。"
            "Gemini 3 用 thinking_level，Gemini 2.5 用 thinking_budget。"
            "模型名未被标成支持思考时，仍按所选档位发送。上游拒绝时，错误原样返回"
        ),
        data="enable",
        options=["adaptive", "enable", "disable", "minimal", "low", "medium", "high", "xhigh"],
    ),
    "max_concurrency": GsIntConfig(
        title="允许并发数",
        desc=(
            "该配置允许同时进行的LLM请求数(1~10)。并发占满后，新请求自动切换到"
            "AI配置中的备用(2nd)配置，实现多 provider 同时工作与负载均衡"
        ),
        data=1,
        max_value=10,
        options=[1, 2, 3, 4, 5, 6, 8, 10],
    ),
}

PROVIDER_TEMPLATES: dict[ProviderType, dict[str, GSC]] = {
    "openai": OPENAI_CONFIG_TEMPLATE,
    "anthropic": ANTHROPIC_CONFIG_TEMPLATE,
    "gemini": GEMINI_CONFIG_TEMPLATE,
}

PROVIDER_MANAGERS: dict[ProviderType, ConfigSetManager] = {
    "openai": ConfigSetManager(
        base_path=OPENAI_CONFIGS_PATH,
        config_template=OPENAI_CONFIG_TEMPLATE,
        name_suffix="OpenAI",
    ),
    "anthropic": ConfigSetManager(
        base_path=ANTHROPIC_CONFIGS_PATH,
        config_template=ANTHROPIC_CONFIG_TEMPLATE,
        name_suffix="Anthropic",
    ),
    "gemini": ConfigSetManager(
        base_path=GEMINI_CONFIGS_PATH,
        config_template=GEMINI_CONFIG_TEMPLATE,
        name_suffix="Gemini",
    ),
}


def is_provider_type(provider: str) -> TypeGuard[ProviderType]:
    return provider in PROVIDER_MANAGERS


def provider_manager(provider: str) -> ConfigSetManager:
    """根据 provider 类型获取对应的配置管理器。"""
    if is_provider_type(provider):
        return PROVIDER_MANAGERS[provider]
    raise ValueError(t("不支持的 provider 类型: {provider}", provider=provider))


def get_provider_config(
    provider: str,
    config_name: str,
) -> StringConfig:
    """获取指定 provider 的 StringConfig 实例。"""
    return provider_manager(provider).get_config(config_name)


def list_available_provider_configs(
    provider: str,
) -> list[str]:
    """列出所有可用的 provider 配置文件名（不含扩展名）。"""
    return provider_manager(provider).list_available()
