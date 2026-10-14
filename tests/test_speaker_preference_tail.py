"""说话人偏好在本轮 user 尾，不进共享 system，也不靠问句词面。"""

from __future__ import annotations

import asyncio

from gsuid_core.ai_core.self_cognition import (
    note_applies_to_speaker,
    render_speaker_preference_tail,
    collect_speaker_preference_rules,
)


def test_note_with_other_user_id_is_hidden() -> None:
    assert note_applies_to_speaker("回复保持简短", "100000010")
    assert note_applies_to_speaker("用户 100000010 所在城市为广州", "100000010")
    assert not note_applies_to_speaker("用户 99999 在上海", "100000010")
    assert note_applies_to_speaker("下周二开门", "100000010")


def test_weather_question_still_keeps_location_rule() -> None:
    """问句没有城市名。自述槽优先于一堆纠错，别人的笔记不进来。"""
    notes = [
        "用户 100000010 所在城市为广州。今后天气默认按广州处理。",
        "用户 99999 所在城市为上海",
        "回复保持简短",
    ]
    scoped = [("general", f"纠错规则{i}", True) for i in range(6)]
    scoped.append(("location", "在广州", False))
    rules = collect_speaker_preference_rules(notes, scoped, "100000010", limit=3)
    assert rules[0] == "location：在广州"
    assert all("上海" not in rule for rule in rules)
    wide = collect_speaker_preference_rules(notes, scoped, "100000010", limit=12)
    assert any("100000010" in rule and "广州" in rule for rule in wide)
    assert "回复保持简短" in wide
    assert all("上海" not in rule for rule in wide)
    text = render_speaker_preference_tail(rules)
    assert text.startswith("【当前说话人的偏好】")
    assert "别的群友不适用" in text
    other = collect_speaker_preference_rules(notes, [("location", "在广州", False)], "99999", limit=8)
    assert all("100000010" not in rule for rule in other)
    assert any("上海" in rule for rule in other)


def test_stable_self_model_omits_learned_preferences(monkeypatch) -> None:
    from gsuid_core.ai_core import self_cognition as mod

    async def fake_model(bot_id: str) -> dict[str, list[str]]:
        _ = bot_id
        return {
            "commitments": ["保持短句"],
            "preferences_learned": ["用户 100000010 所在城市为广州"],
            "recurring_topics": [],
            "self_notes": [],
        }

    async def fake_onto(bot_id: str) -> str:
        _ = bot_id
        return ""

    monkeypatch.setattr(mod, "get_self_model", fake_model)
    monkeypatch.setattr(mod, "ensure_self_ontology", fake_onto)
    text = asyncio.run(mod.build_self_cognition_context("bot", include_relationship=False))
    assert "保持短句" in text
    assert "广州" not in text
    assert "我学到的偏好" not in text


def test_dynamic_context_appends_speaker_tail(monkeypatch) -> None:
    from gsuid_core.ai_core import self_cognition as mod
    from gsuid_core.ai_core.context_assembly import SOFT_TRIGGER_NOTE, assemble_dynamic_context

    async def fake_tail(bot_id: str, speaker_id: str) -> str:
        _ = bot_id
        assert speaker_id == "100000010"
        return "【当前说话人的偏好】\n• location：在广州"

    monkeypatch.setattr(mod, "load_speaker_preference_tail", fake_tail)
    full, _has = asyncio.run(
        assemble_dynamic_context(
            query="查查我这边的天气多少",
            user_id="100000010",
            bot_id="bot",
            persona_name=None,
            mood_key="100000010",
            history_context="[历史对话] 旧→新\n小明: 你好",
            memory_context_text="用户喜欢喝美式",
            soft_triggered=True,
        )
    )
    assert "location：在广州" in full
    assert full.find(SOFT_TRIGGER_NOTE) < full.find("location：在广州")
    assert full.rstrip().endswith("• location：在广州")


def test_speaker_preference_with_timestamp_and_expired_tag() -> None:
    from datetime import datetime, timezone, timedelta

    from gsuid_core.ai_core.memory.relative_schedule import (
        one_off_schedule_expired,
        looks_like_one_off_relative_schedule,
    )

    past = datetime.now(timezone.utc) - timedelta(days=7)
    recent = datetime.now(timezone.utc) - timedelta(hours=20)
    scoped = [
        ("location", "我在西部", False, past),
        ("location", "明天要去学校（返校）", False, past),
        ("general", "凌晨说的明天指当天早上", False, past),
        ("location", "明天要去学校", False, recent),
    ]
    rules = collect_speaker_preference_rules([], scoped, "100000010", limit=5)
    west = next(rule for rule in rules if "我在西部" in rule)
    old_trip = next(rule for rule in rules if "明天要去学校（返校）" in rule)
    gloss = next(rule for rule in rules if "凌晨说的明天指当天早上" in rule)
    fresh = next(rule for rule in rules if "明天要去学校" in rule and "返校" not in rule)
    assert past.astimezone().strftime("[%Y.%m.%d]") in west
    assert "（已过期日程）" not in west
    assert "（已过期日程）" in old_trip
    assert "（已过期日程）" not in gloss
    assert "（已过期日程）" not in fresh
    assert not looks_like_one_off_relative_schedule("我一般今晚才有空")
    assert looks_like_one_off_relative_schedule("明天返校")
    created = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
    assert not one_off_schedule_expired(created, now=datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc))
    assert one_off_schedule_expired(created, now=datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc))
    naive_utc = datetime(2026, 10, 14, 0, 0)
    assert not one_off_schedule_expired(naive_utc, now=datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc))
