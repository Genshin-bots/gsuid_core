"""基础设施插件的登记与可选调用。

Core 只负责先加载，并把 ``<目录名>.api`` 挂进 ``sys.modules``。
类型写在插件自己的 ``api`` 上。
"""

from __future__ import annotations

import sys
import importlib
from types import ModuleType
from typing import Dict

from gsuid_core.i18n import t
from gsuid_core.logger import logger


class MetaPluginNotFoundError(RuntimeError):
    """``require`` 时对应 provides 尚未登记。"""

    def __init__(self, provides: str) -> None:
        self.provides = provides
        super().__init__(f"基础设施插件 {provides!r} 未加载。")


_PROVIDES_TO_MODULE: Dict[str, ModuleType] = {}
_PLUGIN_TO_PROVIDES: Dict[str, str] = {}
_PLUGIN_ALIASES: Dict[str, list[str]] = {}


def register_meta_plugin(provides: str, module: ModuleType, plugin_name: str) -> None:
    """登记 api 模块，并挂上 ``<plugin_name>`` / ``<plugin_name>.api`` 供正常 import。"""
    stale = [name for name, key in _PLUGIN_TO_PROVIDES.items() if key == provides and name != plugin_name]
    for name in stale:
        _drop_aliases(name)
        del _PLUGIN_TO_PROVIDES[name]
    _PROVIDES_TO_MODULE[provides] = module
    _PLUGIN_TO_PROVIDES[plugin_name] = provides
    _install_aliases(plugin_name, module)
    logger.debug(t("log.server.meta_plugin_register", plugin_name=plugin_name, provides=provides))


def _install_aliases(plugin_name: str, api_module: ModuleType) -> None:
    keys = [f"{plugin_name}.api"]
    sys.modules[f"{plugin_name}.api"] = api_module
    parent_qual = api_module.__name__.rsplit(".", 1)[0] if "." in api_module.__name__ else ""
    if parent_qual and parent_qual in sys.modules:
        sys.modules[plugin_name] = sys.modules[parent_qual]
        keys.append(plugin_name)
    _PLUGIN_ALIASES[plugin_name] = keys


def _drop_aliases(plugin_name: str) -> None:
    for key in _PLUGIN_ALIASES.pop(plugin_name, []):
        if key in sys.modules:
            del sys.modules[key]


def unregister_meta_plugin(plugin_name: str) -> None:
    """按插件目录名摘掉登记与短路 import。"""
    _drop_aliases(plugin_name)
    provides = _PLUGIN_TO_PROVIDES.pop(plugin_name, None)
    if provides is not None:
        _PROVIDES_TO_MODULE.pop(provides, None)
        logger.debug(t("log.server.meta_plugin_unregister", plugin_name=plugin_name, provides=provides))


def require(provides: str) -> ModuleType:
    """按 provides 取已登记的 api 模块；未登记则抛错。"""
    if provides not in _PROVIDES_TO_MODULE:
        raise MetaPluginNotFoundError(provides)
    return _PROVIDES_TO_MODULE[provides]


def import_api(package: str) -> ModuleType | None:
    """取 ``<package>.api``。未安装返回 None，``ImportError`` 只留在框架里。"""
    name = f"{package}.api"
    if name in sys.modules:
        return sys.modules[name]
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def provides_of(plugin_name: str) -> str | None:
    """插件目录名 → provides；未登记返回 None。"""
    return _PLUGIN_TO_PROVIDES[plugin_name] if plugin_name in _PLUGIN_TO_PROVIDES else None


def list_meta_plugins() -> Dict[str, str]:
    """plugin_name → provides 的浅拷贝。"""
    return dict(_PLUGIN_TO_PROVIDES)


def bind_meta_plugin_facade(provides: str, plugin_name: str, module_names: list[str]) -> bool:
    """从已 import 的模块名里找 ``*.api`` 并登记。找到返回 True。"""
    api_mod: ModuleType | None = None
    for name in module_names:
        if name.endswith(".api") and name in sys.modules:
            api_mod = sys.modules[name]
            break
    if api_mod is None:
        for candidate in (
            f"plugins.{plugin_name}.{plugin_name}.api",
            f"plugins.{plugin_name}.api",
        ):
            if candidate in sys.modules:
                api_mod = sys.modules[candidate]
                break
    if api_mod is None:
        return False
    register_meta_plugin(provides, api_mod, plugin_name)
    return True
