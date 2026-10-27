import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from gsuid_core.segment import MessageSegment
from gsuid_core.ai_core.utils import _parse_at_segments, _ensure_at_trailing_space
from gsuid_core.ai_core.autonomy.tools import GuardState, bind_guard, reset_guard, send_message_by_ai
from gsuid_core.ai_core.autonomy.models import (
    SpokenLine,
    DeliveryPin,
    DeliveryPlan,
    GroupObservation,
    ObservationDigest,
    CareTargetSnapshot,
)
from gsuid_core.ai_core.autonomy.runner import AutonomyReport
from gsuid_core.ai_core.autonomy.select import (
    _recent_texts,
    render_digest,
)
from gsuid_core.ai_core.autonomy.dispatch import _clamp_drive
from gsuid_core.ai_core.autonomy.scheduler import AutonomyScheduler
from gsuid_core.ai_core.heartbeat.dispatcher import get_dispatcher


def test_proactive_emitter_at_user_forwarding() -> None:
    """emit_proactive_message 应把 at_user_id 传递给 send_chat_result。"""
    from gsuid_core.models import Event
    from gsuid_core.ai_core.proactive.emitter import emit_proactive_message

    get_dispatcher()._last_send.clear()
    ev = Event(
        bot_id="mock_bot",
        bot_self_id="mock_self",
        user_id="mock_user_101",
        user_pm=6,
        user_type="group",
        group_id="mock_group_201",
    )
    with patch("gsuid_core.ai_core.proactive.emitter.send_chat_result", new=AsyncMock()) as mock_send:
        with patch("gsuid_core.ai_core.outbound.record_outbound", new=AsyncMock()):
            with patch("gsuid_core.ai_core.proactive.emitter._sync_to_main_session", new=AsyncMock()):
                with patch("gsuid_core.ai_core.proactive.emitter._resolve_active_bot", return_value=MagicMock()):
                    res = asyncio.run(
                        emit_proactive_message(
                            ev,
                            "天气转凉，记得加衣服",
                            source="autonomy",
                            trigger_reason="test",
                            at_user_id="mock_user_101",
                        )
                    )
                    assert res is True
                    mock_send.assert_called_once()
                    _, kwargs = mock_send.call_args
                    assert kwargs.get("at_user_id") == "mock_user_101"


def test_send_message_by_ai_private_stays_private_and_named_group_is_kept() -> None:
    """私聊不改写到快照群；回群只用调用里的群号，并在该群 @ 对方。"""
    get_dispatcher()._last_send.clear()
    now = 1_000_000.0
    target = CareTargetSnapshot(
        user_id="mock_user_102",
        user_name="测试用户B",
        score=95,
        zone_value="close",
        is_master=False,
        bound_uids={},
        recent_lines=[],
        group_id="mock_group_202",
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        session_key="group:mock_group_202",
        last_human_ts=now - 300,
        last_assistant_ts=now - 1200,
    )
    home = GroupObservation(
        group_id="mock_group_home",
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        session_key="group:mock_group_home",
        last_human_ts=now - 400,
        last_assistant_ts=now - 1200,
        summary="",
        recent_lines=[],
    )
    borrowed = GroupObservation(
        group_id="mock_group_202",
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        session_key="group:mock_group_202",
        last_human_ts=now - 300,
        last_assistant_ts=now - 1200,
        summary="",
        recent_lines=[],
    )
    state = GuardState(
        persona_name="mock_persona",
        whitelist=frozenset({"mock_user_102"}),
        sessions={"mock_user_102": ("mock_group_202", "mock_bot", "mock_self", "mock_ws")},
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        allowed_groups={"mock_group_home": home, "mock_group_202": borrowed},
        allowed_users={"mock_user_102": target},
        now=now,
    )
    token = bind_guard(state)
    try:
        with patch(
            "gsuid_core.ai_core.proactive.emitter.emit_proactive_message", new=AsyncMock(return_value=True)
        ) as mock_emit:
            ctx = MagicMock()
            private = asyncio.run(
                send_message_by_ai(ctx, text="今天多喝温水", target_id="mock_user_102", target_type="user")
            )
            assert "用户 mock_user_102" in private
            assert "群 mock_group_202" not in private
            assert state.last_sent_group_id == ""
            private_event = mock_emit.call_args.args[0]
            assert private_event.user_type == "direct"
            assert private_event.group_id is None
            assert mock_emit.call_args.kwargs.get("at_user_id") is None

            state.sent_count = 0
            grouped = asyncio.run(
                send_message_by_ai(
                    ctx,
                    text="这句回到原群",
                    target_id="mock_group_home",
                    target_type="group",
                    user_id="mock_user_102",
                )
            )
            assert "群 mock_group_home（已前置 @mock_user_102）" in grouped
            assert "mock_group_202" not in grouped
            group_event = mock_emit.call_args.args[0]
            assert group_event.user_type == "group"
            assert group_event.group_id == "mock_group_home"
            assert mock_emit.call_args.kwargs.get("at_user_id") == "mock_user_102"
    finally:
        reset_guard(token)


def test_send_message_by_ai_allowed_when_user_inactive_for_long_time() -> None:
    """用户超过 2 小时未发言不再硬拦截，允许 AI 以自然口吻发信回复。"""
    get_dispatcher()._last_send.clear()
    now = 1_000_000.0
    target = CareTargetSnapshot(
        user_id="mock_user_103",
        user_name="测试用户C",
        score=95,
        zone_value="close",
        is_master=False,
        bound_uids={},
        recent_lines=[],
        group_id="mock_group_203",
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        session_key="group:mock_group_203",
        last_human_ts=now - 7200 - 100,
        last_assistant_ts=now - 3600,
    )
    state = GuardState(
        persona_name="mock_persona",
        whitelist=frozenset({"mock_user_103"}),
        sessions={"mock_user_103": ("mock_group_203", "mock_bot", "mock_self", "mock_ws")},
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        allowed_groups={},
        allowed_users={"mock_user_103": target},
        now=now,
    )
    token = bind_guard(state)
    try:
        with patch("gsuid_core.ai_core.proactive.emitter.emit_proactive_message", new=AsyncMock(return_value=True)):
            ctx = MagicMock()
            result = asyncio.run(
                send_message_by_ai(
                    ctx, text="刚才看到你在聊那个话题，感觉很有趣", target_id="mock_user_103", target_type="user"
                )
            )
            assert "已成功向 用户 mock_user_103 发送消息" in result
            assert state.sent_count == 1
    finally:
        reset_guard(token)


def test_send_message_by_ai_blocked_when_ai_speaks_too_frequently() -> None:
    """AI 刚刚（60秒内）在群内已有发言时，为了防刷屏应当拦截。"""
    get_dispatcher()._last_send.clear()
    now = 1_000_000.0
    group_obs = GroupObservation(
        group_id="mock_group_204",
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        session_key="group:mock_group_204",
        last_human_ts=now - 120,
        last_assistant_ts=now - 30,  # 30秒前刚发过言
        summary="",
        recent_lines=[],
    )
    state = GuardState(
        persona_name="mock_persona",
        whitelist=frozenset(),
        sessions={},
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        allowed_groups={"mock_group_204": group_obs},
        allowed_users={},
        now=now,
    )
    token = bind_guard(state)
    try:
        ctx = MagicMock()
        result = asyncio.run(
            send_message_by_ai(ctx, text="连续发言测试", target_id="mock_group_204", target_type="group")
        )
        assert "发送被拦截：AI 刚刚（60秒内）在群内已有发言" in result
        assert state.sent_count == 0
    finally:
        reset_guard(token)


def test_at_space_normalization() -> None:
    """测试 @xxx 后面跟文本时自动补充空格，防止平台渲染粘连。"""
    # 紧贴文本情况
    segments1 = _parse_at_segments("@123456你好呀")
    assert len(segments1) == 2
    assert segments1[0].type == "at"
    assert segments1[0].data == "123456"
    assert segments1[1].type == "text"
    assert segments1[1].data == " 你好呀"

    # 原本带空格情况
    segments2 = _parse_at_segments("@123456 你好呀")
    assert len(segments2) == 2
    assert segments2[0].type == "at"
    assert segments2[1].type == "text"
    assert segments2[1].data == " 你好呀"

    # _ensure_at_trailing_space 单独生效
    raw = [MessageSegment.at("10001"), MessageSegment.text("收到了")]
    ensured = _ensure_at_trailing_space(raw)
    assert str(ensured[1].data) == " 收到了"


def test_clamp_drive_recovers_care_user_when_group_id_provided() -> None:
    """当模型 TARGET 填群号但在关心列表中唯一属于该群时，智能纠偏为该用户。"""
    care_pin = DeliveryPin(
        user_id="mock_user_101",
        group_id="mock_group_201",
        is_master=False,
        zone_value="close",
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        session_key="group:mock_group_201",
        last_human_ts=1000.0,
        last_assistant_ts=0.0,
    )
    plan = DeliveryPlan(
        drive="care_user",
        target="mock_group_201",
        summary="关心群内成员",
        substantive=True,
        within_hours=True,
        hours_waived=False,
        care=[care_pin],
        groups=[],
        served_user_ids=frozenset(),
        quota_ok_keys=frozenset({"group:mock_group_201"}),
        gap_blocked_keys=frozenset(),
        now=2000.0,
    )
    corrected_drive, corrected_target = _clamp_drive("care_user", "mock_group_201", plan)
    assert corrected_drive == "care_user"
    assert corrected_target == "mock_user_101"


def test_scheduler_never_pushes_spoken_text_without_tool() -> None:
    """当模型未调用 send_message_by_ai 时，即便输出自白文本也不直接推到群里。"""
    scheduler = AutonomyScheduler()
    digest = ObservationDigest(
        persona_name="mock_persona",
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        mood_score=0.5,
        care_targets=[],
        group_digests=[],
        open_trails=[],
        available_plugins_summary=[],
        recently_used_tools=[],
        in_flight_tasks=[],
    )
    report = AutonomyReport(
        text="今天天气真好，去散散步吧！\nDRIVE: care_user\nTARGET: 123\nSUMMARY: 散步\nTRAILS: 散步",
        steps=[],
        duration_ms=100,
        session_id="test_sess",
    )
    with patch.object(scheduler, "_blocked", new=AsyncMock(return_value="")):
        with patch("gsuid_core.ai_core.autonomy.scheduler.build_observation", new=AsyncMock(return_value=digest)):
            with patch("gsuid_core.ai_core.autonomy.scheduler.should_skip_model", return_value=False):
                with patch(
                    "gsuid_core.ai_core.autonomy.scheduler.Persona.load_content", new=AsyncMock(return_value="")
                ):
                    with patch("gsuid_core.ai_core.autonomy.scheduler.render_prompt", return_value=""):
                        with patch(
                            "gsuid_core.ai_core.autonomy.scheduler.run_autonomy_agent",
                            new=AsyncMock(return_value=report),
                        ):
                            with patch(
                                "gsuid_core.ai_core.autonomy.scheduler.AIAutonomyActivity.insert_activity",
                                new=AsyncMock(),
                            ) as mock_store:
                                with patch("gsuid_core.ai_core.autonomy.scheduler.get_dispatcher") as mock_disp_getter:
                                    mock_disp = MagicMock()
                                    mock_disp_getter.return_value = mock_disp
                                    mode = asyncio.run(scheduler.run_persona("mock_persona", force=True))
                                    assert mode in ("staged_merge", "silent_only")
                                    assert mode != "immediate_push"
                                    _, store_kwargs = mock_store.call_args
                                    assert store_kwargs.get("delivery_mode") != "immediate_push"


def test_recent_texts_isolates_broadcast_and_other_directed_messages() -> None:
    """底片历史抽取应隔离快讯/自动签到及针对他人的群广播。"""
    now = 1_000_000.0
    lines = [
        SpokenLine(
            "mock_user_101",
            "测试用户A",
            "我想了解一下营养餐",
            now - 100,
            "mock_group_201",
            "bot",
            "self",
            "ws",
            "user",
        ),
        SpokenLine(
            "mock_user_102",
            "测试用户B",
            "【7x24快讯】今日天气晴朗",
            now - 80,
            "mock_group_201",
            "bot",
            "self",
            "ws",
            "user",
        ),
        SpokenLine(
            "assistant",
            "早柚",
            "【自动签到已完成】",
            now - 60,
            "mock_group_201",
            "bot",
            "self",
            "ws",
            "assistant",
        ),
        SpokenLine(
            "assistant",
            "早柚",
            "@100000002 这是给别人的建议",
            now - 40,
            "mock_group_201",
            "bot",
            "self",
            "ws",
            "assistant",
        ),
        SpokenLine(
            "assistant",
            "早柚",
            "收到啦，这就帮你查",
            now - 20,
            "mock_group_201",
            "bot",
            "self",
            "ws",
            "assistant",
        ),
    ]
    recent = _recent_texts(lines, "mock_user_101", group_id="mock_group_201", now=now, limit=6)
    assert not any("快讯" in r for r in recent)
    assert not any("自动签到" in r for r in recent)
    assert not any("给别人的建议" in r for r in recent)
    assert any("我想了解一下营养餐" in r for r in recent)
    assert any("收到啦" in r for r in recent)


def test_render_digest_anti_repetition_and_softened_trails() -> None:
    """底片展示当前时间、最近已发信记录、关心对象时间感知与连续性规划。"""
    now = 1_000_000.0
    target = CareTargetSnapshot(
        user_id="mock_user_101",
        user_name="测试用户A",
        score=90,
        zone_value="close",
        is_master=False,
        bound_uids={},
        recent_lines=["刚刚 你好呀"],
        group_id="mock_group_201",
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        session_key="group:mock_group_201",
        last_human_ts=now - 14400,
        last_assistant_ts=0.0,
    )
    digest = ObservationDigest(
        persona_name="mock_persona",
        bot_id="mock_bot",
        bot_self_id="mock_self",
        ws_bot_id="mock_ws",
        mood_score=0.2,
        care_targets=[target],
        group_digests=[],
        open_trails=["探讨长高技巧"],
        available_plugins_summary=[],
        recently_used_tools=[],
        in_flight_tasks=[],
        recent_proactive_records=["1小时前向 mock_group_201 发信：注意防寒保暖"],
    )
    target.preferences = ["习惯晚睡", "喜欢喝茶"]
    rendered = render_digest(digest, now=now)
    assert "当前时间：" in rendered
    assert "偏好画像：习惯晚睡；喜欢喝茶" in rendered
    assert "最近已主动发信记录（请勿就相同话题再次打扰或张冠李戴）：" in rendered
    assert "1小时前向 mock_group_201 发信：注意防寒保暖" in rendered
    assert "上次思考规划与线索（鼓励推进或跟进探索）：探讨长高技巧" in rendered
    assert "最后发言在4小时前" in rendered
    assert "上次没做完：" not in rendered
    assert "已较久未发言，非即时需求请勿在群内突兀打扰" not in rendered


def test_recent_texts_cross_context_and_chronological() -> None:
    """验证群聊中关心对象的发言提取包含前后群友发言（交叉语境），且按时间正序排列。"""
    now = 1_000_000.0
    lines = [
        SpokenLine("user_a", "张三", "今晚有人打深渊吗？", now - 300, "group_1", "bot", "self", "ws", "user"),
        SpokenLine("user_b", "李四", "我可以来，八点上线", now - 200, "group_1", "bot", "self", "ws", "user"),
        SpokenLine("user_c", "王五", "带我一个", now - 100, "group_1", "bot", "self", "ws", "user"),
    ]
    recent = _recent_texts(lines, "user_b", group_id="group_1", now=now, limit=8)
    assert len(recent) == 3
    # 验证时间正序（300s前 -> 200s前 -> 100s前）
    assert "张三: 今晚有人打深渊吗？" in recent[0]
    assert "[目标] 李四: 我可以来，八点上线" in recent[1]
    assert "王五: 带我一个" in recent[2]
