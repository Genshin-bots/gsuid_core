"""命令代跑的权限、名单和工具表白名单。"""

from pathlib import Path
from collections.abc import Sequence

from sqlmodel import Field

from gsuid_core.models import Event
from gsuid_core.ai_core.tool_risk import HIGH_RISK_TOOL_NAMES
from gsuid_core.ai_core.autonomy.binds import iter_bind_tables, collect_bound_uids
from gsuid_core.ai_core.autonomy.tools import (
    USER_PM,
    CommandHit,
    write_artifact,
    run_guarded_command,
    build_autonomy_tools,
    autonomy_tool_allowed,
)
from gsuid_core.utils.database.base_models import Bind


def _hit(keyword: str, *, kind: str = "command", pm: int = 6, plugin_pm: int = 6) -> CommandHit:
    return CommandHit("plugin", "sv", kind, keyword, pm, plugin_pm)


def test_longest_admin_command_is_not_replaced_by_a_shorter_public_one() -> None:
    import asyncio

    seen: list[Event] = []

    async def _run(hit: CommandHit, event: Event) -> str:
        seen.append(event)
        return f"ok {hit.keyword}"

    async def _go() -> str:
        return await run_guarded_command(
            "签到 今天",
            target_user_id="u1",
            whitelist=frozenset({"u1"}),
            persona_name="p",
            bot_id="onebot",
            bot_self_id="bot",
            ws_bot_id="ws",
            group_id="g1",
            hits=[_hit("签", pm=6), _hit("签到", pm=1)],
            executor=_run,
        )

    assert asyncio.run(_go()) == "权限不足：这条命令需要更高权限"
    assert seen == []


def test_allowed_user_runs_as_a_normal_member_even_if_they_are_a_master() -> None:
    import asyncio

    seen: list[Event] = []

    async def _run(_hit: CommandHit, event: Event) -> str:
        seen.append(event)
        return "查到了"

    async def _go() -> str:
        return await run_guarded_command(
            "面板",
            target_user_id="master",
            whitelist=frozenset({"master"}),
            persona_name="p",
            bot_id="onebot",
            bot_self_id="bot",
            ws_bot_id="",
            group_id=None,
            hits=[_hit("面板")],
            executor=_run,
        )

    assert asyncio.run(_go()) == "查到了"
    assert seen[0].user_pm == USER_PM
    assert seen[0].user_id == "master"
    assert seen[0].user_pm == 6


def test_unknown_user_and_regex_never_execute() -> None:
    import asyncio

    async def _run(_hit: CommandHit, _event: Event) -> str:
        raise AssertionError("不应该执行")

    async def _unknown() -> str:
        return await run_guarded_command(
            "面板",
            target_user_id="stranger",
            whitelist=frozenset({"u1"}),
            persona_name="p",
            bot_id="onebot",
            bot_self_id="",
            ws_bot_id="",
            group_id=None,
            hits=[_hit("面板")],
            executor=_run,
        )

    async def _regex() -> str:
        return await run_guarded_command(
            "随便",
            target_user_id="",
            whitelist=frozenset(),
            persona_name="p",
            bot_id="onebot",
            bot_self_id="",
            ws_bot_id="",
            group_id=None,
            hits=[_hit("随便", kind="regex")],
            executor=_run,
        )

    assert "观察名单" in asyncio.run(_unknown())
    assert "没有匹配" in asyncio.run(_regex())


def test_autonomy_tools_exclude_send_and_high_risk() -> None:
    assert autonomy_tool_allowed("run_bot_command")
    assert not autonomy_tool_allowed("send_message_by_ai")
    for name in HIGH_RISK_TOOL_NAMES:
        assert not autonomy_tool_allowed(name)
    mounted = {tool.name for tool in build_autonomy_tools()}
    assert "run_bot_command" in mounted
    assert "send_message_by_ai" not in mounted
    assert mounted <= {
        "find_tools",
        "web_search_tool",
        "web_fetch_tool",
        "read_image",
        "search_cognition",
        "list_plugin_commands",
        "run_bot_command",
        "artifact_put",
    }


def test_artifact_rejects_empty_and_oversized_notes(tmp_path: Path) -> None:
    assert write_artifact("标题", "  ", persona_name="p", root=tmp_path).startswith("拒绝")
    huge = "字" * (32 * 1024)
    assert "32KB" in write_artifact("标题", huge, persona_name="p", root=tmp_path)
    written = write_artifact("今日笔记", "看过了", persona_name="人格", root=tmp_path)
    assert written.startswith("已写入")
    assert (tmp_path / "人格" / "今日笔记.txt").read_text(encoding="utf-8") == "看过了"


def test_unmapped_bind_subclass_is_skipped_and_explicit_class_is_read() -> None:
    class PlainBind(Bind):
        pass

    class QuotaBind(Bind):
        free_quota: int = Field(default=3)

    class ToyBind(Bind):
        @classmethod
        async def get_uid_by_game(
            cls,
            user_id: str,
            bot_id: str,
            game_name: str | None = None,
        ) -> str | None:
            del game_name
            if bot_id == "bind-test" and user_id == "u1":
                return "UID1"
            return None

    class RowBind(Bind):
        uid: str | None = Field(default=None)
        sr_uid: str | None = Field(default=None)

        @classmethod
        async def select_data_list(
            cls,
            user_id: str,
            bot_id: str | None = None,
        ) -> Sequence["RowBind"] | None:
            del bot_id
            if user_id != "u1":
                return None
            return [
                cls(user_id="u1", bot_id="other", group_id=None, uid="10001_10002", sr_uid="80001"),
                cls(user_id="u1", bot_id="onebot", group_id=None, uid="20001", sr_uid=None),
            ]

    assert PlainBind not in iter_bind_tables()
    assert QuotaBind not in iter_bind_tables()
    assert ToyBind not in iter_bind_tables()

    import asyncio

    found = asyncio.run(collect_bound_uids("u1", "bind-test", classes=[QuotaBind, ToyBind]))
    assert found == {"ToyBind": "UID1"}
    empty = asyncio.run(collect_bound_uids("u1", "onebot", classes=[QuotaBind, ToyBind]))
    assert empty == {}
    by_user = asyncio.run(collect_bound_uids("u1", "", classes=[RowBind]))
    assert by_user == {"RowBind": "10001", "RowBind.sr": "80001"}
    prefer_bot = asyncio.run(collect_bound_uids("u1", "onebot", classes=[RowBind]))
    assert prefer_bot == {"RowBind": "20001", "RowBind.sr": "80001"}
