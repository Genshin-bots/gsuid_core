"""
Anthropic 配置管理器模块

管理多个 Anthropic 兼容格式配置文件的读取、写入和热切换。
使用 StringConfig 对象进行配置管理。
"""

from typing import Any, Dict, List

from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import ANTHROPIC_CONFIGS_PATH
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import ConfigSetManager

# Anthropic 配置模板
ANTHROPIC_CONFIG_TEMPLATE: Dict[str, GSC] = {
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
}


class AnthropicConfigManager(ConfigSetManager):
    """
    Anthropic 配置管理器

    继承自 ConfigSetManager，使用 StringConfig 对象进行配置管理。
    支持：
    - 列出所有可用的配置文件
    - 获取/设置当前激活的配置
    - 读取/写入/删除配置文件
    - 热切换配置
    """

    def __init__(self):
        super().__init__(
            base_path=ANTHROPIC_CONFIGS_PATH,
            config_template=ANTHROPIC_CONFIG_TEMPLATE,
            name_suffix="Anthropic",
        )

    def _list_configs(self) -> List[str]:
        """列出所有配置文件"""
        if not self._base_path.exists():
            return []

        configs = []
        for file in self._base_path.iterdir():
            if file.is_file() and file.suffix == ".json":
                configs.append(file.stem)
        return sorted(configs)

    def get_config_dict(self, config_name: str) -> Dict[str, Any] | None:
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

            # 删除旧配置
            self.delete(old_name)

            # 创建新配置（使用 get_config 会自动创建）
            new_config = self.get_config(new_name)
            for key, value in config_data.items():
                new_config.set_config(key, value)

            logger.info(f"已将 Anthropic 配置文件从 '{old_name}' 重命名为 '{new_name}'")
            return True, "ok"
        except Exception as e:
            logger.error(f"重命名 Anthropic 配置失败: {e}")
            return False, str(e)


# 全局单例
anthropic_config_manager = AnthropicConfigManager()
