"""AI 关闭时，知识库等 RAG 运行模块用空壳代替，不执行真实导入。"""

from __future__ import annotations

import sys
import importlib.abc
import importlib.machinery
from types import ModuleType

from gsuid_core.i18n import t
from gsuid_core.logger import logger

SHELL_MODULES = (
    "gsuid_core.ai_core.rag.knowledge",
    "gsuid_core.ai_core.rag.tools",
    "gsuid_core.ai_core.rag.image_rag",
    "gsuid_core.ai_core.rag.reranker",
    "gsuid_core.ai_core.rag.startup",
)

_SYNC_NAMES = {
    "build_knowledge_text",
    "build_image_text",
    "load_image_from_path",
    "get_reranker",
    "reset_reranker",
}
_LIST_MARKS = ("search", "query", "list")
_warned = False
_installed = False


def warn_rag_shell() -> None:
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(t("log.ai.rag_shell_skip"))


def shell_attr(name: str) -> object:
    if name in _SYNC_NAMES or name.startswith("build_") or name.startswith("load_"):

        def _sync(*args: object, **kwargs: object) -> None:
            return None

        return _sync

    async def _async(*args: object, **kwargs: object) -> list[object] | None:
        for mark in _LIST_MARKS:
            if mark in name:
                return []
        return None

    return _async


class _ShellModule(ModuleType):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.__path__: list[str] = []
        self.__package__ = name
        self.__file__ = "<ai-off-rag>"

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)
        value = shell_attr(name)
        setattr(self, name, value)
        return value


class _ShellLoader(importlib.abc.Loader):
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> _ShellModule:
        return _ShellModule(spec.name)

    def exec_module(self, module: ModuleType) -> None:
        warn_rag_shell()


class _RagShellFinder(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname not in SHELL_MODULES or fullname in sys.modules:
            return None
        return importlib.machinery.ModuleSpec(fullname, _ShellLoader(), is_package=False)


def install_rag_shell_finder() -> None:
    global _installed
    if _installed:
        return
    sys.meta_path.insert(0, _RagShellFinder())
    _installed = True


def ai_enabled() -> bool:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return bool(ai_config.get_config("enable").data)
