"""只读刚才这条命令所属插件目录。不能读目录以外的文件。"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from collections.abc import Callable

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool

from gsuid_core.ai_core.models import ToolContext

_SUFFIXES = frozenset({".py", ".md", ".json", ".toml", ".txt", ".yml", ".yaml"})
_MAX_LIST = 200
_MAX_BYTES = 256_000
_MAX_CHARS = 20_000


def plugin_anchor(func: Callable[..., object]) -> tuple[Path, str] | None:
    """插件根目录，以及入口文件相对该根的路径。包装函数先 unwrap。"""
    try:
        found = inspect.getfile(inspect.unwrap(func))
    except (TypeError, OSError):
        return None
    path = Path(found).resolve()
    parts = path.parts
    root: Path | None = None
    for marker in ("plugins", "buildin_plugins"):
        if marker not in parts:
            continue
        index = parts.index(marker)
        if index + 1 < len(parts):
            root = Path(*parts[: index + 2])
            break
    if root is None:
        return None
    root_resolved = root.resolve()
    if not path.is_relative_to(root_resolved):
        return None
    return root_resolved, path.relative_to(root_resolved).as_posix()


def make_plugin_source_tool(root: Path) -> Tool[ToolContext]:
    async def read_command_plugin_file(ctx: RunContext[ToolContext], relative_path: str) -> str:
        """读取刚刚这条命令所属插件目录里的文件。

        relative_path 相对插件根目录，例如 ``pkg/handler.py``。
        传空字符串则列出目录中的文本文件。只读，不能读该目录以外的路径。
        """
        del ctx
        return await asyncio.to_thread(_read_or_list, root, relative_path)

    return Tool(read_command_plugin_file, name="read_command_plugin_file")


def resolve_plugin_file(root: Path, relative_path: str) -> Path | None:
    raw = relative_path.strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        return None
    if any(part == ".." for part in raw.split("/")):
        return None
    root_resolved = root.resolve()
    candidate = (root_resolved / raw).resolve()
    if not candidate.is_relative_to(root_resolved):
        return None
    if not candidate.is_file():
        return None
    if candidate.suffix.lower() not in _SUFFIXES:
        return None
    return candidate


def _read_or_list(root: Path, relative_path: str) -> str:
    if not relative_path.strip():
        return _list_files(root)
    path = resolve_plugin_file(root, relative_path)
    if path is None:
        return "路径不在这条命令的插件目录内，或不是可读的文本文件。"
    if path.stat().st_size > _MAX_BYTES:
        return "文件超过 256KB，没有读入。"
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > _MAX_CHARS:
        return text[:_MAX_CHARS] + "\n…已截断"
    return text


def _list_files(root: Path) -> str:
    root_resolved = root.resolve()
    lines: list[str] = []
    for path in root_resolved.rglob("*"):
        if len(lines) >= _MAX_LIST:
            lines.append("…还有更多文件，请缩小路径。")
            break
        if not path.is_file():
            continue
        if path.suffix.lower() not in _SUFFIXES:
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root_resolved):
            continue
        lines.append(resolved.relative_to(root_resolved).as_posix())
    if not lines:
        return "插件目录里没有可读的文本文件。"
    return "\n".join(lines)
