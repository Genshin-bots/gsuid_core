"""观察名单怎么圈人、会话怎么选、底片怎么写给模型。"""

from typing import Literal

from gsuid_core.ai_core.autonomy.models import SpokenLine, ObservationDigest
from gsuid_core.ai_core.autonomy.select import (
    render_digest,
    preferred_line,
    digest_is_empty,
    eligible_user_ids,
    minimum_score_for,
    assemble_care_targets,
)
from gsuid_core.ai_core.relationship.zones import Zone


def _line(
    user_id: str,
    text: str,
    ts: float,
    *,
    group_id: str | None,
    role: Literal["user", "assistant"] = "user",
) -> SpokenLine:
    return SpokenLine(
        user_id=user_id,
        user_name="[你]" if role == "assistant" else user_id,
        text=text,
        ts=ts,
        group_id=group_id,
        bot_id="onebot",
        bot_self_id="bot",
        ws_bot_id="ws",
        role=role,
    )


def test_care_floor_comes_from_the_zone_table() -> None:
    assert minimum_score_for(Zone.FAMILIAR) == 50
    assert minimum_score_for(Zone.CLOSE) == 80


def test_masters_stay_and_recently_cared_users_drop() -> None:
    favors = [("low", 10), ("close", 90), ("familiar", 60)]
    ids = eligible_user_ids(favors, ["low"], Zone.FAMILIAR, {"close"})
    assert "close" not in ids
    assert "low" in ids
    assert "familiar" in ids
    strict = eligible_user_ids(favors, [], Zone.CLOSE, set())
    assert strict == ["close"]


def test_private_chat_beats_a_later_group_message() -> None:
    now = 1_000_000.0
    lines = [
        _line("u", "群里说的", now - 10, group_id="g1"),
        _line("u", "私聊说的", now - 100, group_id=None),
    ]
    chosen = preferred_line(lines, "u", now=now, allowed_groups={"g1"}, allow_private=True)
    assert chosen is not None
    assert chosen.group_id is None
    assert chosen.text == "私聊说的"
    group_only = preferred_line(lines, "u", now=now, allowed_groups={"g1"}, allow_private=False)
    assert group_only is not None
    assert group_only.group_id == "g1"


def test_group_outside_the_persona_scope_is_not_preferred() -> None:
    now = 1_000_000.0
    lines = [_line("u", "别的群", now - 5, group_id="other")]
    assert preferred_line(lines, "u", now=now, allowed_groups={"mine"}, allow_private=True) is None


def test_empty_signal_users_sort_after_people_who_spoke_and_cap_at_ten() -> None:
    now = 1_000_000.0
    ids = [f"u{i}" for i in range(12)]
    scores = {uid: 90 - index for index, uid in enumerate(ids)}
    lines = [
        _line("u0", "刚说过话", now - 20, group_id="g1"),
        _line("bot", "慢慢看，看完再跟你说", now - 10, group_id="g1", role="assistant"),
    ]
    targets = assemble_care_targets(
        ids,
        scores,
        set(),
        lines,
        {uid: {} for uid in ids},
        {},
        now=now,
        allowed_groups=None,
        allow_private=True,
    )
    assert len(targets) == 10
    assert targets[0].user_id == "u0"
    assert targets[0].bound_uids == {}
    assert targets[0].last_human_ts == now - 20
    assert targets[0].last_assistant_ts == now - 10
    assert "[你] 慢慢看，看完再跟你说" in targets[0].recent_lines[0]
    assert "刚说过话" in targets[0].recent_lines[1]


def test_empty_digest_skips_the_model_and_render_points_at_the_persona() -> None:
    empty = ObservationDigest("p", "", "", "", 0.0, [], [], [], [], [])
    assert digest_is_empty(empty)
    text = render_digest(empty, now=1_000_000.0)
    assert "人格自述" in text
    assert "DRIVE:" in text
    assert "早柚" not in text
    with_task = ObservationDigest(
        "p",
        "",
        "",
        "",
        0.0,
        [],
        [],
        [],
        [],
        [],
        in_flight_tasks=["[running] 渲染对比图 (群 914411529)"],
    )
    rendered_with_task = render_digest(with_task, now=1_000_000.0)
    assert "正在推进的后台任务" in rendered_with_task
    assert "渲染对比图" in rendered_with_task
