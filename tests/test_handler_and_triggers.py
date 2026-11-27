"""消息处理与触发器索引测试：最长匹配、权限过滤、IME 空格、索引构建、收件箱与热重载。"""

from __future__ import annotations

import sys
import time
import asyncio
from typing import Literal, Iterator
from pathlib import Path
from dataclasses import dataclass

import pytest

from gsuid_core.sv import SL, SV, Plugins
from gsuid_core.bot import _Bot
from tests._support import isolated_history
from gsuid_core.models import Event, Message, MessageReceive
from gsuid_core.handler import _sv_authorized, dispatch_sort_key
from gsuid_core.trigger_index import get_trigger_index, reset_trigger_index
from gsuid_core.ai_core.inbox.turn import InboxTurn
from gsuid_core.ai_core.inbox.state import Strength, WakeItem, WakeKind, PendingEntry, ReceiptContext
from gsuid_core.ai_core.inbox.scheduler import TurnOutcome, InboxScheduler

_CHAR_PANEL_RE = r"^(?:yh|异环)?(?P<char_name>[\u4e00-\u9fa5A-Za-z0-9·]{1,16})(?:面板|信息)$"


def _make_event(raw: str, pm: int = 6, bot_self_id: str = "") -> Event:
    ev = Event("OneBot", "123", "m1", "group", "999", "456", {}, pm)
    ev.raw_text = raw
    ev.text = raw
    ev.bot_self_id = bot_self_id
    return ev


def _make_recv(
    msg_id: str,
    content: list[Message],
    *,
    uid: str = "100000002",
    group_id: str = "70009",
    nickname: str = "",
) -> MessageReceive:
    sender: dict[str, str] = {"nickname": nickname} if nickname else {}
    return MessageReceive(
        bot_id="OneBot",
        bot_self_id="900000001",
        msg_id=msg_id,
        user_type="group",
        group_id=group_id,
        user_id=uid,
        sender=sender,
        user_pm=6,
        content=content,
    )


class _InboxRig:
    """假时钟 InboxScheduler 测试底座，用公开 submit/drain 驱动状态机。"""

    def __init__(
        self,
        *,
        start: float = 1000.0,
        ws_id: str = "test_ws",
        group_id: str = "70009",
        persona: str = "测试人格",
        manual_outcomes: bool = False,
    ) -> None:
        self.now = start
        self.group_id = group_id
        self.persona = persona
        self.ws = _Bot(ws_id)
        self.sleeps: list[float] = []
        self.batches: list[InboxTurn] = []
        self.turn_times: list[float] = []
        self.manual_outcomes = manual_outcomes
        self.outcomes: asyncio.Queue[TurnOutcome] = asyncio.Queue()
        self.in_flight = asyncio.Event()
        self.release_turn = asyncio.Event()
        self.release_turn.set()
        self._sleep_gate = asyncio.Event()
        self._sleep_gate.set()
        self.hold_next_turn = False
        self.answer_all = True
        self.emit_tool_obs = False
        self.emit_mem_dep = False
        self.sched = InboxScheduler(clock=lambda: self.now, sleep=self._sleep, runner=self._runner)

    async def _sleep(self, seconds: float) -> None:
        target = self.now + seconds
        self.sleeps.append(seconds)
        await self._sleep_gate.wait()
        self.now = max(self.now, target)

    async def _runner(self, turn: InboxTurn, _ws: _Bot) -> TurnOutcome:
        self.batches.append(turn)
        self.turn_times.append(self.now)
        if self.emit_tool_obs:
            turn.observations.append("ob_test0001")
        if self.emit_mem_dep:
            turn.memory_dependent = True
        self.in_flight.set()
        if self.manual_outcomes:
            return await self.outcomes.get()
        if self.hold_next_turn:
            self.hold_next_turn = False
            await self.release_turn.wait()
        ans = frozenset(e.item.seq for e in turn.entries) if self.answer_all else frozenset()
        return TurnOutcome(reached_model=True, error=False, answered=ans)

    def pause_sleep(self) -> None:
        self._sleep_gate.clear()

    def release_sleep(self) -> None:
        self._sleep_gate.set()

    def hold_turn(self) -> None:
        self.in_flight.clear()
        self.release_turn.clear()
        self.hold_next_turn = True

    async def pump(self, ticks: int = 12) -> None:
        for _ in range(ticks):
            await asyncio.sleep(0)

    def make_event(
        self,
        msg_id: str,
        *,
        uid: str = "100000001",
        raw_text: str = "",
        user_type: Literal["group", "direct"] = "group",
        group_id: str | None = "",
        at_bot: bool = False,
        at_list: list[str] | None = None,
        image_id: str | None = None,
    ) -> Event:
        gid = None if user_type == "direct" else (self.group_id if group_id == "" else group_id)
        ev = Event("OneBot", "900000001", msg_id, user_type, gid, uid, {}, 6, raw_text=raw_text, at_bot=at_bot)
        if at_list:
            ev.at = at_list[0]
            ev.at_list = list(at_list)
        if image_id:
            ev.image_id = image_id
            ev.image_id_list = [image_id]
        return ev

    def submit(
        self,
        seq: int,
        *,
        uid: str = "100000001",
        kind: WakeKind = "name",
        strength: Strength = "hard",
        created_at: float | None = None,
        raw_text: str = "",
        event: Event | None = None,
        receipt: ReceiptContext | None = None,
        at_bot: bool = False,
        at_list: list[str] | None = None,
        image_id: str | None = None,
    ) -> PendingEntry:
        ev = (
            event
            if event is not None
            else self.make_event(
                f"m{seq}", uid=uid, raw_text=raw_text, at_bot=at_bot, at_list=at_list, image_id=image_id
            )
        )
        ts = self.now if created_at is None else created_at
        entry = PendingEntry(WakeItem(seq, uid, kind, strength, ts), ev, receipt=receipt, persona_name=self.persona)
        self.sched.submit(self.ws, entry)
        return entry

    async def start_next(self) -> asyncio.Task[None]:
        self.in_flight.clear()
        await self.pump()
        ctx = self.ws.queue.get_nowait()

        async def _run() -> None:
            await ctx.coro

        task: asyncio.Task[None] = asyncio.create_task(_run())
        await self.in_flight.wait()
        return task

    async def drain_next(self, outcome: TurnOutcome | None = None) -> InboxTurn:
        if outcome is not None:
            task = await self.start_next()
            self.outcomes.put_nowait(outcome)
            await task
            await self.pump()
            return self.batches[-1]
        await self.pump()
        await self.ws.queue.get_nowait().coro
        await self.pump()
        return self.batches[-1]


@dataclass(frozen=True)
class _CfgVal:
    data: object


class _StubCfg:
    def __init__(self, values: dict[str, object]) -> None:
        self.config = values

    def get_config(self, key: str) -> _CfgVal:
        return _CfgVal(self.config[key])


class _StubPersonaMgr:
    def __init__(self) -> None:
        self._cfg = _StubCfg({"ai_mode": ["提及应答"], "keywords": []})

    def get_persona_for_session(self, _sid: str) -> str:
        return "测试人格"

    def get_config(self, _name: str) -> _StubCfg:
        return self._cfg


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

    for white, black, bot_self_id, expected in (
        (["bot1"], [], "bot1", True),
        (["bot1"], [], "bot2", False),
        (["bot1"], [], "", False),
        (["bot1", "bot2"], ["bot1"], "bot1", False),
        (["bot1", "bot2"], ["bot1"], "bot2", True),
    ):
        sample_sv.plugins.bot_white_list = white
        sample_sv.plugins.bot_black_list = black
        assert _sv_authorized(sample_sv, _make_event("测帮助", pm=1, bot_self_id=bot_self_id), 1) is expected


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
        cases: list[tuple[str, str, str, str | None]] = [
            ("测绑定uid123", "command", "绑定uid", "123"),
            ("测绑定123", "command", "绑定", "123"),
            ("测刷新面板", "fullmatch", "刷新面板", None),
        ]
        for raw, expected_type, expected_kw, expected_arg in cases:
            ev = _make_event(raw, pm=0)
            ranked = sorted(
                [t for t in index.candidates(ev) if t.check_command(ev)],
                key=lambda t: dispatch_sort_key(t, sample_sv.priority),
            )
            assert ranked[0].type == expected_type and ranked[0].keyword == expected_kw
            if expected_arg is not None:
                assert (await ranked[0].get_command(ev)).text == expected_arg

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
    fm_trig = next(t for t in sample_sv.TL["fullmatch"].values() if t.prefix == "测")
    ascii_trig = next(t for t in sample_sv.TL["command"].values() if t.prefix == "test" and t.keyword == "help")

    for trig, raw, expected in (
        (cmd_trig, "测 绑定 123", True),
        (fm_trig, "测状态", True),
        (fm_trig, "测 状态", True),
        (fm_trig, "测状态 额外参数", False),
        (ascii_trig, "testhelp", True),
        (ascii_trig, "test help", True),
        (ascii_trig, "testhell", False),
    ):
        assert trig.check_command(_make_event(raw, pm=0)) is expected, raw

    ev_arg = _make_event("测绑定 123 456", pm=0)
    assert cmd_trig.check_command(ev_arg) is True
    assert asyncio.run(cmd_trig.get_command(ev_arg)).text == "123 456"


def test_trigger_index_rebuild_and_owner(sample_sv: SV) -> None:
    before = get_trigger_index().version

    @sample_sv.on_command("新动态命令")
    async def _dyn(bot, ev):
        pass

    second = get_trigger_index()
    assert second.version > before
    ev = _make_event("测新动态命令", pm=0)
    candidates = list(second.candidates(ev))
    assert any(t.keyword == "新动态命令" for t in candidates)
    for trigger in candidates:
        owner = second.owner_of(trigger)
        assert owner is not None and owner.name == sample_sv.name


def test_inbox_scheduler_settle_and_one_time_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    """收件箱调度：并批、不复活、read_seq 推进、TTL、错误重试与静默窗。"""
    from gsuid_core.ai_core.inbox.state import classify_wake

    async def main() -> None:
        rig = _InboxRig(start=1000.0, group_id="70001", manual_outcomes=True)
        sid = rig.make_event("probe", uid="100000002").session_id

        # 1) settle 首轮；运行中到达 3 项并入单轮；清空后无活跃会话
        rig.submit(1, uid="100000002", kind="mention")
        t1 = await rig.start_next()
        for seq in (2, 3, 4):
            rig.submit(seq, uid="100000003", kind="mention")
        rig.outcomes.put_nowait(TurnOutcome(reached_model=True, error=False, answered=frozenset({1})))
        await t1
        t2 = await rig.start_next()
        assert [e.item.seq for e in rig.batches[0].entries] == [1]
        assert [e.item.seq for e in rig.batches[1].entries] == [2, 3, 4]
        rig.outcomes.put_nowait(TurnOutcome(reached_model=True, error=False, answered=frozenset({2})))
        await t2
        await rig.pump()
        assert rig.ws.queue.empty() and not rig.sched.has_active_session(sid)

        # 2) 已落定项不复活；TTL 过期项直接 expired 不成批
        for seq in (10, 11):
            rig.submit(seq, uid="100000003", kind="mention")
        turn3 = await rig.drain_next(TurnOutcome(reached_model=True, error=False, answered=frozenset()))
        assert [e.item.seq for e in turn3.entries] == [10, 11]
        assert rig.ws.queue.empty() and not rig.sched.has_active_session(sid)

        stale = rig.submit(8, uid="100000002", kind="mention", created_at=rig.now - 1000.0)
        await rig.pump()
        assert stale.item.state == "expired" and rig.ws.queue.empty() and not rig.sched.has_active_session(sid)

        # 3) runner 报错：模型前异常重试一次后过期；已达模型故障不重试直接过期
        e9 = rig.submit(9, uid="100000002", kind="mention")
        fail_pre = TurnOutcome(reached_model=False, error=True, answered=frozenset())
        await rig.drain_next(fail_pre)
        await rig.drain_next(fail_pre)
        assert e9.item.state == "expired" and e9.item.retries == 2
        assert rig.ws.queue.empty() and not rig.sched.has_active_session(sid)

        e19 = rig.submit(19, uid="100000002", kind="mention")
        await rig.drain_next(TurnOutcome(reached_model=True, error=True, answered=frozenset()))
        assert e19.item.state == "expired" and e19.item.retries == 0
        assert rig.ws.queue.empty() and not rig.sched.has_active_session(sid)

        # 4) 跨清空保留 last_read_seq；reached_model=False 不推进游标；清史回绕归 0
        base_ev = Event("OneBot", "900000001", "g0", "group", "70002", "100000002", {}, 6)
        with isolated_history(base_ev) as hm:
            hm.add_message(base_ev, role="user", content="消息1", metadata={"msg_id": "g1"})
            rec2 = hm.add_message(base_ev, role="user", content="@机器人 消息2", metadata={"msg_id": "g2"})
            assert rec2.link is not None
            rig.submit(rec2.link.seq, uid="100000002", kind="mention", event=base_ev)
            await rig.drain_next(TurnOutcome(reached_model=True, error=False, answered=frozenset({rec2.link.seq})))
            assert not rig.sched.has_active_session(base_ev.session_id)

            hm.add_message(base_ev, role="user", content="中间静默消息3", metadata={"msg_id": "g3"})
            rec4 = hm.add_message(base_ev, role="user", content="@机器人 消息4", metadata={"msg_id": "g4"})
            assert rec4.link is not None
            e_miss = rig.submit(rec4.link.seq, uid="100000002", kind="mention", event=base_ev)
            await rig.drain_next(TurnOutcome(reached_model=False, error=False, answered=frozenset()))
            assert e_miss.item.state == "dismissed" and not rig.sched.has_active_session(base_ev.session_id)

            rec5 = hm.add_message(base_ev, role="user", content="@机器人 消息5", metadata={"msg_id": "g5"})
            assert rec5.link is not None
            rig.submit(rec5.link.seq, uid="100000002", kind="mention", event=base_ev)
            turn_after_miss = await rig.drain_next(
                TurnOutcome(reached_model=True, error=False, answered=frozenset({rec5.link.seq}))
            )
            assert turn_after_miss.from_seq == rec2.link.seq

            hm.clear_history(base_ev)
            rec_new = hm.add_message(base_ev, role="user", content="清史后消息1", metadata={"msg_id": "g_new1"})
            assert rec_new.link is not None and rec_new.link.seq == 1
            rig.submit(rec_new.link.seq, uid="100000002", kind="mention", event=base_ev)
            turn_wrapped = await rig.drain_next(TurnOutcome(reached_model=True, error=False, answered=frozenset({1})))
            assert turn_wrapped.from_seq == 0

        # 5) 静默窗时长表驱动：通过公开 submit + 记录的 sleeps 验证首窗策略
        settle_cases: list[tuple[Literal["group", "direct"], WakeKind, Strength, str, bool, float]] = [
            ("direct", "private", "hard", "你好", False, 4.0),
            ("group", "mention", "hard", "今日指标如何？", False, 4.0),
            ("group", "mention", "hard", "", True, 8.0),
            ("group", "mention", "hard", "你好", True, 4.0),
            ("group", "name", "hard", "测试人格", False, 8.0),
            ("group", "name", "hard", "测试人格早安", False, 4.0),
            ("group", "name", "hard", "测试别名", False, 8.0),
            ("group", "followup", "soft", "顺带一提", False, 8.0),
        ]
        with monkeypatch.context() as mp:
            mp.setattr("gsuid_core.ai_core.memory.group_profile.collect_persona_surfaces", lambda _n: ("测试别名",))
            for idx, (user_type, kind, strength, raw_text, at_bot, expected_s) in enumerate(settle_cases, start=100):
                rig_s = _InboxRig(start=4000.0, group_id=f"709{idx}")
                ev_s = rig_s.make_event(f"sw_{idx}", user_type=user_type, raw_text=raw_text, at_bot=at_bot)
                rig_s.submit(idx, kind=kind, strength=strength, event=ev_s)
                await rig_s.drain_next()
                assert rig_s.sleeps == [expected_s], (user_type, kind, raw_text)

    asyncio.run(main())

    wake_cases: list[tuple[str, str | None, str, WakeKind]] = [
        ("group", "self", "哈哈哈", "quote_self"),
        ("group", "self", "测试人格 你看看这个", "name"),
        ("direct", None, "", "private"),
    ]
    for user_type, reply_to_author, raw_text, expected_kind in wake_cases:
        assert (
            classify_wake(
                user_type=user_type,
                at_self=False,
                reply_to_author=reply_to_author,
                raw_text=raw_text,
                wake_names=["测试人格"],
                in_followup=False,
                has_at_others=False,
            )
            == expected_kind
        )


def test_quoted_image_attribution_and_unread_image_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    """引用图拆入 reply_image_id_list、引用自动 @ 区分、二次引用新图与待读图过滤。"""
    from gsuid_core.handler import msg_process, _record_inbound_history
    from gsuid_core.message_history import MessageRecord
    from gsuid_core.ai_core.turn_pipeline import extract_pending_unread_images
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.multimodal.payload import prepare_media_payload

    # 显式固定懒读图配置，防止本地 config.json 漂移或发起真实外网图片请求
    monkeypatch.setattr(ai_config.get_config("lazy_image_read"), "data", True)

    async def scenario() -> None:
        base_ev = Event("OneBot", "900000001", "m0", "group", "70009", "100000001", {}, 6)

        async def _ingest(
            msg_id: str, content: list[Message], *, uid: str = "100000002", nickname: str = ""
        ) -> tuple[Event, MessageRecord | None]:
            ev = await msg_process(_make_recv(msg_id, content, uid=uid, nickname=nickname))
            return ev, _record_inbound_history(ev)

        with isolated_history(base_ev) as hm:
            hm.add_message(
                base_ev,
                role="assistant",
                content="[图片/文件]",
                metadata={"msg_id": "bot_card_1", "speech_channel": "command"},
            )
            ev_quote, rec_q = await _ingest(
                "u_quote_1",
                [
                    Message("text", "测试人格，注意防潮"),
                    Message("reply_id", "bot_card_1"),
                    Message("image", "https://example.invalid/bot_card.png"),
                    Message("reply", ""),
                ],
                nickname="测试用户B",
            )
            assert len(ev_quote.image_id_list) == 1 and ev_quote.reply_image_id_list == ev_quote.image_id_list
            assert rec_q is not None and "image_id_list" not in rec_q.metadata and "images" not in rec_q.metadata
            assert rec_q.metadata["reply_image_id_list"] == ev_quote.reply_image_id_list

            ev_auto, rec_auto = await _ingest(
                "u_auto_at_1",
                [
                    Message("reply_id", "out_of_window_bot_msg"),
                    Message("reply", "测试播报正文"),
                    Message("at", "900000001"),
                    Message("text", "这"),
                ],
                nickname="测试用户B",
            )
            assert ev_auto.at_bot is True and ev_auto.quote_auto_at is True
            assert rec_auto is not None and rec_auto.link is not None
            assert rec_auto.link.at_self is False and rec_auto.link.reply_to_author == "self"

            ev_explicit, rec_exp = await _ingest(
                "u_explicit_at_2",
                [
                    Message("reply_id", "out_of_window_bot_msg"),
                    Message("reply", "测试播报正文"),
                    Message("at", "900000001"),
                    Message("at", "900000001"),
                    Message("text", "帮我看看"),
                ],
                nickname="测试用户B",
            )
            assert ev_explicit.at_bot is True and ev_explicit.quote_auto_at is False
            assert rec_exp is not None and rec_exp.link is not None and rec_exp.link.at_self is True

            payload, _ = await prepare_media_payload([(2, ev_quote)], "[群消息 #1–#2]\n#2: 测试人格，注意防潮")
            joined = "\n".join(p for p in payload if isinstance(p, str))
            assert "引用的历史图片" in joined and "用户发送了图片" not in joined

            ev_chain, rec_chain = await _ingest(
                "u_chain_own_img",
                [
                    Message("reply_id", "u_quote_1"),
                    Message("reply", "测试人格，注意防潮"),
                    Message("image", "https://example.invalid/user_c_own.png"),
                    Message("text", "帮我看下这张新图"),
                ],
                uid="100000005",
                nickname="测试用户C",
            )
            assert ev_chain.reply_image_id_list == []
            assert rec_chain is not None and rec_chain.metadata["image_id_list"] == ev_chain.image_id_list
            payload_c, _ = await prepare_media_payload([(3, ev_chain)], "[群消息 #2–#3]\n#3: 帮我看下这张新图")
            joined_c = "\n".join(p for p in payload_c if isinstance(p, str))
            assert "用户发送了图片" in joined_c and "引用的历史图片" not in joined_c

            ev_quote_pre, _ = await _ingest(
                "u_quote_2",
                [
                    Message("reply_id", "bot_card_1"),
                    Message("reply", ""),
                    Message("image", "https://example.invalid/bot_card.png"),
                    Message("text", "不用代理能存上去？"),
                ],
                nickname="测试用户B",
            )
            assert ev_quote_pre.reply_image_id_list == ev_quote_pre.image_id_list
            ev_followup, _ = await _ingest("u_joke_3", [Message("text", "给测试人格装上")])
            assert extract_pending_unread_images(ev_followup, set()) == []

            await _ingest(
                "u_meme_other",
                [Message("at", "100000001"), Message("image", "https://example.invalid/meme.png")],
                uid="100000003",
            )
            ev_ask_bot, _ = await _ingest("u_ask_meme", [Message("text", "测试人格来点表情包")], uid="100000003")
            assert extract_pending_unread_images(ev_ask_bot, set()) == []

            await _ingest("u_split_1", [Message("image", "https://example.invalid/doc.png")], uid="100000004")
            ev_split_q, _ = await _ingest("u_split_2", [Message("text", "测试人格帮我看下这张图")], uid="100000004")
            pending_imgs = extract_pending_unread_images(ev_split_q, set())
            assert len(pending_imgs) == 1
            split_payload, _ = await prepare_media_payload(
                [(9, ev_split_q)], "[群消息 #8–#9]", pending_unread_images=pending_imgs
            )
            split_joined = "\n".join(p for p in split_payload if isinstance(p, str))
            assert "仅当本句确在追问或讨论该图时调用" in split_joined

    asyncio.run(scenario())


def test_group_multi_speaker_coalesce_dedup_and_handle_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """群聊同人连发聚批、去重维度、settling 续发封顶、handle_event 端到端与速率门控。"""
    import gsuid_core.handler as handler_mod
    import gsuid_core.ai_core.hooks as hooks_mod
    import gsuid_core.ai_core.startup as startup_mod
    import gsuid_core.ai_core.persona.config as persona_cfg_mod
    import gsuid_core.ai_core.inbox.scheduler as inbox_sched_mod
    import gsuid_core.buildin_plugins.core_command.core_ai_control.state as ai_ctrl_mod
    from gsuid_core.message_history import MessageRecord
    from gsuid_core.ai_core.followup_window import group_is_too_busy_for_followup

    async def scenario() -> None:
        # 1) 仅同说话人连发顺延；不同说话人不延长当前结算窗且独立成轮
        rig_c = _InboxRig(start=2000.0, ws_id="coalesce_ws")
        rig_c.submit(21, uid="100000001", raw_text="在吗")
        rig_c.now = 2000.3
        rig_c.submit(22, uid="100000001", raw_text="帮我查个东西")
        rig_c.now = 2000.5
        rig_c.submit(23, uid="100000002", raw_text="我也问一下")
        first_turn = await rig_c.drain_next()
        assert rig_c.turn_times[0] == pytest.approx(2004.8)
        second_turn = await rig_c.drain_next()
        assert [e.item.seq for e in first_turn.entries] == [21, 22]
        assert [e.item.seq for e in second_turn.entries] == [23]

        # 2) 同批全等折叠 + 跨轮零工具 +1 消解 + 有工具观测跨人放行
        rig_d = _InboxRig(start=2100.0, ws_id="dedup_ws")
        rig_d.hold_turn()
        rig_d.submit(31, uid="100000001", raw_text="测试人格，发测试凭据给我")
        t_run1 = await rig_d.start_next()
        rig_d.now += 14.0
        rig_d.submit(32, uid="100000002", raw_text="测试人格，发测试凭据给我")
        rig_d.release_turn.set()
        await t_run1
        await rig_d.pump()
        assert rig_d.ws.queue.empty() and len(rig_d.batches) == 1

        rig_d.emit_tool_obs = True
        rig_d.submit(41, uid="100000001", raw_text="测试人格，领每日奖励")
        rig_d.submit(42, uid="100000001", raw_text="测试人格，领每日奖励", created_at=rig_d.now + 0.1)
        rig_d.submit(43, uid="100000002", raw_text="测试人格，领每日奖励", created_at=rig_d.now + 0.2)
        rig_d.submit(
            44, uid="100000001", raw_text="测试人格，领每日奖励", created_at=rig_d.now + 0.3, image_id="img_own_1"
        )
        assert [e.item.seq for e in (await rig_d.drain_next()).entries] == [41, 44]
        assert [e.item.seq for e in (await rig_d.drain_next()).entries] == [43]

        rig_d.submit(45, uid="100000001", raw_text="测试人格，领每日奖励", created_at=rig_d.now + 5.0)
        await rig_d.pump()
        assert rig_d.ws.queue.empty()
        rig_d.submit(46, uid="100000004", raw_text="测试人格，领每日奖励", created_at=rig_d.now + 6.0)
        assert [e.item.seq for e in (await rig_d.drain_next()).entries] == [46]

        # 3) 去重维度：未应答不缓存、at_bot、at_list、memory_dependent
        rig_s = _InboxRig(start=2200.0, ws_id="sel_ws")
        rig_s.answer_all = False
        rig_s.submit(51, uid="100000001", kind="followup", strength="soft", raw_text="在吗")
        await rig_s.drain_next()
        rig_s.answer_all = True
        rig_s.submit(52, uid="100000001", kind="followup", strength="soft", raw_text="在吗", created_at=rig_s.now + 1.0)
        await rig_s.drain_next()
        rig_s.submit(53, uid="100000002", kind="mention", raw_text="在吗", at_bot=True, created_at=rig_s.now + 2.0)
        assert [e.item.seq for e in (await rig_s.drain_next()).entries] == [53]

        rig_s.submit(
            54, uid="100000001", raw_text="测试人格帮我看看", at_list=["100000008"], created_at=rig_s.now + 3.0
        )
        await rig_s.drain_next()
        rig_s.submit(
            55, uid="100000002", raw_text="测试人格帮我看看", at_list=["100000009"], created_at=rig_s.now + 4.0
        )
        assert [e.item.seq for e in (await rig_s.drain_next()).entries] == [55]

        rig_s.emit_mem_dep = True
        rig_s.submit(56, uid="100000001", raw_text="测试人格，我上次说的那个怎么样", created_at=rig_s.now + 5.0)
        await rig_s.drain_next()
        rig_s.submit(57, uid="100000001", raw_text="测试人格，我上次说的那个怎么样", created_at=rig_s.now + 6.0)
        await rig_s.pump()
        assert rig_s.ws.queue.empty()
        rig_s.submit(58, uid="100000002", raw_text="测试人格，我上次说的那个怎么样", created_at=rig_s.now + 7.0)
        assert [e.item.seq for e in (await rig_s.drain_next()).entries] == [58]

        # 4) 同说话人 settling 续发：is_awaiting_speaker、4s 滑动、12s 封顶、soft+hard 保序与回执隔离
        rig_k = _InboxRig(start=3000.0, ws_id="cont_ws")
        sid_c = rig_k.make_event("probe_c").session_id
        assert rig_k.sched.is_awaiting_speaker(sid_c, "测试人格", "100000001") is False
        rig_k.submit(71, uid="100000001", kind="mention", raw_text="帮我看看", at_bot=True)
        assert rig_k.sched.is_awaiting_speaker(sid_c, "测试人格", "100000001") is True
        assert rig_k.sched.is_awaiting_speaker(sid_c, "测试人格", "100000002") is False
        rig_k.now = 3002.5
        rig_k.submit(72, uid="100000001", kind="followup", strength="soft", raw_text="刚才报错了")
        turn_slide = await rig_k.drain_next()
        assert rig_k.turn_times[-1] == pytest.approx(3006.5)
        assert [e.item.seq for e in turn_slide.entries] == [71, 72] and turn_slide.primary.item.seq == 71

        rig_k.now = 3100.0
        rig_k.submit(710, uid="100000001", kind="mention", raw_text="帮我看看", at_bot=True)
        rig_k.now = 3102.5
        rig_k.submit(720, uid="100000001", kind="followup", strength="soft", raw_text="刚才报错了")
        rig_k.now = 3110.0
        rig_k.submit(730, uid="100000001", kind="mention", raw_text="急用", at_bot=True)
        turn_cap = await rig_k.drain_next()
        assert rig_k.turn_times[-1] == pytest.approx(3112.0)
        assert [e.item.seq for e in turn_cap.entries] == [710, 720, 730] and turn_cap.primary.item.seq == 710

        rig_k.submit(74, uid="100000001", kind="followup", strength="soft", raw_text="等等")
        rig_k.submit(75, uid="100000001", kind="mention", raw_text="查一下", at_bot=True)
        turn_sh = await rig_k.drain_next()
        assert [e.item.seq for e in turn_sh.entries] == [74, 75] and turn_sh.primary.item.seq == 75

        rig_k.hold_turn()
        rig_k.submit(76, uid="100000001", kind="mention", raw_text="第一轮", at_bot=True)
        t_in_flight = await rig_k.start_next()
        assert rig_k.sched.is_awaiting_speaker(sid_c, "测试人格", "100000001") is False
        rig_k.submit(77, uid="100000002", kind="mention", raw_text="排队句1", at_bot=True)
        assert rig_k.sched.is_awaiting_speaker(sid_c, "测试人格", "100000002") is True
        rig_k.submit(78, uid="100000002", kind="followup", strength="soft", raw_text="排队句2")
        rig_k.release_turn.set()
        await t_in_flight
        assert [e.item.seq for e in (await rig_k.drain_next()).entries] == [77, 78]

        rig_k.submit(
            79,
            uid="100000003",
            kind="receipt",
            strength="soft",
            raw_text="/签到",
            receipt=ReceiptContext(None, "测试插件", "test", "签到成功"),
        )
        assert rig_k.sched.is_awaiting_speaker(sid_c, "测试人格", "100000003") is False
        await rig_k.drain_next()

        # 5) handle_event 真实入口：follow_up_window=0 下先 @Bot 再无 @ 续发仍合并
        e2e_t0 = time.time()
        rig_e2e = _InboxRig(start=e2e_t0, ws_id="e2e_ws", group_id="70019")
        rig_e2e.pause_sleep()
        stub_ai_cfg = _StubCfg(
            {
                "enable": True,
                "black_list": [],
                "white_list": [],
                "follow_up_window": 0,
                "follow_up_max_total": 300,
                "follow_up_max_replies": 2,
            }
        )
        monkeypatch.setattr(handler_mod, "ai_config", stub_ai_cfg)
        monkeypatch.setattr(handler_mod, "_schedule_user_group_write", lambda *_args: None)
        monkeypatch.setattr(hooks_mod, "should_fire", lambda _point: False)
        monkeypatch.setattr(ai_ctrl_mod, "is_scope_banned", lambda _sid: False)
        monkeypatch.setattr(startup_mod, "is_ai_core_ready", lambda: True)
        monkeypatch.setattr(persona_cfg_mod, "persona_config_manager", _StubPersonaMgr())
        monkeypatch.setattr(inbox_sched_mod, "_scheduler", rig_e2e.sched)

        async def _e2e(msg_id: str, content: list[Message], uid: str, nickname: str) -> None:
            await handler_mod.handle_event(
                rig_e2e.ws, _make_recv(msg_id, content, uid=uid, group_id="70019", nickname=nickname)
            )

        e2e_base = Event("OneBot", "900000001", "e2e_0", "group", "70019", "100000001", {}, 6)
        e2e_base.WS_BOT_ID = "e2e_ws"
        with isolated_history(e2e_base):
            await _e2e("e2e_1", [Message("at", "900000001"), Message("text", "帮我看看")], "100000001", "测试用户A")
            rig_e2e.now = e2e_t0 + 2.0
            await _e2e("e2e_2", [Message("text", "刚才报了个错")], "100000001", "测试用户A")
            rig_e2e.now = e2e_t0 + 2.5
            await _e2e("e2e_3", [Message("at", "100000009"), Message("text", "你先吃")], "100000001", "测试用户A")
            await _e2e("e2e_4", [Message("text", "路过打个招呼")], "100000002", "测试用户B")
            rig_e2e.release_sleep()
            await rig_e2e.drain_next()
            assert len(rig_e2e.batches) == 1
            assert [e.event.msg_id for e in rig_e2e.batches[0].entries] == ["e2e_1", "e2e_2"]
            assert rig_e2e.batches[0].primary.event.msg_id == "e2e_1"
            assert rig_e2e.ws.queue.empty() and rig_e2e.turn_times == [pytest.approx(e2e_t0 + 6.0)]

        # 6) 活跃大群速率门控
        busy_recs = [
            MessageRecord(
                role="user", content=f"消息{k}", user_id=f"10000010{k % 5}", user_name="群友", timestamp=1000.0 - k * 2
            )
            for k in range(12)
        ]
        assert group_is_too_busy_for_followup(busy_recs, speaker_uid="100000001", now=1000.0, busy_per_min=12) is True
        assert (
            group_is_too_busy_for_followup(busy_recs[:3], speaker_uid="100000001", now=1000.0, busy_per_min=12) is False
        )

    asyncio.run(scenario())


class _ReloadPluginHarness:
    """隔离构建与清理临时热重载插件的测试脚手架。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
        import gsuid_core.server as server_mod

        self.plugin_name = f"TestReloadPlugin{suffix}"
        self.meta_plugin_name = f"TestReloadMetaPlugin{suffix}"
        self.sv_name = f"TestReloadSV{suffix}"
        self.plugins_dir = tmp_path / "plugins"
        self.plugin_dir = self.plugins_dir / self.plugin_name
        self.sub_dir = self.plugin_dir / "main"
        self.sub_dir.mkdir(parents=True)
        (self.plugin_dir / "__init__.py").write_text("", encoding="utf-8")
        (self.plugin_dir / "__full__.py").write_text("", encoding="utf-8")
        self.init_file = self.sub_dir / "__init__.py"
        self.state_file = self.plugin_dir / "state.py"
        self.events_file = tmp_path / f"events_{suffix}.log"
        self.sync_gate = tmp_path / f"sync_gate_{suffix}"
        self.orphan_gate = tmp_path / f"orphan_gate_{suffix}"

        self.meta_dir = self.plugins_dir / self.meta_plugin_name
        self.meta_sub = self.meta_dir / "main"
        self.meta_api_dir = self.meta_dir / "api"
        self.meta_marker = self.meta_dir / "__meta_plugin__.py"
        self.meta_api_file = self.meta_api_dir / "__init__.py"
        self.meta_init_file = self.meta_sub / "__init__.py"
        self._extra_registered_admins: list[type] = []

        monkeypatch.setattr(server_mod, "PLUGIN_PATH", self.plugins_dir)
        monkeypatch.setattr(sys, "dont_write_bytecode", True)

    def _log_helper_lines(self) -> list[str]:
        return [
            f"_LOG = Path({str(self.events_file)!r})",
            "def _log(msg):",
            '    with _LOG.open("a", encoding="utf-8") as f: f.write(msg + "\\n")',
        ]

    def write_plugin(
        self,
        version: int,
        command: str,
        *,
        fail: bool = False,
        shutdown_delay: float = 0.01,
        sync_shutdown_gated: bool = False,
        multi_priority_gated: bool = False,
        slow_start: bool = False,
        with_hooks: bool = True,
    ) -> None:
        self.state_file.write_text(f"VERSION = {version}\n", encoding="utf-8")
        if fail:
            half_body = [
                "from pathlib import Path",
                "from gsuid_core.sv import SV",
                "from gsuid_core.server import on_core_shutdown",
                *self._log_helper_lines(),
                f"sv = SV({self.sv_name!r})",
                f"@sv.on_fullmatch({command + '半残'!r})",
                "async def _half_cmd(bot, ev): return None",
                "@on_core_shutdown",
                f'async def _half_stop(): _log("shutdown:{version}:half")',
                'raise RuntimeError("reload_boom")',
            ]
            self.init_file.write_text("\n".join(half_body) + "\n", encoding="utf-8")
            return
        body = [
            "import time, asyncio",
            "from pathlib import Path",
            "from gsuid_core.sv import SV",
            "from gsuid_core.server import on_core_start, on_core_shutdown",
            *self._log_helper_lines(),
            f'_log("import:{version}")',
            f"sv = SV({self.sv_name!r})",
            f"@sv.on_fullmatch({command!r})",
            "async def _cmd(bot, ev): return None",
        ]
        if with_hooks:
            if slow_start:
                body.extend(
                    [
                        "@on_core_start",
                        "async def _start():",
                        f'    _log("start:{version}:begin")',
                        "    try:",
                        "        await asyncio.sleep(5)",
                        "    except asyncio.CancelledError:",
                        f'        _log("start:{version}:cancelled")',
                        "        raise",
                    ]
                )
            elif sync_shutdown_gated:
                body.extend(
                    [
                        f"_GATE = Path({str(self.sync_gate)!r})",
                        "@on_core_start",
                        f'async def _start(): _log("start:{version}:sync")',
                        "@on_core_shutdown",
                        "def _stop_sync():",
                        f'    _log("shutdown:{version}:sync:begin")',
                        "    deadline = time.monotonic() + 5.0",
                        "    while not _GATE.exists() and time.monotonic() < deadline:",
                        "        time.sleep(0.01)",
                        f'    _log("shutdown:{version}:sync:done")',
                    ]
                )
            elif multi_priority_gated:
                body.extend(
                    [
                        f"_GATE = Path({str(self.orphan_gate)!r})",
                        "@on_core_start",
                        f'async def _start(): _log("start:{version}:mp")',
                        "@on_core_shutdown(priority=0)",
                        "async def _stop_p0():",
                        f'    _log("shutdown:{version}:p0:begin")',
                        "    try:",
                        "        await asyncio.sleep(5.0)",
                        "    finally:",
                        "        deadline = time.monotonic() + 5.0",
                        "        while not _GATE.exists() and time.monotonic() < deadline:",
                        "            await asyncio.sleep(0.01)",
                        f'        _log("shutdown:{version}:p0:finally_done")',
                        "@on_core_shutdown(priority=1)",
                        "async def _stop_p1():",
                        f'    _log("shutdown:{version}:p1:begin")',
                        "    await asyncio.sleep(5.0)",
                    ]
                )
            else:
                body.extend(
                    [
                        "@on_core_start",
                        "async def _start():",
                        f"    from {self.plugin_name} import state",
                        f'    _log(f"start:{version}:state={{state.VERSION}}")',
                        "@on_core_shutdown",
                        "async def _stop():",
                        f"    from {self.plugin_name} import state",
                        f'    _log(f"shutdown:{version}:state={{state.VERSION}}:begin")',
                        f"    await asyncio.sleep({shutdown_delay})",
                        f'    _log(f"shutdown:{version}:state={{state.VERSION}}:done")',
                    ]
                )
        self.init_file.write_text("\n".join(body) + "\n", encoding="utf-8")

    def write_meta_plugin(self, version: int, *, is_meta: bool = True) -> None:
        self.meta_sub.mkdir(parents=True, exist_ok=True)
        self.meta_api_dir.mkdir(parents=True, exist_ok=True)
        (self.meta_dir / "__init__.py").write_text("", encoding="utf-8")
        (self.meta_dir / "__full__.py").write_text("", encoding="utf-8")
        if is_meta:
            self.meta_marker.write_text("", encoding="utf-8")
        elif self.meta_marker.exists():
            self.meta_marker.unlink()
        self.meta_api_file.write_text(f"API_VERSION = {version}\n", encoding="utf-8")
        meta_body = [
            "from pathlib import Path",
            "from gsuid_core import meta_plugins",
            "from gsuid_core.server import on_core_shutdown",
            *self._log_helper_lines(),
            f'_log("meta_import:{version}")',
            "@on_core_shutdown",
            "async def _meta_stop():",
            f"    from {self.meta_plugin_name}.api import API_VERSION",
            f"    req = meta_plugins.require({self.meta_plugin_name!r})",
            f'    _log(f"meta_shutdown:{version}:api={{API_VERSION}}:req={{req.API_VERSION}}")',
        ]
        self.meta_init_file.write_text("\n".join(meta_body) + "\n", encoding="utf-8")

    async def await_start(self) -> None:
        from gsuid_core.utils.plugins_update import reload_plugin as reload_mod

        state = reload_mod._get_loop_state(asyncio.get_running_loop())
        await state.start_tasks[self.plugin_name]

    async def reload_and_await_start(
        self,
        version: int,
        command: str,
        *,
        shutdown_delay: float = 0.01,
        sync_shutdown_gated: bool = False,
        multi_priority_gated: bool = False,
        with_hooks: bool = True,
    ) -> str:
        from gsuid_core.utils.plugins_update import reload_plugin as reload_mod

        self.write_plugin(
            version,
            command,
            shutdown_delay=shutdown_delay,
            sync_shutdown_gated=sync_shutdown_gated,
            multi_priority_gated=multi_priority_gated,
            with_hooks=with_hooks,
        )
        res = await reload_mod.reload_plugin_async(self.plugin_name)
        assert res.startswith("✨"), res
        if with_hooks:
            await self.await_start()
        return res

    def read_events(self) -> list[str]:
        if not self.events_file.exists():
            return []
        return self.events_file.read_text(encoding="utf-8").splitlines()

    async def wait_for_event(self, marker: str) -> None:
        for _ in range(200):
            if marker in self.read_events():
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"Timed out waiting for event {marker!r}; got {self.read_events()}")

    def matches_command(self, cmd: str) -> bool:
        ev = _make_event(cmd, pm=6)
        idx = get_trigger_index()
        return any(
            (owner := idx.owner_of(trig)) is not None and owner.name == self.sv_name and trig.check_command(ev)
            for trig in idx.candidates(ev)
        )

    async def cleanup(self) -> None:
        import gsuid_core.server as server_mod
        from gsuid_core import meta_plugins
        from gsuid_core.sv import config_plugins
        from gsuid_core.config import plugin_config_store
        from gsuid_core.utils.plugins_update import reload_plugin as reload_mod
        from gsuid_core.webconsole.mount_app import site

        self.sync_gate.write_text("release", encoding="utf-8")
        self.orphan_gate.write_text("release", encoding="utf-8")
        state = reload_mod._get_loop_state(asyncio.get_running_loop())
        if state.active_reload_tasks:
            await asyncio.gather(*list(state.active_reload_tasks), return_exceptions=True)
        if state.orphaned_shutdown_tasks:
            await asyncio.gather(*list(state.orphaned_shutdown_tasks), return_exceptions=True)
        for pname in (self.plugin_name, self.meta_plugin_name):
            for task_map in (state.start_tasks, state.shutdown_tasks):
                pending_task = task_map.pop(pname, None)
                if pending_task is not None and not pending_task.done():
                    pending_task.cancel()
                    await asyncio.gather(pending_task, return_exceptions=True)
            state.pending_shutdown_hooks.pop(pname, None)
            meta_plugins.unregister_meta_plugin(pname)
            SL.plugins.pop(pname, None)
            config_plugins.pop(pname, None)
            plugin_config_store._dirty.discard(pname)
            server_mod._import_memory_mb.pop(pname, None)
            site.unregister_plugin_admins(pname, lambda obj, p=pname: reload_mod._belongs_to_plugin(obj.__module__, p))
            for mod_key in [k for k in sys.modules if reload_mod._belongs_to_plugin(k, pname)]:
                sys.modules.pop(mod_key, None)
            for cache_key in [k for k in list(server_mod._module_cache) if pname in k]:
                server_mod._module_cache.pop(cache_key, None)
            for hook_set in (server_mod.core_start_def, server_mod.core_start_before_def, server_mod.core_shutdown_def):
                stale = {
                    h for h in hook_set if reload_mod._belongs_to_plugin(reload_mod._resolve_func_module(h.func), pname)
                }
                hook_set -= stale
        for admin_cls in self._extra_registered_admins:
            site._registered.pop(admin_cls, None)
        sv_obj = SL.lst.pop(self.sv_name, None)
        if sv_obj is not None:
            SL.detail_lst.pop(sv_obj.plugins, None)
        for added in (str(self.plugins_dir), str(self.plugin_dir), str(self.meta_dir)):
            server_mod._added_paths.discard(added)
            while added in sys.path:
                sys.path.remove(added)
        reset_trigger_index()


def test_reload_plugin_lifecycle_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证热重载基本生命周期：旧模块存活期跑 shutdown、清理管理模型、半导入回滚与 Meta 插件。"""
    from gsuid_core import meta_plugins
    from gsuid_core.utils.plugins_update import reload_plugin as reload_mod
    from gsuid_core.webconsole.mount_app import GsAdminModel, site

    harness = _ReloadPluginHarness(tmp_path, monkeypatch, "Lifecycle")

    class _DummyAdmin(GsAdminModel):
        pass

    class _DummyRegisteredAdmin(GsAdminModel):
        pass

    _DummyRegisteredAdmin.__module__ = f"plugins.{harness.plugin_name}.models"
    harness._extra_registered_admins.append(_DummyRegisteredAdmin)

    class _DummyExternalSingleFileAdmin(GsAdminModel):
        pass

    _DummyExternalSingleFileAdmin.__module__ = "external_pkg.shared_admin"

    async def scenario() -> None:
        try:
            await harness.reload_and_await_start(1, "测重指令一")
            assert harness.read_events() == ["import:1", "start:1:state=1"]
            assert harness.matches_command("测重指令一") is True

            site.plugins_page[harness.plugin_name] = [_DummyAdmin]
            site.plugins_page[f"{harness.plugin_name}.py"] = [_DummyExternalSingleFileAdmin]
            site.register_admin(_DummyRegisteredAdmin, _ADD=True)
            harness.write_plugin(2, "测重指令二", shutdown_delay=0.03)
            assert (await reload_mod.reload_plugin_async(harness.plugin_name)).startswith("✨")
            assert harness.plugin_name not in site.plugins_page
            assert f"{harness.plugin_name}.py" not in site.plugins_page
            assert _DummyRegisteredAdmin not in site._registered
            await harness.await_start()
            assert harness.read_events() == [
                "import:1",
                "start:1:state=1",
                "shutdown:1:state=1:begin",
                "shutdown:1:state=1:done",
                "import:2",
                "start:2:state=2",
            ]
            assert harness.matches_command("测重指令一") is False and harness.matches_command("测重指令二") is True

            before_fail = len(harness.read_events())
            harness.write_plugin(3, "测重指令三", fail=True)
            assert (await reload_mod.reload_plugin_async(harness.plugin_name)).startswith("❌")
            assert harness.matches_command("测重指令二") is False and harness.matches_command("测重指令三半残") is False
            assert harness.read_events()[before_fail:] == ["shutdown:2:state=2:begin", "shutdown:2:state=2:done"]

            before_meta = len(harness.read_events())
            harness.write_meta_plugin(1, is_meta=True)
            assert (await reload_mod.reload_plugin_async(harness.meta_plugin_name)).startswith("✨")
            assert meta_plugins.provides_of(harness.meta_plugin_name) == harness.meta_plugin_name
            assert meta_plugins.require(harness.meta_plugin_name).API_VERSION == 1

            harness.write_meta_plugin(2, is_meta=True)
            assert (await reload_mod.reload_plugin_async(harness.meta_plugin_name)).startswith("✨")
            assert meta_plugins.require(harness.meta_plugin_name).API_VERSION == 2

            harness.write_meta_plugin(3, is_meta=False)
            assert (await reload_mod.reload_plugin_async(harness.meta_plugin_name)).startswith("✨")
            assert meta_plugins.provides_of(harness.meta_plugin_name) is None
            assert f"{harness.meta_plugin_name}.api" not in sys.modules
            assert harness.read_events()[before_meta:] == [
                "meta_import:1",
                "meta_shutdown:1:api=1:req=1",
                "meta_import:2",
                "meta_shutdown:2:api=2:req=2",
                "meta_import:3",
            ]
        finally:
            await harness.cleanup()

    asyncio.run(scenario())


def test_reload_plugin_concurrency_cancellation_and_timeouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证热重载并发串行、取消隔离、同步兼容队列与超时预算。"""
    from gsuid_core.utils.plugins_update import reload_plugin as reload_mod

    harness = _ReloadPluginHarness(tmp_path, monkeypatch, "Concurrency")

    async def scenario() -> None:
        state = reload_mod._get_loop_state(asyncio.get_running_loop())
        try:
            await harness.reload_and_await_start(1, "并发指令一", shutdown_delay=0.04)
            before_step1 = len(harness.read_events())
            harness.write_plugin(2, "并发指令二", shutdown_delay=0.04)
            t1 = asyncio.create_task(reload_mod.reload_plugin_async(harness.plugin_name))
            await harness.wait_for_event("shutdown:1:state=1:begin")
            t2 = asyncio.create_task(reload_mod.reload_plugin_async(harness.plugin_name))
            r1, r2 = await asyncio.gather(t1, t2)
            assert r1.startswith("✨") and r2.startswith("✨")
            await harness.await_start()
            assert harness.read_events()[before_step1:] == [
                "shutdown:1:state=1:begin",
                "shutdown:1:state=1:done",
                "import:2",
                "import:2",
                "start:2:state=2",
            ]
            assert harness.matches_command("并发指令二") is True

            before_step2 = len(harness.read_events())
            harness.write_plugin(3, "并发指令三")
            assert reload_mod.reload_plugin(harness.plugin_name).startswith("✨")
            await harness.wait_for_event("shutdown:2:state=3:begin")
            harness.write_plugin(4, "并发指令四")
            assert reload_mod.reload_plugin(harness.plugin_name).startswith("✨")
            await harness.await_start()
            step2_events = harness.read_events()[before_step2:]
            assert "shutdown:2:state=3:done" in step2_events
            assert step2_events.index("shutdown:2:state=3:done") < step2_events.index("start:4:state=4")
            assert not any(ev.startswith("shutdown:3:") for ev in step2_events)

            before_step3 = len(harness.read_events())
            harness.write_plugin(5, "并发指令五", slow_start=True)
            assert (await reload_mod.reload_plugin_async(harness.plugin_name)).startswith("✨")
            await harness.wait_for_event("start:5:begin")
            await harness.reload_and_await_start(6, "并发指令六", with_hooks=False)
            assert harness.read_events()[before_step3:] == [
                "shutdown:4:state=4:begin",
                "shutdown:4:state=4:done",
                "import:5",
                "start:5:begin",
                "start:5:cancelled",
                "import:6",
            ]

            await harness.reload_and_await_start(7, "并发指令七", shutdown_delay=0.04)
            before_step4 = len(harness.read_events())
            harness.write_plugin(8, "并发指令八", shutdown_delay=0.04)
            t_cancel = asyncio.create_task(reload_mod.reload_plugin_async(harness.plugin_name))
            await harness.wait_for_event("shutdown:7:state=7:begin")
            t_cancel.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t_cancel
            await harness.wait_for_event("import:8")
            await harness.await_start()
            step4_events = harness.read_events()[before_step4:]
            assert "shutdown:7:state=7:done" in step4_events and "start:8:state=8" in step4_events
            assert harness.matches_command("并发指令八") is True

            await harness.reload_and_await_start(9, "并发指令九", sync_shutdown_gated=True)
            before_step5 = len(harness.read_events())
            with monkeypatch.context() as mp:
                mp.setattr(reload_mod, "_RELOAD_SHUTDOWN_TIMEOUT", 0.06)
                t0 = time.monotonic()
                await harness.reload_and_await_start(10, "并发指令十", with_hooks=False)
                elapsed = time.monotonic() - t0
            assert elapsed < 1.5 and harness.matches_command("并发指令十") is True
            harness.sync_gate.write_text("go", encoding="utf-8")
            await harness.wait_for_event("shutdown:9:sync:done")
            assert "shutdown:9:sync:begin" in harness.read_events()[before_step5:]

            await harness.reload_and_await_start(11, "并发指令十一", multi_priority_gated=True)
            before_step6 = len(harness.read_events())
            with monkeypatch.context() as mp:
                mp.setattr(reload_mod, "_RELOAD_SHUTDOWN_TIMEOUT", 0.06)
                harness.write_plugin(12, "并发指令十二", shutdown_delay=0.04)
                t0 = time.monotonic()
                res12 = await reload_mod.reload_plugin_async(harness.plugin_name)
                elapsed_mp = time.monotonic() - t0
            assert res12.startswith("✨") and elapsed_mp < 1.5 and len(state.orphaned_shutdown_tasks) == 1
            await harness.await_start()
            harness.orphan_gate.write_text("go", encoding="utf-8")
            await harness.wait_for_event("shutdown:11:p0:finally_done")
            await asyncio.gather(*list(state.orphaned_shutdown_tasks), return_exceptions=True)
            await asyncio.sleep(0)
            assert not state.orphaned_shutdown_tasks
            step6_events = harness.read_events()[before_step6:]
            assert "shutdown:11:p0:begin" in step6_events and "shutdown:11:p1:begin" not in step6_events

            before_step7 = len(harness.read_events())
            harness.write_plugin(13, "并发指令十三")
            t13 = asyncio.create_task(reload_mod.reload_plugin_async(harness.plugin_name))
            await asyncio.sleep(0)
            assert reload_mod.reload_plugin(harness.plugin_name).startswith("❌")
            await harness.wait_for_event("shutdown:12:state=12:begin")
            assert reload_mod.reload_plugin(harness.plugin_name).startswith("❌")
            assert (await t13).startswith("✨")
            await harness.await_start()
            assert harness.read_events()[before_step7:] == [
                "shutdown:12:state=12:begin",
                "shutdown:12:state=12:done",
                "import:13",
                "start:13:state=13",
            ]
        finally:
            await harness.cleanup()

    asyncio.run(scenario())


def test_extract_update_failure_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """update_from_git_async 重载失败产出失败行且可提取；成功时 ❌ 提交日志不误判。"""
    from typing import TypeVar
    from collections.abc import Callable, Coroutine

    from gsuid_core.utils.plugins_update import _plugins as plugins_mod

    _T = TypeVar("_T")

    def _async_const(value: _T) -> Callable[..., Coroutine[None, None, _T]]:
        async def _fn(*_args: object, **_kwargs: object) -> _T:
            return value

        return _fn

    # 1) 提取侧：准确识别各类失败行，含 ❌ 的正常提交记录不误判
    git_err = [
        "❌ 更新插件 DemoPlugin 失败",
        "📋 原因：本地代码与远程代码存在合并冲突",
        "💡 解决：使用「强制更新」覆盖本地修改",
    ]
    reload_err = f"{plugins_mod.AUTO_RELOAD_FAIL_PREFIX}[DemoPlugin] 重载失败: boom"
    failure_cases: list[tuple[list[str] | str, str | None]] = [
        (["✅本次插件 DemoPlugin , 更新内容如下：", "❌ 移除废弃指令"], None),
        (["✅本次插件 DemoPlugin , 更新内容如下：", reload_err], reload_err),
        (git_err, "\n".join(git_err)),
        (["更新失败, 不存在该插件!"], "更新失败, 不存在该插件!"),
        ([], "插件更新失败"),
        ("请后跟有效的插件名称！", "请后跟有效的插件名称！"),
    ]
    for logs, expected in failure_cases:
        assert plugins_mod.extract_update_failure(logs) == expected

    # 2) 生产侧：pull 成功后重载失败必须在更新日志末尾追加失败行
    repo_dir = tmp_path / "DemoPlugin"
    repo_dir.mkdir()
    monkeypatch.setattr(plugins_mod, "is_reload", True)
    monkeypatch.setattr(plugins_mod, "git_is_valid_repo", _async_const(True))
    monkeypatch.setattr(plugins_mod, "git_fetch", _async_const((True, "")))
    monkeypatch.setattr(plugins_mod, "git_get_current_branch", _async_const("main"))
    monkeypatch.setattr(plugins_mod, "git_diff_commits", _async_const(["❌ 清理旧配置项"]))
    monkeypatch.setattr(plugins_mod, "git_pull", _async_const((True, "Updated")))

    async def scenario() -> None:
        monkeypatch.setattr(plugins_mod, "reload_plugin_async", _async_const("❌ 重载失败: boom"))
        fail_logs = await plugins_mod.update_from_git_async(0, repo_dir)
        assert fail_logs[-1] == reload_err
        assert plugins_mod.extract_update_failure(fail_logs) == reload_err

        monkeypatch.setattr(plugins_mod, "reload_plugin_async", _async_const("✨ 已重载插件 DemoPlugin!"))
        ok_logs = await plugins_mod.update_from_git_async(0, repo_dir)
        assert "❌ 清理旧配置项" in ok_logs
        assert plugins_mod.extract_update_failure(ok_logs) is None

    asyncio.run(scenario())
