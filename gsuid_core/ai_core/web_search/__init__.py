"""
Web Search 模块

提供统一的 web 搜索接口，通过延迟导出避免导入 ``date_utils`` 等子模块时提前拉起全部源。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gsuid_core.ai_core.web_search.search import web_search, web_search_with_context

_EXPORTS: dict[str, str] = {
    "web_search": "gsuid_core.ai_core.web_search.search",
    "web_search_with_context": "gsuid_core.ai_core.web_search.search",
}

__all__ = ["web_search", "web_search_with_context"]


def __getattr__(name: str) -> object:
    if name in _EXPORTS:
        module = importlib.import_module(_EXPORTS[name])
        value = module.__dict__[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
