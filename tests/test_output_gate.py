"""统一输出闸门 pre_send_gate 与尖括号守卫测试。"""

from __future__ import annotations


def test_allow_clean_text() -> None:
    from gsuid_core.ai_core.output_gate import GateDecision, pre_send_gate

    r = pre_send_gate("呼…困了zzz", {}, channel="main")
    assert r.decision is GateDecision.ALLOW


def test_angle_bracket_rewrite_then_fuse() -> None:
    from gsuid_core.ai_core.output_gate import (
        GateDecision,
        is_fused,
        pre_send_gate,
        begin_response_batch,
    )
    from gsuid_core.ai_core.angle_bracket_guard import MAX_RETRIES

    extra: dict = {}
    dirty = "点歌？<bubble/>找主人"
    for _ in range(MAX_RETRIES - 1):
        begin_response_batch(extra)
        r = pre_send_gate(dirty, extra, channel="main")
        assert r.decision is GateDecision.REWRITE
        assert r.policy == "angle_bracket"
        assert r.feedback
        assert not is_fused(extra)

    begin_response_batch(extra)
    r3 = pre_send_gate(dirty, extra, channel="main")
    assert r3.decision is GateDecision.FUSE
    assert is_fused(extra)


def test_merge_rewrite_feedbacks() -> None:
    from gsuid_core.ai_core.output_gate import merge_rewrite_feedbacks

    assert merge_rewrite_feedbacks([]) == ""
    assert merge_rewrite_feedbacks(["only"]) == "only"
    m = merge_rewrite_feedbacks(["a", "b"])
    assert "a" in m and "b" in m and "---" in m


def test_plan_angle_after_run_fuse_keeps_ooc_rewrite() -> None:
    from gsuid_core.ai_core.output_gate import (
        pre_send_gate,
        begin_response_batch,
        plan_angle_after_run,
    )

    extra: dict = {}
    dirty = "x<bubble/>y"
    for _ in range(3):
        begin_response_batch(extra)
        pre_send_gate(dirty, extra, channel="main")
    plan = plan_angle_after_run(extra, clean_sent=[])
    assert plan.fused
    assert plan.drop_blocked
    assert plan.skip_ooc_rewrite is False


def test_replace_map_single_blocked_uses_last_clean() -> None:
    from gsuid_core.ai_core.output_gate import (
        pre_send_gate,
        begin_response_batch,
        plan_angle_after_run,
    )

    extra: dict = {}
    begin_response_batch(extra)
    pre_send_gate("脏<br>1", extra, channel="main")
    plan = plan_angle_after_run(extra, clean_sent=["干净回复"])
    assert plan.replace_map == {"脏<br>1": "干净回复"}
    assert not plan.rewrite_original


def test_gate_state_is_typed_bag_only() -> None:
    from gsuid_core.ai_core.output_gate import _STATE_KEY, GateBag, pre_send_gate

    extra: dict = {}
    pre_send_gate("a<br>b", extra, channel="main")
    assert _STATE_KEY in extra
    assert isinstance(extra[_STATE_KEY], GateBag)


def test_cjk_control_tags_are_illegal() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import has_illegal_angle_tags, find_illegal_angle_tags

    text = "<要求用其他语言复述用户的请求>\n早柚：困"
    assert has_illegal_angle_tags(text)
    assert any("要求" in t for t in find_illegal_angle_tags(text))
    assert not has_illegal_angle_tags("1 < 2 且 3 > 0")


def test_allows_protocol_tags_only() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import has_illegal_angle_tags

    assert not has_illegal_angle_tags("唔…困了")
    assert not has_illegal_angle_tags("<SILENCE>")
    assert not has_illegal_angle_tags("<沉默>")
    assert not has_illegal_angle_tags("困 `<meme: 困>` 呼")
    assert not has_illegal_angle_tags("叫 <@100000001> 过来")
    assert not has_illegal_angle_tags("叫 < @ 小明 > 过来")
    assert has_illegal_angle_tags('<report title="a">|x|y|\n|---|---|\n|1|2|</report>')


def test_code_span_and_fence_exempt() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import has_illegal_angle_tags, sanitize_illegal_angle_tags

    assert not has_illegal_angle_tags("可以用 `<br>` 换行")
    assert not has_illegal_angle_tags("```html\n<br>\n```")
    assert has_illegal_angle_tags("前面<br>后面，代码里是 `<br>`")
    san = sanitize_illegal_angle_tags("外<br>内 `<br>` 尾")
    assert "br" not in san.split("`")[0].lower()


def test_loose_meme_tags_stay_protocol() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import strip_protocol_tags, has_illegal_angle_tags

    for sample in ("< meme:愤怒 怼人>", "<meme :愤怒>", "＜meme:开心＞"):
        assert not has_illegal_angle_tags(sample)
        assert "meme" not in strip_protocol_tags(sample).lower()


def test_sanitize_removes_illegal_keeps_meme() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import sanitize_illegal_angle_tags

    raw = "困了<bubble/>别闹`<meme: 困>`"
    out = sanitize_illegal_angle_tags(raw)
    assert "bubble" not in out.lower()
    assert "meme" in out.lower()
    assert "困了" in out


def test_no_false_positive_on_comparisons_generics_email() -> None:
    from gsuid_core.ai_core.angle_bracket_guard import find_illegal_angle_tags

    assert find_illegal_angle_tags("a < b and c > d") == []
    assert find_illegal_angle_tags("List<str> and Map<int,str>") == []
    assert find_illegal_angle_tags("1 < 2 > 0") == []
    assert find_illegal_angle_tags("email <user@x.com>") == []
