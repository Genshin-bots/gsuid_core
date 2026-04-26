"""
Persona 配置管理模块

提供每个 Persona 的独立配置管理，包括：
- AI 行动模式 (ai_mode)
- 启用范围 (scope: disabled/global/specific)
- 目标群聊 (target_groups: 当 scope 为 specific 时使用)

注意：所有 Persona 中只能有一个配置为 "global" (对所有群/角色启用)
"""

from typing import Dict, List, Optional
from pathlib import Path

from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig, ConfigSetManager

from ..resource import PERSONA_PATH

# 默认配置项
DEFAULT_PERSONA_CONFIG: Dict[str, GSC] = {
    "ai_mode": GsListStrConfig(
        "AI行动模式",
        "指定AI的行动模式, AI只会在预定条件完成时才会执行操作",
        ["提及应答"],
        options=["提及应答", "定时巡检", "趣向捕捉(暂不可用)", "困境救场(暂不可用)"],
    ),
    "scope": GsStrConfig(
        "启用范围",
        "指定该人格的启用范围: disabled(不对任何群聊启用), "
        "global(对所有群/角色启用), specific(仅对指定群聊启用)。注意：全部人格中只能有一个配置为 global",
        "disabled",
        options=["disabled", "global", "specific"],
    ),
    "target_groups": GsListStrConfig(
        "目标群聊/角色",
        "当启用范围为 'specific' 时，指定该人格对哪些群聊/角色启用",
        [],
        options=[],
    ),
    "inspect_interval": GsIntConfig(
        "定时巡检间隔",
        "当AI行动模式包含'定时巡检'时，指定巡检间隔（分钟）。默认30分钟",
        30,
        options=[5, 10, 15, 30, 60],
    ),
    "keywords": GsListStrConfig(
        "唤醒关键词",
        "当消息中包含这些关键词时，即使没有@机器人也会触发AI响应。多个关键词用换行分隔",
        [],
        options=[],
    ),
}


class PersonaConfigManager(ConfigSetManager):
    """
    Persona 配置管理器

    继承自 ConfigSetManager，使用 StringConfig 对象进行配置管理。
    确保全局唯一性约束：只能有一个 Persona 配置为 "global" (对所有群/角色启用)
    """

    def __init__(self):
        super().__init__(
            base_path=PERSONA_PATH,
            config_template=DEFAULT_PERSONA_CONFIG,
            name_suffix="Persona",
        )

    def _get_config_path(self, config_name: str) -> Path:
        """获取指定 persona 的配置文件路径（目录结构）"""
        return self._base_path / config_name / "config.json"

    def _list_configs(self) -> List[str]:
        """列出所有 Persona 配置"""
        if not self._base_path.exists():
            return []

        configs = []
        for item in self._base_path.iterdir():
            if item.is_dir():
                configs.append(item.name)
        return sorted(configs)

    def get_all_configs(self) -> Dict[str, StringConfig]:
        """
        获取所有 Persona 的配置

        Returns:
            字典，key 为 persona 名称，value 为 StringConfig 实例
        """
        configs = {}
        if not self._base_path.exists():
            return configs

        for persona_dir in self._base_path.iterdir():
            if persona_dir.is_dir():
                persona_name = persona_dir.name
                configs[persona_name] = self.get_config(persona_name)
        return configs

    def get_global_persona(self) -> Optional[str]:
        """
        获取当前配置为 "global" (对所有群/角色启用) 的 Persona 名称

        Returns:
            Persona 名称，如果没有则返回 None
        """
        for persona_name, config in self.get_all_configs().items():
            scope = config.get_config("scope").data
            if scope == "global":
                return persona_name
        return None

    def validate_global_uniqueness(self, persona_name: str, scope: str) -> tuple[bool, Optional[str]]:
        """
        验证全局唯一性约束

        检查是否可以将指定 persona 设置为 "global"

        Args:
            persona_name: 要设置的 Persona 名称
            scope: 启用范围

        Returns:
            (是否有效, 冲突的 Persona 名称)
            - 如果 scope 不是 "global"，总是返回 (True, None)
            - 如果 scope 是 "global"，检查是否已有其他 Persona 配置为 global
        """
        # 如果不是设置为 global，不需要检查全局唯一性
        if scope != "global":
            return True, None

        # 检查是否已有其他 Persona 配置为 global
        current_global = self.get_global_persona()
        if current_global is not None and current_global != persona_name:
            return False, current_global

        return True, None

    def set_scope(self, persona_name: str, scope: str) -> tuple[bool, str]:
        """
        设置 Persona 的启用范围

        会自动处理全局唯一性约束

        Args:
            persona_name: Persona 名称
            scope: 启用范围，可选值为 "disabled", "global", "specific"

        Returns:
            (是否成功, 消息)
        """
        if scope not in ["disabled", "global", "specific"]:
            return False, f"无效的启用范围: {scope}"

        config = self.get_config(persona_name)

        # 验证全局唯一性
        is_valid, conflict_persona = self.validate_global_uniqueness(persona_name, scope)
        if not is_valid:
            return False, f"无法设置为对所有群/角色启用，因为 '{conflict_persona}' 已配置为全局启用"

        # 设置配置
        success = config.set_config("scope", scope)
        if success:
            logger.info(f"[PersonaConfig] 已更新 '{persona_name}' 的启用范围: {scope}")
            return True, "ok"
        else:
            return False, "配置写入失败"

    def set_target_groups(self, persona_name: str, target_groups: List[str]) -> tuple[bool, str]:
        """
        设置 Persona 的目标群聊

        Args:
            persona_name: Persona 名称
            target_groups: 目标群聊列表

        Returns:
            (是否成功, 消息)
        """
        config = self.get_config(persona_name)

        success = config.set_config("target_groups", target_groups)
        if success:
            logger.info(f"[PersonaConfig] 已更新 '{persona_name}' 的目标群聊: {target_groups}")
            return True, "ok"
        else:
            return False, "配置写入失败"

    def set_ai_mode(self, persona_name: str, ai_mode: List[str]) -> tuple[bool, str]:
        """
        设置 Persona 的 AI 行动模式

        Args:
            persona_name: Persona 名称
            ai_mode: AI 行动模式列表

        Returns:
            (是否成功, 消息)
        """
        config = self.get_config(persona_name)

        # 验证选项有效性
        valid_options = ["提及应答", "定时巡检", "趣向捕捉(暂不可用)", "困境救场(暂不可用)"]
        for mode in ai_mode:
            if mode not in valid_options:
                return False, f"无效的 AI 行动模式: {mode}"

        success = config.set_config("ai_mode", ai_mode)
        if success:
            logger.info(f"[PersonaConfig] 已更新 '{persona_name}' 的 AI 模式: {ai_mode}")
            return True, "ok"
        else:
            return False, "配置写入失败"

    def set_inspect_interval(self, persona_name: str, inspect_interval: int) -> tuple[bool, str]:
        """
        设置 Persona 的定时巡检间隔

        Args:
            persona_name: Persona 名称
            inspect_interval: 巡检间隔（分钟）

        Returns:
            (是否成功, 消息)
        """
        if inspect_interval not in [5, 10, 15, 30, 60]:
            return False, "无效的巡检间隔，可选值: 5, 10, 15, 30, 60"

        config = self.get_config(persona_name)

        success = config.set_config("inspect_interval", inspect_interval)
        if success:
            logger.info(f"[PersonaConfig] 已更新 '{persona_name}' 的巡检间隔: {inspect_interval} 分钟")
            return True, "ok"
        else:
            return False, "配置写入失败"

    def set_keywords(self, persona_name: str, keywords: List[str]) -> tuple[bool, str]:
        """
        设置 Persona 的唤醒关键词

        Args:
            persona_name: Persona 名称
            keywords: 唤醒关键词列表

        Returns:
            (是否成功, 消息)
        """
        config = self.get_config(persona_name)

        success = config.set_config("keywords", keywords)
        if success:
            logger.info(f"[PersonaConfig] 已更新 '{persona_name}' 的唤醒关键词: {keywords}")
            return True, "ok"
        else:
            return False, "配置写入失败"

    def get_persona_for_session(self, session_id: str) -> Optional[str]:
        """
        根据 Session ID 获取应该使用的 Persona

        Session ID 格式:
        - 群聊: "bot:{bot_id}:group:{group_id}"
        - 私聊: "bot:{bot_id}:private:{user_id}"

        匹配规则：
        1. 首先查找专门针对该群聊的 Persona（scope 为 specific 且 target_groups 包含该群聊）
        2. 如果没有找到，查找配置为 "global" 的 Persona
        3. 如果没有找到，返回 None

        Args:
            session_id: Session ID

        Returns:
            Persona 名称，如果没有匹配的则返回 None
        """
        group_id: Optional[str] = None
        user_id: Optional[str] = None
        is_private_chat = False

        # 解析 session_id 获取 group_id 或 user_id
        # 格式: bot:{bot_id}:group:{group_id} 或 bot:{bot_id}:private:{user_id}
        if session_id.startswith("bot:"):
            parts = session_id.split(":", 3)
            if len(parts) >= 4:
                if parts[2] == "group":
                    group_id = parts[3]
                elif parts[2] == "private":
                    user_id = parts[3]
                    is_private_chat = True
        else:
            # 无法解析格式
            raise ValueError(f"Invalid session_id format: {session_id}")

        global_persona: Optional[str] = None

        for persona_name, config in self.get_all_configs().items():
            scope = config.get_config("scope").data
            target_groups = config.get_config("target_groups").data

            # 检查是否专门针对该群聊或用户
            if scope == "specific":
                if group_id and group_id in target_groups:
                    return persona_name
                if user_id and user_id in target_groups:
                    return persona_name

            # 记录全局启用的 persona
            if scope == "global":
                global_persona = persona_name

        # 私聊也使用 global persona
        if is_private_chat:
            return global_persona

        return global_persona

    def get_persona_config_dict(self, persona_name: str) -> Optional[Dict]:
        """
        获取指定 Persona 的配置字典（用于 API 返回）

        Args:
            persona_name: Persona 名称

        Returns:
            配置字典，如果 persona 不存在则返回 None
        """
        config_path = self._get_config_path(persona_name)
        if not config_path.exists():
            return None

        config = self.get_config(persona_name)
        return {
            "ai_mode": config.get_config("ai_mode").data,
            "scope": config.get_config("scope").data,
            "target_groups": config.get_config("target_groups").data,
            "inspect_interval": config.get_config("inspect_interval").data,
            "keywords": config.get_config("keywords").data,
        }

    def delete_persona_config(self, persona_name: str) -> bool:
        """
        删除 Persona 的配置文件

        Args:
            persona_name: Persona 名称

        Returns:
            是否成功删除
        """
        if persona_name in self._cache:
            del self._cache[persona_name]

        config_path = self._get_config_path(persona_name)
        if config_path.exists():
            try:
                config_path.unlink()
                logger.info(f"[PersonaConfig] 已删除 '{persona_name}' 的配置文件")
                return True
            except Exception as e:
                logger.error(f"[PersonaConfig] 删除 '{persona_name}' 的配置文件失败: {e}")
                return False
        return True


# 全局实例
persona_config_manager = PersonaConfigManager()
