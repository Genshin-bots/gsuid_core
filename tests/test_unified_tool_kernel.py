"""群/私同一通道核：名单、检索跳过、query 不串味。"""

from gsuid_core.ai_core.agent_run.tools import should_skip_tool_search, complete_kernel_family_names
from gsuid_core.ai_core.interaction_scaffold import (
    MAIN_AGENT_CORE_TOOLS,
    build_tool_search_query,
)

_KERNEL_REQUIRED = (
    "find_tools",
    "search_cognition",
    "create_subagent",
    "capability_map",
    "send_meme",
    "send_message_by_ai",
    "read_handle",
    "dispute_directive",
    "add_once_task",
    "add_interval_task",
)
_KERNEL_FORBIDDEN = (
    "list_scheduled_tasks",
    "web_search_tool",
    "web_fetch_tool",
    "state_set",
    "run_command",
    "get_self_info",
    "attach_article",
)


def test_skip_search_idle_both_channels() -> None:
    assert (
        should_skip_tool_search(
            is_group=False,
            call_to_self=True,
            in_flight_short=False,
            followup_detected=False,
            has_active_task=False,
            has_media=False,
            intent="闲聊",
        )
        is False
    )
    assert (
        should_skip_tool_search(
            is_group=True,
            call_to_self=True,
            in_flight_short=False,
            followup_detected=False,
            has_active_task=False,
            has_media=False,
            intent="闲聊",
        )
        is False
    )
    assert not should_skip_tool_search(
        is_group=False,
        call_to_self=True,
        in_flight_short=False,
        followup_detected=False,
        has_active_task=False,
        has_media=False,
        intent="工具",
    )
    assert not should_skip_tool_search(
        is_group=True,
        call_to_self=True,
        in_flight_short=False,
        followup_detected=False,
        has_active_task=False,
        has_media=False,
        intent="工具",
    )


def test_group_idle_request_limit_addressed_not_capped() -> None:
    from gsuid_core.ai_core.agent_run.tools import group_idle_request_limit

    assert (
        group_idle_request_limit(
            20,
            is_group=True,
            followup_detected=False,
            has_active_task=False,
            idle_cap=2,
            call_to_self=True,
        )
        == 20
    )
    assert (
        group_idle_request_limit(
            20,
            is_group=True,
            followup_detected=False,
            has_active_task=False,
            idle_cap=2,
            call_to_self=False,
        )
        == 2
    )


def test_skip_search_group_bystander() -> None:
    assert should_skip_tool_search(
        in_flight_short=False,
        is_group=True,
        followup_detected=False,
        has_active_task=False,
        has_media=False,
        call_to_self=False,
        intent="工具",
    )


def test_skip_search_followup_still_searches() -> None:
    assert not should_skip_tool_search(
        in_flight_short=False,
        is_group=False,
        followup_detected=True,
        has_active_task=False,
        has_media=False,
        call_to_self=True,
        intent="闲聊",
    )


def test_search_query_default_is_current_only() -> None:
    q = build_tool_search_query("明天下午3点提醒我开会", ["早上好"], ["游戏"])
    assert q == "明天下午3点提醒我开会"
    q2 = build_tool_search_query("改成后天", ["明早八点叫我"], include_recent=True)
    assert "改成后天" in q2 and "明早八点" in q2


def test_kernel_family_close_skips_attach_article() -> None:
    import gsuid_core.ai_core.buildin_tools  # noqa: F401

    names = complete_kernel_family_names(MAIN_AGENT_CORE_TOOLS, exclusive=set())
    assert "search_cognition" in names
    assert "add_once_task" in names
    assert "attach_article" not in names
    assert "list_scheduled_tasks" not in names
