"""AI 关闭时，不把 onnxruntime / fastembed 装进进程。

插件常在模块顶上写 ``import fastembed`` / ``import onnxruntime``。
解释器执行到这行就会加载原生库。这里在真正的查找器之前接一层：
AI 关闭时这些导入得到占位模块，进程不映射对应的 .pyd。
真正调用占位对象时抛 ImportError，而不是在启动时把库拉起来。

playwright 不进闸：插件浏览器渲染（HTML 出图）与 AI 总开关无关。
"""

from __future__ import annotations

import sys
import importlib.abc
import importlib.machinery
from types import ModuleType

from gsuid_core.i18n import t
from gsuid_core.logger import logger

_ROOTS = ("fastembed", "onnxruntime")
_installed = False


class HeavyImportBlocked(ImportError):
    """AI 关闭时调用了被拦住的原生库。"""


class _BlockedModule(ModuleType):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.__path__: list[str] = []
        self.__package__ = name
        self.__file__ = "<ai-off>"

    def __getattr__(self, name: str) -> _BlockedCall:
        if name.startswith("_"):
            raise AttributeError(name)
        return _BlockedCall(f"{self.__name__}.{name}")


class _BlockedCall:
    def __init__(self, qualname: str) -> None:
        self._qualname = qualname

    def __call__(self, *args: object, **kwargs: object) -> object:
        raise HeavyImportBlocked(self._qualname)

    def __getattr__(self, name: str) -> _BlockedCall:
        if name.startswith("_"):
            raise AttributeError(name)
        return _BlockedCall(f"{self._qualname}.{name}")


class _BlockedLoader(importlib.abc.Loader):
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> _BlockedModule:
        return _BlockedModule(spec.name)

    def exec_module(self, module: ModuleType) -> None:
        return None


class _HeavyGate(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname in sys.modules:
            return None
        blocked = False
        for root in _ROOTS:
            if fullname == root or fullname.startswith(root + "."):
                blocked = True
                break
        if not blocked:
            return None
        return importlib.machinery.ModuleSpec(
            fullname,
            _BlockedLoader(),
            is_package=True,
        )


def install_ai_off_heavy_gate(*, force: bool = False) -> bool:
    """AI 关闭时安装导入闸。已安装或 AI 仍开启时什么都不做。"""
    global _installed
    if _installed:
        return True
    if not force:
        from gsuid_core.ai_core.configs.ai_config import ai_config

        if ai_config.get_config("enable").data:
            return False
    sys.meta_path.insert(0, _HeavyGate())
    _installed = True
    logger.info(t("log.ai.heavy_import_blocked"))
    return True
