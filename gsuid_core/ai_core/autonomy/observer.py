"""阶段一：不调用模型，把好感、绑定和群摘要收成一份底片。"""

import time

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.config import core_config
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.persona.mood import get_all_mood_states
from gsuid_core.ai_core.autonomy.binds import collect_bound_uids
from gsuid_core.ai_core.autonomy.codec import load_steps, load_str_list
from gsuid_core.ai_core.persona.config import (
    scope_covers_session,
    read_autonomy_runtime,
    persona_config_manager,
    persona_should_inspect_session,
)
from gsuid_core.ai_core.autonomy.models import SpokenLine, GroupObservation, ObservationDigest
from gsuid_core.ai_core.autonomy.select import (
    GROUP_CAP,
    CARE_COOLDOWN_SECONDS,
    GROUP_LOOKBACK_SECONDS,
    care_floor,
    eligible_user_ids,
    minimum_score_for,
    assemble_care_targets,
)
from gsuid_core.ai_core.database.models import UserFavorability, AIAutonomyActivity
from gsuid_core.message_history.manager import get_history_manager
from gsuid_core.ai_core.interaction_scaffold import extract_message_body


async def build_observation(persona_name: str, *, now: float | None = None) -> ObservationDigest:
    clock = time.time() if now is None else now
    config = persona_config_manager.get_config(persona_name)
    runtime = read_autonomy_runtime(config)
    scope = _text(config.get_config("scope").data)
    targets = _str_list(config.get_config("target_groups").data)
    if scope == "disabled":
        return _empty(persona_name)
    allowed = _allowed_groups(scope, targets)
    allow_private = scope_covers_session(scope, is_private=True) or scope == "specific"
    masters = _master_ids()
    floor = care_floor(runtime.care_min_zone)
    favor_rows = await UserFavorability.list_at_least(minimum_score_for(floor), limit=50)
    scores: dict[str, int] = {}
    names: dict[str, str] = {}
    favor_bot: dict[str, str] = {}
    pairs: list[tuple[str, int]] = []
    for row in favor_rows:
        uid = row.user_id.strip()
        if not uid:
            continue
        pairs.append((uid, row.favorability))
        previous = scores[uid] if uid in scores else None
        if previous is None or row.favorability > previous:
            scores[uid] = row.favorability
            favor_bot[uid] = row.bot_id
            if isinstance(row.user_name, str) and row.user_name.strip():
                names[uid] = row.user_name.strip()
    served = set(await AIAutonomyActivity.user_ids_cared_since(persona_name, clock - CARE_COOLDOWN_SECONDS))
    user_ids = eligible_user_ids(pairs, masters, floor, served)
    for uid in user_ids:
        if uid in scores:
            continue
        extra = await UserFavorability.get_any_favorability(uid)
        if extra is None:
            scores[uid] = 0
            continue
        scores[uid] = extra.favorability
        favor_bot[uid] = extra.bot_id
        if isinstance(extra.user_name, str) and extra.user_name.strip():
            names[uid] = extra.user_name.strip()
    lines = _spoken_lines(scope, targets, clock)
    binds: dict[str, dict[str, str]] = {}
    for uid in user_ids:
        bot_id = favor_bot[uid] if uid in favor_bot and favor_bot[uid] else _bot_for(lines, uid)
        binds[uid] = await collect_bound_uids(uid, bot_id)
    care = assemble_care_targets(
        user_ids,
        scores,
        set(masters),
        lines,
        binds,
        names,
        now=clock,
        allowed_groups=allowed,
        allow_private=allow_private,
    )
    groups = await _groups(lines, scope, targets)
    trails, recent_tools = await _previous(persona_name, clock)
    bot_id, bot_self_id, ws_bot_id = _identity(lines, groups)
    return ObservationDigest(
        persona_name=persona_name,
        bot_id=bot_id,
        bot_self_id=bot_self_id,
        ws_bot_id=ws_bot_id,
        mood_score=_mood(persona_name),
        care_targets=care,
        group_digests=groups,
        open_trails=trails,
        available_plugins_summary=_plugin_names(),
        recently_used_tools=recent_tools,
    )


def _spoken_lines(scope: str, target_groups: list[str], now: float) -> list[SpokenLine]:
    history = get_history_manager()
    found: list[SpokenLine] = []
    for event in history.list_sessions():
        group_id = event.group_id if event.user_type != "direct" else None
        if group_id:
            if not persona_should_inspect_session(scope, group_id=group_id, target_groups=target_groups):
                continue
        elif not scope_covers_session(scope, is_private=True) and scope != "specific":
            continue
        elif scope == "specific" and event.user_id not in target_groups:
            continue
        for record in history.get_history(event):
            if record.role != "user":
                continue
            if now - record.timestamp > GROUP_LOOKBACK_SECONDS:
                continue
            text = extract_message_body(record.content).strip()
            if not text:
                continue
            user_id = record.user_id.strip() or event.user_id.strip()
            if not user_id:
                continue
            found.append(
                SpokenLine(
                    user_id=user_id,
                    user_name=record.user_name.strip() if isinstance(record.user_name, str) else user_id,
                    text=text,
                    ts=record.timestamp,
                    group_id=group_id,
                    bot_id=event.bot_id,
                    bot_self_id=event.bot_self_id,
                    ws_bot_id=event.WS_BOT_ID or "",
                )
            )
    return found


async def _groups(
    lines: list[SpokenLine],
    scope: str,
    target_groups: list[str],
) -> list[GroupObservation]:
    latest: dict[str, SpokenLine] = {}
    for line in lines:
        if not line.group_id:
            continue
        if not persona_should_inspect_session(scope, group_id=line.group_id, target_groups=target_groups):
            continue
        current = latest[line.group_id] if line.group_id in latest else None
        if current is None or line.ts > current.ts:
            latest[line.group_id] = line
    ordered = sorted(latest.values(), key=lambda line: line.ts, reverse=True)[:GROUP_CAP]
    found: list[GroupObservation] = []
    for line in ordered:
        if line.group_id is None:
            continue
        recent = [
            f"{item.user_name}: {item.text[:80]}"
            for item in sorted(lines, key=lambda item: item.ts, reverse=True)
            if item.group_id == line.group_id
        ][:10]
        found.append(
            GroupObservation(
                group_id=line.group_id,
                summary=await _group_summary(line.group_id),
                recent_lines=recent,
                last_human_ts=line.ts,
                bot_id=line.bot_id,
                bot_self_id=line.bot_self_id,
                ws_bot_id=line.ws_bot_id,
                session_key=f"group:{line.group_id}",
            )
        )
    return found


async def _group_summary(group_id: str) -> str:
    from gsuid_core.ai_core.memory.ingestion.hiergraph import AIMemHierarchicalGraphMeta

    scope_key = make_scope_key(ScopeType.GROUP, group_id)
    try:
        meta = await AIMemHierarchicalGraphMeta.get_or_none(scope_key=scope_key)
    except SQLAlchemyError:
        return ""
    if meta is None or not isinstance(meta.group_summary_cache, str):
        return ""
    return meta.group_summary_cache.strip()[:400]


async def _previous(persona_name: str, now: float) -> tuple[list[str], list[str]]:
    rows = await AIAutonomyActivity.list_recent_by_persona(persona_name, limit=1)
    if not rows:
        return [], []
    row = rows[0]
    if now - row.created_at > GROUP_LOOKBACK_SECONDS:
        return [], []
    tools: list[str] = []
    for step in load_steps(row.steps_json):
        name = step["tool_name"]
        if name and name not in tools:
            tools.append(name)
    return load_str_list(row.open_trails_json), tools


def _mood(persona_name: str) -> float:
    prefix = f"{persona_name}@@"
    score = 0.0
    for key, state in get_all_mood_states().items():
        if key.startswith(prefix):
            score = max(score, state.effective_intensity)
    return score


def _plugin_names() -> list[str]:
    from gsuid_core.sv import SL

    names = [name for name in SL.plugins if isinstance(name, str) and name]
    names.sort()
    return names[:30]


def _master_ids() -> list[str]:
    raw = core_config.get_config("masters")
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            found.append(item.strip())
        elif isinstance(item, int) and not isinstance(item, bool):
            found.append(str(item))
    return found


def _allowed_groups(scope: str, target_groups: list[str]) -> set[str] | None:
    if scope == "specific":
        return set(target_groups)
    if scope in {"global", "global_group"}:
        return None
    return set()


def _bot_for(lines: list[SpokenLine], user_id: str) -> str:
    mine = [line for line in lines if line.user_id == user_id and line.bot_id]
    if not mine:
        return ""
    return max(mine, key=lambda line: line.ts).bot_id


def _identity(lines: list[SpokenLine], groups: list[GroupObservation]) -> tuple[str, str, str]:
    if lines:
        latest = max(lines, key=lambda line: line.ts)
        return latest.bot_id, latest.bot_self_id, latest.ws_bot_id
    if groups:
        group = groups[0]
        return group.bot_id, group.bot_self_id, group.ws_bot_id
    return "", "", ""


def _empty(persona_name: str) -> ObservationDigest:
    return ObservationDigest(
        persona_name=persona_name,
        bot_id="",
        bot_self_id="",
        ws_bot_id="",
        mood_score=0.0,
        care_targets=[],
        group_digests=[],
        open_trails=[],
        available_plugins_summary=[],
        recently_used_tools=[],
    )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
