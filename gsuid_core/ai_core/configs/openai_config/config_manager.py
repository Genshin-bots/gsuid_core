"""
OpenAI 配置管理器模块

管理多个 OpenAI 兼容格式配置文件的读取、写入和热切换。
使用 StringConfig 对象进行配置管理。
"""

from typing import Any, Dict, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import OPENAI_CONFIGS_PATH
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import ConfigSetManager

# OpenAI 配置模板
OPENAI_CONFIG_TEMPLATE: Dict[str, GSC] = {
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
        desc="指定模型思考性能, 不同模型的标准不同，该选项并不保证能真实起效",
        data="enable",
        options=["enable", "disable", "minimal", "low", "medium", "high", "xhigh"],
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
}


class OpenAIConfigManager(ConfigSetManager):
    """
    OpenAI 配置管理器

    继承自 ConfigSetManager，使用 StringConfig 对象进行配置管理。
    支持：
    - 列出所有可用的配置文件
    - 获取/设置当前激活的配置
    - 读取/写入/删除配置文件
    - 热切换配置
    """

    def __init__(self):
        super().__init__(
            base_path=OPENAI_CONFIGS_PATH,
            config_template=OPENAI_CONFIG_TEMPLATE,
            name_suffix="OpenAI",
        )

    def get_config_dict(self, config_name: str) -> Optional[Dict[str, Any]]:
        """
        获取配置的字典形式

        Args:
            config_name: 配置文件名

        Returns:
            配置字典
        """
        config = self.get_config(config_name)
        result = {}
        for key in self._config_template.keys():
            result[key] = config.get_config(key).data
        return result

    def rename(self, old_name: str, new_name: str) -> tuple[bool, str]:
        """
        重命名配置文件

        Args:
            old_name: 原配置文件名（不含扩展名）
            new_name: 新配置文件名（不含扩展名）

        Returns:
            (是否成功, 消息)
        """

        # 检查原配置文件是否存在
        old_path = self._get_config_path(old_name)
        if not old_path.exists():
            return False, f"配置文件 '{old_name}' 不存在"

        # 检查新配置文件名是否已存在
        if self.exists(new_name):
            return False, f"配置文件 '{new_name}' 已存在"

        try:
            # 获取配置数据
            config = self.get_config(old_name)
            config_data = {}
            for key in self._config_template.keys():
                config_data[key] = config.get_config(key).data

            # 创建新配置文件
            new_path = self._get_config_path(new_name)
            new_path.parent.mkdir(parents=True, exist_ok=True)

            # 写入新文件
            import json

            with open(new_path, "w", encoding="UTF-8") as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)

            # 删除旧文件
            old_path.unlink()

            # 清除缓存
            if old_name in self._cache:
                del self._cache[old_name]

            logger.info(
                t("[OpenAIConfig] 已重命名配置文件: {old_name} -> {new_name}", old_name=old_name, new_name=new_name)
            )
            return True, "ok"
        except Exception as e:
            logger.error(t("[OpenAIConfig] 重命名配置文件失败: {e}", e=e))
            return False, str(e)


# 全局单例
openai_config_manager = OpenAIConfigManager()
