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


def _make_event(raw: str, pm: int = 6) -> Event:
    ev = Event("OneBot", "123", "m1", "group", "999", "456", {}, pm)
    ev.raw_text = raw
    ev.text = raw
    ev.bot_self_id = ""
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


def test_unauthorized_command_does_not_match(sample_sv: SV) -> None:
    @sample_sv.on_fullmatch("帮助")
    async def help_handler(bot, ev):
        pass

    ev = _make_event("测帮助", pm=6)
    matched = []
    if _sv_authorized(sample_sv, ev, ev.user_pm):
        for trigger_dict in sample_sv.TL.values():
            for trigger in trigger_dict.values():
                if trigger.check_command(ev):
                    matched.append(trigger)
    assert matched == []

    ev_ok = _make_event("测帮助", pm=0)
    matched_ok = []
    if _sv_authorized(sample_sv, ev_ok, ev_ok.user_pm):
        for trigger_dict in sample_sv.TL.values():
            for trigger in trigger_dict.values():
                if trigger.check_command(ev_ok):
                    matched_ok.append(trigger)
    assert len(matched_ok) >= 1


def test_sv_bot_white_and_black_list(sample_sv: SV) -> None:
    ev_bot1 = _make_event("测帮助", pm=1)
    ev_bot1.bot_self_id = "bot1"
    ev_bot2 = _make_event("测帮助", pm=1)
    ev_bot2.bot_self_id = "bot2"
    ev_default = _make_event("测帮助", pm=1)

    sample_sv.plugins.bot_white_list = ["bot1"]
    sample_sv.plugins.bot_black_list = []
    assert _sv_authorized(sample_sv, ev_bot1, 1) is True
    assert _sv_authorized(sample_sv, ev_bot2, 1) is False
    assert _sv_authorized(sample_sv, ev_default, 1) is False

    sample_sv.plugins.bot_white_list = ["bot1", "bot2"]
    sample_sv.plugins.bot_black_list = ["bot1"]
    assert _sv_authorized(sample_sv, ev_bot1, 1) is False
    assert _sv_authorized(sample_sv, ev_bot2, 1) is True


def test_longest_match_alias_ordering(sample_sv: SV) -> None:
    async def _test() -> None:
        @sample_sv.on_command(("绑定", "绑定uid", "绑定UID"))
        async def _dummy(bot, ev):
            return None

        reset_trigger_index()
        index = get_trigger_index()

        ev1 = _make_event("测绑定uid123", pm=0)
        candidates1 = [t for t in index.candidates(ev1) if t.check_command(ev1)]
        sorted1 = sorted(candidates1, key=lambda t: dispatch_sort_key(t, sample_sv.priority))
        assert sorted1[0].keyword == "绑定uid"
        ev1_cmd = await sorted1[0].get_command(ev1)
        assert ev1_cmd.text == "123"

        ev2 = _make_event("测绑定123", pm=0)
        candidates2 = [t for t in index.candidates(ev2) if t.check_command(ev2)]
        sorted2 = sorted(candidates2, key=lambda t: dispatch_sort_key(t, sample_sv.priority))
        assert sorted2[0].keyword == "绑定"
        ev2_cmd = await sorted2[0].get_command(ev2)
        assert ev2_cmd.text == "123"

    asyncio.run(_test())


def test_fullmatch_refresh_panel_beats_char_name_regex() -> None:
    name = "TestNtePanelUID"
    sv = SV.__new__(SV, name)
    sv.name = name
    sv.priority = 5
    sv.pm = 6
    sv.area = "ALL"
    sv.enabled = True
    sv.black_list = []
    sv.white_list = []
    sv.TL = {}
    sv.plugins = Plugins(name=name, prefix=["yh"], force_prefix=[], allow_empty_prefix=False, force=True)
    SL.lst[name] = sv
    try:

        @sv.on_fullmatch(("刷新面板", "面板"), block=True)
        async def _refresh(bot, ev):
            return None

        @sv.on_regex(_CHAR_PANEL_RE, block=True)
        async def _detail(bot, ev):
            return None

        reset_trigger_index()
        ev = _make_event("yh刷新面板")
        hits = [t for t in get_trigger_index().candidates(ev) if t.check_command(ev)]
        ordered = sorted(hits, key=lambda t: dispatch_sort_key(t, sv.priority))
        assert ordered[0].type == "fullmatch"
        assert ordered[0].keyword == "刷新面板"
    finally:
        SL.lst.pop(name, None)
        SL.plugins.pop(name, None)
        reset_trigger_index()


def test_command_accepts_gap_between_prefix_and_keyword(sample_sv: SV) -> None:
    @sample_sv.on_command("绑定")
    async def _cmd(bot, ev):
        pass

    ev1 = _make_event("测 绑定 123", pm=0)
    ev2 = _make_event("测绑定 123", pm=0)
    trigger = next(t for t in sample_sv.TL["command"].values() if t.prefix == "测")
    assert trigger.check_command(ev1) is True
    assert trigger.check_command(ev2) is True


def test_cjk_internal_space_is_argument_not_part_of_keyword(sample_sv: SV) -> None:
    @sample_sv.on_command("绑定")
    async def _cmd(bot, ev):
        pass

    ev = _make_event("测绑定 123 456", pm=0)
    trigger = next(t for t in sample_sv.TL["command"].values() if t.prefix == "测")
    assert trigger.check_command(ev) is True
    cmd = asyncio.run(trigger.get_command(ev))
    assert cmd.text == "123 456"


def test_fullmatch_and_prefix_keep_their_boundary(sample_sv: SV) -> None:
    @sample_sv.on_fullmatch("状态")
    async def _fm(bot, ev):
        pass

    trigger = next(t for t in sample_sv.TL["fullmatch"].values() if t.prefix == "测")
    assert trigger.check_command(_make_event("测状态", pm=0)) is True
    assert trigger.check_command(_make_event("测 状态", pm=0)) is True
    assert trigger.check_command(_make_event("测状态 额外参数", pm=0)) is False


def test_short_ascii_prefix_does_not_eat_following_word() -> None:
    name = "TestPrefixBoundary"
    sv = SV.__new__(SV, name)
    sv.name = name
    sv.priority = 5
    sv.pm = 6
    sv.area = "ALL"
    sv.enabled = True
    sv.black_list = []
    sv.white_list = []
    sv.TL = {}
    sv.plugins = Plugins(name=name, prefix=["gs"], force_prefix=[], allow_empty_prefix=False, force=True)
    SL.lst[name] = sv
    try:

        @sv.on_command("help")
        async def _h(bot, ev):
            pass

        trigger = next(t for t in sv.TL["command"].values() if t.prefix == "gs")
        assert trigger.check_command(_make_event("gshelp")) is True
        assert trigger.check_command(_make_event("gs help")) is True
        assert trigger.check_command(_make_event("gshell")) is False
    finally:
        SL.lst.pop(name, None)
        SL.plugins.pop(name, None)


def test_get_trigger_index_rebuilds_on_new_registration(sample_sv: SV) -> None:
    first = get_trigger_index()
    before = first.version
    assert before > 0

    @sample_sv.on_command("新动态命令")
    async def _dyn(bot, ev):
        pass

    second = get_trigger_index()
    assert second.version > before
    ev = _make_event("测新动态命令", pm=0)
    assert any(t.keyword == "新动态命令" for t in second.candidates(ev))


def test_owner_maps_back_to_sv(sample_sv: SV) -> None:
    @sample_sv.on_command("帮助")
    async def _h(bot, ev):
        pass

    index = TriggerIndex()
    ev = _make_event("测帮助", pm=0)
    candidates = list(index.candidates(ev))
    assert len(candidates) >= 1
    for trigger in candidates:
        owner = index.owner_of(trigger)
        assert owner is not None
        assert owner.name == sample_sv.name
