"""
OpenAI 配置管理器模块

管理多个 OpenAI 兼容格式配置文件的读取、写入和热切换。
使用 StringConfig 对象进行配置管理。
"""

from typing import Any, Dict, Optional

from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import OPENAI_CONFIGS_PATH
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsStrConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import ConfigSetManager

# OpenAI 配置模板
OPENAI_CONFIG_TEMPLATE: Dict[str, GSC] = {
    "base_url": GsStrConfig(
        title="OpenAI API基础URL",
        desc="指定OpenAI API的基础URL, 注意是以 /v1 结尾",
        data="https://api.openai.com/v1",
        options=[
            "https://api.openai.com/v1",
            "https://api.bltcy.ai/v1",
            "https://api.minimaxi.com/v1",
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
            "gpt-4o-mini",
            "gpt-4o",
            "gemini-2.5-flash",
            "gemini-3.1-flash-lite-preview",
            "MiniMax-M2.7",
        ],
    ),
    "embedding_model": GsStrConfig(
        title="嵌入模型(暂不支持远程嵌入)",
        desc="指定OpenAI API的嵌入模型, 该模型将会用于处理文本嵌入",
        data="text-embedding-3-small",
        options=[
            "text-embedding-3-small",
            "text-embedding-3-large",
            "text-embedding-2",
        ],
    ),
    "model_support": GsListStrConfig(
        title="模型支持能力",
        desc="显式指定模型支持能力，如是否能看图、能处理文件/音频/视频等",
        data=["text"],
        options=["text", "image", "audio", "video"],
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

            logger.info(f"[OpenAIConfig] 已重命名配置文件: {old_name} -> {new_name}")
            return True, "ok"
        except Exception as e:
            logger.error(f"[OpenAIConfig] 重命名配置文件失败: {e}")
            return False, str(e)


# 全局单例
openai_config_manager = OpenAIConfigManager()
