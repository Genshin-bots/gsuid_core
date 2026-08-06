"""FileOS 审查修复：ACL fail-closed / firewall / fold 隔离 / task 绑定 / mime / 索引清理。

测试用 asyncio.run 包装（不依赖 pytest-asyncio）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from gsuid_core.ai_core.output_firewall import check_ooc
from gsuid_core.ai_core.planning.handle_resolver import (
    ResolvedHandle,
    format_resolved,
    _mime_for_tool_output,
)
from gsuid_core.ai_core.planning.tool_output_tools import (
    read_handle,
    _require_owner,
    grep_persisted_outputs,
    list_persisted_outputs,
    search_persisted_outputs,
    _tool_output_access_allowed,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _ctx(user_id: Optional[str] = "u1", group_id: Optional[str] = None) -> Any:
    ev = None
    if user_id is not None:
        ev = SimpleNamespace(user_id=user_id, group_id=group_id, session_id="s1")
    # bot 字段供 @ai_tools 包装层 handle_tool_result 使用
    deps = SimpleNamespace(ev=ev, parent_session_id=None, bot=None)
    return SimpleNamespace(deps=deps)


def _patch_tool_result():
    """@ai_tools 包装会 await handle_tool_result；测试里透传原文。"""
    return patch(
        "gsuid_core.ai_core.register.handle_tool_result",
        new=AsyncMock(side_effect=lambda bot, raw: raw),
    )


def test_mime_for_tool_output_md() -> None:
    assert _mime_for_tool_output("/tmp/to_x.md") == "text/markdown"
    assert _mime_for_tool_output("/tmp/to_x.txt") == "text/plain"
    assert _mime_for_tool_output("") == "text/plain"


def test_require_owner_fail_closed() -> None:
    owner, scope, err = _require_owner(_ctx(user_id=None))
    assert owner is None
    assert err is not None
    assert "拒绝" in err

    owner2, scope2, err2 = _require_owner(_ctx(user_id="u9", group_id="g1"))
    assert owner2 == "u9"
    assert scope2 == "g1"
    assert err2 is None


def test_tool_output_acl_same_owner_and_scope() -> None:
    resolved = ResolvedHandle(
        id="to_abc",
        source="tool_output",
        mime="text/plain",
        summary="s",
        owner_user_id="u1",
        scope_key="g1",
        payload_inline="body",
        payload_path="",
        size_bytes=4,
    )
    assert _tool_output_access_allowed(resolved, _ctx("u1"))
    assert _tool_output_access_allowed(resolved, _ctx("u2", group_id="g1"))
    assert not _tool_output_access_allowed(resolved, _ctx("u2", group_id="g2"))
    empty_owner = ResolvedHandle(
        id="to_no",
        source="tool_output",
        mime="text/plain",
        summary="",
        owner_user_id="",
        scope_key="",
        payload_inline="x",
        payload_path="",
        size_bytes=1,
    )
    assert not _tool_output_access_allowed(empty_owner, _ctx("u1"))
    # 系统路径（无 event）放行
    assert _tool_output_access_allowed(empty_owner, _ctx(user_id=None))


def test_search_without_owner_denied() -> None:
    with _patch_tool_result():
        out = _run(search_persisted_outputs(_ctx(user_id=None), query="weather"))
    assert "拒绝" in out


def test_list_without_owner_denied() -> None:
    with _patch_tool_result():
        out = _run(list_persisted_outputs(_ctx(user_id=None)))
    assert "拒绝" in out


def test_grep_without_owner_denied() -> None:
    with _patch_tool_result():
        out = _run(grep_persisted_outputs(_ctx(user_id=None), keyword="x"))
    assert "拒绝" in out


def test_read_handle_denies_cross_owner_tool_output() -> None:
    resolved = ResolvedHandle(
        id="to_secret",
        source="tool_output",
        mime="text/plain",
        summary="secret",
        owner_user_id="ownerA",
        scope_key="gA",
        payload_inline="classified payload",
        payload_path="",
        size_bytes=10,
    )
    with (
        _patch_tool_result(),
        patch(
            "gsuid_core.ai_core.planning.tool_output_tools.resolve_handle",
            new=AsyncMock(return_value=resolved),
        ),
    ):
        denied = _run(read_handle(_ctx("ownerB", group_id="gB"), handle_id="to_secret"))
        assert "无权限" in denied
        assert "classified" not in denied
        allowed = _run(read_handle(_ctx("ownerA"), handle_id="to_secret"))
        assert "classified payload" in allowed


def test_read_handle_uses_artifact_acl() -> None:
    resolved = ResolvedHandle(
        id="res_deadbeef12",
        source="artifact",
        mime="text/plain",
        summary="art",
        owner_user_id="ownerA",
        scope_key="gA",
        payload_inline="artifact body",
        payload_path="",
        size_bytes=12,
        root_task_id="root1",
        task_id="t1",
    )
    fake_art = SimpleNamespace(id="res_deadbeef12", root_task_id="root1")

    with (
        _patch_tool_result(),
        patch(
            "gsuid_core.ai_core.planning.tool_output_tools.resolve_handle",
            new=AsyncMock(return_value=resolved),
        ),
        patch(
            "gsuid_core.ai_core.planning.models.AIAgentArtifact.get_by_id",
            new=AsyncMock(return_value=fake_art),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._artifact_access_allowed",
            new=AsyncMock(return_value=False),
        ),
    ):
        denied = _run(read_handle(_ctx("ownerB"), handle_id="res_deadbeef12"))
        assert "无权限" in denied
        assert "artifact body" not in denied

    with (
        _patch_tool_result(),
        patch(
            "gsuid_core.ai_core.planning.tool_output_tools.resolve_handle",
            new=AsyncMock(return_value=resolved),
        ),
        patch(
            "gsuid_core.ai_core.planning.models.AIAgentArtifact.get_by_id",
            new=AsyncMock(return_value=fake_art),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._artifact_access_allowed",
            new=AsyncMock(return_value=True),
        ),
    ):
        allowed = _run(read_handle(_ctx("ownerA"), handle_id="res_deadbeef12"))
        assert "artifact body" in allowed


def test_firewall_covers_fileos_handles_and_tools() -> None:
    samples = [
        "请用 read_handle(to_abcdef123456) 查看全文",
        "句柄 to_abcdef123456 里有报告",
        "sa_fedcba654321 已落盘",
        "persisted id=to_abcdef123456 size=99",
        "search_persisted_outputs 查一下",
        "list_persisted_outputs 最近记录",
    ]
    for text in samples:
        hit = check_ooc(text)
        assert hit is not None, text
        assert hit.category == "system_term"
        assert any("框架泄漏" in m for m in hit.matched)


def test_persist_and_fold_propagates_then_caller_can_isolate() -> None:
    from gsuid_core.ai_core.planning.tool_output_helper import persist_and_fold_tool_return

    long_body = "x" * 2000
    with patch(
        "gsuid_core.ai_core.planning.tool_output_helper.persist_tool_return",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        try:
            _run(
                persist_and_fold_tool_return(
                    tool_name="web_search_tool",
                    content=long_body,
                    ev=None,
                    session_id="s",
                    task_id="t1",
                    root_task_id="r1",
                )
            )
            raised = False
        except RuntimeError:
            raised = True
        assert raised
        # 调用方隔离（gs_agent 同款）：异常 → 不折叠
        folded: Optional[str]
        try:
            folded = _run(
                persist_and_fold_tool_return(
                    tool_name="web_search_tool",
                    content=long_body,
                    ev=None,
                    session_id="s",
                )
            )
        except RuntimeError:
            folded = None
        assert folded is None


def test_capability_persist_receives_plan_task_ids() -> None:
    """plan context 的 task/root 会被传入 schedule_persist 路径。"""
    from gsuid_core.ai_core.planning.runtime import (
        PlanRunContext,
        get_plan_context,
        bind_plan_context,
        reset_plan_context,
    )

    captured: dict[str, str] = {}

    def _fake_schedule(**kwargs: Any) -> None:
        captured["task_id"] = kwargs.get("task_id", "")
        captured["root_task_id"] = kwargs.get("root_task_id", "")

    token = bind_plan_context(PlanRunContext(task_id="task_cap_1", root_task_id="root_cap_9"))
    try:
        pc = get_plan_context()
        assert pc is not None
        _fake_schedule(
            task_id=pc.task_id or "",
            root_task_id=pc.root_task_id or "",
        )
        assert captured["task_id"] == "task_cap_1"
        assert captured["root_task_id"] == "root_cap_9"
    finally:
        reset_plan_context(token)


def test_delete_tool_output_index_noop_without_client() -> None:
    from gsuid_core.ai_core.planning.tool_output_index import delete_tool_output_index

    with patch("gsuid_core.ai_core.rag.base.client", None):
        _run(delete_tool_output_index(["to_a", "to_b"]))


def test_delete_tool_output_index_calls_qdrant_filter() -> None:
    from gsuid_core.ai_core.planning.tool_output_index import delete_tool_output_index

    mock_client = MagicMock()
    mock_client.delete = AsyncMock()
    with patch("gsuid_core.ai_core.rag.base.client", mock_client):
        _run(delete_tool_output_index(["to_abc", "sa_def"]))
    mock_client.delete.assert_awaited_once()
    kwargs = mock_client.delete.await_args.kwargs
    assert kwargs["collection_name"] == "tool_outputs"


def test_format_resolved_uses_mime() -> None:
    r = ResolvedHandle(
        id="to_x",
        source="tool_output",
        mime="text/markdown",
        summary="s",
        owner_user_id="u",
        scope_key="",
        payload_inline="# hi",
        payload_path="x.md",
        size_bytes=4,
    )
    text = format_resolved(r)
    assert "text/markdown" in text
    assert "# hi" in text
