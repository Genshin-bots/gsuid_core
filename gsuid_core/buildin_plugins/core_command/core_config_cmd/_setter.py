from typing import Set, Optional

from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsListConfig,
    GsTimeConfig,
    GsTimeRConfig,
    GsListStrConfig,
)

_BOOL_ON = {"开", "开启", "true", "on", "1", "是"}
_BOOL_OFF = {"关", "关闭", "false", "off", "0", "否"}
_CLEAR = {"空", "none", "clear", "清空"}
_AREA_ALIAS = {"全部": "ALL", "群聊": "GROUP", "私聊": "DIRECT"}
_LIST_HINT = "黑白名单用法: 直接输入=添加, -xxx=移除, =xxx=覆盖, 清空=清空"

_INT_PARAMS = {
    "优先级": "priority",
    "priority": "priority",
}

PLUGIN_AREA = {"GROUP", "DIRECT", "ALL", "SV"}
PLUGIN_PARAMS = f"开关, 权限, 优先级, 作用范围, 前缀, 别名, 黑名单, 白名单\n{_LIST_HINT}"
PLUGIN_EXTRA = {"前缀": "prefix", "prefix": "prefix", "别名": "alias", "alias": "alias"}

SV_AREA = {"GROUP", "DIRECT", "ALL"}
SV_PARAMS = f"开关, 权限, 优先级, 作用范围, 黑名单, 白名单\n{_LIST_HINT}"


def _split(raw: str):
    return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]


def parse_val(item: GSC, raw: str):
    """把用户输入解析成与配置项匹配的值, 失败返回 None"""
    if isinstance(item, GsBoolConfig):
        if raw.lower() in _BOOL_ON:
            return True
        if raw.lower() in _BOOL_OFF:
            return False
    elif isinstance(item, GsIntConfig):
        try:
            v = int(raw)
            if item.options and v not in item.options:
                return None
            if item.max_value is not None and v > item.max_value:
                return None
            return v
        except ValueError:
            pass
    elif isinstance(item, (GsStrConfig, GsTimeConfig)):
        if raw.lower() in _CLEAR:
            return ""
        if isinstance(item, GsStrConfig) and item.options and raw not in item.options:
            return None
        return raw
    elif isinstance(item, GsListStrConfig):
        if raw.lower() in _CLEAR:
            return []
        vals = _split(raw)
        if item.options and any(v not in item.options for v in vals):
            return None
        return vals
    elif isinstance(item, GsListConfig):
        if raw.lower() in _CLEAR:
            return []
        try:
            return [int(x) for x in _split(raw)]
        except ValueError:
            pass
    elif isinstance(item, GsTimeRConfig):
        try:
            h, m = int(raw.split(":")[0]), int(raw.split(":")[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return [h, m]
        except (ValueError, IndexError):
            pass
    return None


def _parse_hint(item: GSC) -> str:
    """根据配置项类型生成输入格式提示"""
    if isinstance(item, GsBoolConfig):
        return "请输入: 开启 / 关闭"
    if isinstance(item, GsIntConfig):
        parts = ["请输入整数"]
        if item.max_value is not None:
            parts.append(f"最大 {item.max_value}")
        if item.options:
            parts.append(f"可选: {', '.join(str(o) for o in item.options)}")
        return ", ".join(parts)
    if isinstance(item, GsStrConfig):
        if item.options:
            return f"可选值: {', '.join(item.options)}"
        return "请输入字符串"
    if isinstance(item, GsListStrConfig):
        hint = "请输入逗号分隔的字符串, 或输入 清空"
        if item.options:
            hint += f"\n可选值: {', '.join(item.options)}"
        return hint
    if isinstance(item, GsListConfig):
        return "请输入逗号分隔的整数"
    if isinstance(item, GsTimeRConfig):
        return "请输入时间格式 HH:MM, 例如 01:05"
    return "请检查输入格式"


def _apply_list(target, label: str, name: str, field: str, raw_value: str) -> str:
    current: list = getattr(target, field, [])
    current_set = set(current)

    if raw_value.lower() in _CLEAR:
        target.set(is_lazy=False, **{field: []})
        return f"{label} {name}: 已清空"

    if raw_value.startswith("="):
        vals = _split(raw_value[1:])
        target.set(is_lazy=False, **{field: vals})
        return f"{label} {name} 已设为: {', '.join(vals)}"

    if raw_value.startswith("-"):
        items = _split(raw_value[1:])
        hit = [x for x in items if x in current_set]
        miss = [x for x in items if x not in current_set]
        target.set(is_lazy=False, **{field: [x for x in current if x not in set(items)]})
        parts = []
        if hit:
            parts.append(f"已移除: {', '.join(hit)}")
        if miss:
            parts.append(f"不存在: {', '.join(miss)}")
        return f"{label} {name} {'; '.join(parts)}"

    items = _split(raw_value.lstrip("+"))
    added = [x for x in items if x not in current_set]
    duped = [x for x in items if x in current_set]
    target.set(is_lazy=False, **{field: current + added})
    parts = []
    if added:
        parts.append(f"已添加: {', '.join(added)}")
    if duped:
        parts.append(f"已存在: {', '.join(duped)}")
    return f"{label} {name} {'; '.join(parts)}"


def apply_set(
    target,
    label: str,
    param: str,
    raw_value: str,
    area_values: Set[str],
    all_params: str,
    extra_params: Optional[dict] = None,
) -> str:
    """对插件/命令执行属性设置, 返回结果消息"""
    if param in ("开关", "enabled"):
        v = raw_value.lower()
        if v in _BOOL_ON:
            target.set(is_lazy=False, enabled=True)
            return f"{label} 已开启"
        if v in _BOOL_OFF:
            target.set(is_lazy=False, enabled=False)
            return f"{label} 已关闭"
        return "开关值请输入: 开启 / 关闭"

    if param in ("权限", "pm"):
        try:
            v = int(raw_value)
        except ValueError:
            return "权限必须是整数 (0=master, 1=superuser, 2=管理员, 6=普通用户)"
        if not 0 <= v <= 6:
            return "权限范围: 0-6 (0=master, 1=superuser, 2=管理员, 6=普通用户)"
        target.set(is_lazy=False, pm=v)
        return f"{label} 权限已设置为 {v}"

    if param in _INT_PARAMS:
        field = _INT_PARAMS[param]
        try:
            target.set(is_lazy=False, **{field: int(raw_value)})
            return f"{label} {param}已设置为 {raw_value}"
        except ValueError:
            return f"{param}必须是整数"

    if param in ("作用范围", "area"):
        val = _AREA_ALIAS.get(raw_value, raw_value.upper())
        if val not in area_values:
            return f"作用范围可选: {', '.join(sorted(area_values))} (全部/群聊/私聊)"
        target.set(is_lazy=False, area=val)
        return f"{label} 作用范围已设置为 {val}"

    if param in ("黑名单", "black_list"):
        return _apply_list(target, label, "黑名单", "black_list", raw_value)

    if param in ("白名单", "white_list"):
        return _apply_list(target, label, "白名单", "white_list", raw_value)

    if extra_params and param in extra_params:
        field = extra_params[param]
        vals = [] if raw_value.lower() in _CLEAR else _split(raw_value)
        target.set(is_lazy=False, **{field: vals})
        result = f"{label} {param}: {'已清空' if not vals else ', '.join(vals)}"
        if field == "prefix":
            result += "\n(前缀修改需重启后生效)"
        return result

    return f"未知参数: {param}\n可用: {all_params}"


def set_config_val(
    cfg,
    plugin_name: str,
    key: str,
    raw_value: str,
    is_skip,
    fmt,
) -> str:
    """设置配置项, is_skip/fmt 由调用方注入, 避免依赖渲染层"""
    item = cfg.config[key]

    if is_skip(item):
        return "该类型配置请使用 WebConsole 修改"

    value = parse_val(item, raw_value)
    if value is None:
        return f"值解析失败: {key}\n  输入: {raw_value}\n  {_parse_hint(item)}"

    if cfg.set_config(key, value):
        return f"[{plugin_name}] {key} 已设置为: {fmt(cfg.config[key], key)}"
    return "设置失败, 类型不匹配"
