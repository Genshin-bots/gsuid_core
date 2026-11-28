"""交互脚手架（C-1/C-2/C-3）双向回归：正向要触发、良性绝不触发。

判据只允许结构/语言学范畴，两个方向都必须锁住（见 .agents/skills/gscore-development §12.22b）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import TextPart, ToolCallPart, ModelResponse

from gsuid_core.ai_core.hooks.models import AgentHookContext
from gsuid_core.ai_core.hooks.points import AgentHookPoint
from gsuid_core.ai_core.kits.scaffold import ScaffoldKit
from gsuid_core.ai_core.interaction_scaffold import (
    TurnGraph,
    admit_pre_router,
    build_turn_graph,
    allow_group_speak,
    count_style_pushes,
    extract_message_body,
    has_recent_tool_call,
    is_addressed_to_self,
    is_pure_stop_ellipsis,
    build_tool_search_query,
    is_manage_ellipsis_form,
    detect_ellipsis_followup,
    scaffold_hints_from_graph,
    references_task_management,
)
from gsuid_core.ai_core.prompt_group.catalog import default_body
from gsuid_core.ai_core.classifier.turn_router import (
    TurnIntent,
    MemoryIntent,
    TurnRouteDecision,
    route_turn,
    structural_route,
)


@pytest.fixture(autouse=True)
def _disable_group_lurk_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    monkeypatch.setattr(ai_config.get_config("group_lurk_mode"), "data", False)


def _route(
    intent: TurnIntent = "闲聊",
    memory_intent: MemoryIntent = "none",
    *,
    live: bool = False,
    work: bool = False,
) -> TurnRouteDecision:
    return TurnRouteDecision(
        intent=intent,
        memory_intent=memory_intent,
        soft_speak=False,
        is_live_lookup=live,
        needs_committed_work=work,
    )


H_REMIND = [("user", "帮我定个明晚的提醒"), ("assistant", "好嘞，明晚喊你~")]
H_WEATHER = [("user", "帮我查下上海明天的天气"), ("assistant", "好的，上海明天多云转晴。")]
H_CHAT = [("user", "今天好累啊"), ("assistant", "唔…摸摸。")]


def test_ellipsis_followup_and_speaker_isolation() -> None:
    for text, hist in [
        ("改成八点半吧，我想多睡会儿", H_REMIND),
        ("那深圳呢？", H_WEATHER),
        ("不用了，把那个取消掉吧", H_REMIND),
        ("改到10点吧", H_REMIND),
        ("阿珍(用户ID:2001)：提前到七点", H_REMIND),
        ("把早上喝水那个去掉，其它保留", H_REMIND),
        ("先把它停了别删，回来再开", H_REMIND),
    ]:
        assert detect_ellipsis_followup(text, hist, recent_tool_call=True), f"C-1 漏检: {text}"
        assert not detect_ellipsis_followup(text, hist, recent_tool_call=False), f"C-1 无 tool 应拒: {text}"

    for text, hist in [
        ("测试人格早上好呀，今天天气真不错", []),
        ("我们聊点别的吧，你最近在忙啥", H_CHAT),
        ("帮我明天10点提醒我交报告", H_REMIND),
        ("算了不设了，帮我算算 88 乘以 12 是多少", H_REMIND),
        ("你把他的认知改成猫猫", H_REMIND),
    ]:
        assert not detect_ellipsis_followup(text, hist, recent_tool_call=True), f"C-1 误触: {text}"

    assert is_manage_ellipsis_form("改回8点") and is_manage_ellipsis_form("那明天呢")
    for non_ellipsis in (
        "再设一个每天喝水提醒",
        "我生日是哪天来着，你还记得吗？",
        "小明你倒是说句话啊，这方案到底行不行？",
    ):
        assert not is_manage_ellipsis_form(non_ellipsis), non_ellipsis

    h_multi = [
        ("user", "小明(用户ID:9001)：@测试人格 帮我查北京天气"),
        ("assistant", "唔…北京今天晴，25度。"),
    ]
    assert not detect_ellipsis_followup(
        "小红(用户ID:9002)：那明天呢", h_multi, recent_tool_call=True, speaker_id="9002"
    )
    assert detect_ellipsis_followup("小明(用户ID:9001)：那明天呢", h_multi, recent_tool_call=True, speaker_id="9001")

    assert has_recent_tool_call([ModelResponse(parts=[ToolCallPart(tool_name="schedule_task", args="{}")])])
    assert not has_recent_tool_call([ModelResponse(parts=[TextPart(content="好呀")])])

    assert build_tool_search_query("那家店怎么样", ["最近在聊对照资料"], ["信息", "对照"]) == "那家店怎么样"
    q_follow = build_tool_search_query("那家店怎么样", ["最近在聊对照资料"], ["信息", "对照"], include_recent=True)
    assert "对照资料" in q_follow and "信息" in q_follow

    for m in ["帮我查下我现在设了几个提醒", "我现在都设了哪些定时提醒", "把我那个每天吃药的提醒取消掉"]:
        assert references_task_management(m)
    for m in ["测试人格早上好呀今天天气真不错", "帮我算算88乘以12"]:
        assert not references_task_management(m)

    # 异体字符协议标签折叠（<มeme:害怕> -> <meme:害怕>）与 presence 30m 可见窗
    from gsuid_core.ai_core.persona.presence import PresenceRibbon, SocialEchoDict, render_presence
    from gsuid_core.ai_core.control.angle_guard import (
        sanitize_illegal_angle_tags,
        fold_protocol_tag_homoglyphs,
    )

    msg_stop = "测试用户A(用户ID:100000001)：不用了"
    assert is_pure_stop_ellipsis(msg_stop) and not is_pure_stop_ellipsis("改成八点半吧")
    assert fold_protocol_tag_homoglyphs("吓一跳<มeme:害怕>") == "吓一跳<meme:害怕>"
    assert sanitize_illegal_angle_tags("吓一跳<มeme:害怕>") == "吓一跳<meme:害怕>"

    now_ts = 10_000.0
    echo_item: SocialEchoDict = {
        "target_key": "group_1",
        "is_private": False,
        "summary": "大家早呀",
        "emitted_at": now_ts - 200,
        "answered": True,
        "settled": True,
    }
    ribbon = PresenceRibbon(
        offscreen_doing="翻了会儿手账",
        offscreen_at=now_ts - 300,
        offscreen_scope="self",
        echoes=[echo_item],
    )
    rendered_pres = render_presence(ribbon, "", target_key="group_1", is_private=False, now=now_ts)
    assert "翻了会儿手账" in rendered_pres and "大家早呀" in rendered_pres
    assert render_presence(ribbon, "", target_key="group_1", is_private=False, now=now_ts + 1900) == ""


def test_turn_router_and_scaffold_kit_contract() -> None:
    for msg in ("", "收到", "好的", "早安"):
        res = structural_route(msg)
        assert res.intent == "闲聊" and res.memory_intent == "none", msg
    for qa_msg in ("上海明天天气怎么样？", "How many projects have I led?", "今日指标如何"):
        assert structural_route(qa_msg).intent == "问答", qa_msg
    followup = structural_route("那明天呢", prev_turn_used_tools=True)
    assert followup.intent == "工具" and followup.memory_intent == "point_latest"
    assert structural_route("帮我查一下今天的日程").intent == "工具"
    for cmd in ("画个图", "发个公告", "删除那条提醒", "生成一张表"):
        assert structural_route(cmd, wake_kind="mention").intent == "工具", cmd
    for mem_msg, expected_mem in [
        ("对比一下上次和这次", "version_diff"),
        ("按时间线梳理", "timeline_order"),
        ("总结一下本周安排", "broad_summary"),
        ("你看这个", "point_latest"),
    ]:
        assert structural_route(mem_msg, wake_kind="mention").memory_intent == expected_mem, mem_msg

    name_start = structural_route("测试人格，帮我看看这个", wake_kind="name", persona_names=("测试人格",))
    name_mid = structural_route("大佬们测试人格连的哪个地址", wake_kind="name", persona_names=("测试人格",))
    assert name_start.soft_speak is True and name_start.addressing == "direct_ask"
    assert name_mid.soft_speak is False and name_mid.addressing == "to_other"

    from gsuid_core.ai_core.classifier.turn_router import _parse_decision

    for raw, exp_soft, exp_addr, exp_intent in [
        ('{"a":"direct_ask","i":"qa","m":"latest","l":false,"w":false}', True, "direct_ask", "问答"),
        ('{"a":"follow_self","i":"chat","m":"none","l":false,"w":false}', True, "follow_self", "闲聊"),
        ('{"a":"to_other","i":"chat","m":"none","l":false,"w":false}', False, "to_other", "闲聊"),
        ('{"a":"comment_receipt","i":"chat","m":"none","l":false,"w":false}', False, "comment_receipt", "闲聊"),
        ('{"i":"tool","m":"none","s":true,"l":true,"w":true}', True, None, "工具"),
    ]:
        parsed = _parse_decision(raw)
        assert parsed is not None
        assert (parsed.soft_speak, parsed.addressing, parsed.intent) == (exp_soft, exp_addr, exp_intent), raw

    dec = asyncio.run(route_turn("我上次说的预算是多少？"))
    assert dec.source == "structural_fallback" and dec.soft_speak is False
    assert (dec.intent, dec.memory_intent) == ("问答", "point_latest")

    kit = ScaffoldKit(kit_id="gscore.scaffold", slot="scaffold", display_name="交互脚手架")
    tg_group = TurnGraph(
        user_type="group", message_text="看看这个", persona_name="测试人格", is_tome=True, primary_speaker="9001"
    )
    idle = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        intent="闲聊",
        query="看看这个",
        turn_graph=tg_group,
        persona_name="测试人格",
        route_decision=_route("闲聊", "none"),
    )
    asyncio.run(kit.inject_style(idle))
    assert "chitchat_style" in idle.blocks and "transaction_priority" not in idle.blocks

    work = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        intent="工具",
        query="帮我查一下库存",
        turn_graph=tg_group,
        persona_name="测试人格",
        route_decision=_route("工具", "none", live=True, work=True),
    )
    asyncio.run(kit.inject_style(work))
    assert "chitchat_style" not in work.blocks and "transaction_priority" not in work.blocks


def test_turn_graph_hints_and_group_gates() -> None:
    quote_tome_hint = default_body("quote_tome")

    tg_dm = build_turn_graph(
        "帮我设个提醒", persona_name="测试人格", is_tome=True, user_type="direct", primary_speaker="u1"
    )
    assert allow_group_speak(tg_dm) is True
    assert scaffold_hints_from_graph(tg_dm, intent="工具") == []

    tg_chat = build_turn_graph(
        "小明(用户ID:9001)：食堂不错 / 小红(用户ID:9002)：确实",
        persona_name="测试人格",
        is_tome=False,
        user_type="group",
        primary_speaker="9001",
    )
    assert tg_chat.multi_speaker and admit_pre_router(tg_chat) and allow_group_speak(tg_chat)

    tg_open = build_turn_graph(
        "小明(用户ID:9001)：早上好",
        persona_name="测试人格",
        is_tome=False,
        user_type="group",
        primary_speaker="9001",
    )
    assert admit_pre_router(tg_open) and allow_group_speak(tg_open)

    tg_at = build_turn_graph(
        "小明(用户ID:9001)：@测试人格 半小时后提醒我取快递",
        persona_name="测试人格",
        is_tome=False,
        user_type="group",
        primary_speaker="9001",
    )
    assert tg_at.call_to_self and allow_group_speak(tg_at)
    assert scaffold_hints_from_graph(tg_at, intent="工具") == []

    hist = [("user", "小明(用户ID:9001)：@测试人格 现在几点"), ("assistant", "唔…三点。")]
    tg_quote_follow = build_turn_graph(
        "小明(用户ID:9001)：你仔细搜搜",
        persona_name="测试人格",
        is_tome=True,
        user_type="group",
        primary_speaker="9001",
        recent=hist,
        has_reply=True,
    )
    assert not tg_quote_follow.quoted_tome and allow_group_speak(tg_quote_follow)

    tg_quote = build_turn_graph(
        "买个贵点的椅子是对的家人们",
        persona_name="测试人格",
        is_tome=True,
        user_type="group",
        primary_speaker="9001",
        has_reply=True,
    )
    assert tg_quote.quoted_tome and quote_tome_hint in scaffold_hints_from_graph(tg_quote)


def test_group_open_gate_and_addressing() -> None:
    for text, persona, extra, expected in [
        ("小明(用户ID:1)：@测试人格 在吗", "测试人格", (), True),
        ("小明(用户ID:1)：测试人格你看这个", "测试人格", (), True),
        ("小明(用户ID:1)：小测 帮我看一眼", "测试人格", ("小测",), True),
        ("小明(用户ID:1)：我昨天梦到测试人格了哈哈", "测试人格", (), False),
        ("小明(用户ID:1)：测试人格子你在不", "测试人格", (), False),
        ("p早呀今天好安静", "p", (), True),
        ("小姐姐今天天气不错", "小", (), False),
        ("demo今天什么天气啊", "测试人格", ("demo",), True),
        ("demobot帮我看看", "测试人格", ("demo",), False),
        ("小明(用户ID:1)：帮我查一下", "测试人格", (), False),
    ]:
        assert is_addressed_to_self(text, persona, False, extra_names=extra) is expected, text

    for text, exp_call in [
        ("小明(用户ID:1)：我设过提醒来着吗", True),
        ("小明(用户ID:1)：帮我查一下", False),
    ]:
        tg_call = build_turn_graph(text, persona_name="测试人格", is_tome=False, user_type="group", primary_speaker="1")
        assert tg_call.call_to_self is exp_call, text

    # 结构化 A 轨复读阈值：同人第 2 次仍放行，第 3 次（忽略他人穿插与半边括号）拦截
    tg_rep_ok = build_turn_graph(
        "测试复读句 (:",
        persona_name="测试人格",
        is_tome=False,
        user_type="group",
        primary_speaker="9001",
        recent_user_bodies=[("9001", "测试复读句 (:"), ("9002", "测试复读句 (:")],
    )
    tg_rep_block = build_turn_graph(
        "测试复读句 (:",
        persona_name="测试人格",
        is_tome=False,
        user_type="group",
        primary_speaker="9001",
        recent_user_bodies=[
            ("9001", "测试复读句 (:"),
            ("9002", "测试复读句 (:"),
            ("9001", "测试复读句 (:"),
        ],
    )
    assert admit_pre_router(tg_rep_ok) and not admit_pre_router(tg_rep_block)


def test_interactive_turn_group_router_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """群聊收件箱轮认 TurnRouter(source="llm").soft_speak，structural_fallback 对 hard 保底放行。"""
    from pydantic_ai.models.test import TestModel

    import gsuid_core.ai_core.session_logger as slog_mod
    from gsuid_core.bot import Bot, _Bot
    from gsuid_core.models import Event
    from gsuid_core.ai_core.kits import occupants_of, load_enabled_kits
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.handle_ai import InteractiveTurnResult, run_interactive_turn
    from gsuid_core.ai_core.inbox.turn import InboxTurn
    from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry
    from gsuid_core.ai_core.relationship import view_from_score

    monkeypatch.setattr(slog_mod, "AI_SESSION_LOGS_PATH", tmp_path)

    ev_grp = Event(
        "OneBot", "900000001", "m_g1", "group", "group_1", "100000001", {}, 6, raw_text="测试群友甲，测试人格走了"
    )
    bot_grp = Bot(_Bot("ws_g1"), ev_grp)
    stub_decision = [
        TurnRouteDecision(
            "闲聊", "none", soft_speak=False, is_live_lookup=False, needs_committed_work=False, source="llm"
        )
    ]

    async def _fake_route_turn(*_args: object, **_kwargs: object) -> TurnRouteDecision:
        return stub_decision[0]

    monkeypatch.setattr("gsuid_core.ai_core.classifier.turn_router.route_turn", _fake_route_turn)
    monkeypatch.setattr(
        "gsuid_core.ai_core.gs_agent.get_model_for_task",
        lambda _level, _kind=None: TestModel(custom_output_text="测试回复"),
    )
    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_config_name_for_task", lambda _level, _kind=None: "test:m")
    monkeypatch.setattr(
        "gsuid_core.ai_core.gs_agent.get_model_fingerprint_for_task",
        lambda _level, _kind=None: "fp",
    )

    async def scenario() -> None:
        if not occupants_of("classifier"):
            await load_enabled_kits(run_init_steps=False)

        sess = GsCoreAIAgent(
            system_prompt="你是测试角色。",
            persona_name="测试人格",
            create_by="Chat",
            dynamic_tools=False,
            thinking=False,
        )

        async def _run_group_turn(
            event: Event,
            decision: TurnRouteDecision,
            *,
            seq: int,
            entries: tuple[PendingEntry, ...] | None = None,
        ) -> tuple[InboxTurn, InteractiveTurnResult]:
            stub_decision[0] = decision
            turn = InboxTurn(
                session_id=event.session_id,
                persona_name="测试人格",
                entries=entries
                or (
                    PendingEntry(
                        WakeItem(seq, "100000001", "name", "hard", float(seq)), event, persona_name="测试人格"
                    ),
                ),
                from_seq=0,
                head_seq=seq,
                backfill=sess.thread_backfill_due,
            )
            hctx = AgentHookContext(
                point=AgentHookPoint.BEFORE_AI_CHAT,
                ev=event,
                bot=bot_grp,
                session_id=event.session_id,
                relationship=view_from_score(20, False),
            )
            res = await run_interactive_turn(
                bot=bot_grp,
                event=event,
                session=sess,
                query=event.raw_text,
                hook_ctx=hctx,
                soft_triggered=False,
                return_mode="return",
                deliver=False,
                settle=False,
                inbox_turn=turn,
            )
            return turn, res

        try:
            sess.thread_backfill_due = True
            t_llm_block, res_block = await _run_group_turn(
                ev_grp,
                TurnRouteDecision(
                    "闲聊", "none", soft_speak=False, is_live_lookup=False, needs_committed_work=False, source="llm"
                ),
                seq=1,
            )
            assert res_block.is_silence and res_block.silenced_early and not t_llm_block.reached_model
            assert sess.thread_backfill_due is True

            t_fallback_open, res_fallback = await _run_group_turn(
                ev_grp,
                TurnRouteDecision(
                    "问答",
                    "point_latest",
                    soft_speak=False,
                    is_live_lookup=False,
                    needs_committed_work=False,
                    source="structural_fallback",
                ),
                seq=2,
            )
            assert t_fallback_open.reached_model and not res_fallback.silenced_early
            assert sess.thread_backfill_due is False

            # name + @他人：句首呼名（@为宾语）放行，非句首提及且 @他人前置静默
            dec_tool_open = TurnRouteDecision(
                "工具", "point_latest", soft_speak=True, is_live_lookup=False, needs_committed_work=True, source="llm"
            )
            ev_name_obj = Event(
                "OneBot",
                "900000001",
                "m_g3",
                "group",
                "group_1",
                "100000001",
                {},
                6,
                raw_text="测试人格 @测试用户B 帮我查他的数据",
            )
            ev_name_obj.at_list = ["100000002"]
            t_name_obj, res_obj = await _run_group_turn(ev_name_obj, dec_tool_open, seq=3)
            assert t_name_obj.reached_model and not res_obj.silenced_early

            ev_at_other_name = Event(
                "OneBot",
                "900000001",
                "m_g4",
                "group",
                "group_1",
                "100000001",
                {},
                6,
                raw_text="大佬们测试人格连的哪个地址",
            )
            ev_at_other_name.at_list = ["100000002"]
            t_at_other_name, res_at_other = await _run_group_turn(ev_at_other_name, dec_tool_open, seq=4)
            assert res_at_other.silenced_early and not t_at_other_name.reached_model

            # 含确认 @Bot 的合并批次即使附带引用续句，TurnRouter 只做分类、不沉默显式召唤
            ev_at_main = Event(
                "OneBot",
                "900000001",
                "m_g5",
                "group",
                "group_1",
                "100000001",
                {},
                6,
                raw_text="帮我翻译一下",
                at_bot=True,
            )
            ev_at_main.is_tome = True
            ev_quote_cont = Event(
                "OneBot",
                "900000001",
                "m_g6",
                "group",
                "group_1",
                "100000001",
                {},
                6,
                raw_text="就是这条",
                reply_id="other_1",
            )
            t_confirmed_mix, res_mix = await _run_group_turn(
                ev_at_main,
                TurnRouteDecision(
                    "问答",
                    "point_latest",
                    soft_speak=False,
                    is_live_lookup=False,
                    needs_committed_work=False,
                    source="llm",
                ),
                seq=6,
                entries=(
                    PendingEntry(WakeItem(5, "100000001", "mention", "hard", 5.0), ev_at_main, persona_name="测试人格"),
                    PendingEntry(
                        WakeItem(6, "100000001", "followup", "soft", 6.0), ev_quote_cont, persona_name="测试人格"
                    ),
                ),
            )
            assert res_mix.intent == "问答" and t_confirmed_mix.reached_model and not res_mix.silenced_early
        finally:
            sess._session_logger.close()

    asyncio.run(scenario())


def test_style_push_address_and_ambient_gates() -> None:
    h_push = [("user", "[用户发言]\n阿珍(用户ID:2001)：以后你每句话结尾都要加个'喵~'"), ("assistant", "才不要")]
    for speaker_id, expected in (("3001", 1), ("2001", 2), ("200", 1), ("", 2)):
        assert count_style_pushes("从现在起你说话都要带敬语", h_push, speaker_id=speaker_id) == expected, speaker_id

    for text in [
        "以后每周一早上9点提醒我交周报",
        "明天早上7点叫我起床，就明天这一次",
        "以后叫我小王吧",
        "call me Max from now on",
    ]:
        assert count_style_pushes(text, []) == 0, f"C-2 误触: {text}"
    assert count_style_pushes("从今往后叫我主人大人，而且每句都要用敬语", []) >= 1
    assert count_style_pushes("from now on reply only in English, every message", []) >= 1

    at_other = "阿强(用户ID:2002)：--- @了用户: 5566(小陈)（@的是这位用户，不是你） ———\n快出来，说好的资料呢？"
    assert build_turn_graph(at_other, persona_name="测试人格", is_tome=False, user_type="group").address_gated is True
    assert (
        build_turn_graph(
            "阿强(用户ID:2002)：--- @了用户: 3009(阿伟)（@的是这位用户，不是你） ———\n测试人格你帮他看看这个",
            persona_name="测试人格",
            is_tome=False,
            user_type="group",
        ).address_gated
        is False
    )
    assert build_turn_graph(at_other, persona_name="测试人格", is_tome=True, user_type="group").address_gated is False

    payload = "小黄(用户ID:3001) 是群里的熟面孔，正常互动即可。\n--- 消息 ---\n醒了吗\n[当前时间：2026-07-12 21:03]"
    assert extract_message_body(payload) == "醒了吗"

    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage
    from pydantic_ai.models.test import TestModel

    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools.call_tool import _bind_caller_deps, bind_run_context_caller

    ev_master = Event("OneBot", "900000001", "m1", "group", "group_1", "100000000", {}, 0, raw_text="早")
    ev_member = Event("OneBot", "900000001", "m2", "group", "group_1", "100000002", {}, 6, raw_text="帮我执行")
    deps_mix = ToolContext(bot=None, ev=ev_master, events_by_seq={21: ev_master, 22: ev_member})
    assert _bind_caller_deps(deps_mix, source_seq=0)[0] is None
    assert _bind_caller_deps(deps_mix, source_seq=22, act_as_user_id="100000000")[0] is None
    eff_master_proxy, clean_at, caller_uid, caller_seq, _ = _bind_caller_deps(
        deps_mix, source_seq=21, at_user_id="@100000002", act_as_user_id="100000002"
    )
    assert eff_master_proxy is not None and eff_master_proxy.ev is not None
    assert eff_master_proxy.ev.user_id == "100000002" and clean_at == "100000002"
    assert caller_uid == "100000000" and caller_seq == 21
    rctx = RunContext(deps=deps_mix, model=TestModel(), usage=RunUsage())
    assert bind_run_context_caller(rctx, 0)[0] is None
    neutral_ctx, _ = bind_run_context_caller(rctx, 0, neutral_group_fallback=True)
    assert neutral_ctx is not None and neutral_ctx.deps.ev is not None
    assert neutral_ctx.deps.ev.user_id == "__group_shared__" and neutral_ctx.deps.ev.user_pm == 6
