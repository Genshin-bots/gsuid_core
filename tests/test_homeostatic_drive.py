"""Phase 3 动态平衡 / 存在感：冲击-恢复曲线、作息基线带、巡检门、ACL 渲染。"""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

import pytest

from gsuid_core.ai_core.persona.config import DriveTraits
from gsuid_core.ai_core.persona.presence import (
    PresenceRibbon,
    settle_echoes,
    render_presence,
    refresh_persona_state,
)
from gsuid_core.ai_core.persona.homeostasis import (
    PersonaHomeostasis,
    touch_topic,
    cooled_topics,
    baseline_energy,
    apply_turn_shock,
    homeostasis_hint,
    advance_homeostasis,
    heartbeat_should_skip,
)


def _ts(hour: int, minute: int = 0) -> float:
    """固定时区的当天时间戳，供 baseline_energy 解析作息窗。"""
    dt = datetime(2024, 1, 1, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
    return dt.timestamp()


def test_turn_shock_drains_energy_and_raises_satiety() -> None:
    traits = DriveTraits(social_initiative=50, energy_endurance=50)
    state = PersonaHomeostasis(energy=0.75, social_satiety=0.40, last_updated_at=1000.0)
    for i in range(8):
        apply_turn_shock(state, traits, now=1000.0 + i * 60)
    assert state.energy <= 0.75 - 8 * (0.09 - 0.03) + 1e-9
    assert state.social_satiety == pytest.approx(min(1.0, 0.40 + 8 * 0.18))
    assert state.consecutive_turns == 8

    # 20 分钟空窗后连续轮次归零重数
    apply_turn_shock(state, traits, now=1000.0 + 8 * 60 + 1300)
    assert state.consecutive_turns == 1


def test_recovery_is_faster_for_high_traits() -> None:
    now = 10_000.0
    low = DriveTraits(social_initiative=10, energy_endurance=10)
    high = DriveTraits(social_initiative=90, energy_endurance=90)
    s_low = PersonaHomeostasis(energy=0.10, social_satiety=1.0, last_updated_at=100.0)
    s_high = PersonaHomeostasis(energy=0.10, social_satiety=1.0, last_updated_at=100.0)
    advance_homeostasis(s_low, low, now, baseline=0.80)
    advance_homeostasis(s_high, high, now, baseline=0.80)
    # 高耐力能量回归更快（tau 更小），高主动度饱食度掉得更快
    assert s_high.energy > s_low.energy
    assert s_high.social_satiety < s_low.social_satiety


def test_baseline_transition_bands() -> None:
    hours = ["09:00-12:00"]
    tz = "Asia/Shanghai"
    # 窗头 45 分钟线性 0.45→0.85
    start = baseline_energy(_ts(9, 0), hours, tz, 50)
    mid_up = baseline_energy(_ts(9, 22), hours, tz, 50)
    plateau = baseline_energy(_ts(10, 0), hours, tz, 50)
    tail = baseline_energy(_ts(11, 59), hours, tz, 50)
    outside = baseline_energy(_ts(15, 0), hours, tz, 50)
    assert start == pytest.approx(0.45)
    assert plateau == pytest.approx(0.80)
    assert start < mid_up < plateau
    assert tail < plateau
    assert outside == pytest.approx(0.35)
    # 高耐力在窗内不累到 0.75 以下
    assert baseline_energy(_ts(11, 59), hours, tz, 85) == pytest.approx(0.75)
    # 无有效窗 → 精力充沛默认 0.80
    assert baseline_energy(_ts(9, 0), [], tz, 50) == pytest.approx(0.80)
    assert baseline_energy(_ts(9, 0), hours, "Bad/Zone", 50) == pytest.approx(0.80)


def test_heartbeat_skip_thresholds_and_hint() -> None:
    assert heartbeat_should_skip(PersonaHomeostasis(social_satiety=0.85))
    assert heartbeat_should_skip(PersonaHomeostasis(energy=0.20))
    assert not heartbeat_should_skip(PersonaHomeostasis(energy=0.5, social_satiety=0.5))
    # 提示只出现状态词，不出数字
    tired = homeostasis_hint(PersonaHomeostasis(energy=0.2))
    eager = homeostasis_hint(PersonaHomeostasis(energy=0.9, social_satiety=0.1))
    assert tired and "0." not in tired
    assert eager and "0." not in eager
    assert homeostasis_hint(PersonaHomeostasis(energy=0.5, social_satiety=0.5)) == ""


def test_touch_and_cool_topic() -> None:
    state = PersonaHomeostasis()
    touch_topic(state, "深渊配队", 100.0)
    touch_topic(state, "深渊配队", 200.0)
    assert cooled_topics(state) == ["深渊配队"]
    # 12h 半衰后回到阈值下（last_updated_at 置位才会真衰减）
    state.last_updated_at = 200.0
    advance_homeostasis(state, DriveTraits(), now=200.0 + 43200, baseline=0.80)
    assert cooled_topics(state) == []


def test_render_presence_acl() -> None:
    now = 10_000.0
    ribbon = PresenceRibbon()
    ribbon.echoes = [
        {
            "target_key": "private:u1",
            "is_private": True,
            "summary": "问了好",
            "emitted_at": now - 600,
            "answered": True,
            "settled": True,
        },
        {
            "target_key": "g1",
            "is_private": False,
            "summary": "群里搭话",
            "emitted_at": now - 300,
            "answered": False,
            "settled": True,
        },
        {
            "target_key": "g2",
            "is_private": False,
            "summary": "别群的事",
            "emitted_at": now - 100,
            "answered": True,
            "settled": True,
        },
    ]
    group_render = render_presence(ribbon, "", target_key="g1", is_private=False, now=now)
    assert "群里搭话" in group_render
    assert "问了好" not in group_render
    assert "别群的事" not in group_render
    assert "当时没人接茬" in group_render
    priv_render = render_presence(ribbon, "", target_key="private:u1", is_private=True, now=now)
    assert "问了好" in priv_render
    assert "群里搭话" not in priv_render
    assert "当时有人接了话" in priv_render
    # 无内容返回空
    assert render_presence(PresenceRibbon(), "", target_key="gX", is_private=False, now=now) == ""


def test_settle_echoes_marks_unanswered_after_15min() -> None:
    now = 100_000.0
    ribbon = PresenceRibbon()
    ribbon.echoes = [
        {
            "target_key": "g1",
            "is_private": False,
            "summary": "s",
            "emitted_at": now - 901,
            "answered": False,
            "settled": False,
        },
        {
            "target_key": "g2",
            "is_private": False,
            "summary": "s2",
            "emitted_at": now - 100,
            "answered": False,
            "settled": False,
        },
    ]
    newly = settle_echoes(ribbon, now, lambda _k, _t: False)
    assert len(newly) == 1 and newly[0]["target_key"] == "g1"
    assert ribbon.echoes[0]["settled"] and not ribbon.echoes[0]["answered"]
    assert not ribbon.echoes[1]["settled"]


def test_refresh_persona_state_cold_shoulder(tmp_path) -> None:
    """未结算且超过 900s 的 echo → 冷肩冲击 + ignore 情绪记录。"""
    now = 100_000.0
    old_ribbon = PresenceRibbon()
    old_ribbon.echoes = [
        {
            "target_key": "g9",
            "is_private": False,
            "summary": "s",
            "emitted_at": now - 2000,
            "answered": False,
            "settled": False,
        }
    ]
    from dataclasses import asdict

    stored = {"presence": asdict(old_ribbon)}

    async def _get(scope: str, key: str):
        return stored.get(key)

    async def _set(scope: str, key: str, value) -> None:
        stored[key] = value

    mood_calls = []

    async def _mood(persona: str, key: str, ev: str, intensity: float, reason: str) -> None:
        mood_calls.append((persona, key, ev))

    from gsuid_core.ai_core.persona.config import DEFAULT_PERSONA_CONFIG
    from gsuid_core.utils.plugins_config.gs_config import StringConfig

    real_cfg = StringConfig("persona_p1", tmp_path / "p1.json", dict(DEFAULT_PERSONA_CONFIG))
    with (
        patch("gsuid_core.ai_core.state_store.state_get_value", new=AsyncMock(side_effect=_get)),
        patch("gsuid_core.ai_core.state_store.state_set_value", new=AsyncMock(side_effect=_set)),
        patch("gsuid_core.ai_core.persona.mood.update_mood", new=AsyncMock(side_effect=_mood)),
        patch(
            "gsuid_core.ai_core.persona.config.persona_config_manager.get_config",
            return_value=real_cfg,
        ),
        patch("gsuid_core.ai_core.persona.presence.history_has_human_after", return_value=False),
    ):
        state, _ribbon = asyncio.run(refresh_persona_state("p1", now))
    assert mood_calls == [("p1", "g9", "ignore")]
    # 冷肩让饱食度上升
    assert state.social_satiety > 0.40
