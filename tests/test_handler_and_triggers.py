"""消息处理与触发器索引测试：最长匹配、权限过滤、IME 空格、索引构建。"""

from __future__ import annotations

import sys
import asyncio
from typing import Iterator
from pathlib import Path

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

        # 5) runner 报错：模型前异常退回重试一次；已达模型的故障直接 expired 不整批重跑
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

        e9_model = entry(19)
        sched.submit(ws, e9_model)
        await pump()
        model_fail = asyncio.create_task(ws.queue.get_nowait().coro)
        await asyncio.sleep(0)
        outcomes.put_nowait(TurnOutcome(reached_model=True, error=True, answered=frozenset()))
        await model_fail
        await pump()
        assert e9_model.item.state == "expired"
        assert e9_model.item.retries == 0
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


def test_quoted_image_attribution_and_group_multi_speaker_coalesce() -> None:
    """引用图不冒充发言人新发图、无关斗图不强制挂起读图，且群聊多人连发点名自动顺延聚批。"""
    from gsuid_core.bot import _Bot
    from gsuid_core.models import Message, MessageReceive
    from gsuid_core.handler import msg_process, _record_inbound_history
    from gsuid_core.ai_core.utils import prepare_media_payload
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.inbox.turn import InboxTurn
    from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry
    from gsuid_core.ai_core.turn_pipeline import extract_pending_unread_images
    from gsuid_core.ai_core.inbox.scheduler import TurnOutcome, InboxScheduler

    async def scenario() -> None:
        hm = get_history_manager()
        base_ev = Event("OneBot", "900000001", "m0", "group", "70009", "100000001", {}, 6)
        hm.clear_history(base_ev)
        try:
            # 1) Bot 先发出一张命令回执图（#1），用户A 引用该回执图调侃（current + quoted 顺序）
            hm.add_message(
                base_ev,
                role="assistant",
                content="[图片/文件]",
                metadata={"msg_id": "bot_card_1", "speech_channel": "command"},
            )
            raw_quote = MessageReceive(
                bot_id="OneBot",
                bot_self_id="900000001",
                msg_id="u_quote_1",
                user_type="group",
                group_id="70009",
                user_id="100000002",
                sender={"nickname": "测试用户B"},
                content=[
                    Message("text", "测试人格，注意防潮"),
                    Message("reply_id", "bot_card_1"),
                    Message("image", "https://example.invalid/bot_card.png"),
                    Message("reply", ""),
                ],
            )
            ev_quote = await msg_process(raw_quote)
            # 传统命令插件仍能读到 ev.image_id_list，但引用图被精准拆入 reply_image_id_list
            assert len(ev_quote.image_id_list) == 1
            assert ev_quote.reply_image_id_list == ev_quote.image_id_list
            rec_q = _record_inbound_history(ev_quote)
            assert rec_q is not None and "image_id_list" not in rec_q.metadata
            assert "images" not in rec_q.metadata
            assert rec_q.metadata["reply_image_id_list"] == ev_quote.reply_image_id_list

            # 1a) 引用自动附带单次前置 @Bot 与显式手动 @Bot 区分（兼容 reply 在前/在后两种段序）
            ev_auto_1 = await msg_process(
                MessageReceive(
                    bot_id="OneBot",
                    bot_self_id="900000001",
                    msg_id="u_auto_at_1",
                    user_type="group",
                    group_id="70009",
                    user_id="100000002",
                    sender={"nickname": "测试用户B"},
                    content=[
                        Message("reply_id", "out_of_window_bot_msg"),
                        Message("reply", "测试播报正文"),
                        Message("at", "900000001"),
                        Message("text", "这"),
                    ],
                )
            )
            assert ev_auto_1.at_bot is True and ev_auto_1.quote_auto_at is True
            rec_auto_1 = _record_inbound_history(ev_auto_1)
            assert rec_auto_1 is not None and rec_auto_1.link is not None
            assert rec_auto_1.link.at_self is False and rec_auto_1.link.reply_to_author == "self"

            ev_explicit_2 = await msg_process(
                MessageReceive(
                    bot_id="OneBot",
                    bot_self_id="900000001",
                    msg_id="u_explicit_at_2",
                    user_type="group",
                    group_id="70009",
                    user_id="100000002",
                    sender={"nickname": "测试用户B"},
                    content=[
                        Message("reply_id", "out_of_window_bot_msg"),
                        Message("reply", "测试播报正文"),
                        Message("at", "900000001"),
                        Message("at", "900000001"),
                        Message("text", "帮我看看"),
                    ],
                )
            )
            assert ev_explicit_2.at_bot is True and ev_explicit_2.quote_auto_at is False
            rec_exp_2 = _record_inbound_history(ev_explicit_2)
            assert rec_exp_2 is not None and rec_exp_2.link is not None
            assert rec_exp_2.link.at_self is True

            payload, _ = await prepare_media_payload([(2, ev_quote)], "[群消息 #1–#2]\n#2: 测试人格，注意防潮")
            joined = "\n".join(p for p in payload if isinstance(p, str))
            assert "引用的历史图片" in joined and "用户发送了图片" not in joined

            # 1b) 二次引用：用户C 引用用户B那条纯文字调侃并附带本人新发图（[reply_id, reply, image, text] 顺序）
            # 用户B 的记录不含 images，用户C 自己的新图必须归为本人新发而非历史引用图
            ev_chain = await msg_process(
                MessageReceive(
                    bot_id="OneBot",
                    bot_self_id="900000001",
                    msg_id="u_chain_own_img",
                    user_type="group",
                    group_id="70009",
                    user_id="100000005",
                    sender={"nickname": "测试用户C"},
                    content=[
                        Message("reply_id", "u_quote_1"),
                        Message("reply", "测试人格，注意防潮"),
                        Message("image", "https://example.invalid/user_c_own.png"),
                        Message("text", "帮我看下这张新图"),
                    ],
                )
            )
            rec_chain = _record_inbound_history(ev_chain)
            assert ev_chain.reply_image_id_list == []
            assert rec_chain is not None and rec_chain.metadata["image_id_list"] == ev_chain.image_id_list
            payload_c, _ = await prepare_media_payload([(3, ev_chain)], "[群消息 #2–#3]\n#3: 帮我看下这张新图")
            joined_c = "\n".join(p for p in payload_c if isinstance(p, str))
            assert "用户发送了图片" in joined_c and "引用的历史图片" not in joined_c

            # 2) quoted + current 适配器顺序同样能归入 reply_image_id_list；随后同人调侃不会把被引用图捞成待读图
            raw_quote_pre = MessageReceive(
                bot_id="OneBot",
                bot_self_id="900000001",
                msg_id="u_quote_2",
                user_type="group",
                group_id="70009",
                user_id="100000002",
                sender={"nickname": "测试用户B"},
                content=[
                    Message("reply_id", "bot_card_1"),
                    Message("reply", ""),
                    Message("image", "https://example.invalid/bot_card.png"),
                    Message("text", "不用代理能存上去？"),
                ],
            )
            ev_quote_pre = await msg_process(raw_quote_pre)
            assert ev_quote_pre.reply_image_id_list == ev_quote_pre.image_id_list
            _record_inbound_history(ev_quote_pre)

            ev_followup = await msg_process(
                MessageReceive(
                    bot_id="OneBot",
                    bot_self_id="900000001",
                    msg_id="u_joke_3",
                    user_type="group",
                    group_id="70009",
                    user_id="100000002",
                    content=[Message("text", "给测试人格装上")],
                )
            )
            _record_inbound_history(ev_followup)
            assert extract_pending_unread_images(ev_followup, set()) == []

            # 3) 对群里别人发的表情包不进入待读图；紧邻纯图+追问则进入中性参考块
            ev_to_other = await msg_process(
                MessageReceive(
                    bot_id="OneBot",
                    bot_self_id="900000001",
                    msg_id="u_meme_other",
                    user_type="group",
                    group_id="70009",
                    user_id="100000003",
                    content=[Message("at", "100000001"), Message("image", "https://example.invalid/meme.png")],
                )
            )
            _record_inbound_history(ev_to_other)
            ev_ask_bot = await msg_process(
                MessageReceive(
                    bot_id="OneBot",
                    bot_self_id="900000001",
                    msg_id="u_ask_meme",
                    user_type="group",
                    group_id="70009",
                    user_id="100000003",
                    content=[Message("text", "测试人格来点表情包")],
                )
            )
            _record_inbound_history(ev_ask_bot)
            assert extract_pending_unread_images(ev_ask_bot, set()) == []

            ev_split_img = await msg_process(
                MessageReceive(
                    bot_id="OneBot",
                    bot_self_id="900000001",
                    msg_id="u_split_1",
                    user_type="group",
                    group_id="70009",
                    user_id="100000004",
                    content=[Message("image", "https://example.invalid/doc.png")],
                )
            )
            _record_inbound_history(ev_split_img)
            ev_split_q = await msg_process(
                MessageReceive(
                    bot_id="OneBot",
                    bot_self_id="900000001",
                    msg_id="u_split_2",
                    user_type="group",
                    group_id="70009",
                    user_id="100000004",
                    content=[Message("text", "测试人格帮我看下这张图")],
                )
            )
            _record_inbound_history(ev_split_q)
            pending_imgs = extract_pending_unread_images(ev_split_q, set())
            assert len(pending_imgs) == 1
            split_payload, _ = await prepare_media_payload(
                [(9, ev_split_q)],
                "[群消息 #8–#9]",
                pending_unread_images=pending_imgs,
            )
            split_joined = "\n".join(p for p in split_payload if isinstance(p, str))
            assert "仅当本句确在追问或讨论该图时调用" in split_joined
            assert "对方正在追问上面刚发出的待讨论图片" not in split_joined

            # 4) 仅同说话人连发顺延合并；不同说话人的 hard 点名不延长当前结算窗且独立成轮
            now = [2000.0]
            sleeps: list[float] = []
            batches: list[InboxTurn] = []

            async def manual_sleep(sec: float) -> None:
                sleeps.append(sec)
                now[0] += sec

            async def ok_runner(turn: InboxTurn, _ws: _Bot) -> TurnOutcome:
                batches.append(turn)
                return TurnOutcome(
                    reached_model=True, error=False, answered=frozenset(e.item.seq for e in turn.entries)
                )

            sched = InboxScheduler(clock=lambda: now[0], sleep=manual_sleep, runner=ok_runner)
            ws = _Bot("coalesce_ws")
            ev_u1a = Event("OneBot", "900000001", "c1a", "group", "70009", "100000001", {}, 6, raw_text="在吗")
            ev_u1b = Event("OneBot", "900000001", "c1b", "group", "70009", "100000001", {}, 6, raw_text="帮我查个东西")
            ev_u2 = Event("OneBot", "900000001", "c2", "group", "70009", "100000002", {}, 6, raw_text="我也问一下")
            sched.submit(
                ws, PendingEntry(WakeItem(21, "100000001", "name", "hard", now[0]), ev_u1a, persona_name="测试人格")
            )
            key = (ev_u1a.session_id, "测试人格")
            first_deadline = sched._inboxes[key].settle_deadline
            sched.submit(
                ws,
                PendingEntry(WakeItem(22, "100000001", "name", "hard", now[0] + 0.3), ev_u1b, persona_name="测试人格"),
            )
            extended_deadline = sched._inboxes[key].settle_deadline
            assert extended_deadline > first_deadline
            sched.submit(
                ws,
                PendingEntry(WakeItem(23, "100000002", "name", "hard", now[0] + 0.5), ev_u2, persona_name="测试人格"),
            )
            assert sched._inboxes[key].settle_deadline == extended_deadline
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            assert len(batches) == 2
            assert [e.item.seq for e in batches[0].entries] == [21, 22]
            assert [e.item.seq for e in batches[1].entries] == [23]

            # 5) 同批全等折叠 + 跨轮零工具 +1 消解 + 有工具观测跨人放行（复刻在途复读场景）
            batches.clear()
            in_flight_gate = asyncio.Event()
            release_turn1 = asyncio.Event()
            emit_tool_obs = [False]

            async def gated_runner(turn: InboxTurn, _ws: _Bot) -> TurnOutcome:
                batches.append(turn)
                if emit_tool_obs[0]:
                    turn.observations.append("ob_test0001")
                if len(batches) == 1:
                    in_flight_gate.set()
                    await release_turn1.wait()
                return TurnOutcome(
                    reached_model=True, error=False, answered=frozenset(e.item.seq for e in turn.entries)
                )

            sched_dup = InboxScheduler(clock=lambda: now[0], sleep=manual_sleep, runner=gated_runner)
            ev_r1 = Event(
                "OneBot", "900000001", "r1", "group", "70009", "100000001", {}, 6, raw_text="测试人格，发测试凭据给我"
            )
            ev_r2 = Event(
                "OneBot", "900000001", "r2", "group", "70009", "100000002", {}, 6, raw_text="测试人格，发测试凭据给我"
            )
            sched_dup.submit(
                ws, PendingEntry(WakeItem(31, "100000001", "name", "hard", now[0]), ev_r1, persona_name="测试人格")
            )
            for _ in range(12):
                await asyncio.sleep(0)
            t_run1 = asyncio.create_task(ws.queue.get_nowait().coro)
            await in_flight_gate.wait()
            # 第 1 轮 running 期间（+14s）用户B 发送全等 +1 消息
            now[0] += 14.0
            sched_dup.submit(
                ws, PendingEntry(WakeItem(32, "100000002", "name", "hard", now[0]), ev_r2, persona_name="测试人格")
            )
            release_turn1.set()
            await t_run1
            for _ in range(12):
                await asyncio.sleep(0)
            # 上一轮零工具调用，用户B 的 +1 全等消息在 _build_turn 直接消解，不起第 2 轮
            assert ws.queue.empty() and len(batches) == 1

            # 同批去重消解同说话人的连点重发；不同说话人自动拆到下一轮（有工具观测时跨人放行）
            ev_b1 = Event(
                "OneBot", "900000001", "b1", "group", "70009", "100000001", {}, 6, raw_text="测试人格，领每日奖励"
            )
            ev_b1_dup = Event(
                "OneBot", "900000001", "b1_dup", "group", "70009", "100000001", {}, 6, raw_text="测试人格，领每日奖励"
            )
            ev_b2 = Event(
                "OneBot", "900000001", "b2", "group", "70009", "100000002", {}, 6, raw_text="测试人格，领每日奖励"
            )
            ev_b3 = Event(
                "OneBot",
                "900000001",
                "b3",
                "group",
                "70009",
                "100000001",
                {},
                6,
                raw_text="测试人格，领每日奖励",
                image_id="img_own_1",
                image_id_list=["img_own_1"],
            )
            emit_tool_obs[0] = True
            sched_dup.submit(
                ws, PendingEntry(WakeItem(41, "100000001", "name", "hard", now[0]), ev_b1, persona_name="测试人格")
            )
            sched_dup.submit(
                ws,
                PendingEntry(
                    WakeItem(42, "100000001", "name", "hard", now[0] + 0.1), ev_b1_dup, persona_name="测试人格"
                ),
            )
            sched_dup.submit(
                ws,
                PendingEntry(WakeItem(43, "100000002", "name", "hard", now[0] + 0.2), ev_b2, persona_name="测试人格"),
            )
            sched_dup.submit(
                ws,
                PendingEntry(WakeItem(44, "100000001", "name", "hard", now[0] + 0.3), ev_b3, persona_name="测试人格"),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            assert [e.item.seq for e in batches[1].entries] == [41, 44]
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            assert [e.item.seq for e in batches[2].entries] == [43]

            # 上一轮产生了工具观测（emit_tool_obs=True）：同人重发仍消解，跨人 +1 正常放行
            sched_dup.submit(
                ws,
                PendingEntry(WakeItem(45, "100000001", "name", "hard", now[0] + 5.0), ev_b1, persona_name="测试人格"),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            assert ws.queue.empty()
            ev_b4 = Event(
                "OneBot", "900000001", "b4", "group", "70009", "100000004", {}, 6, raw_text="测试人格，领每日奖励"
            )
            sched_dup.submit(
                ws,
                PendingEntry(WakeItem(46, "100000004", "name", "hard", now[0] + 6.0), ev_b4, persona_name="测试人格"),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            assert [e.item.seq for e in batches[3].entries] == [46]

            # at_bot 维度区分：无 @ 的软唤醒已答不应吞掉后续显式 @Bot 的同文呼叫；未应答项不写入去重缓存
            emit_tool_obs[0] = False
            answer_none = [True]

            async def selective_runner(turn: InboxTurn, _ws: _Bot) -> TurnOutcome:
                batches.append(turn)
                if emit_mem_dep[0]:
                    turn.memory_dependent = True
                ans = frozenset() if answer_none[0] else frozenset(e.item.seq for e in turn.entries)
                return TurnOutcome(reached_model=True, error=False, answered=ans)

            emit_mem_dep = [False]
            sched_sel = InboxScheduler(clock=lambda: now[0], sleep=manual_sleep, runner=selective_runner)
            ev_unans = Event("OneBot", "900000001", "u1", "group", "70009", "100000001", {}, 6, raw_text="在吗")
            sched_sel.submit(
                ws,
                PendingEntry(WakeItem(51, "100000001", "followup", "soft", now[0]), ev_unans, persona_name="测试人格"),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            # #51 未应答（answered 为空），用户重发不会被吞
            answer_none[0] = False
            sched_sel.submit(
                ws,
                PendingEntry(
                    WakeItem(52, "100000001", "followup", "soft", now[0] + 1.0), ev_unans, persona_name="测试人格"
                ),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            # #52（at_bot=False）已应答后，另一人显式 @Bot（at_bot=True）发「在吗」仍正常放行
            ev_at = Event(
                "OneBot", "900000001", "u2", "group", "70009", "100000002", {}, 6, raw_text="在吗", at_bot=True
            )
            sched_sel.submit(
                ws,
                PendingEntry(
                    WakeItem(53, "100000002", "mention", "hard", now[0] + 2.0), ev_at, persona_name="测试人格"
                ),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            assert [e.item.seq for e in batches[-1].entries] == [53]

            # at_list 维度区分：@测试用户甲 与 @测试用户乙 的同正文请求不会互相吞掉
            ev_at_u1 = Event(
                "OneBot",
                "900000001",
                "u_at1",
                "group",
                "70009",
                "100000001",
                {},
                6,
                raw_text="测试人格帮我看看",
                at="100000008",
                at_list=["100000008"],
            )
            ev_at_u2 = Event(
                "OneBot",
                "900000001",
                "u_at2",
                "group",
                "70009",
                "100000002",
                {},
                6,
                raw_text="测试人格帮我看看",
                at="100000009",
                at_list=["100000009"],
            )
            sched_sel.submit(
                ws,
                PendingEntry(
                    WakeItem(54, "100000001", "name", "hard", now[0] + 3.0), ev_at_u1, persona_name="测试人格"
                ),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            sched_sel.submit(
                ws,
                PendingEntry(
                    WakeItem(55, "100000002", "name", "hard", now[0] + 4.0), ev_at_u2, persona_name="测试人格"
                ),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            assert [e.item.seq for e in batches[-1].entries] == [55]

            # 零工具但依赖个人记忆（memory_dependent=True）：同人重发消解，跨人同问放行
            emit_mem_dep[0] = True
            ev_mem1 = Event(
                "OneBot",
                "900000001",
                "m_q1",
                "group",
                "70009",
                "100000001",
                {},
                6,
                raw_text="测试人格，我上次说的那个怎么样",
            )
            ev_mem2 = Event(
                "OneBot",
                "900000001",
                "m_q2",
                "group",
                "70009",
                "100000002",
                {},
                6,
                raw_text="测试人格，我上次说的那个怎么样",
            )
            sched_sel.submit(
                ws,
                PendingEntry(WakeItem(56, "100000001", "name", "hard", now[0] + 5.0), ev_mem1, persona_name="测试人格"),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            sched_sel.submit(
                ws,
                PendingEntry(WakeItem(57, "100000001", "name", "hard", now[0] + 6.0), ev_mem1, persona_name="测试人格"),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            assert ws.queue.empty()
            sched_sel.submit(
                ws,
                PendingEntry(WakeItem(58, "100000002", "name", "hard", now[0] + 7.0), ev_mem2, persona_name="测试人格"),
            )
            for _ in range(12):
                await asyncio.sleep(0)
            await ws.queue.get_nowait().coro
            assert [e.item.seq for e in batches[-1].entries] == [58]

            # 未说完起手式（纯 @Bot / 极短呼名）首窗拉长至 4.0s；完整问句保持 1.5s
            ev_bare_at = Event("OneBot", "900000001", "bare1", "group", "70009", "100000001", {}, 6, raw_text="")
            ev_bare_at.at_bot = True
            ev_short_name = Event(
                "OneBot", "900000001", "sn1", "group", "70009", "100000001", {}, 6, raw_text="测试人格"
            )
            ev_full_q = Event(
                "OneBot", "900000001", "fq1", "group", "70009", "100000001", {}, 6, raw_text="今日指标如何？"
            )
            assert (
                sched_sel._initial_settle_s(
                    PendingEntry(
                        WakeItem(60, "100000001", "mention", "hard", now[0]), ev_bare_at, persona_name="测试人格"
                    )
                )
                == 4.0
            )
            assert (
                sched_sel._initial_settle_s(
                    PendingEntry(
                        WakeItem(61, "100000001", "name", "hard", now[0]), ev_short_name, persona_name="测试人格"
                    )
                )
                == 4.0
            )
            assert (
                sched_sel._initial_settle_s(
                    PendingEntry(
                        WakeItem(62, "100000001", "mention", "hard", now[0]), ev_full_q, persona_name="测试人格"
                    )
                )
                == 1.5
            )
            ev_short_greet = Event("OneBot", "900000001", "sg1", "group", "70009", "100000001", {}, 6, raw_text="你好")
            ev_short_greet.at_bot = True
            ev_name_greet = Event(
                "OneBot", "900000001", "ng1", "group", "70009", "100000001", {}, 6, raw_text="测试人格早安"
            )
            assert (
                sched_sel._initial_settle_s(
                    PendingEntry(
                        WakeItem(64, "100000001", "mention", "hard", now[0]), ev_short_greet, persona_name="测试人格"
                    )
                )
                == 1.5
            )
            assert (
                sched_sel._initial_settle_s(
                    PendingEntry(
                        WakeItem(65, "100000001", "name", "hard", now[0]), ev_name_greet, persona_name="测试人格"
                    )
                )
                == 1.5
            )
            # 别名起手式（如小名/关键词）剥除后也触发 4.0s 首窗
            from gsuid_core.ai_core.memory import group_profile as _gp

            _orig_surfaces = _gp.collect_persona_surfaces
            _gp.collect_persona_surfaces = lambda _name: ("测试别名",)
            try:
                ev_alias_name = Event(
                    "OneBot", "900000001", "an1", "group", "70009", "100000001", {}, 6, raw_text="测试别名"
                )
                assert (
                    sched_sel._initial_settle_s(
                        PendingEntry(
                            WakeItem(63, "100000001", "name", "hard", now[0]), ev_alias_name, persona_name="测试人格"
                        )
                    )
                    == 4.0
                )
            finally:
                _gp.collect_persona_surfaces = _orig_surfaces

            # 活跃大群速率门控 + parse_reply_plan 禁止引用非本轮 pending seq
            from gsuid_core.message_history import MessageRecord
            from gsuid_core.ai_core.reply_plan import parse_reply_plan
            from gsuid_core.ai_core.followup_window import group_is_too_busy_for_followup

            busy_recs = [
                MessageRecord(
                    role="user",
                    content=f"消息{k}",
                    user_id=f"10000010{k % 5}",
                    user_name="群友",
                    timestamp=1000.0 - k * 2,
                )
                for k in range(12)
            ]
            assert (
                group_is_too_busy_for_followup(busy_recs, speaker_uid="100000001", now=1000.0, busy_per_min=12) is True
            )
            assert (
                group_is_too_busy_for_followup(busy_recs[:3], speaker_uid="100000001", now=1000.0, busy_per_min=12)
                is False
            )

            plan_bad = parse_reply_plan(
                "<reply #65>\n旧消息回复",
                valid_seqs=frozenset({72}),
                default_seqs=(72,),
                max_targets=3,
            )
            assert plan_bad.invalid_refs == (65,) and not any(65 in g.seqs for g in plan_bad.groups)
            plan_ok = parse_reply_plan(
                "<reply #72>\n本轮回复",
                valid_seqs=frozenset({72}),
                default_seqs=(72,),
                max_targets=3,
            )
            assert plan_ok.invalid_refs == () and plan_ok.groups[0].seqs == (72,)
        finally:
            hm.clear_history(base_ev)

    asyncio.run(scenario())


def test_reload_plugin_shutdown_before_start_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """热重载先断流并在旧模块存活期跑完 shutdown，再导入新版与启动，兼顾并发与清理。"""
    import gsuid_core.server as server_mod
    from gsuid_core.sv import config_plugins
    from gsuid_core.config import plugin_config_store
    from gsuid_core.utils.plugins_update import reload_plugin as reload_mod
    from gsuid_core.webconsole.mount_app import GsAdminModel, site

    plugin_name = "TestReloadPlugin"
    sv_name = "TestReloadSV"
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / plugin_name
    sub_dir = plugin_dir / "main"
    sub_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "__full__.py").write_text("", encoding="utf-8")
    init_file = sub_dir / "__init__.py"
    state_file = plugin_dir / "state.py"
    events_file = tmp_path / "events.log"

    def write_plugin(
        version: int,
        command: str,
        *,
        fail: bool = False,
        shutdown_delay: float = 0.01,
        slow_start: bool = False,
        with_hooks: bool = True,
    ) -> None:
        state_file.write_text(f"VERSION = {version}\n", encoding="utf-8")
        if fail:
            init_file.write_text('raise RuntimeError("reload_boom")\n', encoding="utf-8")
            return
        body = [
            "import asyncio",
            "from pathlib import Path",
            "from gsuid_core.sv import SV",
            "from gsuid_core.server import on_core_start, on_core_shutdown",
            f"_LOG = Path({str(events_file)!r})",
            'with _LOG.open("a", encoding="utf-8") as _f:',
            f'    _f.write("import:{version}\\n")',
            f"sv = SV({sv_name!r})",
            f"@sv.on_fullmatch({command!r})",
            "async def _cmd(bot, ev):",
            "    return None",
        ]
        if with_hooks:
            if slow_start:
                body.extend(
                    [
                        "@on_core_start",
                        "async def _start():",
                        '    with _LOG.open("a", encoding="utf-8") as f:',
                        f'        f.write("start:{version}:begin\\n")',
                        "    try:",
                        "        await asyncio.sleep(5)",
                        "    except asyncio.CancelledError:",
                        '        with _LOG.open("a", encoding="utf-8") as f:',
                        f'            f.write("start:{version}:cancelled\\n")',
                        "        raise",
                    ]
                )
            else:
                body.extend(
                    [
                        "@on_core_start",
                        "async def _start():",
                        f"    from {plugin_name} import state",
                        '    with _LOG.open("a", encoding="utf-8") as f:',
                        f'        f.write(f"start:{version}:state={{state.VERSION}}\\n")',
                        "@on_core_shutdown",
                        "async def _stop():",
                        f"    from {plugin_name} import state",
                        '    with _LOG.open("a", encoding="utf-8") as f:',
                        f'        f.write(f"shutdown:{version}:state={{state.VERSION}}:begin\\n")',
                        f"    await asyncio.sleep({shutdown_delay})",
                        '    with _LOG.open("a", encoding="utf-8") as f:',
                        f'        f.write(f"shutdown:{version}:state={{state.VERSION}}:done\\n")',
                    ]
                )
        init_file.write_text("\n".join(body) + "\n", encoding="utf-8")

    def read_events() -> list[str]:
        if not events_file.exists():
            return []
        return events_file.read_text(encoding="utf-8").splitlines()

    async def wait_for_event(marker: str) -> None:
        for _ in range(200):
            if marker in read_events():
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"Timed out waiting for event {marker!r}; got {read_events()}")

    def matches_command(cmd: str) -> bool:
        ev = _make_event(cmd, pm=6)
        idx = get_trigger_index()
        return any(
            (owner := idx.owner_of(trig)) is not None and owner.name == sv_name and trig.check_command(ev)
            for trig in idx.candidates(ev)
        )

    class _DummyAdmin(GsAdminModel):
        pass

    monkeypatch.setattr(server_mod, "PLUGIN_PATH", plugins_dir)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    async def scenario() -> None:
        try:
            # 1) 首次异步重载：注册命令与启停钩子，验证索引可命中
            write_plugin(1, "测重指令一")
            res1 = await reload_mod.reload_plugin_async(plugin_name)
            assert res1.startswith("✨")
            await reload_mod._plugin_start_tasks[plugin_name]
            assert read_events() == ["import:1", "start:1:state=1"]
            assert matches_command("测重指令一") is True

            # 2) 二次异步重载：旧 shutdown 延迟导入必须命中 v1 且先于 v2 import；同步清管理模型
            site.plugins_page[plugin_name] = [_DummyAdmin]
            write_plugin(2, "测重指令二", shutdown_delay=0.04)
            res2 = await reload_mod.reload_plugin_async(plugin_name)
            assert res2.startswith("✨")
            assert plugin_name not in site.plugins_page
            await reload_mod._plugin_start_tasks[plugin_name]
            assert read_events() == [
                "import:1",
                "start:1:state=1",
                "shutdown:1:state=1:begin",
                "shutdown:1:state=1:done",
                "import:2",
                "start:2:state=2",
            ]
            assert matches_command("测重指令一") is False
            assert matches_command("测重指令二") is True

            # 3) 在途 shutdown 期间并发触发下一次异步重载：串行保底不断开 v2，未启动的中间版不误跑 shutdown
            before_step3 = len(read_events())
            write_plugin(4, "测重指令四", shutdown_delay=0.04)
            t3 = asyncio.create_task(reload_mod.reload_plugin_async(plugin_name))
            await wait_for_event("shutdown:2:state=2:begin")
            t4 = asyncio.create_task(reload_mod.reload_plugin_async(plugin_name))
            res3, res4 = await asyncio.gather(t3, t4)
            assert res3.startswith("✨")
            assert res4.startswith("✨")
            await reload_mod._plugin_start_tasks[plugin_name]
            step3_events = read_events()[before_step3:]
            assert step3_events == [
                "shutdown:2:state=2:begin",
                "shutdown:2:state=2:done",
                "import:4",
                "import:4",
                "start:4:state=4",
            ]
            assert matches_command("测重指令四") is True

            # 4) 同步兼容入口在途 shutdown 期间快速二次重载：受保护关闭队列不被取消，且在 start:6 前跑完
            before_step4 = len(read_events())
            write_plugin(5, "测重指令五")
            res5 = reload_mod.reload_plugin(plugin_name)
            assert res5.startswith("✨")
            await wait_for_event("shutdown:4:state=5:begin")
            write_plugin(6, "测重指令六")
            res6 = reload_mod.reload_plugin(plugin_name)
            assert res6.startswith("✨")
            await reload_mod._plugin_start_tasks[plugin_name]
            step4_events = read_events()[before_step4:]
            assert "shutdown:4:state=5:done" in step4_events
            assert step4_events.index("shutdown:4:state=5:done") < step4_events.index("start:6:state=6")
            assert not any(ev.startswith("shutdown:5:") for ev in step4_events)
            assert matches_command("测重指令六") is True

            # 5) 异步重载导入失败：先在旧模块存活期跑完 v6 shutdown，再失败返回 ❌ 并重置索引
            before_step5 = len(read_events())
            write_plugin(7, "测重指令七", fail=True)
            res7 = await reload_mod.reload_plugin_async(plugin_name)
            assert res7.startswith("❌")
            assert matches_command("测重指令六") is False
            assert read_events()[before_step5:] == [
                "shutdown:6:state=6:begin",
                "shutdown:6:state=6:done",
            ]

            # 6) 新版无启动钩子时，仍会取消并等待旧版在途 start 任务退出
            before_step6 = len(read_events())
            write_plugin(8, "测重指令八", slow_start=True)
            res8 = await reload_mod.reload_plugin_async(plugin_name)
            assert res8.startswith("✨")
            await wait_for_event("start:8:begin")
            write_plugin(9, "测重指令九", with_hooks=False)
            res9 = await reload_mod.reload_plugin_async(plugin_name)
            assert res9.startswith("✨")
            assert read_events()[before_step6:] == [
                "import:8",
                "start:8:begin",
                "start:8:cancelled",
                "import:9",
            ]
        finally:
            for task_map in (reload_mod._plugin_start_tasks, reload_mod._plugin_shutdown_tasks):
                pending_task = task_map.pop(plugin_name, None)
                if pending_task is not None and not pending_task.done():
                    pending_task.cancel()
                    await asyncio.gather(pending_task, return_exceptions=True)
            reload_mod._start_task_meta.clear()
            reload_mod._pending_shutdown_hooks.pop(plugin_name, None)
            reload_mod._plugin_reload_locks.pop(plugin_name, None)
            sv_obj = SL.lst.pop(sv_name, None)
            if sv_obj is not None:
                SL.detail_lst.pop(sv_obj.plugins, None)
            SL.plugins.pop(plugin_name, None)
            config_plugins.pop(plugin_name, None)
            plugin_config_store._dirty.discard(plugin_name)
            server_mod._import_memory_mb.pop(plugin_name, None)
            for added in (str(plugins_dir), str(plugin_dir)):
                server_mod._added_paths.discard(added)
                while added in sys.path:
                    sys.path.remove(added)
            site.plugins_page.pop(plugin_name, None)
            for mod_key in [k for k in sys.modules if reload_mod._belongs_to_plugin(k, plugin_name)]:
                sys.modules.pop(mod_key, None)
            for cache_key in [k for k in list(server_mod._module_cache) if plugin_name in k]:
                server_mod._module_cache.pop(cache_key, None)
            for hook_set in (
                server_mod.core_start_def,
                server_mod.core_start_before_def,
                server_mod.core_shutdown_def,
            ):
                stale = {
                    h
                    for h in hook_set
                    if reload_mod._belongs_to_plugin(reload_mod._resolve_func_module(h.func), plugin_name)
                }
                hook_set -= stale
            plugins_dir_str = str(plugins_dir)
            while plugins_dir_str in sys.path:
                sys.path.remove(plugins_dir_str)
            reset_trigger_index()

    asyncio.run(scenario())
