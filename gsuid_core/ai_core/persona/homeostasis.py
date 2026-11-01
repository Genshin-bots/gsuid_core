"""人格动态平衡（homeostasis）：纯数学，无 LLM。

能量 / 社交饱食度随时间向基线回归、随发言与冷场受冲击；话题疲劳按 12h 半衰期衰减。
状态存 ``state_store`` 的 ``persona:{persona_name}`` scope ``homeostasis`` 键——
按人格而非 bot 分桶，因为驱动力是人格属性。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import field, asdict, dataclass

from gsuid_core.ai_core.persona.config import parse_active_window

if TYPE_CHECKING:
    from gsuid_core.ai_core.persona.config import DriveTraits

_PERSONA_SCOPE_PREFIX = "persona:"
_HOMEOSTASIS_KEY = "homeostasis"
# 话题疲劳半衰期（秒）：12 小时
_TOPIC_HALF_LIFE = 43200.0
_TOPIC_MAX_ENTRIES = 20


class TopicFatigueEntry(TypedDict):
    fatigue: float
    last_touch: float


@dataclass(slots=True)
class PersonaHomeostasis:
    energy: float = 0.75
    social_satiety: float = 0.40
    consecutive_turns: int = 0
    last_turn_at: float = 0.0
    last_updated_at: float = 0.0
    topic_fatigue: dict[str, TopicFatigueEntry] = field(default_factory=dict)


def persona_scope(persona_name: str) -> str:
    return f"{_PERSONA_SCOPE_PREFIX}{persona_name}"


def baseline_energy(now_ts: float, active_hours: list[str], tz_name: str, endurance: int) -> float:
    """作息窗内的能量基线。窗头 45 分钟 0.45→0.85，窗尾 60 分钟 0.80→0.35，窗外 0.35。

    无有效作息窗或时区非法时按精力充沛处理返回 0.80；跨午夜窗与
    ``is_within_active_hours`` 同规则（晚于开始或早于结束即在窗内）。
    """
    zone_name = (tz_name or "").strip()
    windows: list[tuple[int, int]] = []
    for raw in active_hours:
        parsed = parse_active_window(raw) if isinstance(raw, str) else None
        if parsed is not None:
            windows.append(parsed)
    if not windows or not zone_name:
        return 0.80
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        return 0.80
    clock = datetime.fromtimestamp(now_ts, tz=zone)
    minute = clock.hour * 60 + clock.minute
    value = 0.80
    in_window = False
    for start, end in windows:
        if start <= end:
            if not (start <= minute <= end):
                continue
            in_window = True
            ds, de = minute - start, end - minute
        elif minute >= start or minute <= end:
            in_window = True
            ds = minute - start if minute >= start else 1440 - start + minute
            de = 1440 - minute + end if minute >= start else end - minute
        else:
            continue
        if ds <= 45:
            value = 0.45 + 0.40 * ds / 45
        elif de <= 60:
            value = 0.35 + 0.45 * de / 60
        break
    if not in_window:
        return 0.35
    # 高耐力在作息窗内不累到 0.75 以下；窗外不设下限
    if endurance >= 80:
        value = max(value, 0.75)
    return value


def advance_homeostasis(
    state: PersonaHomeostasis,
    traits: "DriveTraits",
    now: float,
    baseline: float,
) -> PersonaHomeostasis:
    """按时间衰减推进能量与饱食度。首次调用只打时间戳，不产生跳变。"""
    if state.last_updated_at <= 0:
        state.last_updated_at = now
        return state
    dt = max(0.0, now - state.last_updated_at)
    tau_e = 2700 - 15 * traits.energy_endurance
    state.energy = baseline + (state.energy - baseline) * 0.5 ** (dt / tau_e)
    tau_s = 3600 - 24 * traits.social_initiative
    s_eq = 0.65 - 0.004 * traits.social_initiative
    state.social_satiety = s_eq + (state.social_satiety - s_eq) * 0.5 ** (dt / tau_s)
    state.energy = min(1.0, max(0.0, state.energy))
    state.social_satiety = min(1.0, max(0.0, state.social_satiety))
    decay = 0.5 ** (dt / _TOPIC_HALF_LIFE)
    for key in list(state.topic_fatigue):
        entry = state.topic_fatigue[key]
        entry["fatigue"] *= decay
        if entry["fatigue"] < 0.05:
            del state.topic_fatigue[key]
    state.last_updated_at = now
    return state


def apply_turn_shock(state: PersonaHomeostasis, traits: "DriveTraits", now: float) -> None:
    """一次实际可见发言的冲击：耗能、涨饱食、累计连续轮次（20 分钟内算连续）。"""
    state.energy = max(0.10, state.energy - (0.09 - 0.0006 * traits.energy_endurance))
    state.social_satiety = min(1.0, state.social_satiety + 0.18)
    state.consecutive_turns = state.consecutive_turns + 1 if now - state.last_turn_at < 1200 else 1
    state.last_turn_at = now


def apply_cold_shoulder(state: PersonaHomeostasis) -> None:
    """主动发言没人接茬：饱食度再抬一档，减少继续自说自话。"""
    state.social_satiety = min(1.0, state.social_satiety + 0.25)


def touch_topic(state: PersonaHomeostasis, topic_key: str, now: float) -> None:
    entries = state.topic_fatigue
    if topic_key in entries:
        entries[topic_key]["fatigue"] = min(1.0, entries[topic_key]["fatigue"] + 0.45)
        entries[topic_key]["last_touch"] = now
    else:
        entries[topic_key] = {"fatigue": 0.45, "last_touch": now}
    if len(entries) > _TOPIC_MAX_ENTRIES:
        weakest = min(entries, key=lambda k: entries[k]["fatigue"])
        del entries[weakest]


def cooled_topics(state: PersonaHomeostasis) -> list[str]:
    return [key for key, entry in state.topic_fatigue.items() if entry["fatigue"] >= 0.70]


def heartbeat_should_skip(state: PersonaHomeostasis) -> bool:
    return state.social_satiety >= 0.80 or state.energy <= 0.22


def homeostasis_hint(state: PersonaHomeostasis) -> str:
    """当轮的内心状态提示。绝不渲染数字。"""
    from gsuid_core.ai_core.control.lines import HOMEO_EAGER_LINE, HOMEO_TIRED_LINE

    if state.energy < 0.30:
        return HOMEO_TIRED_LINE
    if state.social_satiety < 0.20:
        return HOMEO_EAGER_LINE
    return ""


async def load_homeostasis(persona_name: str) -> PersonaHomeostasis:
    from gsuid_core.ai_core.state_store import state_get_value

    raw = await state_get_value(persona_scope(persona_name), _HOMEOSTASIS_KEY)
    state = PersonaHomeostasis()
    if not isinstance(raw, dict):
        return state
    if "energy" in raw and isinstance(raw["energy"], (int, float)):
        state.energy = float(raw["energy"])
    if "social_satiety" in raw and isinstance(raw["social_satiety"], (int, float)):
        state.social_satiety = float(raw["social_satiety"])
    if "consecutive_turns" in raw and isinstance(raw["consecutive_turns"], int):
        state.consecutive_turns = raw["consecutive_turns"]
    for attr in ("last_turn_at", "last_updated_at"):
        if attr in raw and isinstance(raw[attr], (int, float)):
            setattr(state, attr, float(raw[attr]))
    topics = raw["topic_fatigue"] if "topic_fatigue" in raw else None
    if isinstance(topics, dict):
        for key, entry in topics.items():
            if isinstance(entry, dict) and "fatigue" in entry and isinstance(entry["fatigue"], (int, float)):
                state.topic_fatigue[str(key)] = {
                    "fatigue": float(entry["fatigue"]),
                    "last_touch": (
                        float(entry["last_touch"])
                        if "last_touch" in entry and isinstance(entry["last_touch"], (int, float))
                        else 0.0
                    ),
                }
    return state


async def save_homeostasis(persona_name: str, state: PersonaHomeostasis) -> None:
    from gsuid_core.ai_core.state_store import state_set_value

    await state_set_value(persona_scope(persona_name), _HOMEOSTASIS_KEY, asdict(state))
