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
    assert plan.fused and plan.drop_blocked and plan.skip_ooc_rewrite is False

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
