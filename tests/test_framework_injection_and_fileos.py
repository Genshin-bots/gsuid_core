"""框架注入通道 / 异步 ack 契约 / FileOS 门 / artifact 分页 / @ 去重回归。"""

from __future__ import annotations

from types import SimpleNamespace

from pydantic_ai.messages import ToolReturnPart

from gsuid_core.ai_core.utils import _is_framework_prompt_content
from gsuid_core.ai_core.gs_agent import (
    _tool_return_looks_failed,
    _tool_return_is_async_pending,
)
from gsuid_core.ai_core.planning.tool_output_helper import should_persist_tool_return


def test_async_pending_ack_detected() -> None:
    part = ToolReturnPart(
        tool_name="create_subagent",
        content="⏳ 子任务后台执行中（已同步等 5s，将自动回灌）。task#1",
        tool_call_id="t1",
    )
    assert _tool_return_is_async_pending(part)
    assert not _tool_return_looks_failed(part)


def test_final_delivery_not_pending() -> None:
    part = ToolReturnPart(
        tool_name="create_subagent",
        content="【子Agent交付完毕】角色短句 + 有图则发。\n\n# 标题\n\n| a | b |\n",
        tool_call_id="t2",
    )
    assert not _tool_return_is_async_pending(part)


def test_should_persist_skips_short_and_pending() -> None:
    assert not should_persist_tool_return("web_search_tool", "ok")
    assert should_persist_tool_return("web_search_tool", "x" * 40)
    pending = "⏳ 子任务后台执行中（已同步等 5s，将自动回灌）。" + ("x" * 900)
    assert not should_persist_tool_return("create_subagent", pending)
    long_body = "# report\n\n" + ("data line\n" * 100)
    assert should_persist_tool_return("web_search_tool", long_body)


def test_framework_prompt_content_detection() -> None:
    assert _is_framework_prompt_content("[框架·任务完成]\n交付")
    assert _is_framework_prompt_content("[系统·子任务异步交付]\nx")
    assert _is_framework_prompt_content("（系统校验：本轮你被直接呼叫")
    assert _is_framework_prompt_content("[用户发言]\n[框架·任务完成]\nx")
    assert not _is_framework_prompt_content("[用户发言]\n你好")


def test_delivery_format_is_handle_first() -> None:
    from gsuid_core.ai_core.planning.kanban_executor import _format_delivery_for_main_agent

    task = SimpleNamespace(
        ordinal=1,
        display_name="单元测交付",
        failure_reason=None,
    )
    art = SimpleNamespace(
        id="res_abc123456789",
        mime="text/markdown",
        summary="摘要一行即可",
        payload_path="/tmp/x.md",
        payload_inline=None,
    )
    text = _format_delivery_for_main_agent(task, "A" * 50_000, [art])  # type: ignore[arg-type]
    assert "res_abc123456789" in text
    assert "A" * 100 not in text  # 全文不得进交付包
    assert "摘要一行即可" in text
    assert "read_handle" in text
    assert "persisted id=" in text


def test_artifact_format_pagination() -> None:
    from gsuid_core.ai_core.planning.kanban_tools import _format_artifact

    art = SimpleNamespace(
        id="res_pagetest",
        artifact_kind="output",
        mime="text/plain",
        summary="s",
        payload_inline="abcdefghijklmnopqrstuvwxyz",
        payload_path="",
    )
    page = _format_artifact(art, offset=0, limit=10)  # type: ignore[arg-type]
    assert "abcdefghij" in page
    assert "【读窗口】" in page
    assert "offset=0" in page
    assert "分页" in page
    page2 = _format_artifact(art, offset=10, limit=10)  # type: ignore[arg-type]
    assert "klmnopqrst" in page2
    assert "offset=10" in page2


def test_relean_drops_framework_user_parts() -> None:
    """框架注入 UserPrompt 不得进 B 轨（避免被当成群友发言）。"""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from gsuid_core.ai_core.utils import _relean_user_turn

    msgs = [
        ModelRequest(parts=[UserPromptPart(content="[框架·任务完成]\n【子任务交付】任务#1 已完成。\n产物 res_abc")])
    ]
    _relean_user_turn(msgs, lean_content="")
    # 整段被剥掉 → parts 空
    assert len(msgs[0].parts) == 0


def test_relean_keeps_real_user_and_strips_nudge() -> None:
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from gsuid_core.ai_core.utils import _relean_user_turn

    msgs = [
        ModelRequest(
            parts=[
                UserPromptPart(content="[用户发言]\n你好\n\n[历史对话] 很长…"),
                UserPromptPart(content="（系统校验：本轮你被直接呼叫"),
            ]
        )
    ]
    _relean_user_turn(msgs, lean_content="[用户发言]\n你好")
    assert len(msgs[0].parts) == 1
    assert msgs[0].parts[0].content == "[用户发言]\n你好"
