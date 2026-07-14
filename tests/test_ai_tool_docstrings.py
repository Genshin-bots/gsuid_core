"""每个 `@ai_tools` 工具都必须有真正的 docstring。

工具入库向量的文本 = `name + "\\n" + description`，而 description 只来自 docstring
（`register.py::ai_tools`）。没有 docstring = 向量里只剩一个英文函数名，中文提问几乎
不可能召回它——工具"注册了"但**永远调不到**，且全程静默无报错。

2026-07-15 生产事故：XutheringWavesUID 的 5 个面板工具把 docstring 写在了函数体
第一条语句（`logger.info(...)`）**之后**。那样它只是个普通字符串表达式，`__doc__`
为 None。于是"看下我玄翎秧秧面板"召不回任何鸣潮工具，AI 只能拿异环工具硬答。

这个坑没有任何运行时症状，只能靠静态检查兜住——故有此测试。
"""

import ast
from typing import List, Tuple
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "gsuid_core"


def _is_ai_tool(node: ast.AST) -> bool:
    if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
        return False
    return any("ai_tools" in ast.unparse(dec) for dec in node.decorator_list)


def _has_stray_string_in_body(node: ast.AST) -> bool:
    """函数体首条语句之后还躺着一个孤立字符串字面量 = docstring 位置写错了。"""
    if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
        return False
    for stmt in node.body[1:]:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            if isinstance(stmt.value.value, str):
                return True
    return False


def _collect_tools_without_docstring() -> List[Tuple[str, str, bool]]:
    offenders: List[Tuple[str, str, bool]] = []
    for py in _SRC.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        # utf-8-sig：部分插件源码带 BOM，ast.parse 收到 U+FEFF 会直接 SyntaxError
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not _is_ai_tool(node):
                continue
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if ast.get_docstring(node) is not None:
                continue
            rel = py.relative_to(_ROOT).as_posix()
            offenders.append((rel, node.name, _has_stray_string_in_body(node)))
    return offenders


def test_every_ai_tool_has_a_docstring() -> None:
    offenders = _collect_tools_without_docstring()

    lines: List[str] = []
    for rel, name, misplaced in offenders:
        hint = "  ← docstring 被写在了函数体首条语句之后，挪到最前面" if misplaced else ""
        lines.append(f"  {rel}::{name}{hint}")

    assert not offenders, "以下 @ai_tools 工具没有 docstring，向量检索只剩函数名、永远召不回：\n" + "\n".join(lines)


def test_scanner_actually_sees_the_tools() -> None:
    """防止上面那条测试因为扫不到任何工具而"空过"。"""
    count = 0
    for py in _SRC.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        # utf-8-sig：部分插件源码带 BOM，ast.parse 收到 U+FEFF 会直接 SyntaxError
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        count += sum(1 for node in ast.walk(tree) if _is_ai_tool(node))

    assert count > 50, f"只扫到 {count} 个 @ai_tools 工具，扫描逻辑可能已失效"
