"""
Gemini 配置管理器模块

管理多个 Gemini(Google GenAI)兼容格式配置文件的读取、写入和热切换。
使用 StringConfig 对象进行配置管理。
"""

from typing import Any, Dict, List

from gsuid_core.i18n import t
from gsuid_core.logger import logger
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

            logger.info(
                t("已将 Gemini 配置文件从 '{old_name}' 重命名为 '{new_name}'", old_name=old_name, new_name=new_name)
            )
            return True, "ok"
        except Exception as e:
            logger.error(t("重命名 Gemini 配置失败: {e}", e=e))
            return False, str(e)


# 全局单例
gemini_config_manager = GeminiConfigManager()
