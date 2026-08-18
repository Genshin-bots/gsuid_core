"""Responses hosted web_search 接线。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

from pydantic_ai.tools import Tool
from pydantic_ai.messages import TextPart, ToolCallPart, NativeToolCallPart, NativeToolReturnPart

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.agent_run.state import RunOnceState
from gsuid_core.ai_core.output_firewall import _FRAMEWORK_LEAK_RE
from gsuid_core.utils.plugins_config.models import GsStrConfig
from gsuid_core.ai_core.agent_run.remote_web_search import (
    LOCAL_WEB_SEARCH_TOOL,
    HOSTED_WEB_SEARCH_TOOL,
    ANTHROPIC_NATIVE_METHOD,
    attach_remote_web_search,
    is_hosted_web_search_name,
    remote_web_search_should_attach,
)


def test_should_attach_follows_request_method_not_host() -> None:
    assert remote_web_search_should_attach(
        enabled=True,
        request_method="responses",
        has_local_web_search=True,
    )
    assert not remote_web_search_should_attach(
        enabled=False,
        request_method="responses",
        has_local_web_search=True,
    )
    assert not remote_web_search_should_attach(
        enabled=True,
        request_method="chat_completions",
        has_local_web_search=True,
    )
    assert not remote_web_search_should_attach(
        enabled=True,
        request_method="responses",
        has_local_web_search=False,
    )


def test_chat_completions_ignores_switch() -> None:
    assert not remote_web_search_should_attach(
        enabled=True,
        request_method="chat_completions",
        has_local_web_search=True,
    )


def test_anthropic_native_attaches_when_on() -> None:
    assert remote_web_search_should_attach(
        enabled=True,
        request_method=ANTHROPIC_NATIVE_METHOD,
        has_local_web_search=True,
    )
    assert not remote_web_search_should_attach(
        enabled=False,
        request_method=ANTHROPIC_NATIVE_METHOD,
        has_local_web_search=True,
    )


def test_template_places_remote_web_search_after_request_method() -> None:
    from gsuid_core.ai_core.configs.openai_config import OPENAI_CONFIG_TEMPLATE

    keys = list(OPENAI_CONFIG_TEMPLATE)
    assert keys.index("remote_web_search") == keys.index("request_method") + 1
    cfg = OPENAI_CONFIG_TEMPLATE["remote_web_search"]
    assert isinstance(cfg, GsStrConfig)
    assert cfg.data == "on"
    assert cfg.options == ["off", "on"]


def test_anthropic_template_defaults_remote_web_search_on() -> None:
    from gsuid_core.ai_core.configs.anthropic_config import ANTHROPIC_CONFIG_TEMPLATE

    cfg = ANTHROPIC_CONFIG_TEMPLATE["remote_web_search"]
    assert isinstance(cfg, GsStrConfig)
    assert cfg.data == "on"
    assert cfg.options == ["off", "on"]


async def _dummy_search(query: str) -> str:
    return query


def _make_state(tool_names: List[str]) -> RunOnceState:
    dummy = Tool(_dummy_search, name=LOCAL_WEB_SEARCH_TOOL)
    st = RunOnceState(
        user_message="hi",
        bot=None,
        ev=None,
        rag_context=None,
        tools=[dummy] if LOCAL_WEB_SEARCH_TOOL in tool_names else [],
        return_mode="return",
        output_type=None,
        intent=None,
        has_active_task=False,
        budget_gate=False,
        suppress_intermediate_text=False,
        fake_done_retry=False,
        turn_graph=None,
        cheap_gate=None,
        is_framework_injection=False,
    )
    st.tool_names = list(tool_names)
    st.context = ToolContext()
    return st


def test_attach_pops_local_tool_and_blocks_recall(monkeypatch: Any) -> None:
    from gsuid_core.ai_core.agent_run import remote_web_search as rws

    dummy = Tool(_dummy_search, name=LOCAL_WEB_SEARCH_TOOL)
    monkeypatch.setattr(
        rws,
        "read_remote_web_search_flag",
        lambda _name: (True, "responses"),
    )
    monkeypatch.setattr(rws, "find_tool_base", lambda _n: SimpleNamespace(tool=dummy))

    st = _make_state([LOCAL_WEB_SEARCH_TOOL, "read_handle"])
    cap = attach_remote_web_search(st, "openai++official")
    assert cap is not None
    assert LOCAL_WEB_SEARCH_TOOL not in {t.name for t in st.tools}
    assert LOCAL_WEB_SEARCH_TOOL in st.tool_names
    ctx = st.context
    assert ctx is not None
    assert LOCAL_WEB_SEARCH_TOOL in ctx.blocked_tool_names


def test_attach_skips_when_flag_off(monkeypatch: Any) -> None:
    from gsuid_core.ai_core.agent_run import remote_web_search as rws

    monkeypatch.setattr(
        rws,
        "read_remote_web_search_flag",
        lambda _name: (False, "responses"),
    )
    st = _make_state([LOCAL_WEB_SEARCH_TOOL])
    assert attach_remote_web_search(st, "openai++official") is None
    assert any(t.name == LOCAL_WEB_SEARCH_TOOL for t in st.tools)


def test_attach_anthropic_native(monkeypatch: Any) -> None:
    from gsuid_core.ai_core.agent_run import remote_web_search as rws

    dummy = Tool(_dummy_search, name=LOCAL_WEB_SEARCH_TOOL)
    monkeypatch.setattr(rws, "read_remote_web_search_flag", lambda _name: (True, ANTHROPIC_NATIVE_METHOD))
    monkeypatch.setattr(rws, "find_tool_base", lambda _n: SimpleNamespace(tool=dummy))
    st = _make_state([LOCAL_WEB_SEARCH_TOOL])
    assert attach_remote_web_search(st, "anthropic++claude") is not None


def test_attach_skips_chat_completions_even_when_on(monkeypatch: Any) -> None:
    from gsuid_core.ai_core.agent_run import remote_web_search as rws

    monkeypatch.setattr(
        rws,
        "read_remote_web_search_flag",
        lambda _name: (True, "chat_completions"),
    )
    st = _make_state([LOCAL_WEB_SEARCH_TOOL])
    assert attach_remote_web_search(st, "openai++chat") is None
    assert any(t.name == LOCAL_WEB_SEARCH_TOOL for t in st.tools)


def test_hosted_name_and_firewall() -> None:
    assert is_hosted_web_search_name(HOSTED_WEB_SEARCH_TOOL)
    assert not is_hosted_web_search_name(LOCAL_WEB_SEARCH_TOOL)
    assert _FRAMEWORK_LEAK_RE.search("please call web_search now")
    assert _FRAMEWORK_LEAK_RE.search("web_search_tool")


def test_builtin_web_search_counts_as_tool_but_not_intermediate() -> None:
    """CallTools 语义：hosted call 进 tool_call_list，但不该触发中间文本抑制。"""
    call = NativeToolCallPart(tool_name=HOSTED_WEB_SEARCH_TOOL, args={"query": "news"}, tool_call_id="ws_1")
    ret = NativeToolReturnPart(tool_name=HOSTED_WEB_SEARCH_TOOL, content={"status": "completed"}, tool_call_id="ws_1")
    text = TextPart(content="今天的正面新闻是……")
    saw_fn_tool = False
    tool_call_list: list[str] = []
    saw_web = False
    for part in (call, ret, text):
        if isinstance(part, ToolCallPart):
            saw_fn_tool = True
            tool_call_list.append(part.tool_name)
        elif isinstance(part, (NativeToolCallPart, NativeToolReturnPart)):
            if is_hosted_web_search_name(part.tool_name):
                saw_web = True
            if isinstance(part, NativeToolCallPart):
                tool_call_list.append(part.tool_name)
    assert tool_call_list == [HOSTED_WEB_SEARCH_TOOL]
    assert saw_web
    assert not saw_fn_tool
