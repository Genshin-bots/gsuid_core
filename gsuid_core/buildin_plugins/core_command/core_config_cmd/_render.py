from typing import List

from gsuid_core.sv import SL
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsBoolConfig,
    GsDictConfig,
    GsImageConfig,
    GsTimeRConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

_SENSITIVE_KEYS = {"token", "key", "secret", "password", "密钥", "口令", "apikey", "api_key"}
_SKIP_TYPES = (GsDictConfig, GsImageConfig)


def is_skip_type(item) -> bool:
    return isinstance(item, _SKIP_TYPES)


def _join(lst) -> str:
    return ", ".join(str(i) for i in lst) if lst else "无"


def fmt_val(item: GSC, key: str = "") -> str:
    """格式化配置项的值, 敏感字段脱敏"""
    if isinstance(item, GsBoolConfig):
        return "开启" if item.data else "关闭"
    if isinstance(item, GsTimeRConfig):
        return f"{item.data[0]:02d}:{item.data[1]:02d}"
    val = str(item.data)
    if key and val and any(s in key.lower() for s in _SENSITIVE_KEYS):
        return val[:2] + "****" + val[-2:] if len(val) > 4 else "****"
    return val


def render_config_list(names: List[str]) -> str:
    return "可配置插件列表:\n" + "\n".join(f"  · {n}" for n in names)


def render_config_items(plugin_name: str, configs: dict) -> str:
    lines = [f"[{plugin_name}] 配置项:"]
    for cfg_name, cfg in configs.items():
        lines.append(f"\n  ── {cfg_name} ──")
        for key, item in cfg.config.items():
            if isinstance(item, _SKIP_TYPES):
                tag = "Dict" if isinstance(item, GsDictConfig) else "Image"
                lines.append(f"  {key}  [{tag}]  {item.title}  (请使用WebConsole修改)")
            else:
                tag = type(item).__name__.replace("Gs", "").replace("Config", "")
                lines.append(f"  {key}  [{tag}]  {item.title}  = {fmt_val(item, key)}")
    return "\n".join(lines)


def render_config_detail(plugin_name: str, key: str, cfg: StringConfig, item: GSC) -> str:
    return (
        f"[{plugin_name}] {key}\n"
        f"  配置组: {cfg.config_name}\n"
        f"  标题: {item.title}\n"
        f"  描述: {item.desc}\n"
        f"  类型: {type(item).__name__}\n"
        f"  当前值: {fmt_val(item, key)}"
    )


def render_plugin_list() -> str:
    lines = ["已加载插件列表:"]
    for name, p in sorted(SL.plugins.items()):
        status = "ON" if p.enabled else "OFF"
        alias = f"  ({', '.join(p.alias)})" if p.alias else ""
        lines.append(f"  [{status}] {name}  pm={p.pm}{alias}")
    return "\n".join(lines)


def render_plugin_detail(plugin_name: str, p) -> str:
    return "\n".join(
        [
            f"插件 [{plugin_name}]",
            f"  状态: {'开启' if p.enabled else '关闭'}  权限: {p.pm}  优先级: {p.priority}",
            f"  作用范围: {p.area}",
            f"  别名: {_join(p.alias)}",
            f"  前缀: {_join(p.prefix)}",
            f"  强制前缀: {_join(p.force_prefix)}",
            f"  黑名单: {_join(p.black_list)}",
            f"  白名单: {_join(p.white_list)}",
        ]
    )


def render_sv_list(plugin_name: str = "") -> str:
    if plugin_name:
        lines = [f"[{plugin_name}] 命令列表:"]
        for name, sv in sorted(SL.lst.items()):
            if sv.self_plugin_name == plugin_name:
                status = "ON" if sv.enabled else "OFF"
                lines.append(f"  [{status}] {name}  pm={sv.pm}")
        return "\n".join(lines)
    lines = ["已加载命令列表:"]
    for name, sv in sorted(SL.lst.items()):
        status = "ON" if sv.enabled else "OFF"
        lines.append(f"  [{status}] {name}  pm={sv.pm}  ({sv.self_plugin_name})")
    return "\n".join(lines)


def render_sv_detail(sv) -> str:
    return (
        f"命令 [{sv.name}]\n"
        f"  所属插件: {sv.self_plugin_name}\n"
        f"  状态: {'开启' if sv.enabled else '关闭'}  权限: {sv.pm}  优先级: {sv.priority}\n"
        f"  作用范围: {sv.area}\n"
        f"  黑名单: {_join(sv.black_list)}\n"
        f"  白名单: {_join(sv.white_list)}"
    )
