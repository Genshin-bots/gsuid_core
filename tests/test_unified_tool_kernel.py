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


def test_group_request_limit_only_narrowed_for_pure_bystander() -> None:
    """收窄只针对纯旁观轮：点名与省略续聊轮必须拿满 multi_agent_lenth。

    收窄曾把「find_tools → 真正查」这类两跳链路砍到 2 轮，撞线后走无上下文强制总结，
    由此产出「内部库没你的分值」这类把工具缺失讲成数据事实的出戏句。但受害的是
    ellipsis_followup / task_management 轮（``followup_detected``），它们本来就该跑满。
    纯旁观轮（无人寻址 + 无跟进 + 无在途任务）仍需上限兜住零工具空转。
    """
    import gsuid_core.ai_core.agent_run.tools as tools_mod
    from gsuid_core.ai_core.agent_run.tools import group_idle_request_limit
    from gsuid_core.ai_core.configs.ai_config import ai_config

    assert "group_idle_max_iterations" in ai_config.config_list
    assert hasattr(tools_mod, "group_idle_request_limit")

    full = 20
    # 纯旁观轮收窄
    assert (
        group_idle_request_limit(
            full,
            is_group=True,
            followup_detected=False,
            has_active_task=False,
            idle_cap=2,
            call_to_self=False,
        )
        == 2
    )
    # 点名 / 省略续聊 / 在途任务 → 放行拿满
    assert (
        group_idle_request_limit(
            full,
            is_group=True,
            followup_detected=False,
            has_active_task=False,
            idle_cap=2,
            call_to_self=True,
        )
        == full
    )
    assert (
        group_idle_request_limit(
            full,
            is_group=True,
            followup_detected=True,
            has_active_task=False,
            idle_cap=2,
            call_to_self=False,
        )
        == full
    )
    assert (
        group_idle_request_limit(
            full,
            is_group=True,
            followup_detected=False,
            has_active_task=True,
            idle_cap=2,
            call_to_self=False,
        )
        == full
    )
    # 私聊永不收窄
    assert (
        group_idle_request_limit(
            full,
            is_group=False,
            followup_detected=False,
            has_active_task=False,
            idle_cap=2,
            call_to_self=False,
        )
        == full
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
