"""
Gemini 配置管理器模块

管理多个 Gemini(Google GenAI)兼容格式配置文件的读取、写入和热切换。
使用 StringConfig 对象进行配置管理。
"""

from typing import Dict

from gsuid_core.ai_core.resource import GEMINI_CONFIGS_PATH
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import ConfigSetManager

# Gemini 配置模板
GEMINI_CONFIG_TEMPLATE: Dict[str, GSC] = {
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


class GeminiConfigManager(ConfigSetManager):
    """
    Gemini 配置管理器

    继承自 ConfigSetManager，使用 StringConfig 对象进行配置管理。
    支持：
    - 列出所有可用的配置文件
    - 获取/设置当前激活的配置
    - 读取/写入/删除配置文件
    - 热切换配置
    """

    def __init__(self):
        super().__init__(
            base_path=GEMINI_CONFIGS_PATH,
            config_template=GEMINI_CONFIG_TEMPLATE,
            name_suffix="Gemini",
        )


# 全局单例
gemini_config_manager = GeminiConfigManager()
