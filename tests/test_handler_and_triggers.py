"""消息处理与触发器索引测试：最长匹配、权限过滤、IME 空格、索引构建。"""

from __future__ import annotations

import asyncio
from typing import Iterator

import pytest

from gsuid_core.sv import SL, SV, Plugins
from gsuid_core.models import Event
from gsuid_core.handler import _sv_authorized, dispatch_sort_key
from gsuid_core.trigger_index import (
    TriggerIndex,
    get_trigger_index,
    reset_trigger_index,
)

_CHAR_PANEL_RE = r"^(?:yh|异环)?(?P<char_name>[\u4e00-\u9fa5A-Za-z0-9·]{1,16})(?:面板|信息)$"


def _make_event(raw: str, pm: int = 6, bot_self_id: str = "") -> Event:
    ev = Event("OneBot", "123", "m1", "group", "999", "456", {}, pm)
    ev.raw_text = raw
    ev.text = raw
    ev.bot_self_id = bot_self_id
    return ev


@pytest.fixture()
def sample_sv() -> Iterator[SV]:
    name = "TestTriggerUID"
    sv = SV.__new__(SV, name)
    sv.name = name
    sv.priority = 5
    sv.pm = 3
    sv.area = "ALL"
    sv.enabled = True
    sv.black_list = []
    sv.white_list = []
    sv.TL = {}
    sv.plugins = Plugins(
        name=name,
        prefix=["测", "test"],
        force_prefix=["force"],
        allow_empty_prefix=False,
        force=False,
    )
    SL.lst[name] = sv
    try:
        yield sv
    finally:
        SL.lst.pop(name, None)
        SL.plugins.pop(name, None)
        reset_trigger_index()


def test_sv_authorization_and_bot_lists(sample_sv: SV) -> None:
    @sample_sv.on_fullmatch("帮助")
    async def help_handler(bot, ev):
        pass

    assert not _sv_authorized(sample_sv, _make_event("测帮助", pm=6), 6)
    ev_ok = _make_event("测帮助", pm=0)
    assert _sv_authorized(sample_sv, ev_ok, 0)
    assert any(t.check_command(ev_ok) for td in sample_sv.TL.values() for t in td.values())

    sample_sv.plugins.bot_white_list = ["bot1"]
    sample_sv.plugins.bot_black_list = []
    assert _sv_authorized(sample_sv, _make_event("测帮助", pm=1, bot_self_id="bot1"), 1) is True
    assert _sv_authorized(sample_sv, _make_event("测帮助", pm=1, bot_self_id="bot2"), 1) is False
    assert _sv_authorized(sample_sv, _make_event("测帮助", pm=1), 1) is False

    sample_sv.plugins.bot_white_list = ["bot1", "bot2"]
    sample_sv.plugins.bot_black_list = ["bot1"]
    assert _sv_authorized(sample_sv, _make_event("测帮助", pm=1, bot_self_id="bot1"), 1) is False
    assert _sv_authorized(sample_sv, _make_event("测帮助", pm=1, bot_self_id="bot2"), 1) is True


def test_dispatch_sort_and_longest_match(sample_sv: SV) -> None:
    async def _test() -> None:
        @sample_sv.on_command(("绑定", "绑定uid", "绑定UID"))
        async def _dummy(bot, ev):
            return None

        @sample_sv.on_fullmatch(("刷新面板", "面板"), block=True)
        async def _refresh(bot, ev):
            return None

        @sample_sv.on_regex(_CHAR_PANEL_RE, block=True)
        async def _detail(bot, ev):
            return None

        reset_trigger_index()
        index = get_trigger_index()

        ev1 = _make_event("测绑定uid123", pm=0)
        sorted1 = sorted(
            [t for t in index.candidates(ev1) if t.check_command(ev1)],
            key=lambda t: dispatch_sort_key(t, sample_sv.priority),
        )
        assert sorted1[0].keyword == "绑定uid"
        assert (await sorted1[0].get_command(ev1)).text == "123"

        ev2 = _make_event("测绑定123", pm=0)
        sorted2 = sorted(
            [t for t in index.candidates(ev2) if t.check_command(ev2)],
            key=lambda t: dispatch_sort_key(t, sample_sv.priority),
        )
        assert sorted2[0].keyword == "绑定"
        assert (await sorted2[0].get_command(ev2)).text == "123"

        ev3 = _make_event("测刷新面板", pm=0)
        sorted3 = sorted(
            [t for t in index.candidates(ev3) if t.check_command(ev3)],
            key=lambda t: dispatch_sort_key(t, sample_sv.priority),
        )
        assert sorted3[0].type == "fullmatch" and sorted3[0].keyword == "刷新面板"

    asyncio.run(_test())


def test_prefix_gap_cjk_space_and_ascii_boundary(sample_sv: SV) -> None:
    @sample_sv.on_command("绑定")
    async def _cmd(bot, ev):
        pass

    @sample_sv.on_fullmatch("状态")
    async def _fm(bot, ev):
        pass

    @sample_sv.on_command("help")
    async def _h(bot, ev):
        pass

    cmd_trig = next(t for t in sample_sv.TL["command"].values() if t.prefix == "测" and t.keyword == "绑定")
    assert cmd_trig.check_command(_make_event("测 绑定 123", pm=0)) is True
    ev_arg = _make_event("测绑定 123 456", pm=0)
    assert cmd_trig.check_command(ev_arg) is True
    assert asyncio.run(cmd_trig.get_command(ev_arg)).text == "123 456"

    fm_trig = next(t for t in sample_sv.TL["fullmatch"].values() if t.prefix == "测")
    assert fm_trig.check_command(_make_event("测状态", pm=0)) is True
    assert fm_trig.check_command(_make_event("测 状态", pm=0)) is True
    assert fm_trig.check_command(_make_event("测状态 额外参数", pm=0)) is False

    ascii_trig = next(t for t in sample_sv.TL["command"].values() if t.prefix == "test" and t.keyword == "help")
    assert ascii_trig.check_command(_make_event("testhelp", pm=0)) is True
    assert ascii_trig.check_command(_make_event("test help", pm=0)) is True
    assert ascii_trig.check_command(_make_event("testhell", pm=0)) is False


def test_trigger_index_rebuild_and_owner(sample_sv: SV) -> None:
    before = get_trigger_index().version
    assert before > 0

    @sample_sv.on_command("新动态命令")
    async def _dyn(bot, ev):
        pass

    second = get_trigger_index()
    assert second.version > before
    ev = _make_event("测新动态命令", pm=0)
    candidates = list(TriggerIndex().candidates(ev))
    assert any(t.keyword == "新动态命令" for t in candidates)
    for trigger in candidates:
        owner = second.owner_of(trigger)
        assert owner is not None and owner.name == sample_sv.name
