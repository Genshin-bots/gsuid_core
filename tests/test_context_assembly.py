"""装配统一（§5.3）与上下文管线测试：边界锁、动态块预算、历史压缩与线程视图。"""

from __future__ import annotations

import ast
import time
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections.abc import Sequence

import pytest
from pydantic_ai.messages import (
    ImageUrl,
    TextPart,
    ModelMessage,
    ModelRequest,
    ToolCallPart,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
    SystemPromptPart,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from tests._support import make_run_state, isolated_history
from gsuid_core.models import Event
from gsuid_core.message_history import MessageRecord
from gsuid_core.ai_core.inbox.turn import InboxTurn
from gsuid_core.ai_core.inbox.state import Strength, WakeItem, WakeKind, PendingEntry, ReceiptContext
from gsuid_core.message_history.link import EventLink

_ROOT = Path(__file__).resolve().parent.parent


def _ast_calls_and_imports(rel: str) -> tuple[set[str], set[str]]:
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    calls: set[str] = set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
        elif isinstance(node, ast.Import):
            imports.update(a.name.split(".")[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(a.name for a in node.names)
    return calls, imports


def _load_kits_once() -> None:
    from gsuid_core.ai_core.kits import occupants_of, load_enabled_kits

    if not occupants_of("mood"):
        asyncio.run(load_enabled_kits(run_init_steps=False))


def _user_rec(
    seq: int,
    uid: str,
    name: str,
    text: str,
    ts: float,
    *,
    msg_id: str | None = None,
    user_type: str = "group",
    reply_to_seq: int | None = None,
    reply_to_author: str | None = None,
    at_self: bool = False,
    extra_meta: dict[str, object] | None = None,
) -> MessageRecord:
    mid = msg_id if msg_id is not None else f"m{seq}"
    meta: dict[str, object] = {"user_type": user_type, "msg_id": mid}
    if extra_meta:
        meta.update(extra_meta)
    return MessageRecord(
        role="user",
        content=text,
        user_id=uid,
        user_name=name,
        timestamp=ts,
        metadata=meta,
        link=EventLink(
            seq=seq,
            msg_id=mid,
            reply_to_seq=reply_to_seq,
            reply_to_author=reply_to_author,
            at_self=at_self,
        ),
    )


def _bot_rec(
    seq: int,
    text: str,
    ts: float,
    *,
    msg_id: str,
    reply_to_uid: str,
    reply_to_name: str = "",
    in_reply_to: tuple[int, ...] = (),
    seen_seq: int | None = None,
    evidence: tuple[str, ...] = (),
    speech_channel: str = "persona",
    user_id: str = "900000001",
    persona: str = "测试人格",
) -> MessageRecord:
    meta: dict[str, object] = {
        "msg_id": msg_id,
        "speech_channel": speech_channel,
        "persona_name": persona,
        "reply_to_user_id": reply_to_uid,
    }
    if reply_to_name:
        meta["reply_to_user_name"] = reply_to_name
    return MessageRecord(
        role="assistant",
        content=text,
        user_id=user_id,
        user_name=persona,
        timestamp=ts,
        metadata=meta,
        link=EventLink(
            seq=seq,
            msg_id=msg_id,
            in_reply_to_seqs=in_reply_to,
            seen_seq=seen_seq,
            evidence=evidence,
        ),
    )


def _make_turn(
    seq: int,
    uid: str,
    kind: WakeKind,
    strength: Strength,
    ts: float,
    *,
    session_id: str = "OneBot:onebot:900000001:group:70002",
    group_id: str = "70002",
    msg_id: str | None = None,
    raw_text: str = "",
    user_pm: int = 6,
    from_seq: int | None = None,
    head_seq: int | None = None,
    persona: str = "测试人格",
    receipt: ReceiptContext | None = None,
    reply_id: str | None = None,
    reply: str | None = None,
) -> InboxTurn:
    ev = Event(
        "OneBot",
        "900000001",
        msg_id if msg_id is not None else f"m{seq}",
        "group",
        group_id,
        uid,
        {},
        user_pm,
        raw_text=raw_text,
        reply_id=reply_id,
        reply=reply,
    )
    return InboxTurn(
        session_id=session_id,
        persona_name=persona,
        entries=(PendingEntry(WakeItem(seq, uid, kind, strength, ts), ev, receipt=receipt, persona_name=persona),),
        from_seq=seq - 1 if from_seq is None else from_seq,
        head_seq=seq if head_seq is None else head_seq,
    )


def _user_texts(msgs: Sequence[ModelMessage]) -> list[str]:
    return [
        p.content
        for m in msgs
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart) and isinstance(p.content, str)
    ]


def test_ai_entries_consume_shared_assembly() -> None:
    """各 AI 入口必须走统一编排，禁止旁路直接调用记忆检索或结算。"""
    endpoint_calls, endpoint_imports = _ast_calls_and_imports("gsuid_core/webconsole/chat_with_history_api.py")
    assert {"run_interactive_turn", "build_session_system_prompt"} <= endpoint_calls
    assert endpoint_calls.isdisjoint({"dual_route_retrieve", "settle_turn"})
    assert endpoint_imports.isdisjoint({"dual_route_retrieve", "settle_turn", "classifier_service"})
    assert "build_session_system_prompt" in _ast_calls_and_imports("gsuid_core/ai_core/ai_router.py")[0]
    assert "assemble_dynamic_context" in _ast_calls_and_imports("gsuid_core/ai_core/handle_ai.py")[0]


def test_dynamic_context_ordering_and_suffix_policy() -> None:
    """动态上下文块顺序、产品块超限截断/丢弃与未寻址/软触发群轮后缀剥离。"""
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.hooks.points import AgentHookPoint
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context
    from gsuid_core.ai_core.interaction_scaffold import TurnGraph

    _load_kits_once()
    full, _ = asyncio.run(
        assemble_dynamic_context(
            query="那深圳呢",
            user_id="test_u",
            bot_id="TEST",
            persona_name=None,
            mood_key="test_u",
            rel=None,
            history_context="[历史对话] 旧→新\n小明: 你好",
            memory_context_text="用户喜欢喝美式",
            soft_triggered=True,
        )
    )
    assert 0 <= full.find("[历史对话]") < full.find("[长期记忆")

    def _run_with_ctx(ctx: AgentHookContext) -> str:
        return asyncio.run(
            assemble_dynamic_context(
                query="hi",
                user_id="u1",
                bot_id="TEST",
                persona_name=None,
                mood_key="u1",
                rel=None,
                history_context="H" * 500,
                memory_context_text="M" * 80,
                hook_ctx=ctx,
            )
        )[0]

    # 产品块超预算时按优先级截断低优块并丢弃溢出块；豁免结构块（口吻/历史/收件箱/记忆）原样保留
    tg = TurnGraph(
        user_type="group",
        message_text="hi",
        persona_name="测试人格",
        is_tome=True,
        primary_speaker="u1",
        call_to_self=True,
    )
    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=tg)
    voice = "（口吻：" + "轻软简短" * 45 + "）"
    pending = "（待你回应）" + "甲" * 160
    task_text = "事项#1｜进行中" + "X" * 170
    ctx.blocks = {
        "voice_anchor": voice,
        "inbox_pending": pending,
        "task": task_text,
        "relationship": "R" * 150,
        "presence": "P" * 80,
    }
    assembled = _run_with_ctx(ctx)
    assert voice in assembled and "H" * 500 in assembled and pending in assembled
    assert "M" * 80 in assembled and task_text in assembled
    assert "R" * 10 in assembled and "R" * 150 not in assembled and "…" in assembled
    assert "P" * 10 not in assembled

    # 未寻址群轮与软触发群轮：剥离产品块与记忆，保留口吻/历史/收件箱结构块
    idle = TurnGraph(
        user_type="group",
        message_text="hi",
        persona_name="测试人格",
        is_tome=False,
        primary_speaker="u1",
        call_to_self=False,
    )
    for soft in (False, True):
        idle_ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=idle, soft_triggered=soft)
        idle_ctx.blocks = {"voice_anchor": voice, "inbox_pending": pending, "task": "任务块"}
        out = _run_with_ctx(idle_ctx)
        assert voice in out and pending in out and "H" * 500 in out
        assert "任务块" not in out and "M" * 80 not in out


def test_alias_filtering_and_session_history_relean() -> None:
    """别名命中排除当前人格与旁人历史；WebConsole 历史加载、主动补轮与入史剥离保完整。"""
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.entity_index import clear_entity_index, register_entity_surface
    from gsuid_core.ai_core.control.lines import journal_delta_line
    from gsuid_core.ai_core.history_compact import _relean_user_turn
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context
    from gsuid_core.webconsole.chat_with_history_api import ChatHistoryTurn, _load_request_history

    _load_kits_once()
    clear_entity_index()
    register_entity_surface("北辰星", "北辰星", "SomePlugin")
    register_entity_surface("长离", "长离", "OtherPlugin")
    try:

        def _assemble(query: str, history: str) -> str:
            return asyncio.run(
                assemble_dynamic_context(
                    query=query,
                    user_id="test_u",
                    bot_id="TEST",
                    persona_name="北辰星",
                    mood_key="test_u",
                    history_context=history,
                )
            )[0]

        assert "别名表命中" not in _assemble("北辰星早上好", "")
        assert "别名表命中" not in _assemble(
            "傻孩子我是怕你被压扁",
            "[历史对话] 旧→新\n#3 [15:40:01] 路人(用户ID:other_u): 长离今天怎么样\n#4 [15:40:05] 你: 嗯嗯〔已发〕",
        )
        mixed = _assemble("北辰星，长离用什么武器", "")
        assert "长离→OtherPlugin" in mixed and "北辰星→" not in mixed
    finally:
        clear_entity_index()

    dummy_model = OpenAIChatModel("test-offline", provider=OpenAIProvider(api_key="test"))
    agent = GsCoreAIAgent(
        openai_chat_model=dummy_model, system_prompt="你是测试角色。", persona_name="测试人格", create_by="Chat"
    )
    _load_request_history(
        agent,
        [ChatHistoryTurn(role="user", content="你好"), ChatHistoryTurn(role="assistant", content="你好呀")],
        guard_on=False,
    )
    assert isinstance(agent.history[0], ModelRequest)
    assert any(isinstance(p, SystemPromptPart) and p.content == "你是测试角色。" for p in agent.history[0].parts)

    proactive = GsCoreAIAgent(
        openai_chat_model=dummy_model, system_prompt="你是巡检角色。", persona_name="测试人格", create_by="Chat"
    )
    proactive.append_proactive_assistant_turn(content="群里好安静呀", source="heartbeat", trigger_reason="test")
    assert any(isinstance(p, SystemPromptPart) and p.content == "你是巡检角色。" for p in proactive.history[0].parts)

    delta = journal_delta_line("[15:05 | 模拟盘记账] 记完收盘账")
    full_req = ModelRequest(parts=[UserPromptPart(content=f"[历史对话]\nA: 1\n\n[用户发言]\n你好\n{delta}")])
    _relean_user_turn([full_req], "[用户发言]\n你好")
    assert isinstance(full_req.parts[0], UserPromptPart) and full_req.parts[0].content == "[用户发言]\n你好"

    mm_req = ModelRequest(
        parts=[
            UserPromptPart(
                content=[f"[历史对话]\nA: 1\n\n[用户发言]\n看图\n{delta}", ImageUrl(url="https://x.invalid/a.png")]
            )
        ]
    )
    _relean_user_turn([mm_req], "[用户发言]\n看图")
    assert isinstance(mm_req.parts[0], UserPromptPart) and isinstance(mm_req.parts[0].content, list)
    assert mm_req.parts[0].content[0] == "[用户发言]\n看图" and isinstance(mm_req.parts[0].content[1], ImageUrl)


def test_thread_view_and_quote_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    """线程视图：引用边、连发合并、依据透传、静默/命令遮蔽、切片折叠、隐形昵称与滞后追问。"""
    import gsuid_core.ai_core.outbound as _outbound_mod
    from gsuid_core.ai_core.utils import sanitize_display_name, _build_relationship_description
    from gsuid_core.ai_core.outbound import QuoteResolve
    from gsuid_core.ai_core.thread_view import render_delta, render_window, render_router_view
    from gsuid_core.ai_core.inbox.blocks import quote_fact_lines, render_pending_block
    from gsuid_core.ai_core.evidence.store import Observation, get_evidence_store
    from gsuid_core.ai_core.history_format import is_persona_speech, remember_silence_handles

    base = 1_780_000_000.0
    records = [
        _user_rec(1, "100000003", "测试用户C", "pil活", base + 8),
        _user_rec(2, "100000003", "测试用户C", "让你画图", base + 16),
        _user_rec(3, "100000002", "测试用户B", "ぴー活是什么意思", base + 24, at_self=True),
        _user_rec(4, "100000002", "测试用户B", "神了", base + 32, reply_to_seq=2, reply_to_author="100000003"),
        _bot_rec(
            5,
            "唔…P活一般是パパ活的缩写，简单说就是…",
            base + 40,
            msg_id="b5",
            reply_to_uid="100000002",
            reply_to_name="测试用户B",
            in_reply_to=(3,),
            seen_seq=4,
            user_id="100000002",
        ),
        _user_rec(6, "100000004", "测试用户D", "谁叫汉堡", base + 48, reply_to_seq=5, reply_to_author="self"),
        _user_rec(7, "100000004", "测试用户D", "x2", base + 56),
        _user_rec(8, "100000004", "测试用户D", "x3", base + 64),
    ]

    out = render_delta(
        records,
        from_seq=0,
        head_seq=8,
        backfill=False,
        pending_seqs=frozenset({4, 6}),
        persona_name="测试人格",
        private=False,
    )
    lines = out.splitlines()
    quote_idx = next(i for i, ln in enumerate(lines) if "神了" in ln)
    sent_idx = next(i for i, ln in enumerate(lines) if "〔已发〕" in ln)
    assert quote_idx < sent_idx and "↩#2(测试用户C" in lines[quote_idx]

    quoted = next(ln for ln in lines if "谁叫汉堡" in ln)
    assert quoted.startswith("#6 ") and "↩#5(你→测试用户B" in quoted and "前 · 闲聊回复" in quoted
    for w in ("要回", "应该", "请回", "必须", "不要"):
        assert w not in quoted
    assert "x2 / x3" in next(ln for ln in lines if ln.startswith("#7–#8"))

    # metadata 不含 session_id 时，delta / router / window 均透传〔依据〕标签
    sess_id = "OneBot:onebot:900000001:group:70001"
    store = get_evidence_store(sess_id)
    store.observations.clear()
    store.append(
        Observation(
            obs_id="ob_basis01",
            session_id=sess_id,
            tool_name="daily_checkin",
            service="TestPlugin",
            requested_by="100000002",
            requested_by_seq=3,
            credential_owner="100000002",
            outcome="data",
            observed_at=time.time(),
            summary="已完成签到",
            handle="",
        )
    )
    try:
        ev_records = [
            *records[:4],
            _bot_rec(
                5,
                "已帮你完成签到啦",
                base + 42,
                msg_id="b_ev",
                reply_to_uid="100000002",
                reply_to_name="测试用户B",
                in_reply_to=(3,),
                seen_seq=4,
                evidence=("ob_basis01",),
            ),
            _user_rec(6, "100000004", "测试用户D", "测试人格，帮我签到", base + 48),
        ]
        delta_ev = render_delta(
            ev_records,
            from_seq=0,
            head_seq=6,
            backfill=False,
            pending_seqs=(6,),
            persona_name="测试人格",
            private=False,
            session_id=sess_id,
        )
        turn_ev = _make_turn(
            6,
            "100000004",
            "name",
            "hard",
            base + 48,
            session_id=sess_id,
            group_id="70001",
            raw_text="测试人格，帮我签到",
        )
        assert "〔依据: 替测试用户B查" in delta_ev
        assert "〔依据: 替测试用户B查" in render_router_view(turn_ev, ev_records)
        assert "〔依据: 替测试用户B查" in render_window(ev_records, persona_name="测试人格", session_id=sess_id)
    finally:
        store.observations.clear()

    # 静默委派句柄与命令命中遮蔽：均不污染窗口渲染
    ev_dlg = Event("OneBot", "900000001", "dlg_m1", "group", "group_dlg_1", "100000001", {}, 6)
    with isolated_history(ev_dlg) as hm:
        cmd_rec = hm.add_message(ev_dlg, role="user", content="全天候", metadata={"msg_id": "dlg_cmd_1"})
        assert hm.mark_command_hit(ev_dlg, "dlg_cmd_1") is True
        cmd_rendered = render_window([cmd_rec])
        assert "[用户调用了 bot 功能]" in cmd_rendered and "全天候" not in cmd_rendered

        hm.add_message(ev_dlg, role="user", content="帮我画一张测试指标图", user_name="测试用户A")
        remember_silence_handles(ev_dlg, ["dlg_2f7fe0a6-db1c-4aff-8195-b80c721ae644"], manager=hm)
        stored = hm.get_history(ev_dlg)
        assert not is_persona_speech(stored[-1])
        assert "dlg_2f7fe0a6" not in render_window(stored, persona_name="测试人格")

    # 群聊切片、私聊冷启动、刷屏折叠、路由器视图与窗口外引用解析
    t_now = base + 7200.0
    slice_records = [
        _user_rec(
            10, "100000099", "测试用户Z", "旧指令触发", t_now - 3600.0, msg_id="old1", extra_meta={"command_hit": True}
        ),
        _bot_rec(
            11,
            "[图片]",
            t_now - 3595.0,
            msg_id="old2",
            reply_to_uid="100000099",
            reply_to_name="测试用户Z",
            speech_channel="command_aside",
        ),
        _bot_rec(
            12,
            "这是之前回复给测试用户A 的内容",
            t_now - 90.0,
            msg_id="bot12",
            reply_to_uid="100000001",
            reply_to_name="测试用户A",
            in_reply_to=(10,),
        ),
        *[
            _user_rec(
                13 + idx,
                "100000002",
                "测试用户B",
                "哈哈",
                t_now - 60.0 + idx * 5.0,
                msg_id=f"rep_{idx}",
                reply_to_seq=12,
                reply_to_author="self",
            )
            for idx in range(4)
        ],
        _user_rec(
            17,
            "100000001",
            "测试用户A",
            "测试一下这个",
            t_now,
            msg_id="q17",
            reply_to_seq=12,
            reply_to_author="self",
        ),
    ]
    sliced_delta = render_delta(
        slice_records,
        from_seq=9,
        head_seq=17,
        backfill=True,
        pending_seqs=(17,),
        persona_name="测试人格",
        private=False,
    )
    assert "#10 " not in sliced_delta and "#11 " not in sliced_delta
    assert "(×4)" in sliced_delta and "#17 " in sliced_delta

    dm_old_records = [
        _user_rec(1, "100000001", "测试用户A", "两小时前的私聊问题", t_now - 7200.0, msg_id="dm1", user_type="direct"),
        _user_rec(2, "100000001", "测试用户A", "继续聊", t_now, msg_id="dm2", user_type="direct"),
    ]
    assert "两小时前的私聊问题" in render_delta(
        dm_old_records, from_seq=1, head_seq=2, backfill=True, pending_seqs=(2,), persona_name="测试人格", private=True
    )

    unquoted_spam = [
        _user_rec(20 + idx, "100000002", "测试用户B", "刷屏词", t_now - 30.0 + idx * 2.0, msg_id=f"sp_{idx}")
        for idx in range(4)
    ] + [_user_rec(25, "100000001", "测试用户A", "正经问题", t_now, msg_id="sp_q")]
    assert "刷屏词 (×4)" in render_delta(
        unquoted_spam,
        from_seq=19,
        head_seq=25,
        backfill=False,
        pending_seqs=(25,),
        persona_name="测试人格",
        private=False,
    )

    turn_qself = _make_turn(
        17, "100000001", "quote_self", "soft", t_now, msg_id="q17", raw_text="测试一下这个", user_pm=0, reply_id="bot12"
    )
    rview_q = render_router_view(turn_qself, slice_records, relation_label="主人", group_rate_1m=5)
    assert "引用对象=你→本人" in rview_q and "[场景] 关系=主人 · 近1分钟群消息=5条 · 触发=quote_self" in rview_q
    assert "(用户ID:" not in rview_q

    turn_rcpt = _make_turn(
        11,
        "100000099",
        "receipt",
        "soft",
        t_now,
        msg_id="old1",
        raw_text="旧指令触发",
        receipt=ReceiptContext(None, "测试插件", "entry.py", "错误码 -400016"),
    )
    rview_rcpt = render_router_view(turn_rcpt, slice_records)
    assert "★#11 receipt · 插件=测试插件" in rview_rcpt and "你上次回他是" not in rview_rcpt
    pblock_rcpt = render_pending_block(turn_rcpt, slice_records)
    assert "★#11 [命令回执] 插件=测试插件" in pblock_rcpt and "AI(" not in pblock_rcpt

    resolve_calls: list[str] = []

    async def _fake_resolve_quote(ev: Event) -> QuoteResolve:
        resolve_calls.append(ev.msg_id)
        return QuoteResolve(
            line="（被引用的是你于 14:40 发给群内的「测试播报」）", topic="测试播报", handle="", target="群内"
        )

    monkeypatch.setattr(_outbound_mod, "resolve_quote", _fake_resolve_quote)
    turn_oow = _make_turn(
        18,
        "100000001",
        "quote_self",
        "soft",
        t_now,
        msg_id="q_oow",
        raw_text="这",
        user_pm=0,
        reply_id="missing_mid",
        reply="测试播报内容",
    )
    oow_rec = _user_rec(
        18,
        "100000001",
        "测试用户A",
        "这",
        t_now,
        msg_id="q_oow",
        reply_to_seq=None,
        reply_to_author="self",
        extra_meta={"reply_id": "missing_mid", "reply_preview": "测试播报内容"},
    )
    assert asyncio.run(quote_fact_lines(turn_oow, [*slice_records, oow_rec])) == [
        "★#18 （被引用的是你于 14:40 发给群内的「测试播报」）"
    ]
    assert asyncio.run(quote_fact_lines(turn_qself, slice_records)) == [] and resolve_calls == ["q_oow"]

    # 隐形字符昵称清洗 + 滞后追问已有出站交付标注
    for raw_name, expected in (
        ("小\u200b明\ufeff", "小明"),
        ("\u200b\u3164\u2800", "群友_0001"),
        ("100000001\u200b", ""),
        ("", ""),
    ):
        assert sanitize_display_name(raw_name, "100000001") == expected
    assert "群友_0001(用户ID:100000001)" in _build_relationship_description("\u200b\u3164", "100000001")

    t0 = 1_760_000_000.0
    recs = [
        _user_rec(10, "100000001", "\u200b\u3164", "查一下测试数据", t0),
        _user_rec(11, "100000001", "\u200b\u3164", "好了没", t0 + 20),
        _bot_rec(
            12,
            "测试数据图已发给你啦",
            t0 + 30,
            msg_id="m12",
            reply_to_uid="100000001",
            in_reply_to=(10,),
            user_id="bot",
        ),
    ]
    turn_lag = _make_turn(
        11,
        "100000001",
        "mention",
        "hard",
        t0 + 20,
        session_id="OneBot:onebot:900000001:group:group_1",
        group_id="group_1",
        raw_text="好了没",
        from_seq=9,
        head_seq=12,
    )
    pending_txt = render_pending_block(turn_lag, recs)
    assert "群友_0001(用户ID:100000001)" in pending_txt
    assert "在该条(#11)之后你已于 #12 向其发过回复/交付" in pending_txt and "你上次回他是 #12" not in pending_txt
    delta_txt = render_delta(
        recs,
        from_seq=9,
        head_seq=12,
        backfill=False,
        pending_seqs=frozenset({11}),
        persona_name="测试人格",
        private=False,
    )
    assert "群友_0001(用户ID:100000001)" in delta_txt and "#12 " in delta_txt
    late_rec = _user_rec(13, "100000002", "测试用户B", "一小时后的新追问", t0 + 3600)
    delta_late = render_delta(
        [*recs, late_rec],
        from_seq=9,
        head_seq=13,
        backfill=False,
        pending_seqs=frozenset({11, 13}),
        persona_name="测试人格",
        private=False,
    )
    assert "#12 " in delta_late and "#13 " in delta_late


def test_compact_session_history_and_anchor_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """历史压缩：保头折尾/防改原对象、过期前缀滚出保 SystemPrompt、单轮工具链保护与滚出补锚。"""
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.history_compact import compact_session_history
    from gsuid_core.ai_core.configs.ai_config import ai_config

    # 1) 保头字节不变、仅折尾段只读工具返回，且不原地修改原始 ModelRequest
    head_big = "H" * 1500
    tail_big = "T" * 1500
    tail_sc_part = ToolReturnPart(tool_name="search_cognition", content=tail_big, tool_call_id="c_tail")
    tail_sc_req = ModelRequest(parts=[tail_sc_part])
    hist_fold: list[ModelRequest | ModelResponse] = [
        ModelRequest(parts=[UserPromptPart(content="u0")]),
        ModelResponse(parts=[ToolCallPart(tool_name="search_cognition", args="{}", tool_call_id="c_head")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="search_cognition", content=head_big, tool_call_id="c_head")]),
        ModelResponse(parts=[TextPart(content="a0")]),
    ]
    for i in range(1, 10):
        hist_fold.append(ModelRequest(parts=[UserPromptPart(content=f"u{i}")]))
        hist_fold.append(ModelResponse(parts=[TextPart(content=f"a{i}")]))
    hist_fold.extend(
        [
            ModelRequest(parts=[UserPromptPart(content="u_last")]),
            ModelResponse(parts=[ToolCallPart(tool_name="search_cognition", args="{}", tool_call_id="c_tail")]),
            tail_sc_req,
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="call_tool", args='{"tool_name":"read_chat_history"}', tool_call_id="c_ro")
                ]
            ),
            ModelRequest(parts=[ToolReturnPart(tool_name="call_tool", content="R" * 1600, tool_call_id="c_ro")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="call_tool", args='{"tool_name":"register_kanban_task"}', tool_call_id="c_mut"
                    )
                ]
            ),
            ModelRequest(parts=[ToolReturnPart(tool_name="call_tool", content="M" * 1600, tool_call_id="c_mut")]),
            ModelResponse(parts=[TextPart(content="a_last")]),
        ]
    )
    compacted_fold, did_fold = compact_session_history(hist_fold, max_history=24, trim_ratio=0.75)
    assert did_fold is True
    by_cid: dict[str, ToolReturnPart] = {
        part.tool_call_id: part
        for msg in compacted_fold
        if isinstance(msg, ModelRequest)
        for part in msg.parts
        if isinstance(part, ToolReturnPart) and part.tool_call_id
    }
    assert by_cid["c_head"].content == head_big
    assert isinstance(by_cid["c_tail"].content, str) and len(by_cid["c_tail"].content) < 500
    assert tail_sc_req.parts[0] is tail_sc_part
    assert isinstance(by_cid["c_ro"].content, str) and len(by_cid["c_ro"].content) < 500
    assert by_cid["c_mut"].content == "M" * 1600

    # 2) 活跃窗保头 vs 3h 过期前缀滚出（含 UTC / naive 归一化）
    now_dt = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
    old_dt = now_dt - timedelta(hours=3)
    fresh_dt = now_dt - timedelta(minutes=2)

    def _make_hist(head_dt: datetime) -> list[ModelRequest | ModelResponse]:
        msgs: list[ModelRequest | ModelResponse] = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="固定系统提示词", timestamp=head_dt),
                    UserPromptPart(content="三小时前大盘走势", timestamp=head_dt),
                ]
            ),
            ModelResponse(parts=[TextPart(content="三小时前大盘回复")], timestamp=head_dt),
        ]
        for i in range(1, 10):
            ts = fresh_dt + timedelta(seconds=i * 5)
            msgs.append(ModelRequest(parts=[UserPromptPart(content=f"近期提问{i}", timestamp=ts)]))
            msgs.append(ModelResponse(parts=[TextPart(content=f"近期回复{i}")], timestamp=ts))
        return msgs

    for head_dt, expect_head_kept in (
        (fresh_dt, True),
        (old_dt, False),
        (datetime(2026, 11, 24, 11, 58, 0), True),
        (datetime(2026, 11, 24, 9, 0, 0), False),
    ):
        comp, did = compact_session_history(
            _make_hist(head_dt), max_history=12, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
        )
        assert did is True and isinstance(comp[0], ModelRequest)
        assert any(isinstance(p, SystemPromptPart) and p.content == "固定系统提示词" for p in comp[0].parts)
        users = _user_texts(comp)
        assert ("三小时前大盘走势" in users) is expect_head_kept and "近期提问9" in users

    def _append_tool_chain(msgs: list[ModelRequest | ModelResponse], count: int, prefix: str) -> None:
        for i in range(count):
            ts = fresh_dt + timedelta(seconds=i * 2)
            msgs.append(
                ModelResponse(
                    parts=[ToolCallPart(tool_name="demo_tool", args={"i": i}, tool_call_id=f"{prefix}{i}")],
                    timestamp=ts,
                )
            )
            msgs.append(
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name="demo_tool", content=f"ok{i}", tool_call_id=f"{prefix}{i}", timestamp=ts
                        )
                    ]
                )
            )

    # 单轮长工具链不产出 SystemPrompt -> ToolCall；滚出过期首轮后仍超限则压回 max_history
    single_turn_tools: list[ModelRequest | ModelResponse] = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="固定系统提示词", timestamp=old_dt),
                UserPromptPart(content="超长单轮工具提问", timestamp=old_dt),
            ]
        )
    ]
    _append_tool_chain(single_turn_tools, 8, "c")
    safe_tools_hist, _ = compact_session_history(
        single_turn_tools, max_history=10, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
    )
    assert _user_texts(safe_tools_hist[:1])

    oversized_after_roll: list[ModelRequest | ModelResponse] = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="固定系统提示词", timestamp=old_dt),
                UserPromptPart(content="过期旧提问", timestamp=old_dt),
            ]
        ),
        ModelResponse(parts=[TextPart(content="过期旧回复")], timestamp=old_dt),
        ModelRequest(parts=[UserPromptPart(content="后续唯一干净提问", timestamp=fresh_dt)]),
    ]
    _append_tool_chain(oversized_after_roll, 10, "r")
    capped_rolled, did_cap_roll = compact_session_history(
        oversized_after_roll, max_history=12, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
    )
    capped_users = _user_texts(capped_rolled)
    assert did_cap_roll is True and len(capped_rolled) <= 12
    assert "过期旧提问" not in capped_users and "后续唯一干净提问" in capped_users

    # 3) 滚出过期首轮后下一真人轮自动一次性补锚
    monkeypatch.setattr(ai_config.get_config("agent_max_history"), "data", 15)
    monkeypatch.setattr(ai_config.get_config("enable_deepseek_rp"), "data", True)
    dummy_model = OpenAIChatModel("test-offline", provider=OpenAIProvider(api_key="test"))
    agent_main = GsCoreAIAgent(openai_chat_model=dummy_model, system_prompt="固定系统提示词", create_by="Chat")
    agent_main.history = _make_hist(old_dt)
    agent_main.thread_backfill_due = False
    agent_main.extract_history()
    assert agent_main.thread_backfill_due is True

    def _inject_marker(text: str) -> tuple[str, str]:
        st = make_run_state(text)
        st.final_user_message = text
        st.lean_user_message = text
        agent_main._inject_deepseek_rp_marker(st)
        assert isinstance(st.final_user_message, str) and isinstance(st.lean_user_message, str)
        return st.final_user_message, st.lean_user_message

    re_cur, re_lean = _inject_marker("新回合消息")
    assert "【角色沉浸要求】" in re_cur and "【角色沉浸要求】" in re_lean
    assert _inject_marker("再下一轮") == ("再下一轮", "再下一轮")
