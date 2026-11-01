"""存在感丝带：主动发信回响 + 闲时动作。

主动发信写一条 echo；15 分钟后按真实历史结算有没有被接茬，
没人接则喂一记冷肩冲击 + 冷淡情绪。全部按 persona 存 state_store。
"""

from __future__ import annotations

from typing import Callable, TypedDict
from dataclasses import field, asdict, dataclass

from gsuid_core.ai_core.persona.homeostasis import (
    PersonaHomeostasis,
    persona_scope,
    baseline_energy,
    load_homeostasis,
    save_homeostasis,
    advance_homeostasis,
    apply_cold_shoulder,
)

_ECHO_AGE_SECONDS = 900.0
_OFFSCREEN_WINDOW = 21600.0
_ECHO_CAP = 5
_SUMMARY_MAX = 36
_OFFSCREEN_MAX = 45
_PRESENCE_KEY = "presence"


class SocialEchoDict(TypedDict):
    target_key: str
    is_private: bool
    summary: str
    emitted_at: float
    answered: bool
    settled: bool


@dataclass(slots=True)
class PresenceRibbon:
    offscreen_doing: str = ""
    offscreen_at: float = 0.0
    echoes: list[SocialEchoDict] = field(default_factory=list)


async def load_presence(persona_name: str) -> PresenceRibbon:
    from gsuid_core.ai_core.state_store import state_get_value

    raw = await state_get_value(persona_scope(persona_name), _PRESENCE_KEY)
    ribbon = PresenceRibbon()
    if not isinstance(raw, dict):
        return ribbon
    if isinstance(raw.get("offscreen_doing"), str):
        ribbon.offscreen_doing = raw["offscreen_doing"]
    if isinstance(raw.get("offscreen_at"), (int, float)):
        ribbon.offscreen_at = float(raw["offscreen_at"])
    echoes = raw.get("echoes")
    if isinstance(echoes, list):
        for item in echoes:
            if isinstance(item, dict) and isinstance(item.get("target_key"), str):
                ribbon.echoes.append(
                    {
                        "target_key": item["target_key"],
                        "is_private": bool(item.get("is_private")),
                        "summary": str(item.get("summary") or ""),
                        "emitted_at": float(item.get("emitted_at") or 0.0),
                        "answered": bool(item.get("answered")),
                        "settled": bool(item.get("settled")),
                    }
                )
    return ribbon


async def save_presence(persona_name: str, ribbon: PresenceRibbon) -> None:
    from gsuid_core.ai_core.state_store import state_set_value

    await state_set_value(persona_scope(persona_name), _PRESENCE_KEY, asdict(ribbon))


async def record_echo(persona_name: str, target_key: str, is_private: bool, summary: str, now: float) -> None:
    ribbon = await load_presence(persona_name)
    ribbon.echoes.append(
        {
            "target_key": target_key,
            "is_private": is_private,
            "summary": summary[:_SUMMARY_MAX],
            "emitted_at": now,
            "answered": False,
            "settled": False,
        }
    )
    ribbon.echoes = ribbon.echoes[-_ECHO_CAP:]
    await save_presence(persona_name, ribbon)


async def record_offscreen(persona_name: str, text: str, now: float) -> None:
    body = text.strip()[:_OFFSCREEN_MAX]
    if not body:
        return
    ribbon = await load_presence(persona_name)
    ribbon.offscreen_doing = body
    ribbon.offscreen_at = now
    await save_presence(persona_name, ribbon)


def settle_echoes(
    ribbon: PresenceRibbon,
    now: float,
    has_human_after: Callable[[str, float], bool],
) -> list[SocialEchoDict]:
    """把超过 900s 未结算的 echo 按真实历史结算。返回本轮新判定「无人接茬」的条目。"""
    unanswered: list[SocialEchoDict] = []
    for echo in ribbon.echoes:
        if echo["settled"] or now - echo["emitted_at"] < _ECHO_AGE_SECONDS:
            continue
        echo["answered"] = has_human_after(echo["target_key"], echo["emitted_at"])
        echo["settled"] = True
        if not echo["answered"]:
            unanswered.append(echo)
    return unanswered


def history_has_human_after(target_key: str, ts: float) -> bool:
    """群聊按 group_id 命中会话；``private:{uid}`` 按私聊 user_id。"""
    from gsuid_core.message_history import get_history_manager

    is_private = target_key.startswith("private:")
    uid = target_key[len("private:") :] if is_private else ""
    manager = get_history_manager()
    for event, records in manager._histories.items():
        if is_private:
            if event.user_type != "direct" or str(event.user_id) != uid:
                continue
        elif not event.group_id or str(event.group_id) != target_key:
            continue
        for record in records:
            if record.role == "user" and record.timestamp > ts:
                return True
    return False


async def refresh_persona_state(persona_name: str, now: float) -> tuple[PersonaHomeostasis, PresenceRibbon]:
    """读两边状态 → 时间推进 → 结算 echo（无人接的喂冷肩 + 冷淡情绪）→ 存回。"""
    from gsuid_core.ai_core.persona.mood import update_mood
    from gsuid_core.ai_core.persona.config import read_drive_traits, read_autonomy_runtime, persona_config_manager

    config = persona_config_manager.get_config(persona_name)
    traits = read_drive_traits(config)
    runtime = read_autonomy_runtime(config)
    state = await load_homeostasis(persona_name)
    advance_homeostasis(
        state, traits, now, baseline_energy(now, list(runtime.active_hours), runtime.timezone, traits.energy_endurance)
    )
    ribbon = await load_presence(persona_name)
    for echo in settle_echoes(ribbon, now, history_has_human_after):
        apply_cold_shoulder(state)
        mood_key = echo["target_key"][len("private:") :] if echo["is_private"] else echo["target_key"]
        await update_mood(persona_name, mood_key, "ignore", 0.15, "主动发言没人接茬")
    await save_homeostasis(persona_name, state)
    await save_presence(persona_name, ribbon)
    return state, ribbon


def render_presence(
    ribbon: PresenceRibbon,
    hint: str,
    *,
    target_key: str,
    is_private: bool,
    now: float,
) -> str:
    """≤120 字的存在感注入块。看不到别会话的 echo；没有内容返回空串。"""
    from gsuid_core.ai_core.control.lines import presence_echo_line, presence_offscreen_line
    from gsuid_core.ai_core.autonomy.select import _age_label

    parts: list[str] = []
    if hint:
        parts.append(hint)
    if ribbon.offscreen_doing and now - ribbon.offscreen_at < _OFFSCREEN_WINDOW:
        parts.append(presence_offscreen_line(_age_label(ribbon.offscreen_at, now), ribbon.offscreen_doing))
    for echo in reversed(ribbon.echoes):
        if echo["target_key"] != target_key or echo["is_private"] != is_private:
            continue
        answered = echo["answered"] if echo["settled"] else None
        parts.append(presence_echo_line(_age_label(echo["emitted_at"], now), echo["summary"], answered))
        break
    return "\n".join(parts)[:120]
