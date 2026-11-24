"""Persona 配置的无状态规则、值对象与解析校验（零磁盘 I/O，无循环依赖）。"""

from __future__ import annotations

import re
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass
from collections.abc import Mapping

from gsuid_core.utils.plugins_config.gs_config import StringConfig

CHANNEL_SCOPE_VALUES: tuple[str, ...] = ("off", "specific", "all")
PERSONA_SCOPE_VALUES: tuple[str, ...] = (
    "disabled",
    "global",
    "specific",
    "global_group",
    "global_private",
)


@dataclass(frozen=True, slots=True)
class ChannelScopes:
    """群聊与私聊两个正交通道的启用范围。"""

    group_scope: str = "off"
    private_scope: str = "off"

    @property
    def is_enabled(self) -> bool:
        return self.group_scope != "off" or self.private_scope != "off"


@dataclass(frozen=True, slots=True)
class ChannelDeploymentSnapshot:
    """部署态范围快照：用于整包/git 覆盖导入时保留本地启用范围与目标列表。"""

    group_scope: str
    private_scope: str
    target_groups: tuple[str, ...]
    target_users: tuple[str, ...]


EMPTY_CHANNEL_DEPLOYMENT = ChannelDeploymentSnapshot(
    group_scope="off",
    private_scope="off",
    target_groups=(),
    target_users=(),
)


# 命令回复发出后，按概率用人设再补一句。默认不在 ai_mode 里，所以关闭。
ASIDE_MODE = "命令补充"
# 改名前写进人格配置的名字。读到时仍算开启。
ASIDE_MODE_ALIASES = frozenset({"余韵点评", "命令接话"})
AUTONOMY_MODE = "自主行动"
AUTONOMY_MODE_ALIASES = frozenset({"自由活动"})
RETIRED_AI_MODES = frozenset({"困境救场(暂不可用)", "困境救场"})
AI_MODE_OPTIONS = [
    "提及应答",
    "定时巡检",
    ASIDE_MODE,
    AUTONOMY_MODE,
]
CURIOSITY_FOCUS_VALUES = frozenset({"balanced", "people", "analytical", "whimsical"})
_ACTIVE_WINDOW = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
_AUTONOMY_PAYLOAD_KEYS = (
    "active_hours",
    "timezone",
    "autonomy_interval_minutes",
    "autonomy_care_min_zone",
)


def normalize_ai_modes(modes: list[str]) -> list[str]:
    """把改名前的模式名收成现在的名字，并去掉已经下线的项。"""
    out: list[str] = []
    for mode in modes:
        if mode in RETIRED_AI_MODES:
            continue
        if mode in ASIDE_MODE_ALIASES:
            item = ASIDE_MODE
        elif mode in AUTONOMY_MODE_ALIASES:
            item = AUTONOMY_MODE
        else:
            item = mode
        if item not in out:
            out.append(item)
    return out


@dataclass(frozen=True, slots=True)
class DriveTraits:
    """人格驱动四参数（读自 config，坏值回退默认）。"""

    social_initiative: int = 50
    energy_endurance: int = 50
    curiosity_focus: str = "balanced"
    arc_persistence: int = 2


def _clamped_int(raw: object, default: int, low: int, high: int) -> int:
    return min(high, max(low, int(raw))) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else default


def read_drive_traits(config: StringConfig) -> DriveTraits:
    """从人格配置读四参数；老 config.json 缺键或类型不对时全用默认。"""
    focus_raw = config.get_config("curiosity_focus").data
    return DriveTraits(
        social_initiative=_clamped_int(config.get_config("social_initiative").data, 50, 0, 100),
        energy_endurance=_clamped_int(config.get_config("energy_endurance").data, 50, 0, 100),
        curiosity_focus=focus_raw if isinstance(focus_raw, str) and focus_raw in CURIOSITY_FOCUS_VALUES else "balanced",
        arc_persistence=_clamped_int(config.get_config("arc_persistence").data, 2, 1, 3),
    )


def parse_active_window(text: str) -> tuple[int, int] | None:
    """返回开始、结束的当天分钟数。格式不对则返回 None。"""
    matched = _ACTIVE_WINDOW.fullmatch(text.strip())
    if matched is None:
        return None
    return (
        int(matched.group(1)) * 60 + int(matched.group(2)),
        int(matched.group(3)) * 60 + int(matched.group(4)),
    )


def is_within_active_hours(
    active_hours: list[str],
    tz_name: str,
    now: datetime | None = None,
) -> bool:
    """作息闭区间。跨午夜用「晚于开始或早于结束」。全非法或时区无效则视为不在作息内。"""
    zone_name = tz_name.strip()
    if not active_hours or not zone_name:
        return False
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        return False
    clock = datetime.now(zone) if now is None else now
    clock = clock.replace(tzinfo=zone) if clock.tzinfo is None else clock.astimezone(zone)
    minute = clock.hour * 60 + clock.minute
    for raw in active_hours:
        parsed = parse_active_window(raw) if isinstance(raw, str) else None
        if parsed is None:
            continue
        start, end = parsed
        if (start <= minute <= end) if start <= end else (minute >= start or minute <= end):
            return True
    return False


_LEGACY_TO_CHANNELS: dict[str, ChannelScopes] = {
    "global": ChannelScopes(group_scope="all", private_scope="all"),
    "global_group": ChannelScopes(group_scope="all", private_scope="off"),
    "global_private": ChannelScopes(group_scope="off", private_scope="all"),
    "specific": ChannelScopes(group_scope="specific", private_scope="off"),
}


def legacy_scope_to_channels(scope: str) -> ChannelScopes:
    """将旧单字段 scope 映射为双通道策略（方案 A：specific 仅迁群聊，私聊置 off）。"""
    return _LEGACY_TO_CHANNELS[scope] if scope in _LEGACY_TO_CHANNELS else ChannelScopes()


def derive_legacy_scope(group_scope: str, private_scope: str) -> str:
    """从双通道策略派生兼容旧字段 scope（保留给旧客户端与版本回滚）。"""
    if group_scope == "off" and private_scope == "off":
        return "disabled"
    if group_scope == "all" and private_scope == "all":
        return "global"
    if group_scope == "all" and private_scope == "off":
        return "global_group"
    if group_scope == "off" and private_scope == "all":
        return "global_private"
    return "specific"


def _normalize_channel_scope(raw: object) -> str:
    return raw if isinstance(raw, str) and raw in CHANNEL_SCOPE_VALUES else "off"


def read_channel_scopes(config: StringConfig) -> ChannelScopes:
    """读取人格的双通道启用范围；无新键时回退旧 scope 推导。"""
    has_group = "group_scope" in config.config
    has_private = "private_scope" in config.config
    if has_group or has_private:
        raw_group = config.config["group_scope"].data if has_group else "off"
        raw_private = config.config["private_scope"].data if has_private else "off"
        return ChannelScopes(
            group_scope=_normalize_channel_scope(raw_group),
            private_scope=_normalize_channel_scope(raw_private),
        )
    raw_legacy = config.config["scope"].data if "scope" in config.config else "disabled"
    return legacy_scope_to_channels(raw_legacy if isinstance(raw_legacy, str) else "disabled")


def read_target_list(config: StringConfig, key: str) -> list[str]:
    """安全读取目标群聊/私聊列表并去重去空白。"""
    if key not in config.config:
        return []
    raw = config.config[key].data
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(item.strip() for item in raw if isinstance(item, str) and item.strip()))


def is_persona_enabled(config: StringConfig) -> bool:
    """任一通道非 off 即视为人格已启用。"""
    return read_channel_scopes(config).is_enabled


def persona_covers_session(
    config: StringConfig,
    *,
    group_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> bool:
    """统一会话覆盖判定：路由、巡检与自主行动共用此真值源。"""
    scopes = read_channel_scopes(config)
    if group_id:
        if scopes.group_scope == "all":
            return True
        return scopes.group_scope == "specific" and group_id in read_target_list(config, "target_groups")
    if scopes.private_scope == "all":
        return True
    return scopes.private_scope == "specific" and bool(user_id) and user_id in read_target_list(config, "target_users")


@dataclass(frozen=True, slots=True)
class AutonomyRuntime:
    """读出来的自主行动配置。缺字段时用默认，坏掉的作息留空以便外面判为不在时段内。"""

    active_hours: tuple[str, ...]
    timezone: str
    interval_minutes: int
    care_min_zone: str


@dataclass(frozen=True, slots=True)
class AutonomySettings:
    """只填要改的项。None 表示不动。"""

    active_hours: list[str] | None = None
    timezone: str | None = None
    interval_minutes: int | None = None
    care_min_zone: str | None = None


def _zone_ok(name: str) -> bool:
    stripped = name.strip()
    if not stripped:
        return False
    try:
        ZoneInfo(stripped)
    except ZoneInfoNotFoundError:
        return False
    return True


def _in_int(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _stored_str(config: StringConfig, key: str, default: str) -> str:
    if key not in config.config:
        return default
    raw = config.get_config(key).data
    return raw if isinstance(raw, str) else default


def _stored_int(config: StringConfig, key: str, default: int, low: int, high: int) -> int:
    if key not in config.config:
        return default
    raw = config.get_config(key).data
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return raw if low <= raw <= high else default


def read_autonomy_runtime(config: StringConfig) -> AutonomyRuntime:
    hours_raw = config.get_config("active_hours").data
    hours = (
        [item.strip() for item in hours_raw if isinstance(item, str) and parse_active_window(item) is not None]
        if isinstance(hours_raw, list)
        else []
    )
    care = _stored_str(config, "autonomy_care_min_zone", "familiar")
    return AutonomyRuntime(
        active_hours=tuple(hours),
        timezone=_stored_str(config, "timezone", "Asia/Shanghai"),
        interval_minutes=_stored_int(config, "autonomy_interval_minutes", 45, 15, 720),
        care_min_zone=care if care in {"familiar", "close"} else "close",
    )


def _read_str(payload: Mapping[str, object], key: str) -> tuple[str | None, str]:
    if key not in payload:
        return None, ""
    value = payload[key]
    return (value, "") if isinstance(value, str) else (None, f"字段 {key} 必须是字符串")


def _read_int(payload: Mapping[str, object], key: str) -> tuple[int | None, str]:
    if key not in payload:
        return None, ""
    value = payload[key]
    return (None, f"字段 {key} 必须是整数") if isinstance(value, bool) or not isinstance(value, int) else (value, "")


def _read_str_list(payload: Mapping[str, object], key: str) -> tuple[list[str] | None, str]:
    if key not in payload:
        return None, ""
    value = payload[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None, f"字段 {key} 必须是字符串列表"
    return [str(item) for item in value], ""


def autonomy_settings_error(settings: AutonomySettings) -> str:
    if settings.active_hours is not None:
        if not settings.active_hours:
            return "作息至少要有一段"
        if any(parse_active_window(item) is None for item in settings.active_hours):
            return "作息时段要写成 HH:MM-HH:MM"
    if settings.timezone is not None and not _zone_ok(settings.timezone):
        return "时区名称无效"
    if settings.interval_minutes is not None and not _in_int(settings.interval_minutes, 15, 720):
        return "自主行动间隔要在 15 到 720 分钟"
    if settings.care_min_zone is not None and settings.care_min_zone not in {"familiar", "close"}:
        return "关心温区只能是 familiar 或 close"
    return ""


def autonomy_settings_from_mapping(payload: Mapping[str, object]) -> tuple[AutonomySettings | None, str]:
    """从控制台请求里取出要改的自主行动字段。没有这些键时返回 None。"""
    if not any(key in payload for key in _AUTONOMY_PAYLOAD_KEYS):
        return None, ""
    hours, hours_error = _read_str_list(payload, "active_hours")
    timezone, timezone_error = _read_str(payload, "timezone")
    care, care_error = _read_str(payload, "autonomy_care_min_zone")
    interval, interval_error = _read_int(payload, "autonomy_interval_minutes")
    for error in (hours_error, timezone_error, care_error, interval_error):
        if error:
            return None, error
    settings = AutonomySettings(
        active_hours=hours,
        timezone=timezone,
        interval_minutes=interval,
        care_min_zone=care,
    )
    return settings, autonomy_settings_error(settings)
