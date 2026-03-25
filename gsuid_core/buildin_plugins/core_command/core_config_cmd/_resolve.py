from typing import Dict, Optional

from gsuid_core.sv import SL, get_plugin_available_prefix
from gsuid_core.utils.plugins_update.api import PLUGINS_PATH
from gsuid_core.utils.plugins_update._plugins import plugins_list, refresh_list
from gsuid_core.utils.plugins_config.gs_config import StringConfig, all_config_list


def prefix() -> str:
    return get_plugin_available_prefix("core_command")


def hint(cmd: str) -> str:
    return f"发送 [{prefix()}{cmd}]"


async def resolve_plugin(name: str) -> Optional[str]:
    """将输入(插件名或别名)解析为 SL.plugins 中的 key"""
    if name in SL.plugins:
        return name
    lo = name.lower()
    for key, p in SL.plugins.items():
        if key.lower() == lo or any(a.lower() == lo for a in p.alias):
            return key

    if not plugins_list:
        await refresh_list()
    for _n, plugin in plugins_list.items():
        if "alias" in plugin:
            for alias in plugin["alias"]:
                if lo == alias.lower():
                    lo = _n.lower()
                    break

    for _n in PLUGINS_PATH.iterdir():
        if lo == _n.name.lower():
            return _n.name
    return None


def resolve_sv(name: str):
    """按名称查找 SV, 支持大小写不敏感, 返回 (name, sv)"""
    sv = SL.lst.get(name)
    if sv:
        return name, sv
    lo = name.lower()
    for key, s in SL.lst.items():
        if key.lower() == lo:
            return key, s
    return name, None


def cfg_match(cfg: StringConfig, name: str) -> bool:
    """判断配置是否属于指定插件/配置名"""
    return cfg.plugin_name == name or (cfg.plugin_name is None and cfg.config_name == name)


def find_config_item(plugin_name: str, key: str):
    """查找配置项, 返回 (StringConfig, GSC) 或 None"""
    for cfg in all_config_list.values():
        if cfg_match(cfg, plugin_name) and key in cfg.config:
            return cfg, cfg.config[key]
    return None


def plugin_configs(name: str) -> Dict[str, StringConfig]:
    return {k: v for k, v in all_config_list.items() if cfg_match(v, name)}


def all_config_names():
    """返回所有可配置的名称集合"""
    return {c.plugin_name for c in all_config_list.values() if c.plugin_name} | {
        c.config_name for c in all_config_list.values() if c.plugin_name is None
    }


def match_config_name(text: str):
    """从文本中最长前缀匹配已知配置名, 返回 (name, rest) 或 None"""
    names = all_config_names()
    best = None
    for name in names:
        if text == name or text.startswith(name + " "):
            if best is None or len(name) > len(best):
                best = name
    if best is None:
        return None
    return best, text[len(best) :].strip()
