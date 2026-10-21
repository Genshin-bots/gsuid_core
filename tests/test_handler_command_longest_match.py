from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from gsuid_core.sv import SL, SV, Plugins
from gsuid_core.models import Event
from gsuid_core.trigger_index import get_trigger_index, reset_trigger_index


@pytest.fixture()
def bind_test_sv() -> Iterator[SV]:
    name = "TestBindMatchUID"
    sv = SV.__new__(SV, name)
    sv.name = name
    sv.priority = 5
    sv.pm = 6
    sv.area = "ALL"
    sv.enabled = True
    sv.black_list = []
    sv.white_list = []
    sv.TL = {}
    sv.plugins = Plugins(
        name=name,
        prefix=["zzz"],
        force_prefix=[],
        allow_empty_prefix=False,
        force=True,
    )
    SL.lst[name] = sv
    try:
        yield sv
    finally:
        SL.lst.pop(name, None)
        SL.plugins.pop(name, None)
        reset_trigger_index()


def _make_event(raw: str) -> Event:
    return Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g1",
        user_id="u1",
        user_pm=6,
        raw_text=raw,
        WS_BOT_ID="ws1",
    )


def test_longest_match_alias_ordering(bind_test_sv: SV) -> None:
    async def _test() -> None:
        # 故意将短命令写在前面，验证最长匹配排序不依赖注册元组顺序
        @bind_test_sv.on_command(("绑定", "绑定uid", "绑定UID"))
        async def _dummy(bot, ev):  # noqa: ANN001, ANN202
            return None

        reset_trigger_index()
        index = get_trigger_index()

        # 1. 用户输入 zzz绑定uid123 -> 必须优先排 绑定uid，text 为 123
        ev1 = _make_event("zzz绑定uid123")
        candidates1 = [t for t in index.candidates(ev1) if t.check_command(ev1)]
        command_triggers1 = {t: bind_test_sv.priority for t in candidates1}
        sorted1 = sorted(
            command_triggers1.items(),
            key=lambda x: (not x[0].prefix, x[1], -len(x[0].head)),
        )
        first_trigger1 = sorted1[0][0]
        assert first_trigger1.keyword == "绑定uid"
        ev1_cmd = await first_trigger1.get_command(ev1)
        assert ev1_cmd.text == "123"
        assert ev1_cmd.command == "绑定uid"

        # 2. 用户输入 zzz绑定123 -> 只能匹配 绑定，text 为 123
        ev2 = _make_event("zzz绑定123")
        candidates2 = [t for t in index.candidates(ev2) if t.check_command(ev2)]
        command_triggers2 = {t: bind_test_sv.priority for t in candidates2}
        sorted2 = sorted(
            command_triggers2.items(),
            key=lambda x: (not x[0].prefix, x[1], -len(x[0].head)),
        )
        assert len(sorted2) == 1
        assert sorted2[0][0].keyword == "绑定"
        ev2_cmd = await sorted2[0][0].get_command(ev2)
        assert ev2_cmd.text == "123"
        assert ev2_cmd.command == "绑定"

        # 3. 用户输入 zzz绑定uid -> 两者命中时优先 绑定uid
        ev3 = _make_event("zzz绑定uid")
        candidates3 = [t for t in index.candidates(ev3) if t.check_command(ev3)]
        command_triggers3 = {t: bind_test_sv.priority for t in candidates3}
        sorted3 = sorted(
            command_triggers3.items(),
            key=lambda x: (not x[0].prefix, x[1], -len(x[0].head)),
        )
        assert sorted3[0][0].keyword == "绑定uid"
        ev3_cmd = await sorted3[0][0].get_command(ev3)
        assert ev3_cmd.text == ""

    asyncio.run(_test())
