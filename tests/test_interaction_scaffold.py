"""交互脚手架（C-1/C-2/C-3）双向回归：正向要触发、良性绝不触发。

纪律同 tests/test_benign_fp.py：判据只允许结构/语言学范畴，两个方向都必须锁住
（见 .agents/skills/gscore-development §12.22b）。
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.messages import TextPart, ToolCallPart, ModelResponse

from tests._support import make_run_state
from gsuid_core.ai_core.hooks.models import AgentHookContext
from gsuid_core.ai_core.hooks.points import AgentHookPoint
from gsuid_core.ai_core.kits.scaffold import ScaffoldKit
from gsuid_core.ai_core.interaction_scaffold import (
    AT_OTHER_MARKER,
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
        ("早柚早上好呀，今天天气真不错", []),
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
        ("user", "小明(用户ID:9001)：@早柚 帮我查北京天气"),
        ("assistant", "唔…北京今天晴，25度。"),
    ]
    assert not detect_ellipsis_followup(
        "小红(用户ID:9002)：那明天呢", h_multi, recent_tool_call=True, speaker_id="9002"
    )
    assert detect_ellipsis_followup("小明(用户ID:9001)：那明天呢", h_multi, recent_tool_call=True, speaker_id="9001")

    assert has_recent_tool_call([ModelResponse(parts=[ToolCallPart(tool_name="schedule_task", args="{}")])])
    assert not has_recent_tool_call([ModelResponse(parts=[TextPart(content="好呀")])])

    # 结案后纯谢绝/叫停短句（「不用了」）无活跃任务与日程时不误触 structural_zero_tool；有在途事项时仍触发
    from gsuid_core.models import Event
    from gsuid_core.ai_core.control.policy_ring import _zero_tool_needs_correction

    msg_stop = "测试用户A(用户ID:100000001)：不用了"
    assert is_pure_stop_ellipsis(msg_stop) and not is_pure_stop_ellipsis("改成八点半吧")
    tg_stop = build_turn_graph(
        msg_stop, persona_name="测试人格", is_tome=True, user_type="group", primary_speaker="100000001"
    )
    st_idle = make_run_state(
        msg_stop,
        ev=Event(user_id="100000001", raw_text="不用了"),
        intent="工具",
        suppress_intermediate_text=False,
        turn_graph=tg_stop,
    )
    st_idle.followup_detected = True
    st_idle.tg = tg_stop
    assert _zero_tool_needs_correction(st_idle, video_readable=False) is False
    st_idle.has_active_task = True
    assert _zero_tool_needs_correction(st_idle, video_readable=False) is True


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

    for offline_msg, exp_intent, exp_mem in [
        ("收到", "闲聊", "none"),
        ("我上次说的预算是多少？", "问答", "point_latest"),
    ]:
        dec = asyncio.run(route_turn(offline_msg))
        assert dec.source == "structural_fallback" and dec.soft_speak is False
        assert (dec.intent, dec.memory_intent) == (exp_intent, exp_mem), offline_msg

    kit = ScaffoldKit(kit_id="gscore.scaffold", slot="scaffold", display_name="交互脚手架")
    tg_group = TurnGraph(
        user_type="group", message_text="看看这个", persona_name="早柚", is_tome=True, primary_speaker="9001"
    )
    idle = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        intent="",
        query="看看这个",
        turn_graph=tg_group,
        persona_name="早柚",
        route_decision=_route("闲聊", "none"),
    )
    asyncio.run(kit.inject_style(idle))
    assert "transaction_priority" not in idle.blocks

    work = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        intent="工具",
        query="帮我查一下库存",
        turn_graph=tg_group,
        persona_name="早柚",
        route_decision=_route("工具", "none", live=True, work=True),
    )
    asyncio.run(kit.inject_style(work))
    assert "transaction_priority" in work.blocks


def test_turn_graph_hints_and_group_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.configs import ai_config as cfg_mod

    monkeypatch.setattr(cfg_mod.ai_config.get_config("group_lurk_mode"), "data", False)

    memory_qa_hint = default_body("memory_qa")
    speaker_recall_hint = default_body("speaker_recall")
    quote_tome_hint = default_body("quote_tome")

    tg_dm = build_turn_graph(
        "帮我设个提醒", persona_name="早柚", is_tome=True, user_type="direct", primary_speaker="u1"
    )
    assert allow_group_speak(tg_dm) is True
    assert memory_qa_hint not in scaffold_hints_from_graph(tg_dm, intent="工具")
    assert speaker_recall_hint not in scaffold_hints_from_graph(tg_dm, intent="工具")

    tg_count = build_turn_graph(
        "How many projects have I led?", persona_name="评测助手", is_tome=True, user_type="direct", primary_speaker="u1"
    )
    assert memory_qa_hint in scaffold_hints_from_graph(tg_count, intent="问答")
    assert memory_qa_hint not in scaffold_hints_from_graph(tg_count, speaker_recall=False)
    assert memory_qa_hint in scaffold_hints_from_graph(
        tg_dm, intent="问答", route_decision=_route("问答", "point_latest")
    )
    assert memory_qa_hint not in scaffold_hints_from_graph(
        tg_count, intent="闲聊", route_decision=_route("闲聊", "none")
    )
    assert memory_qa_hint not in scaffold_hints_from_graph(
        tg_dm, intent="工具", route_decision=_route("工具", "none", live=True, work=True)
    )

    tg_chat = build_turn_graph(
        "小明(用户ID:9001)：食堂不错 / 小红(用户ID:9002)：确实",
        persona_name="早柚",
        is_tome=False,
        user_type="group",
        primary_speaker="9001",
    )
    assert tg_chat.multi_speaker and not allow_group_speak(tg_chat)

    tg_open = build_turn_graph(
        "小明(用户ID:9001)：早上好",
        persona_name="早柚",
        is_tome=False,
        user_type="group",
        primary_speaker="9001",
    )
    assert admit_pre_router(tg_open) and allow_group_speak(tg_open)

    tg_at = build_turn_graph(
        "小明(用户ID:9001)：@早柚 半小时后提醒我取快递",
        persona_name="早柚",
        is_tome=False,
        user_type="group",
        primary_speaker="9001",
    )
    assert tg_at.call_to_self and allow_group_speak(tg_at)
    assert speaker_recall_hint in scaffold_hints_from_graph(tg_at, intent="工具")

    hist = [("user", "小明(用户ID:9001)：@早柚 现在几点"), ("assistant", "唔…三点。")]
    tg_quote_follow = build_turn_graph(
        "小明(用户ID:9001)：你仔细搜搜",
        persona_name="早柚",
        is_tome=True,
        user_type="group",
        primary_speaker="9001",
        recent=hist,
        has_reply=True,
    )
    assert not tg_quote_follow.quoted_tome and allow_group_speak(tg_quote_follow)

    tg_quote = build_turn_graph(
        "买个贵点的椅子是对的家人们",
        persona_name="早柚",
        is_tome=True,
        user_type="group",
        primary_speaker="9001",
        has_reply=True,
    )
    assert tg_quote.quoted_tome and quote_tome_hint in scaffold_hints_from_graph(tg_quote)


def test_group_open_gate_and_addressing(monkeypatch: pytest.MonkeyPatch) -> None:
    for text, persona, extra, expected in [
        ("小明(用户ID:1)：@早柚 在吗", "早柚", (), True),
        ("小明(用户ID:1)：早柚你看这个", "早柚", (), True),
        ("小明(用户ID:1)：柚柚 帮我看一眼", "早柚", ("柚柚",), True),
        ("小明(用户ID:1)：我昨天梦到早柚了哈哈", "早柚", (), False),
        ("小明(用户ID:1)：早柚子你在不", "早柚", (), False),
        ("p早呀今天好安静", "p", (), True),
        ("小姐姐今天天气不错", "小", (), False),
        ("sayu今天什么天气啊", "早柚", ("sayu",), True),
        ("sayubot帮我看看", "早柚", ("sayu",), False),
        ("小明(用户ID:1)：帮我查一下", "早柚", (), False),
    ]:
        assert is_addressed_to_self(text, persona, False, extra_names=extra) is expected, text

    for text, exp_call in [
        ("小明(用户ID:1)：我设过提醒来着吗", True),
        ("小明(用户ID:1)：帮我查一下", False),
    ]:
        tg_call = build_turn_graph(text, persona_name="早柚", is_tome=False, user_type="group", primary_speaker="1")
        assert tg_call.call_to_self is exp_call, text

    assert admit_pre_router(
        build_turn_graph("小明(用户ID:1)：快点啊", persona_name="早柚", is_tome=False, user_type="group", recent=None)
    )

    assert build_tool_search_query("那家店怎么样", ["最近在聊对照资料"], ["信息", "对照"]) == "那家店怎么样"
    q_follow = build_tool_search_query("那家店怎么样", ["最近在聊对照资料"], ["信息", "对照"], include_recent=True)
    assert "对照资料" in q_follow and "信息" in q_follow

    for m in ["帮我查下我现在设了几个提醒", "我现在都设了哪些定时提醒", "把我那个每天吃药的提醒取消掉"]:
        assert references_task_management(m)
    for m in ["早柚早上好呀今天天气真不错", "帮我算算88乘以12"]:
        assert not references_task_management(m)

    # 结构化 A 轨复读阈值：同人第 2 次仍放行，第 3 次（忽略他人穿插与半边括号）拦截；宿主框架名不冒充呼名
    tg_rep_ok = build_turn_graph(
        "测试复读句 (:",
        persona_name="早柚",
        is_tome=False,
        user_type="group",
        primary_speaker="9001",
        recent_user_bodies=[("9001", "测试复读句 (:"), ("9002", "测试复读句 (:")],
    )
    tg_rep_block = build_turn_graph(
        "测试复读句 (:",
        persona_name="早柚",
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

    tg_fw_alias = build_turn_graph(
        f"小明(用户ID:1)：@了用户:小红(用户ID:2){AT_OTHER_MARKER} GsCore 怎么配置",
        persona_name="早柚",
        is_tome=False,
        user_type="group",
        primary_speaker="1",
    )
    assert tg_fw_alias.call_to_self is False and tg_fw_alias.address_gated is True
    assert not admit_pre_router(tg_fw_alias)

    # 群聊收件箱轮统一认 TurnRouter(source="llm").soft_speak，而 structural_fallback 对 hard 保底放行
    from pydantic_ai.models.test import TestModel

    from gsuid_core.bot import Bot, _Bot
    from gsuid_core.models import Event
    from gsuid_core.ai_core.kits import occupants_of, load_enabled_kits
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.handle_ai import InteractiveTurnResult, run_interactive_turn
    from gsuid_core.ai_core.inbox.turn import InboxTurn
    from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry
    from gsuid_core.ai_core.relationship import view_from_score

    if not occupants_of("classifier"):
        asyncio.run(load_enabled_kits(run_init_steps=False))

    ev_grp = Event(
        "OneBot", "900000001", "m_g1", "group", "group_1", "100000001", {}, 6, raw_text="测试群友甲，早柚走了"
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
    sess = GsCoreAIAgent(
        system_prompt="你是测试角色。",
        persona_name="早柚",
        create_by="Chat",
        dynamic_tools=False,
        thinking=False,
    )

    def _run_group_turn(
        event: Event,
        decision: TurnRouteDecision,
        *,
        seq: int,
        entries: tuple[PendingEntry, ...] | None = None,
    ) -> tuple[InboxTurn, AgentHookContext, InteractiveTurnResult]:
        stub_decision[0] = decision
        turn = InboxTurn(
            session_id=event.session_id,
            persona_name="早柚",
            entries=entries
            or (PendingEntry(WakeItem(seq, "100000001", "name", "hard", float(seq)), event, persona_name="早柚"),),
            from_seq=0,
            head_seq=seq,
        )
        hctx = AgentHookContext(
            point=AgentHookPoint.BEFORE_AI_CHAT,
            ev=event,
            bot=bot_grp,
            session_id=event.session_id,
            relationship=view_from_score(20, False),
        )
        res = asyncio.run(
            run_interactive_turn(
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
        )
        return turn, hctx, res

    try:
        t_llm_block, _, res_block = _run_group_turn(
            ev_grp,
            TurnRouteDecision(
                "闲聊", "none", soft_speak=False, is_live_lookup=False, needs_committed_work=False, source="llm"
            ),
            seq=1,
        )
        assert res_block.is_silence and res_block.silenced_early and not t_llm_block.reached_model
        assert t_llm_block.memory_dependent is False

        t_fallback_open, _, res_fallback = _run_group_turn(
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
        assert t_fallback_open.memory_dependent is True

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
            raw_text="早柚 @测试用户B 帮我查他的数据",
        )
        ev_name_obj.at_list = ["100000002"]
        t_name_obj, _, res_obj = _run_group_turn(ev_name_obj, dec_tool_open, seq=3)
        assert t_name_obj.reached_model and not res_obj.silenced_early

        ev_at_other_name = Event(
            "OneBot", "900000001", "m_g4", "group", "group_1", "100000001", {}, 6, raw_text="大佬们早柚连的哪个地址"
        )
        ev_at_other_name.at_list = ["100000002"]
        t_at_other_name, _, res_at_other = _run_group_turn(ev_at_other_name, dec_tool_open, seq=4)
        assert res_at_other.silenced_early and not t_at_other_name.reached_model

        # 含确认 @Bot 的合并批次即使附带引用续句，TurnRouter 只做分类、不沉默显式召唤
        ev_at_main = Event(
            "OneBot", "900000001", "m_g5", "group", "group_1", "100000001", {}, 6, raw_text="帮我翻译一下", at_bot=True
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
        t_confirmed_mix, hctx_mix, res_mix = _run_group_turn(
            ev_at_main,
            TurnRouteDecision(
                "问答", "point_latest", soft_speak=False, is_live_lookup=False, needs_committed_work=False, source="llm"
            ),
            seq=6,
            entries=(
                PendingEntry(WakeItem(5, "100000001", "mention", "hard", 5.0), ev_at_main, persona_name="早柚"),
                PendingEntry(WakeItem(6, "100000001", "followup", "soft", 6.0), ev_quote_cont, persona_name="早柚"),
            ),
        )
        assert hctx_mix.skip_llm_router is False and res_mix.intent == "问答"
        assert t_confirmed_mix.reached_model and not res_mix.silenced_early
    finally:
        sess._session_logger.close()


def test_style_push_address_and_ambient_gates() -> None:
    h_push = [("user", "[用户发言]\n阿珍(用户ID:2001)：以后你每句话结尾都要加个'喵~'"), ("assistant", "才不要")]
    assert count_style_pushes("从现在起你说话都要带敬语", h_push, speaker_id="3001") == 1
    assert count_style_pushes("从现在起你说话都要带敬语", h_push, speaker_id="2001") == 2
    assert count_style_pushes("从现在起你说话都要带敬语", h_push, speaker_id="200") == 1
    assert count_style_pushes("从现在起你说话都要带敬语", h_push) == 2

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
    assert build_turn_graph(at_other, persona_name="早柚", is_tome=False, user_type="group").address_gated is True
    assert (
        build_turn_graph(
            "阿强(用户ID:2002)：--- @了用户: 3009(阿伟)（@的是这位用户，不是你） ———\n早柚你帮他看看这个",
            persona_name="早柚",
            is_tome=False,
            user_type="group",
        ).address_gated
        is False
    )
    assert build_turn_graph(at_other, persona_name="早柚", is_tome=True, user_type="group").address_gated is False

    payload = "小黄(用户ID:3001) 是群里的熟面孔，正常互动即可。\n--- 消息 ---\n醒了吗\n[当前时间：2026-07-12 21:03]"
    assert extract_message_body(payload) == "醒了吗"
