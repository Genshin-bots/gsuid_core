"""tools_change 会话日志：entry 类型白名单 + 字段契约。"""

from gsuid_core.ai_core.session_logger import SESSION_ENTRY_TYPES, AISessionLogger


def test_tools_change_is_registered_entry_type() -> None:
    assert "tools_change" in SESSION_ENTRY_TYPES


def test_log_tools_change_writes_all_keys() -> None:
    """assembly 记增删与提示；find_tools 记挂载并带 tool_call_id。"""
    slog = AISessionLogger("test:tools:change", is_subagent=True)
    try:
        slog.log_tools_change(
            source="assembly",
            added=["a_tool"],
            removed=["b_tool"],
            hinted=["c_tool"],
            total=7,
            from_find_tools=["a_tool"],
        )
        slog.log_tools_change(
            source="find_tools",
            added=["d_tool"],
            removed=[],
            hinted=[],
            total=8,
            from_find_tools=[],
            tool_call_id="call_x",
        )
        rows = [e["data"] for e in slog.entries if e["type"] == "tools_change"]
        assert len(rows) == 2
        assert rows[0] == {
            "source": "assembly",
            "added": ["a_tool"],
            "removed": ["b_tool"],
            "hinted": ["c_tool"],
            "total": 7,
            "from_find_tools": ["a_tool"],
        }
        assert rows[1]["source"] == "find_tools"
        assert rows[1]["added"] == ["d_tool"]
        assert rows[1]["tool_call_id"] == "call_x"
        assert "tool_call_id" not in rows[0]
    finally:
        slog._closed = True
