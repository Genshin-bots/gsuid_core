"""
Persona 配置管理模块

提供每个 Persona 的独立配置模板、落盘持久化与互斥校验（``PersonaConfigManager``）。
纯无状态规则、值对象与解析校验位于 ``gsuid_core.ai_core.persona.config_rules``。

通道全局互斥：全系统最多一人占 ``group_scope=all``，最多一人占 ``private_scope=all``，
两通道互不冲突。
"""

import json
import time
from typing import Dict, List, Optional
from pathlib import Path
from collections.abc import Mapping, Sequence

from msgspec import to_builtins
from boltons.fileutils import atomic_save

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsFloatConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig, ConfigSetManager

from ..resource import PERSONA_PATH
from .config_rules import (
    AI_MODE_OPTIONS,
    CHANNEL_SCOPE_VALUES,
    PERSONA_SCOPE_VALUES,
    AutonomySettings,
    ChannelDeploymentSnapshot,
    read_target_list,
    read_drive_traits,
    normalize_ai_modes,
    derive_legacy_scope,
    read_channel_scopes,
    read_autonomy_runtime,
    persona_covers_session,
    autonomy_settings_error,
    legacy_scope_to_channels,
)

# 默认配置项
DEFAULT_PERSONA_CONFIG: Dict[str, GSC] = {
    "ai_mode": GsListStrConfig(
        "AI行动模式",
        "指定AI的行动模式, AI只会在预定条件完成时才会执行操作",
        ["提及应答"],
        options=list(AI_MODE_OPTIONS),
    ),
    "aside_image_probability": GsFloatConfig(
        "命令补充·只发图片",
        "命令补充开启后，命令回复只有图片时补一句的概率，0 到 1。未抽中也会把这次命令和回复写入当前会话。",
        0.2,
        min_value=0,
        max_value=1,
    ),
    "aside_text_probability": GsFloatConfig(
        "命令补充·只发文字",
        "命令补充开启后，命令回复只有文字时补一句的概率，0 到 1。失败说明和用法提示算文字。",
        0.35,
        min_value=0,
        max_value=1,
    ),
    "aside_mixed_probability": GsFloatConfig(
        "命令补充·图文混合",
        "命令补充开启后，命令回复同时有图和文字时补一句的概率，0 到 1。",
        0.25,
        min_value=0,
        max_value=1,
    ),
    "aside_allow_tools": GsBoolConfig(
        "命令补充允许工具",
        "为真时，点评可以调用当前人格的工具，并阅读这条命令所属插件目录里的源码。默认关闭。",
        False,
    ),
    "aside_skip_plugins": GsListStrConfig(
        "命令补充跳过插件",
        "这些插件发出的回执永远不进入命令补充/点评，默认包含 core_command",
        ["core_command"],
        options=[],
    ),
    "aside_cooldown_seconds": GsFloatConfig(
        "命令补充冷却时间(秒)",
        "同一会话两次命令点评的最小间隔时间（秒）。默认120秒。",
        120.0,
        min_value=0.0,
    ),
    "group_scope": GsStrConfig(
        "群聊启用范围",
        "群聊通道启用范围: off(关闭), specific(仅指定群聊), all(全部群聊，全系统仅允许一个人格)",
        "off",
        options=list(CHANNEL_SCOPE_VALUES),
    ),
    "target_groups": GsListStrConfig(
        "目标群聊",
        "当群聊启用范围为 'specific' 时，指定该人格对哪些群聊启用",
        [],
        options=[],
    ),
    "private_scope": GsStrConfig(
        "私聊启用范围",
        "私聊通道启用范围: off(关闭), specific(仅指定用户), all(全部私聊，全系统仅允许一个人格)",
        "off",
        options=list(CHANNEL_SCOPE_VALUES),
    ),
    "target_users": GsListStrConfig(
        "目标私聊用户",
        "当私聊启用范围为 'specific' 时，指定该人格对哪些私聊用户启用",
        [],
        options=[],
    ),
    "scope": GsStrConfig(
        "启用范围(兼容派生)",
        "由 group_scope 与 private_scope 自动派生，仅作旧接口与版本回滚兼容。",
        "disabled",
        options=list(PERSONA_SCOPE_VALUES),
    ),
    "social_initiative": GsIntConfig(
        "社交主动度",
        "0-100，越高越想主动找人搭话",
        50,
        options=[],
    ),
    "energy_endurance": GsIntConfig(
        "社交耐力",
        "0-100，越高连续互动越不容易倦",
        50,
        options=[],
    ),
    "curiosity_focus": GsStrConfig(
        "闲时关注点",
        "自主行动先看人还是先查群聊话题",
        "balanced",
        options=["balanced", "people", "analytical", "whimsical"],
    ),
    "arc_persistence": GsIntConfig(
        "单话题跟进轮数",
        "1-3，自主行动同一话题最多跟进几轮",
        2,
        options=[1, 2, 3],
    ),
    "inspect_interval": GsIntConfig(
        "定时巡检间隔",
        "当AI行动模式包含'定时巡检'时，指定巡检间隔（分钟）。默认60分钟",
        60,
        options=[5, 10, 15, 30, 60],
    ),
    "keywords": GsListStrConfig(
        "唤醒关键词",
        "当消息中包含这些关键词时，即使没有@机器人也会触发AI响应。多个关键词用换行分隔",
        [],
        options=[],
    ),
    "active_hours": GsListStrConfig(
        "作息时段",
        "HH:MM-HH:MM，可多段，可跨午夜。时段外不巡检，也不自主行动。",
        ["08:00-23:30"],
        options=[],
    ),
    "timezone": GsStrConfig(
        "作息时区",
        "IANA 时区名，例如 Asia/Shanghai。",
        "Asia/Shanghai",
    ),
    "autonomy_interval_minutes": GsIntConfig(
        "自主行动间隔（分钟）",
        "两次自主行动的最短间隔。实际触发会在此基础上上下浮动两成。",
        45,
        max_value=720,
    ),
    "autonomy_care_min_zone": GsStrConfig(
        "自主关心的最低温区",
        "familiar 为熟络及以上，close 为仅亲密。主人始终包含。",
        "familiar",
        options=["familiar", "close"],
    ),
    "speech_len_soft": GsIntConfig(
        "台词软上限（字）",
        "角色卡渲染的建议台词长度，默认 60。留默认即跟随「说话强度」派生",
        60,
        options=[30, 45, 60, 80, 90, 100],
    ),
    "chat_style": GsIntConfig(
        "说话强度",
        "控制发言形态：0=惜字如金（1条/30字）、50=默认（2条/60字）、100=连珠炮（4条/90字）。"
        "与关系温度正交：关系管态度语气，本项管发几条/多长/怎么分段。保存后，用这张人格的各会话会换上新字数。",
        50,
        options=[0, 25, 50, 75, 100],
    ),
    # 检索池按插件收放；能力族固定 dynamic，不进人格模板。
    "enabled_tools": GsListStrConfig(
        "启用工具（按插件）",
        "决定该人格的向量检索池收录哪些插件的工具。默认 * = 全部插件都进检索池；"
        "!插件名 = 排除该插件；只填具体插件名 = 仅这些进检索池。"
        "不影响常驻直装工具（send_message_by_ai 等框架能力）与显式工具白名单里的常驻工具。",
        ["*"],
        options=["*"],
    ),
    "tool_names": GsListStrConfig(
        "显式工具白名单",
        "该人格常驻直装的工具名：每轮直接进工具表，不经向量检索，也不占检索名额。"
        "框架默认常驻工具（send_message_by_ai、find_tools 等）默认已写入，其余按需增删。多个用换行分隔。",
        [],
        options=[],
    ),
    "capability_agents": GsListStrConfig(
        "可委派能力代理",
        "该人格允许 create_subagent 的 node_id。"
        "含 * 表示全部；!node_id 表示禁用（如 !render_agent）。"
        "只填具体 id 则仅这些可委派。空列表表示不可委派任何能力代理。",
        ["*"],
        options=["*"],
    ),
    "prompt_group": GsStrConfig(
        "系统提示词组",
        "该人格使用的系统提示词组。默认组每次启动从程序写回，不能改其正文。",
        "默认",
        options=[],
    ),
    "allow_meme_reply": GsBoolConfig(
        "允许表情回复",
        "打开后，系统提示里写入表情协议，回复可以用 <meme:情绪>。关掉后不写这段，也不发送表情。",
        True,
    ),
    "meme_frequency": GsIntConfig(
        "表情频率",
        "控制系统提示里的表情触发倾向：0=禁用表情，1-19=极少回敬，20-39=低频点缀，40-64=自然适中，65-84=高频活跃，85-100=极高频必配。同时联动未显式指定时的发图冷却。",
        50,
        options=[0, 25, 50, 75, 100],
    ),
    "meme_send_cooldown_sec": GsIntConfig(
        "表情发送冷却",
        "同会话表情发送最小间隔（秒）。0 表示跟随表情频率档位自动联动，也可设定大于 0 的具体秒数固定冷却。",
        0,
        options=[0, 15, 30, 60, 120, 300],
    ),
}

_ASIDE_PROB_FIELDS = frozenset({"aside_image_probability", "aside_text_probability", "aside_mixed_probability"})
_INSPECT_INTERVAL_VALUES = (5, 10, 15, 30, 60)
_ConfigScalar = str | int | float | bool | list[str]


def _effective_prompt_group(persona_name: str) -> str:
    """配置里的组已删除时，接口返回实际发送用的默认组。"""
    from gsuid_core.ai_core.prompt_group.store import group_name_for_persona

    return group_name_for_persona(persona_name)


def _dedupe_str_list(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def _validate_int_range(
    value: object,
    label: str,
    low: int,
    high: int,
    *,
    unit: str = "",
) -> tuple[int | None, str]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{label}必须是整数"
    if value < low or value > high:
        suffix = f" {unit}" if unit else " "
        return None, f"{label}必须在 {low} 到 {high}{suffix}之间"
    return value, ""


def _conflict_error(conflicts: Mapping[str, list[str]]) -> str | None:
    if conflicts["group"]:
        return f"无法设为全部群聊，因为 '{conflicts['group'][0]}' 已占用群聊全局范围"
    if conflicts["private"]:
        return f"无法设为全部私聊，因为 '{conflicts['private'][0]}' 已占用私聊全局范围"
    return None


def _apply_gsc(item: GSC, value: _ConfigScalar, *, commit: bool) -> bool:
    if isinstance(item, GsBoolConfig) and isinstance(value, bool):
        if commit:
            item.data = value
        return True
    if isinstance(item, GsIntConfig) and isinstance(value, int) and not isinstance(value, bool):
        if commit:
            item.data = value
        return True
    if isinstance(item, GsFloatConfig) and isinstance(value, (int, float)) and not isinstance(value, bool):
        if commit:
            item.data = float(value)
        return True
    if isinstance(item, GsStrConfig) and isinstance(value, str):
        if commit:
            item.data = value
        return True
    if isinstance(item, GsListStrConfig) and isinstance(value, list):
        if commit:
            item.data = list(value)
        return True
    return False


def _gsc_dict(key: str, data: str | list[str]) -> dict[str, object]:
    built: object = to_builtins(DEFAULT_PERSONA_CONFIG[key])
    entry: dict[str, object] = dict(built) if isinstance(built, dict) else {}
    entry["data"] = data
    return entry


def rewrite_config_bytes_with_deployment(
    raw_bytes: bytes,
    snapshot: ChannelDeploymentSnapshot,
) -> bytes | None:
    """在内存中改写 config.json 的部署态字段，供整包导入/导出一次落盘且避免伪迁移。"""
    try:
        raw_obj: object = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw_obj, dict):
        return None
    raw_obj["group_scope"] = _gsc_dict("group_scope", snapshot.group_scope)
    raw_obj["private_scope"] = _gsc_dict("private_scope", snapshot.private_scope)
    raw_obj["scope"] = _gsc_dict("scope", derive_legacy_scope(snapshot.group_scope, snapshot.private_scope))
    raw_obj["target_groups"] = _gsc_dict("target_groups", list(snapshot.target_groups))
    raw_obj["target_users"] = _gsc_dict("target_users", list(snapshot.target_users))
    return json.dumps(raw_obj, indent=4, ensure_ascii=False).encode("utf-8")


def _write_channel_scopes(config: StringConfig, group_scope: str, private_scope: str) -> bool:
    """一次性写入双通道与派生旧 scope，单次落盘避免半写入。"""
    if "group_scope" not in config.config or "private_scope" not in config.config or "scope" not in config.config:
        return False
    group_item = config.config["group_scope"]
    private_item = config.config["private_scope"]
    legacy_item = config.config["scope"]
    if (
        not isinstance(group_item, GsStrConfig)
        or not isinstance(private_item, GsStrConfig)
        or not isinstance(legacy_item, GsStrConfig)
    ):
        return False
    group_item.data = group_scope
    private_item.data = private_scope
    legacy_item.data = derive_legacy_scope(group_scope, private_scope)
    config.write_config()
    return True


def _rollback_channel_scopes(released: Sequence[tuple[StringConfig, str, str]]) -> None:
    for prev_cfg, prev_g, prev_p in reversed(released):
        _write_channel_scopes(prev_cfg, prev_g, prev_p)


def _migrate_legacy_scope_file(config_path: Path) -> bool:
    """加载前原位迁移旧 scope 到双通道字段（方案 A），避免 reconcile_config 先补 off。"""
    if not config_path.is_file():
        return False
    try:
        raw_obj: object = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if (
        not isinstance(raw_obj, dict)
        or "group_scope" in raw_obj
        or "private_scope" in raw_obj
        or "scope" not in raw_obj
    ):
        return False

    legacy_scope = "disabled"
    scope_entry: object = raw_obj["scope"]
    if isinstance(scope_entry, dict) and "data" in scope_entry and isinstance(scope_entry["data"], str):
        legacy_scope = scope_entry["data"]

    channels = legacy_scope_to_channels(legacy_scope)
    raw_obj["group_scope"] = _gsc_dict("group_scope", channels.group_scope)
    raw_obj["private_scope"] = _gsc_dict("private_scope", channels.private_scope)
    if "target_users" not in raw_obj:
        raw_obj["target_users"] = _gsc_dict("target_users", [])
    raw_obj["scope"] = _gsc_dict("scope", derive_legacy_scope(channels.group_scope, channels.private_scope))

    payload = json.dumps(raw_obj, indent=4, ensure_ascii=False).encode("utf-8")
    # 复用 Windows 文件锁退避重试，写盘失败必须抛错，防止后续 reconcile_config 静默重置为 off
    last_err: OSError | None = None
    for attempt in range(5):
        try:
            with atomic_save(
                str(config_path),
                text_mode=False,
                overwrite=True,
                overwrite_part=True,
                file_perms=0o644,
            ) as handle:
                if handle is None:
                    raise OSError(f"atomic_save returned None for {config_path}")
                handle.write(payload)
                last_err = None
                break
        except OSError as exc:
            last_err = exc
            if attempt < 4:
                time.sleep(0.25 * (attempt + 1))
    if last_err is not None:
        raise last_err

    persona_name = config_path.parent.name
    logger.info(
        t(
            "log.persona.scope_migrated",
            persona_name=persona_name,
            old_scope=legacy_scope,
            group_scope=channels.group_scope,
            private_scope=channels.private_scope,
        )
    )
    if legacy_scope == "specific" and "target_groups" in raw_obj:
        tg_entry: object = raw_obj["target_groups"]
        if (
            isinstance(tg_entry, dict)
            and "data" in tg_entry
            and isinstance(tg_entry["data"], list)
            and tg_entry["data"]
        ):
            logger.warning(
                t(
                    "log.persona.scope_migrated_specific_dm_hint",
                    persona_name=persona_name,
                    count=len(tg_entry["data"]),
                )
            )
    return True


class PersonaConfigManager(ConfigSetManager):
    """Persona 配置管理器（``StringConfig`` 落盘 + 双通道全局互斥校验）。"""

    def __init__(self) -> None:
        super().__init__(
            base_path=PERSONA_PATH,
            config_template=DEFAULT_PERSONA_CONFIG,
            name_suffix="Persona",
        )

    def _get_config_path(self, config_name: str) -> Path:
        """获取指定 persona 的配置文件路径（目录结构）。"""
        from gsuid_core.utils.path_safety import safe_join

        return safe_join(self._base_path, config_name, "config.json")

    def _create_string_config(self, config_name: str) -> StringConfig:
        _migrate_legacy_scope_file(self._get_config_path(config_name))
        return super()._create_string_config(config_name)

    def _list_configs(self) -> List[str]:
        """列出所有 Persona 配置。"""
        if not self._base_path.exists():
            return []
        return sorted(item.name for item in self._base_path.iterdir() if item.is_dir())

    def get_all_configs(self) -> Dict[str, StringConfig]:
        """获取所有 Persona 的配置（key 为 persona 名称，value 为 StringConfig）。"""
        if not self._base_path.exists():
            return {}
        return {
            p.name: self.get_config(p.name)
            for p in sorted(self._base_path.iterdir(), key=lambda p: p.name)
            if p.is_dir()
        }

    def _write_field(
        self,
        persona_name: str,
        key: str,
        value: _ConfigScalar,
        log_msg: str,
    ) -> tuple[bool, str]:
        if not self.get_config(persona_name).set_config(key, value):
            return False, "配置写入失败"
        logger.info(log_msg)
        return True, "ok"

    def get_global_persona(self) -> Optional[str]:
        """获取同时占群聊与私聊全部通道的 Persona；单通道回退请用 get_fallback_persona。"""
        for persona_name, config in self.get_all_configs().items():
            scopes = read_channel_scopes(config)
            if scopes.group_scope == "all" and scopes.private_scope == "all":
                return persona_name
        return None

    def get_fallback_persona(self, *, is_private: bool) -> Optional[str]:
        """按会话类型取通道全局回退人格（group_scope=all / private_scope=all）。"""
        for persona_name, config in self.get_all_configs().items():
            scopes = read_channel_scopes(config)
            if (scopes.private_scope if is_private else scopes.group_scope) == "all":
                return persona_name
        return None

    def find_channel_conflicts(
        self,
        persona_name: str,
        *,
        group_scope: str | None = None,
        private_scope: str | None = None,
    ) -> dict[str, list[str]]:
        """按通道检查全局互斥：仅当某通道设为 all 时检查该通道占用者。"""
        group_conflicts: list[str] = []
        private_conflicts: list[str] = []
        if group_scope != "all" and private_scope != "all":
            return {"group": group_conflicts, "private": private_conflicts}
        for other_name, config in self.get_all_configs().items():
            if other_name == persona_name:
                continue
            other = read_channel_scopes(config)
            if group_scope == "all" and other.group_scope == "all":
                group_conflicts.append(other_name)
            if private_scope == "all" and other.private_scope == "all":
                private_conflicts.append(other_name)
        return {"group": group_conflicts, "private": private_conflicts}

    def snapshot_channel_deployment(self, persona_name: str) -> ChannelDeploymentSnapshot:
        """抓取人格本地部署态范围与目标列表，供覆盖导入前留底。"""
        cfg = self.get_config(persona_name)
        scopes = read_channel_scopes(cfg)
        return ChannelDeploymentSnapshot(
            group_scope=scopes.group_scope,
            private_scope=scopes.private_scope,
            target_groups=tuple(read_target_list(cfg, "target_groups")),
            target_users=tuple(read_target_list(cfg, "target_users")),
        )

    def restore_channel_deployment(
        self,
        persona_name: str,
        snapshot: ChannelDeploymentSnapshot,
    ) -> bool:
        """一次性恢复双通道范围、派生旧 scope 与目标列表，单次写盘避免中间态。"""
        self.drop_cache(persona_name)
        cfg = self.get_config(persona_name)
        if any(k not in cfg.config for k in ("group_scope", "private_scope", "scope", "target_groups", "target_users")):
            return False
        group_item = cfg.config["group_scope"]
        private_item = cfg.config["private_scope"]
        legacy_item = cfg.config["scope"]
        groups_item = cfg.config["target_groups"]
        users_item = cfg.config["target_users"]
        if (
            not isinstance(group_item, GsStrConfig)
            or not isinstance(private_item, GsStrConfig)
            or not isinstance(legacy_item, GsStrConfig)
            or not isinstance(groups_item, GsListStrConfig)
            or not isinstance(users_item, GsListStrConfig)
        ):
            return False
        derived = derive_legacy_scope(snapshot.group_scope, snapshot.private_scope)
        next_groups = list(snapshot.target_groups)
        next_users = list(snapshot.target_users)
        if (
            group_item.data == snapshot.group_scope
            and private_item.data == snapshot.private_scope
            and legacy_item.data == derived
            and groups_item.data == next_groups
            and users_item.data == next_users
        ):
            return True
        group_item.data = snapshot.group_scope
        private_item.data = snapshot.private_scope
        legacy_item.data = derived
        groups_item.data = next_groups
        users_item.data = next_users
        cfg.write_config()
        return True

    def validate_channel_scopes(
        self,
        persona_name: str,
        *,
        group_scope: str | None = None,
        private_scope: str | None = None,
        legacy_scope: str | None = None,
        takeover_conflicts: bool = False,
    ) -> tuple[bool, str]:
        """校验双通道或兼容旧 scope 的取值与通道全局互斥（不写盘）。"""
        if group_scope is not None or private_scope is not None:
            if group_scope is not None and group_scope not in CHANNEL_SCOPE_VALUES:
                return False, f"无效的群聊启用范围: {group_scope}"
            if private_scope is not None and private_scope not in CHANNEL_SCOPE_VALUES:
                return False, f"无效的私聊启用范围: {private_scope}"
            if takeover_conflicts:
                return True, "ok"
            err = _conflict_error(
                self.find_channel_conflicts(persona_name, group_scope=group_scope, private_scope=private_scope)
            )
            return (False, err) if err else (True, "ok")

        if legacy_scope is not None:
            if legacy_scope not in PERSONA_SCOPE_VALUES:
                return False, f"无效的启用范围: {legacy_scope}"
            if takeover_conflicts:
                return True, "ok"
            current = read_channel_scopes(self.get_config(persona_name))
            if derive_legacy_scope(current.group_scope, current.private_scope) == legacy_scope:
                return True, "ok"
            mapped = legacy_scope_to_channels(legacy_scope)
            err = _conflict_error(
                self.find_channel_conflicts(
                    persona_name,
                    group_scope=mapped.group_scope if mapped.group_scope == "all" else None,
                    private_scope=mapped.private_scope if mapped.private_scope == "all" else None,
                )
            )
            return (False, err) if err else (True, "ok")
        return True, "ok"

    def set_channel_scopes(
        self,
        persona_name: str,
        *,
        group_scope: str | None = None,
        private_scope: str | None = None,
        takeover_conflicts: bool = False,
    ) -> tuple[bool, str]:
        """设置群聊/私聊通道范围，按通道独立校验全局互斥并同步派生旧 scope。"""
        ok, err = self.validate_channel_scopes(
            persona_name,
            group_scope=group_scope,
            private_scope=private_scope,
            takeover_conflicts=takeover_conflicts,
        )
        if not ok:
            return False, err

        config = self.get_config(persona_name)
        current = read_channel_scopes(config)
        final_group = group_scope if group_scope is not None else current.group_scope
        final_private = private_scope if private_scope is not None else current.private_scope

        released: list[tuple[StringConfig, str, str]] = []
        if takeover_conflicts:
            conflicts = self.find_channel_conflicts(
                persona_name,
                group_scope=final_group if group_scope is not None else None,
                private_scope=final_private if private_scope is not None else None,
            )
            # 仅关闭冲突人格的对应通道，保留其另一通道配置
            release_map: dict[str, set[str]] = {}
            for name in conflicts["group"]:
                release_map.setdefault(name, set()).add("group")
            for name in conflicts["private"]:
                release_map.setdefault(name, set()).add("private")
            for other_name, channels in release_map.items():
                other_cfg = self.get_config(other_name)
                other_cur = read_channel_scopes(other_cfg)
                next_g = "off" if "group" in channels else other_cur.group_scope
                next_p = "off" if "private" in channels else other_cur.private_scope
                if not _write_channel_scopes(other_cfg, next_g, next_p):
                    _rollback_channel_scopes(released)
                    return False, f"释放 '{other_name}' 通道冲突失败"
                released.append((other_cfg, other_cur.group_scope, other_cur.private_scope))
                logger.info(
                    t(
                        "log.persona.personaconfig_updated_name_channels",
                        persona_name=other_name,
                        group_scope=next_g,
                        private_scope=next_p,
                    )
                )

        if _write_channel_scopes(config, final_group, final_private):
            logger.info(
                t(
                    "log.persona.personaconfig_updated_name_channels",
                    persona_name=persona_name,
                    group_scope=final_group,
                    private_scope=final_private,
                )
            )
            return True, "ok"
        # 目标写入失败时回滚已释放的旧兜底，避免全系统失去通道覆盖
        _rollback_channel_scopes(released)
        return False, "配置写入失败"

    def apply_field_updates(self, persona_name: str, updates: Mapping[str, _ConfigScalar]) -> tuple[bool, str]:
        """一次性写入多个非通道配置项并单次落盘，避免逐键多次写盘和半写入。"""
        if not updates:
            return True, "ok"
        config = self.get_config(persona_name)
        for key, value in updates.items():
            if key not in config.config:
                return False, f"配置项不存在: {key}"
            if not _apply_gsc(config.config[key], value, commit=False):
                return False, f"配置项类型不匹配: {key}"
        for key, value in updates.items():
            _apply_gsc(config.config[key], value, commit=True)
        config.write_config()
        return True, "ok"

    def set_scope(
        self,
        persona_name: str,
        scope: str,
        *,
        takeover_conflicts: bool = False,
    ) -> tuple[bool, str]:
        """兼容接口：按旧单字段 scope 映射并写入双通道启用范围。"""
        if scope not in PERSONA_SCOPE_VALUES:
            return False, f"无效的启用范围: {scope}"
        # 有损投影幂等保护：旧客户端原样回传派生 scope 时不覆写双通道
        current = read_channel_scopes(self.get_config(persona_name))
        if derive_legacy_scope(current.group_scope, current.private_scope) == scope:
            return True, "ok"
        channels = legacy_scope_to_channels(scope)
        ok, msg = self.set_channel_scopes(
            persona_name,
            group_scope=channels.group_scope,
            private_scope=channels.private_scope,
            takeover_conflicts=takeover_conflicts,
        )
        if ok:
            logger.info(t("log.persona.personaconfig_updated_name_enabled", persona_name=persona_name, scope=scope))
        return ok, msg

    def set_target_groups(self, persona_name: str, target_groups: List[str]) -> tuple[bool, str]:
        """设置 Persona 的目标群聊列表。"""
        cleaned = _dedupe_str_list(target_groups)
        return self._write_field(
            persona_name,
            "target_groups",
            cleaned,
            t("log.persona.personaconfig_updated_name_target", persona_name=persona_name, target_groups=cleaned),
        )

    def set_target_users(self, persona_name: str, target_users: List[str]) -> tuple[bool, str]:
        """设置 Persona 的目标私聊用户列表。"""
        cleaned = _dedupe_str_list(target_users)
        return self._write_field(
            persona_name,
            "target_users",
            cleaned,
            t("log.persona.personaconfig_updated_name_target_users", persona_name=persona_name, target_users=cleaned),
        )

    def set_ai_mode(self, persona_name: str, ai_mode: List[str]) -> tuple[bool, str]:
        """设置 Persona 的 AI 行动模式。"""
        normalized = normalize_ai_modes(ai_mode)
        for mode in normalized:
            if mode not in AI_MODE_OPTIONS:
                return False, f"无效的 AI 行动模式: {mode}"
        return self._write_field(
            persona_name,
            "ai_mode",
            normalized,
            t("log.persona.personaconfig_updated_name_ai", persona_name=persona_name, ai_mode=normalized),
        )

    def set_autonomy_settings(self, persona_name: str, settings: AutonomySettings) -> tuple[bool, str]:
        """保存自主行动和作息。任一字段不合法就整单不写。"""
        error = autonomy_settings_error(settings)
        if error:
            return False, error
        config = self.get_config(persona_name)
        writes: list[tuple[str, str | int | list[str]]] = []
        if settings.active_hours is not None:
            writes.append(("active_hours", [item.strip() for item in settings.active_hours]))
        if settings.timezone is not None:
            writes.append(("timezone", settings.timezone.strip()))
        if settings.interval_minutes is not None:
            writes.append(("autonomy_interval_minutes", settings.interval_minutes))
        if settings.care_min_zone is not None:
            writes.append(("autonomy_care_min_zone", settings.care_min_zone))
        for key, value in writes:
            if not config.set_config(key, value):
                return False, "配置写入失败"
        if writes:
            logger.info(t("log.persona.autonomy_config_updated", persona_name=persona_name))
        return True, "ok"

    def set_aside_probability(self, persona_name: str, field: str, value: float) -> tuple[bool, str]:
        """命令补充的三种触发概率。field 是配置键，value 必须在 0 到 1。"""
        if field not in _ASIDE_PROB_FIELDS:
            return False, "未知的命令补充概率项"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, "命令补充概率必须是 0 到 1 的数字"
        number = float(value)
        if number < 0 or number > 1:
            return False, "命令补充概率必须在 0 到 1 之间"
        return self._write_field(
            persona_name,
            field,
            number,
            t("log.persona.personaconfig_updated_aside", persona_name=persona_name, field=field, value=number),
        )

    def set_aside_allow_tools(self, persona_name: str, allow_tools: bool) -> tuple[bool, str]:
        """命令补充能不能使用工具。"""
        if not isinstance(allow_tools, bool):
            return False, "命令补充允许工具必须是布尔值"
        return self._write_field(
            persona_name,
            "aside_allow_tools",
            allow_tools,
            t(
                "log.persona.personaconfig_updated_aside",
                persona_name=persona_name,
                field="aside_allow_tools",
                value=allow_tools,
            ),
        )

    def set_allow_meme_reply(self, persona_name: str, allow: bool) -> tuple[bool, str]:
        """这个人格能不能在回复里发表情。"""
        if not isinstance(allow, bool):
            return False, "允许表情回复必须是布尔值"
        return self._write_field(
            persona_name,
            "allow_meme_reply",
            allow,
            t("log.persona.allow_meme_reply", persona_name=persona_name, value=allow),
        )

    def set_meme_frequency(self, persona_name: str, value: object) -> tuple[bool, str]:
        """设置表情频率（0 到 100）。0 禁用表情发送；其余档位同时派生默认发图冷却。"""
        from gsuid_core.ai_core.persona.meme_style import MEME_FREQUENCY_MAX, MEME_FREQUENCY_MIN

        parsed, err = _validate_int_range(value, "表情频率", MEME_FREQUENCY_MIN, MEME_FREQUENCY_MAX)
        if parsed is None:
            return False, err
        return self._write_field(
            persona_name,
            "meme_frequency",
            parsed,
            t("log.persona.meme_frequency_set", persona_name=persona_name, value=parsed),
        )

    def set_meme_send_cooldown_sec(self, persona_name: str, value: object) -> tuple[bool, str]:
        """设置表情发送冷却（秒）。0 表示跟随档位自适应，>0 为固定秒数。"""
        parsed, err = _validate_int_range(value, "表情发送冷却", 0, 3600, unit="秒")
        if parsed is None:
            return False, err
        return self._write_field(
            persona_name,
            "meme_send_cooldown_sec",
            parsed,
            t("log.persona.meme_cooldown_set", persona_name=persona_name, value=parsed),
        )

    def set_inspect_interval(self, persona_name: str, inspect_interval: int) -> tuple[bool, str]:
        """设置 Persona 的定时巡检间隔（分钟）。"""
        if inspect_interval not in _INSPECT_INTERVAL_VALUES:
            return False, "无效的巡检间隔，可选值: 5, 10, 15, 30, 60"
        return self._write_field(
            persona_name,
            "inspect_interval",
            inspect_interval,
            t(
                "log.persona.personaconfig_updated_name_inspection",
                persona_name=persona_name,
                inspect_interval=inspect_interval,
            ),
        )

    def set_chat_style(self, persona_name: str, chat_style: int) -> tuple[bool, str]:
        """设置说话强度。0 到 100。新会话才用新的档。"""
        from gsuid_core.ai_core.persona.chat_style import CHAT_STYLE_MAX, CHAT_STYLE_MIN

        parsed, err = _validate_int_range(chat_style, "说话强度", CHAT_STYLE_MIN, CHAT_STYLE_MAX)
        if parsed is None:
            return False, err
        return self._write_field(
            persona_name,
            "chat_style",
            parsed,
            t("log.persona.chat_style_set", persona_name=persona_name, chat_style=parsed),
        )

    def set_speech_len_soft(self, persona_name: str, count: int) -> tuple[bool, str]:
        """设置建议字数。等于模板默认时，发送仍跟随说话强度。"""
        from gsuid_core.ai_core.persona.chat_style import SPEECH_LEN_MAX, SPEECH_LEN_MIN

        parsed, err = _validate_int_range(count, "字数", SPEECH_LEN_MIN, SPEECH_LEN_MAX)
        if parsed is None:
            return False, err
        return self._write_field(
            persona_name,
            "speech_len_soft",
            parsed,
            t("log.persona.speech_len_soft_set", persona_name=persona_name, count=parsed),
        )

    def set_keywords(self, persona_name: str, keywords: List[str]) -> tuple[bool, str]:
        """设置 Persona 的唤醒关键词。"""
        return self._write_field(
            persona_name,
            "keywords",
            keywords,
            t("log.persona.personaconfig_updated_name_wake", persona_name=persona_name, keywords=keywords),
        )

    def set_enabled_tools(self, persona_name: str, enabled_tools: List[str]) -> tuple[bool, str]:
        """设置该人格进入向量检索池的插件名单（含 * / !插件名）。不做注册表强校验。"""
        cleaned = _dedupe_str_list(enabled_tools)
        return self._write_field(
            persona_name,
            "enabled_tools",
            cleaned,
            t("log.persona.personaconfig_updated_name_enabled_tools", persona_name=persona_name, cleaned=cleaned),
        )

    def set_tool_names(self, persona_name: str, tool_names: List[str]) -> tuple[bool, str]:
        """设置 Persona 的显式工具白名单（并入保底池，不校验工具是否已注册）。"""
        cleaned = _dedupe_str_list(tool_names)
        return self._write_field(
            persona_name,
            "tool_names",
            cleaned,
            t("log.persona.personaconfig_updated_name_explicit", persona_name=persona_name, cleaned=cleaned),
        )

    def set_capability_agents(self, persona_name: str, agents: List[str]) -> tuple[bool, str]:
        """设置该人格可委派的能力代理 node_id（含 * / !node_id）。"""
        cleaned = _dedupe_str_list(agents)
        return self._write_field(
            persona_name,
            "capability_agents",
            cleaned,
            t("log.persona.personaconfig_updated_name_agents", persona_name=persona_name, cleaned=cleaned),
        )

    def set_prompt_group(self, persona_name: str, group_name: str) -> tuple[bool, str]:
        """绑定该人格使用的系统提示词组。组必须已存在。"""
        from gsuid_core.ai_core.prompt_group.store import group_exists

        name = group_name.strip()
        if not group_exists(name):
            return False, "系统提示词组不存在"
        return self._write_field(
            persona_name,
            "prompt_group",
            name,
            t("log.persona.prompt_group_bound", persona_name=persona_name, group=name),
        )

    def get_persona_for_session(self, session_id: str) -> Optional[str]:
        """根据 Session ID 路由人格：HTTP 钉选 → 私聊租约 → 通道精准匹配 → 通道全局兜底。"""
        group_id: Optional[str] = None
        user_id: Optional[str] = None
        is_private_chat = False

        parts = session_id.split(":", 4)
        if len(parts) != 5 or parts[3] not in ("group", "private"):
            raise ValueError(f"Invalid session_id format: {session_id}")
        if parts[3] == "group":
            group_id = parts[4]
        else:
            user_id = parts[4]
            is_private_chat = True

        all_configs = self.get_all_configs()

        if parts[0] == "HTTP_AGENT":
            from gsuid_core.ai_core.http_agent.persona import get_http_persona_override

            override_persona = get_http_persona_override(session_id)
            if override_persona and override_persona in all_configs:
                return override_persona

        # 私聊租约优先：主动私聊签约的人格承接后续私聊回复（私聊范围不再覆盖该用户时失效）
        if is_private_chat and user_id:
            from gsuid_core.ai_core.proactive.dm_lease import get_active_dm_lease

            lease = get_active_dm_lease(user_id, parts[1])
            if lease is not None and lease.persona_name in all_configs:
                if persona_covers_session(all_configs[lease.persona_name], user_id=user_id):
                    return lease.persona_name

        for persona_name, config in all_configs.items():
            scopes = read_channel_scopes(config)
            if group_id and scopes.group_scope == "specific":
                if group_id in read_target_list(config, "target_groups"):
                    return persona_name
            if is_private_chat and user_id and scopes.private_scope == "specific":
                if user_id in read_target_list(config, "target_users"):
                    return persona_name

        return self.get_fallback_persona(is_private=is_private_chat)

    def assign_target_to_persona(
        self,
        persona_name: str,
        target_id: str,
        *,
        is_private: bool,
    ) -> tuple[bool, str]:
        """将群聊或私聊目标切换绑定到指定人格（等价于控制台调整目标列表）。"""
        tid = target_id.strip()
        if not tid:
            return False, "未获取到当前群聊或用户 ID，无法切换人格。"

        all_configs = self.get_all_configs()
        if persona_name not in all_configs:
            return False, f"未找到人格：{persona_name}"

        target_cfg = all_configs[persona_name]
        target_scopes = read_channel_scopes(target_cfg)
        if not target_scopes.is_enabled:
            return False, f"人格「{persona_name}」当前未启用，请先在控制台启用后再切换。"

        channel_scope = target_scopes.private_scope if is_private else target_scopes.group_scope
        if channel_scope == "off":
            channel_label = "私聊" if is_private else "群聊"
            return False, f"人格「{persona_name}」未在{channel_label}通道启用，无法在{channel_label}中绑定。"

        target_key = "target_users" if is_private else "target_groups"
        setter = self.set_target_users if is_private else self.set_target_groups

        for other_name, other_cfg in all_configs.items():
            if other_name == persona_name:
                continue
            other_scopes = read_channel_scopes(other_cfg)
            if (other_scopes.private_scope if is_private else other_scopes.group_scope) != "specific":
                continue
            current_other = read_target_list(other_cfg, target_key)
            if tid in current_other:
                ok, msg = setter(other_name, [g for g in current_other if g != tid])
                if not ok:
                    return False, msg

        if channel_scope == "specific":
            current_target = read_target_list(target_cfg, target_key)
            if tid not in current_target:
                ok, msg = setter(persona_name, [*current_target, tid])
                if not ok:
                    return False, msg

        return True, "ok"

    def get_persona_config_dict(self, persona_name: str) -> Optional[Dict[str, object]]:
        """获取指定 Persona 的配置字典（用于 API 返回）；不存在则返回 None。"""
        if not self._get_config_path(persona_name).exists():
            return None

        config = self.get_config(persona_name)
        from gsuid_core.ai_core.persona.chat_style import DEFAULT_CHAT_STYLE, stored_int, speech_shape_spec
        from gsuid_core.ai_core.persona.meme_style import DEFAULT_MEME_FREQUENCY

        shape = speech_shape_spec()
        autonomy = read_autonomy_runtime(config)
        traits = read_drive_traits(config)
        scopes = read_channel_scopes(config)
        return {
            "ai_mode": normalize_ai_modes(list(config.get_config("ai_mode").data)),
            "group_scope": scopes.group_scope,
            "private_scope": scopes.private_scope,
            "scope": derive_legacy_scope(scopes.group_scope, scopes.private_scope),
            "target_groups": read_target_list(config, "target_groups"),
            "target_users": read_target_list(config, "target_users"),
            "inspect_interval": config.get_config("inspect_interval").data,
            "keywords": config.get_config("keywords").data,
            "active_hours": list(autonomy.active_hours),
            "timezone": autonomy.timezone,
            "autonomy_interval_minutes": autonomy.interval_minutes,
            "autonomy_care_min_zone": autonomy.care_min_zone,
            "aside_image_probability": config.get_config("aside_image_probability").data,
            "aside_text_probability": config.get_config("aside_text_probability").data,
            "aside_mixed_probability": config.get_config("aside_mixed_probability").data,
            "aside_allow_tools": config.get_config("aside_allow_tools").data,
            "aside_skip_plugins": (
                config.get_config("aside_skip_plugins").data
                if "aside_skip_plugins" in config.config
                else ["core_command"]
            ),
            "aside_cooldown_seconds": (
                config.get_config("aside_cooldown_seconds").data if "aside_cooldown_seconds" in config.config else 120.0
            ),
            "enabled_tools": config.get_config("enabled_tools").data,
            "tool_names": config.get_config("tool_names").data,
            "capability_agents": config.get_config("capability_agents").data,
            "prompt_group": _effective_prompt_group(persona_name),
            "allow_meme_reply": bool(config.get_config("allow_meme_reply").data),
            "meme_frequency": stored_int(config, "meme_frequency", DEFAULT_MEME_FREQUENCY),
            "meme_send_cooldown_sec": stored_int(config, "meme_send_cooldown_sec", 0),
            "chat_style": stored_int(config, "chat_style", DEFAULT_CHAT_STYLE),
            "speech_len_soft": stored_int(config, "speech_len_soft", shape["soft_default"]),
            "speech_shape": shape,
            "social_initiative": traits.social_initiative,
            "energy_endurance": traits.energy_endurance,
            "curiosity_focus": traits.curiosity_focus,
            "arc_persistence": traits.arc_persistence,
        }

    def delete_persona_config(self, persona_name: str) -> bool:
        """删除 Persona 的配置文件。"""
        if persona_name in self._cache:
            del self._cache[persona_name]

        config_path = self._get_config_path(persona_name)
        if config_path.exists():
            try:
                config_path.unlink()
                logger.info(t("log.persona.personaconfig_deleted_name_config", persona_name=persona_name))
                return True
            except Exception as e:
                logger.error(t("log.persona.personaconfig_delete_name_config", persona_name=persona_name, e=e))
                return False
        return True


# 全局实例
persona_config_manager = PersonaConfigManager()


def persona_allows_meme_reply(persona_name: str | None) -> bool:
    """人格配置「允许表情回复」。没有人格名时视为关闭。"""
    if not persona_name:
        return False
    try:
        config = persona_config_manager.get_config(persona_name)
        return bool(config.get_config("allow_meme_reply").data)
    except (ValueError, OSError, KeyError):
        return True
