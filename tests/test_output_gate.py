"""统一输出闸门 pre_send_gate 与尖括号守卫测试。"""

from __future__ import annotations


def test_pre_send_gate_lifecycle_and_after_run_plan() -> None:
    from gsuid_core.ai_core.output_gate import (
        _STATE_KEY,
        GateBag,
        GateDecision,
        is_fused,
        pre_send_gate,
        begin_response_batch,
        plan_angle_after_run,
        merge_rewrite_feedbacks,
    )
    from gsuid_core.ai_core.angle_bracket_guard import MAX_RETRIES

    assert pre_send_gate("呼…困了zzz", {}, channel="main").decision is GateDecision.ALLOW
    assert merge_rewrite_feedbacks([]) == ""
    assert merge_rewrite_feedbacks(["only"]) == "only"
    merged = merge_rewrite_feedbacks(["a", "b"])
    assert "a" in merged and "b" in merged and "---" in merged

    extra: dict = {}
    dirty = "点歌？<bubble/>找主人"
    for _ in range(MAX_RETRIES - 1):
        begin_response_batch(extra)
        r = pre_send_gate(dirty, extra, channel="main")
        assert r.decision is GateDecision.REWRITE and r.policy == "angle_bracket" and r.feedback
        assert not is_fused(extra)
    assert isinstance(extra[_STATE_KEY], GateBag)

    begin_response_batch(extra)
    assert pre_send_gate(dirty, extra, channel="main").decision is GateDecision.FUSE
    assert is_fused(extra)
    plan = plan_angle_after_run(extra, clean_sent=[])
    assert plan.fused and plan.blocked and plan.scrub_nudges

    extra_single: dict = {}
    begin_response_batch(extra_single)
    pre_send_gate("脏<br>1", extra_single, channel="main")
    plan_single = plan_angle_after_run(extra_single, clean_sent=["干净回复"])
    assert plan_single.replace_map == {"脏<br>1": "干净回复"} and not plan_single.rewrite_original


def test_angle_bracket_guard_detection_and_sanitization() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import (
        strip_protocol_tags,
        has_illegal_angle_tags,
        find_illegal_angle_tags,
        sanitize_illegal_angle_tags,
    )

    cjk_tag = "<要求用其他语言复述用户的请求>\n早柚：困"
    assert has_illegal_angle_tags(cjk_tag)
    assert any("要求" in t for t in find_illegal_angle_tags(cjk_tag))
    assert has_illegal_angle_tags('<report title="a">|x|y|\n|---|---|\n|1|2|</report>')

    for valid in (
        "唔…困了",
        "<SILENCE>",
        "<沉默>",
        "困 `<meme: 困>` 呼",
        "叫 <@100000001> 过来",
        "叫 < @ 小明 > 过来",
        "可以用 `<br>` 换行",
        "```html\n<br>\n```",
    ):
        assert not has_illegal_angle_tags(valid), valid

    for benign in ("a < b and c > d", "List<str> and Map<int,str>", "1 < 2 > 0", "email <user@x.com>"):
        assert find_illegal_angle_tags(benign) == []

    for loose_meme in ("< meme:愤怒 怼人>", "<meme :愤怒>", "＜meme:开心＞"):
        assert not has_illegal_angle_tags(loose_meme)
        assert "meme" not in strip_protocol_tags(loose_meme).lower()

    assert has_illegal_angle_tags("前面<br>后面，代码里是 `<br>`")
    san = sanitize_illegal_angle_tags("外<br>内 `<br>` 尾")
    assert "br" not in san.split("`")[0].lower()

    out = sanitize_illegal_angle_tags("困了<bubble/>别闹`<meme: 困>`")
    assert "bubble" not in out.lower() and "meme" in out.lower() and "困了" in out


def test_parse_reply_plan_table() -> None:
    """回复计划：无标签回落缺省、多目标合并、非法 #seq 丢弃、max_targets 截断、<SILENCE>。"""
    from gsuid_core.ai_core.reply_plan import parse_reply_plan
    from gsuid_core.ai_core.angle_bracket_guard import strip_protocol_tags, find_illegal_angle_tags

    cases = [
        # (正文, valid_seqs, default, max_targets, 期望组, 期望 invalid, 期望 silence)
        ("直接一句话", frozenset({1, 2}), (1,), 3, [((1,), "直接一句话")], (), False),
        ("<reply #2>\n给B", frozenset({1, 2}), (1,), 3, [((2,), "给B")], (), False),
        ("<reply #1 #2>\n合并回复", frozenset({1, 2, 3}), (1,), 3, [((1, 2), "合并回复")], (), False),
        ("<reply #9>\n引错", frozenset({1, 2}), (1,), 3, [((1,), "引错")], (9,), False),
        ("<reply #1 #2 #3>\n截断", frozenset({1, 2, 3}), (1,), 2, [((1, 2), "截断")], (), False),
        ("<reply #2> 同行给B", frozenset({1, 2}), (1,), 3, [((2,), "同行给B")], (), False),
        ("<reply #1>\n<SILENCE>\n<reply #2>\n给B", frozenset({1, 2}), (1,), 3, [((2,), "给B")], (), False),
        ("<SILENCE>", frozenset({1}), (1,), 3, [], (), True),
    ]
    for text, valid, default, mx, groups, invalid, silence in cases:
        plan = parse_reply_plan(text, valid_seqs=valid, default_seqs=default, max_targets=mx)
        assert plan.silence == silence, text
        assert [(g.seqs, g.text) for g in plan.groups] == groups, text
        assert plan.invalid_refs == invalid, text

    assert "<reply #3>" not in find_illegal_angle_tags("好 <reply #3> 嗯")
    assert "<reply" not in strip_protocol_tags("<reply #3>\n你好")


def test_report_speech_and_allow_render_gate() -> None:
    """自然多段对话不被误判为汇报，且无出图人格不因待出图拦截文本。"""
    from gsuid_core.ai_core.agent_run.speech_policy import should_block_user_visible_text
    from gsuid_core.ai_core.control.outbound_detectors import looks_like_report_speech

    casual_paras = (
        "你先别急，我刚刚把这几个步骤在脑子里过了一遍，其实没你想的那么复杂。\n\n"
        "第一步只要先把常用配置确认好，后面就能直接连贯跑起来，中间不用反复切页面。\n\n"
        "第二步注意留好缓冲时间，真卡住了再回来问我，我陪你一段段排查。\n\n"
        "总之放心去试就行，有异常随时告诉我。"
    )
    assert not looks_like_report_speech(casual_paras)

    bullet_report = (
        "整理了以下四项关键改动与详细核对说明供团队排查时参考：\n"
        "- 第一项配置说明：调整默认值、重试策略与超时时间，确保长期高并发环境下稳定运行且不丢包。\n"
        "- 第二项权限检查：在入站入口严格校验目标范围、机器人身份与账号绑定关系，避免多人串线。\n"
        "- 第三项缓存优化：超限分段无损合并至尾部气泡并保留段落格式，防止长文本被静默丢弃。\n"
        "- 第四项日志审计：补充关键路径异常告警信息与追踪上下文，方便运维在事后直接定位根因。"
    )
    assert looks_like_report_speech(bullet_report)

    blocked_allow, _ = should_block_user_visible_text(
        "free",
        bullet_report,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=(),
        fact_pack_pending=True,
        allow_render=True,
    )
    blocked_no_render, _ = should_block_user_visible_text(
        "free",
        bullet_report,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=(),
        fact_pack_pending=True,
        allow_render=False,
    )
    assert blocked_allow is True
    assert blocked_no_render is False

    # 收敛话术策略表：pre_send_gate(speech_ctx=...) 与 settle 出口共享同一评价器
    from gsuid_core.ai_core.output_gate import GateDecision, pre_send_gate
    from gsuid_core.ai_core.agent_run.speech_policy import SpeechContext, evaluate_speech_policy

    ctx_inline = SpeechContext(
        policy="free",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=("search",),
        fact_pack_pending=True,
        allow_render=True,
    )
    gate_res = pre_send_gate(bullet_report, {}, channel="main", speech_ctx=ctx_inline)
    assert gate_res.decision is GateDecision.REWRITE
    assert gate_res.policy == "speech_policy" and gate_res.detail == "report_speech"

    ctx_settle = SpeechContext(
        policy="silence_only",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=(),
        in_settle_exit=True,
    )
    assert evaluate_speech_policy(ctx_settle, "测试用户A 可以试试这个方案。") == (False, "ok")

    from gsuid_core.ai_core.control.policy_ring import (
        _OUTBOUND_RING,
        evaluate_speech_ring,
        rewrite_directive_for,
    )
    from gsuid_core.ai_core.control.outbound_detectors import (
        _OOC_RING,
        SOFT_JUDGE_CATEGORIES,
        NEVER_RELEASE_CATEGORIES,
        LAST_RESORT_SEND_ORIGINAL_CATEGORIES,
        build_ooc_warning,
        inspect_outbound_ooc,
    )

    assert evaluate_speech_ring(ctx_inline, bullet_report) == (True, "report_speech")
    rw = rewrite_directive_for(
        "report_speech",
        allow_render=True,
        tool_calls=1,
        recited_report=True,
        forbid_title="",
        wait_already_sent=False,
    )
    assert rw is not None and rw.reason_code == "report_speech"
    assert (
        rewrite_directive_for(
            "ok",
            allow_render=True,
            tool_calls=0,
            recited_report=False,
            forbid_title="",
            wait_already_sent=False,
        )
        is None
    )
    assert {r.name for r in _OUTBOUND_RING} == {"speech_policy", "angle_bracket", "ooc", "inner_os"}
    assert [r.category for r in _OOC_RING] == [
        "delivery_narration",
        "meta_narration",
        "capability_absence",
        "stale_present",
        "ai_selfref",
        "fund_claim",
        "machine_dump",
        "model_identity",
        "system_term",
    ]
    assert NEVER_RELEASE_CATEGORIES == {
        "fund_claim",
        "machine_dump",
        "capability_absence",
        "meta_narration",
        "stale_present",
    }
    assert SOFT_JUDGE_CATEGORIES == {"model_identity", "ai_selfref"}
    assert LAST_RESORT_SEND_ORIGINAL_CATEGORIES == {"capability_absence", "meta_narration", "stale_present"}
    hit = inspect_outbound_ooc("我是语言模型。")
    assert hit is not None and hit.category == "ai_selfref"
    assert "ai_selfref" in build_ooc_warning(hit)

    # 未知 channel 回落为 main 语义（machine_dump 延迟到 run 末重说而非 tool 当场打回）
    unknown_res = pre_send_gate("Traceback (most recent call last):\n  File x", {}, channel="unknown")
    assert unknown_res.decision is GateDecision.REWRITE and unknown_res.defer_ooc is True

    from gsuid_core.ai_core.agent_run.support import _claims_fake_done

    assert _claims_fake_done("我已经把定时弄好了")
    assert inspect_outbound_ooc("上周那项活动我这边没找到相关记录。") is None
    assert inspect_outbound_ooc("我这边数据库没有这条条目。") is not None


def test_numeric_recitation_and_initial_dispute_contracts() -> None:
    """短对话含 <reply #seq> 与年份不误判念数；首轮误判申辩不把本轮锁死为 <SILENCE>。"""
    import asyncio

    from pydantic_ai.tools import RunContext
    from pydantic_ai.usage import RunUsage
    from pydantic_ai.models.test import TestModel

    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.control.directive import (
        DISPUTE_ACKED_KEY,
        DISPUTE_EXTRA_KEY,
        DISPUTE_CLOSED_KEY,
        DISPUTE_HAS_PRIOR_REPLY_KEY,
    )
    from gsuid_core.ai_core.control.outbound_detectors import looks_like_numeric_recitation
    from gsuid_core.ai_core.buildin_tools.control_tools import dispute_directive

    # 协议头 <reply #seq> 与日历年份/日期不计入业务读数，保持原始 (80, 6, 4/140) 阈值不漂移
    concise_qa = (
        "<reply #1538>\n"
        "按基数比例计算而非总额：个人费率 2%、单位费率 6%；"
        "2026 年与 2026-11 口径下基数区间为 5000 到 25000，封顶按上限折算。"
    )
    assert looks_like_numeric_recitation(concise_qa) is False

    long_prose_metrics = (
        "关于这项指标的本轮测算结果如下：基础系数调整为 12.5%，浮动比例 3.2%，"
        "参考下限 4800，封顶上限 28500；折算后每期固定核定基数会随申报档位同步浮动，"
        "如果叠加补充档位还会额外计入对应差额部分，建议结合当月实际申报明细逐项核对确认后再执行后续系统的配置调整与归档备份。"
    )
    assert len(long_prose_metrics) >= 140
    assert looks_like_numeric_recitation(long_prose_metrics) is True

    dense_list = "- 项目甲：单位 16% / 个人 8%\n- 项目乙：单位 6% / 个人 2%\n- 项目丙：单位 0.8% / 个人 0.2%"
    assert looks_like_numeric_recitation(dense_list) is True

    # 首轮尚未产出过回复时调用 dispute_directive：记录申辩但不置 DISPUTE_CLOSED_KEY
    ctx_init = RunContext(deps=ToolContext(extra={}), model=TestModel(), usage=RunUsage())
    msg_init = asyncio.run(dispute_directive(ctx_init, "用户问的是生猪行情，不是咖啡点单"))
    assert ctx_init.deps.extra[DISPUTE_EXTRA_KEY] == ["用户问的是生猪行情，不是咖啡点单"]
    assert ctx_init.deps.extra[DISPUTE_ACKED_KEY] is True
    assert DISPUTE_CLOSED_KEY not in ctx_init.deps.extra
    assert "正常回复用户" in msg_init and "<SILENCE>" not in msg_init

    # 拦截重跑轮已有暂扣回复时调用 dispute_directive：置 DISPUTE_CLOSED_KEY 走静默放行原回复
    ctx_retry = RunContext(
        deps=ToolContext(extra={DISPUTE_HAS_PRIOR_REPLY_KEY: True}),
        model=TestModel(),
        usage=RunUsage(),
    )
    msg_retry = asyncio.run(dispute_directive(ctx_retry, "上一段是用户明确要求的纯文本口头概括"))
    assert ctx_retry.deps.extra[DISPUTE_CLOSED_KEY] is True
    assert "<SILENCE>" in msg_retry
