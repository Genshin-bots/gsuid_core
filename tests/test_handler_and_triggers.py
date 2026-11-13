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


def test_inbox_scheduler_settle_and_one_time_settlement() -> None:
    """收件箱调度：运行中到达并入下一轮；dismissed 不复活；TTL 过期；错误重试一次后过期。"""
    from gsuid_core.bot import _Bot
    from gsuid_core.ai_core.inbox.turn import InboxTurn
    from gsuid_core.ai_core.inbox.state import WAKE_STRENGTH, WakeItem, PendingEntry, classify_wake
    from gsuid_core.ai_core.inbox.scheduler import TurnOutcome, InboxScheduler

    async def main() -> None:
        now = [1000.0]

        async def fake_sleep(seconds: float) -> None:
            now[0] += seconds

        calls: list[InboxTurn] = []
        gate = asyncio.Event()
        outcomes: asyncio.Queue[TurnOutcome] = asyncio.Queue()

        async def runner(turn: InboxTurn, ws: _Bot) -> TurnOutcome:
            calls.append(turn)
            gate.set()
            return await outcomes.get()

        sched = InboxScheduler(clock=lambda: now[0], sleep=fake_sleep, runner=runner)
        ws = _Bot("test_ws")

        def entry(seq: int, uid: str = "100000002", created: float | None = None) -> PendingEntry:
            ev = Event("OneBot", "900000001", f"m{seq}", "group", "70001", uid, {}, 6)
            return PendingEntry(
                item=WakeItem(
                    seq=seq,
                    user_id=uid,
                    kind="mention",
                    strength="hard",
                    created_at=now[0] if created is None else created,
                ),
                event=ev,
                persona_name="测试人格",
            )

        async def pump(ticks: int = 12) -> None:
            for _ in range(ticks):
                await asyncio.sleep(0)

        # 1) settle 出首个 turn；运行中到达的 3 项并入下一轮，且只有一轮
        sched.submit(ws, entry(1))
        await pump()
        ctx1 = ws.queue.get_nowait()
        t1 = asyncio.create_task(ctx1.coro)
        await gate.wait()
        for s in (2, 3, 4):
            sched.submit(ws, entry(s, uid="100000003"))
        outcomes.put_nowait(TurnOutcome(reached_model=True, error=False, answered=frozenset({1})))
        await t1
        await pump()
        ctx2 = ws.queue.get_nowait()
        t2 = asyncio.create_task(ctx2.coro)
        await asyncio.sleep(0)
        assert [e.item.seq for e in calls[0].entries] == [1]
        assert [e.item.seq for e in calls[1].entries] == [2, 3, 4]
        inbox = next(iter(sched._inboxes.values()))
        outcomes.put_nowait(TurnOutcome(reached_model=True, error=False, answered=frozenset({2})))
        await t2
        await pump()
        assert ws.queue.empty()
        # 会话清空后三张表一起驱逐，_Bot 引用不常驻
        assert inbox.read_seq == calls[1].head_seq
        assert not sched._inboxes and not sched._ws and not sched._settle_tasks

        # 2) 已落定项不复活：再提交两个新项，下一批只有它们
        for s in (10, 11):
            sched.submit(ws, entry(s, uid="100000003"))
        await pump()
        ctx3 = ws.queue.get_nowait()
        t3 = asyncio.create_task(ctx3.coro)
        await asyncio.sleep(0)
        assert [e.item.seq for e in calls[2].entries] == [10, 11]
        outcomes.put_nowait(TurnOutcome(reached_model=True, error=False, answered=frozenset()))
        await t3
        await pump()
        assert ws.queue.empty()
        assert not sched._inboxes

        # 3) reached_model=False：本批全部 dismissed 且 read_seq 不推进
        e7 = entry(7)
        sched.submit(ws, e7)
        await pump()
        ctx4 = ws.queue.get_nowait()
        t4 = asyncio.create_task(ctx4.coro)
        await asyncio.sleep(0)
        inbox2 = next(iter(sched._inboxes.values()))
        read_before = inbox2.read_seq
        outcomes.put_nowait(TurnOutcome(reached_model=False, error=False, answered=frozenset()))
        await t4
        await pump()
        assert e7.item.state == "dismissed"
        assert inbox2.read_seq == read_before
        assert ws.queue.empty()
        assert not sched._inboxes

        # 4) TTL：过期项直接 expired，不组成批次
        stale = entry(8, created=now[0] - 1000.0)
        sched.submit(ws, stale)
        await pump()
        assert stale.item.state == "expired"
        assert ws.queue.empty()
        assert not sched._inboxes

        # 5) runner 报错：退回 pending 重试一次，再错记 expired
        e9 = entry(9)
        sched.submit(ws, e9)
        await pump()
        first = asyncio.create_task(ws.queue.get_nowait().coro)
        await asyncio.sleep(0)
        outcomes.put_nowait(TurnOutcome(reached_model=False, error=True, answered=frozenset()))
        await first
        await pump()
        retry = asyncio.create_task(ws.queue.get_nowait().coro)
        await asyncio.sleep(0)
        outcomes.put_nowait(TurnOutcome(reached_model=False, error=True, answered=frozenset()))
        await retry
        await pump()
        assert e9.item.state == "expired"
        assert e9.item.retries == 2
        assert ws.queue.empty()
        assert not sched._inboxes and not sched._ws

        # 6) 固定静默窗：私聊 hard 0.8s / 群聊 hard 1.5s / 软唤醒 5.0s
        from gsuid_core.ai_core.inbox.scheduler import (
            _SOFT_SETTLE_S,
            _GROUP_HARD_SETTLE_S,
            _PRIVATE_HARD_SETTLE_S,
        )

        assert (_PRIVATE_HARD_SETTLE_S, _GROUP_HARD_SETTLE_S, _SOFT_SETTLE_S) == (0.8, 1.5, 5.0)
        dm_ev = Event("OneBot", "900000001", "dm1", "direct", None, "100000004", {}, 6)
        soft_ev = Event("OneBot", "900000001", "sf1", "group", "70003", "100000005", {}, 6)
        assert (
            sched._initial_settle_s(
                PendingEntry(WakeItem(1, "100000004", "private", "hard", now[0]), dm_ev, persona_name="测试人格")
            )
            == 0.8
        )
        assert sched._initial_settle_s(entry(1)) == 1.5
        assert (
            sched._initial_settle_s(
                PendingEntry(WakeItem(1, "100000005", "followup", "soft", now[0]), soft_ev, persona_name="测试人格")
            )
            == 5.0
        )

        # 7) 跨 _drop 保留 last_read_seq：中间静默消息不漏读，清史回绕后回到 seq-1
        from gsuid_core.message_history import get_history_manager

        hm = get_history_manager()
        base_ev = Event("OneBot", "900000001", "g0", "group", "70002", "100000002", {}, 6)
        hm.clear_history(base_ev)
        try:
            hm.add_message(base_ev, role="user", content="消息1", metadata={"msg_id": "g1"})
            rec2 = hm.add_message(base_ev, role="user", content="@机器人 消息2", metadata={"msg_id": "g2"})
            assert rec2.link is not None
            sched.submit(
                ws,
                PendingEntry(
                    WakeItem(rec2.link.seq, "100000002", "mention", "hard", now[0]), base_ev, persona_name="测试人格"
                ),
            )
            await pump()
            t_r1 = asyncio.create_task(ws.queue.get_nowait().coro)
            await asyncio.sleep(0)
            outcomes.put_nowait(TurnOutcome(reached_model=True, error=False, answered=frozenset({rec2.link.seq})))
            await t_r1
            await pump()
            assert not sched._inboxes

            hm.add_message(base_ev, role="user", content="中间静默消息3", metadata={"msg_id": "g3"})
            rec4 = hm.add_message(base_ev, role="user", content="@机器人 消息4", metadata={"msg_id": "g4"})
            assert rec4.link is not None
            sched.submit(
                ws,
                PendingEntry(
                    WakeItem(rec4.link.seq, "100000002", "mention", "hard", now[0]), base_ev, persona_name="测试人格"
                ),
            )
            await pump()
            t_r2 = asyncio.create_task(ws.queue.get_nowait().coro)
            await asyncio.sleep(0)
            assert calls[-1].from_seq == rec2.link.seq
            outcomes.put_nowait(TurnOutcome(reached_model=True, error=False, answered=frozenset({rec4.link.seq})))
            await t_r2
            await pump()

            hm.clear_history(base_ev)
            rec_new = hm.add_message(base_ev, role="user", content="清史后消息1", metadata={"msg_id": "g_new1"})
            assert rec_new.link is not None and rec_new.link.seq == 1
            sched.submit(
                ws,
                PendingEntry(
                    WakeItem(rec_new.link.seq, "100000002", "mention", "hard", now[0]), base_ev, persona_name="测试人格"
                ),
            )
            await pump()
            t_r3 = asyncio.create_task(ws.queue.get_nowait().coro)
            await asyncio.sleep(0)
            assert calls[-1].from_seq == 0
            outcomes.put_nowait(TurnOutcome(reached_model=True, error=False, answered=frozenset({1})))
            await t_r3
            await pump()
        finally:
            hm.clear_history(base_ev)

    asyncio.run(main())

    # classify_wake 结构判定：引用 Bot（上游已剔除自动@）为 soft quote_self；点名人格为 hard name
    assert WAKE_STRENGTH["quote_self"] == "soft"
    assert WAKE_STRENGTH["name"] == "hard"
    assert (
        classify_wake(
            user_type="group",
            at_self=False,
            reply_to_author="self",
            raw_text="哈哈哈",
            wake_names=["测试人格"],
            in_followup=False,
            has_at_others=False,
        )
        == "quote_self"
    )
    assert (
        classify_wake(
            user_type="group",
            at_self=False,
            reply_to_author="self",
            raw_text="测试人格 你看看这个",
            wake_names=["测试人格"],
            in_followup=False,
            has_at_others=False,
        )
        == "name"
    )
    assert (
        classify_wake(
            user_type="direct",
            at_self=False,
            reply_to_author=None,
            raw_text="",
            wake_names=["测试人格"],
            in_followup=False,
            has_at_others=False,
        )
        == "private"
    )
