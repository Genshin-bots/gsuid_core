"""非主人不能拉起 code_agent，也不能执行高危工具。

锁住 2026-09-22 群会话：说话人不是主人，主人格仍 create_subagent(code_agent)，
子代理直接 execute_file。user_pm=0 不算主人，只认配置里的 masters。
"""

import ast
from pathlib import Path
from unittest.mock import patch

from gsuid_core.models import Event
from gsuid_core.ai_core.tool_risk import (
    HIGH_RISK_TOOL_DENY,
    event_is_master,
    node_requires_master,
    strip_high_risk_tools,
    refuse_master_only_node,
    check_high_risk_operator,
)
from gsuid_core.ai_core.agent_node.models import AgentNode

_MASTER = "master_uid"
_OTHER = "other_uid"


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name


def _masters(uid: str) -> bool:
    return uid == _MASTER


def test_forged_user_pm_is_not_master() -> None:
    """任务行上的 user_pm=0 不能把非主人变成主人。"""
    with patch("gsuid_core.ai_core.tool_risk._is_master_user", side_effect=_masters):
        forged = Event(user_id=_OTHER, user_pm=0)
        assert not event_is_master(forged)
        ok, msg = check_high_risk_operator(forged)
        assert not ok
        assert msg == HIGH_RISK_TOOL_DENY

        master = Event(user_id=_MASTER, user_pm=6)
        assert event_is_master(master)
        ok_master, note = check_high_risk_operator(master)
        assert ok_master and note == ""

        assert not event_is_master(None)
        denied, _ = check_high_risk_operator(None)
        assert not denied


def test_master_only_nodes_and_tool_whitelist() -> None:
    from gsuid_core.ai_core.agent_node import get_node
    from gsuid_core.ai_core.capability_agents.profiles import register_builtin_profiles

    register_builtin_profiles()
    code = get_node("code_agent")
    plugin_dev = get_node("plugin_developer_agent")
    research = get_node("research_agent")
    assert code is not None and code.master_only and node_requires_master(code)
    assert plugin_dev is not None and plugin_dev.master_only and node_requires_master(plugin_dev)
    assert research is not None and not node_requires_master(research)

    by_tool = AgentNode(
        node_id="custom_exec",
        display_name="自定义执行",
        prompt="p",
        tool_names=["execute_file"],
        source="plugin",
    )
    writer = AgentNode(
        node_id="custom_write",
        display_name="只写文件",
        prompt="p",
        tool_names=["write_file_content"],
        source="plugin",
    )
    assert node_requires_master(by_tool)
    assert not node_requires_master(writer)

    with patch("gsuid_core.ai_core.tool_risk._is_master_user", side_effect=_masters):
        blocked = refuse_master_only_node(Event(user_id=_OTHER), code)
        assert blocked is not None and "code_agent" in blocked
        assert refuse_master_only_node(Event(user_id=_MASTER), code) is None
        assert refuse_master_only_node(Event(user_id=_OTHER), research) is None
        assert refuse_master_only_node(None, by_tool) is not None


def test_strip_high_risk_tools_for_non_master() -> None:
    tools = [_Named("execute_file"), _Named("read_file_content"), _Named("run_command")]
    with patch("gsuid_core.ai_core.tool_risk._is_master_user", side_effect=_masters):
        kept = strip_high_risk_tools(tools, Event(user_id=_OTHER))
        assert [item.name for item in kept] == ["read_file_content"]
        kept_master = strip_high_risk_tools(tools, Event(user_id=_MASTER))
        assert [item.name for item in kept_master] == ["execute_file", "read_file_content", "run_command"]
        kept_anon = strip_high_risk_tools(tools, None)
        assert [item.name for item in kept_anon] == ["read_file_content"]
        from gsuid_core.ai_core.tool_risk import high_risk_block

        assert high_risk_block(Event(user_id=_OTHER), "run_skill_script") == HIGH_RISK_TOOL_DENY
        assert high_risk_block(Event(user_id=_MASTER), "run_skill_script") is None
        assert high_risk_block(Event(user_id=_OTHER), "read_file_content") is None


def _decorator_src(rel: str, fn_name: str) -> str:
    path = Path(__file__).resolve().parents[1] / rel
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == fn_name:
            return "\n".join(ast.unparse(dec) for dec in node.decorator_list)
    raise AssertionError(fn_name)


def test_execute_tools_declare_master_gate() -> None:
    """装饰器接线不依赖 AI 总开关：总开关关掉时 @ai_tools 不会注册。"""
    file_dec = _decorator_src("gsuid_core/ai_core/buildin_tools/file_manager.py", "execute_file")
    shell_dec = _decorator_src("gsuid_core/ai_core/buildin_tools/command_executor.py", "execute_shell_command")
    write_dec = _decorator_src("gsuid_core/ai_core/buildin_tools/file_manager.py", "write_file_content")
    for dec in (file_dec, shell_dec):
        assert "check_high_risk_operator" in dec
        assert "visible_to_master_operator" in dec
    assert "check_high_risk_operator" not in write_dec


def test_master_only_roundtrip() -> None:
    from gsuid_core.ai_core.capability_agents.persistence import _dto_to_node, _node_to_dto

    node = AgentNode(
        node_id="my_exec",
        display_name="执行",
        prompt="p",
        tool_names=["execute_shell_command"],
        master_only=True,
        source="user",
    )
    back = _dto_to_node(dict(_node_to_dto(node)))
    assert back is not None and back.master_only is True
    legacy = _dto_to_node({"node_id": "old", "display_name": "old", "prompt": "p"})
    assert legacy is not None and legacy.master_only is False
