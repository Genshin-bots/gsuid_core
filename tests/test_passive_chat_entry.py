"""抽取后 handle_ai_chat 仍先 semaphore 再入口；预算/H01 顺序不变。"""

from __future__ import annotations

import ast
from pathlib import Path


def _async_fn(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing {name}")


def _call_names(node: ast.AST) -> list[str]:
    found: list[tuple[int, int, str]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name:
                found.append((child.lineno, child.col_offset, name))
    found.sort()
    return [name for _line, _col, name in found]


def test_handle_ai_chat_semaphore_then_passive() -> None:
    src = Path("gsuid_core/ai_core/handle_ai.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _async_fn(tree, "handle_ai_chat")
    withs = [n for n in fn.body if isinstance(n, ast.AsyncWith)]
    assert withs, "handle_ai_chat must keep async with _ai_semaphore"
    item = withs[0].items[0].context_expr
    assert isinstance(item, ast.Name) and item.id == "_ai_semaphore"
    inner_calls = _call_names(withs[0])
    assert "run_passive_interactive_chat" in inner_calls
    assert "stale_request" in inner_calls
    # 抽取后 semaphore 内不应再手写 H01 / 长度
    assert inner_calls.index("stale_request") < inner_calls.index("run_passive_interactive_chat")


def test_passive_entry_order() -> None:
    src = Path("gsuid_core/ai_core/handle_ai.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _async_fn(tree, "run_passive_interactive_chat")
    names = _call_names(fn)
    for required in (
        "check_budget_gate",
        "fire_hooks",
        "apply_absolute_length_guard",
        "get_ai_session",
        "run_interactive_turn",
    ):
        assert required in names, required
    assert names.index("check_budget_gate") < names.index("fire_hooks")
    assert names.index("fire_hooks") < names.index("apply_absolute_length_guard")
    assert names.index("apply_absolute_length_guard") < names.index("get_ai_session")
    assert names.index("get_ai_session") < names.index("run_interactive_turn")


def test_check_budget_gate_uses_evaluate_budget() -> None:
    src = Path("gsuid_core/ai_core/turn_pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _async_fn(tree, "check_budget_gate")
    assert "evaluate_budget" in _call_names(fn)


def test_run_has_no_shield_or_gen_task() -> None:
    src = Path("gsuid_core/ai_core/gs_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    class_node = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "GsCoreAIAgent":
            class_node = node
            break
    assert class_node is not None
    run_fn = None
    for node in class_node.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            run_fn = node
            break
    assert run_fn is not None
    text = ast.get_source_segment(src, run_fn) or ""
    assert "asyncio.shield" not in text
    assert "gen_task" not in text
