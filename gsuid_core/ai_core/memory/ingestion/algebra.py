"""Pass 2：预取槽位 + 单次五态记忆代数抽取与代码强护栏落库。

五态：``ADD``（多值并存）/ ``MERGE``（同义强化）/ ``SUPERSEDE``（时序覆盖）/
``CONTRADICT``（无解释硬矛盾，双边保留）/ ``RETRACT``（无替代新值的撤回/取消）。
"""

from __future__ import annotations

import re
import uuid
import asyncio
from typing import TYPE_CHECKING, Literal, Sequence, TypedDict
from datetime import datetime, timezone, timedelta

from sqlmodel import col, or_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.utils import extract_json_from_text
from gsuid_core.ai_core.memory.identity import (
    AliasTrack,
    is_human_speaker_id,
    record_entity_alias,
)
from gsuid_core.utils.database.write_gate import sqlite_write_gate
from gsuid_core.ai_core.configs.task_kinds import TASK_MEMORY_ALGEBRA
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemThread,
    are_entity_kinds_compatible,
)
from gsuid_core.ai_core.memory.database.preference import AIMemPreference

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.observer import ObservationRecord

_DANGLING_FACT_RE = re.compile(
    r"(?:(?:提到|提及|被提及|表示|认为|觉得|提出|指出|强调|透露|说道|谈到|聊到|说起|问到|询问|说)[了过]?的?"
    r"|(?:讨论|谈论|回复|回应|回答|评价|分享|补充|吐槽|感叹)(?:[了过]的?|的))"
    r"\s*[。.!！?？…]*$"
)
_TRAILING_INCOMPLETE_RE = re.compile(r"(?:是不是|有没有|能不能|是否)\s*[。.!！?？…]*$")


def _is_incomplete_fact(fact: str) -> bool:
    clean = fact.strip()
    if len(clean) < 4:
        return True
    return bool(_DANGLING_FACT_RE.search(clean) or _TRAILING_INCOMPLETE_RE.search(clean))


def _is_tautology_fact(fact: str, source: str, target: str) -> bool:
    clean = fact.strip()
    rest = clean.replace(source.strip(), "").replace(target.strip(), "")
    rest = rest.strip(" \t\r\n，,。.!！?？:：;；是的有和与")
    return len(rest) < 2


def is_context_omitted_group_hallucination(fact: str, raw_line: str, scope_key: str) -> bool:
    """结构级防脑补：原话未提及当前群号时，禁止事实边私自嵌入当前数字群号。"""
    if not scope_key.startswith("group:"):
        return False
    gid = scope_key[len("group:") :].strip()
    if len(gid) < 5:
        return False
    return gid in fact and gid not in raw_line


def infer_short_term_ttl_days(explicit_ttl: int | None) -> int | None:
    """仅采信模型显式给出的短期 TTL（1-14 天）；持久事实返回 None。"""
    if explicit_ttl is not None and 1 <= explicit_ttl <= 14:
        return explicit_ttl
    return None


def _cluster_addresses_bot(records: list["ObservationRecord"], lines: list[int]) -> bool:
    from gsuid_core.ai_core.memory.observer import _is_known_bot_speaker
    from gsuid_core.ai_core.memory.group_profile import collect_persona_surfaces

    surfaces_by_persona: dict[str, tuple[str, ...]] = {}
    for idx in lines:
        if not (0 <= idx < len(records)):
            continue
        rec = records[idx]
        if rec.reply_to_bot:
            return True
        pname = rec.persona_name
        if pname:
            if pname not in surfaces_by_persona:
                surfaces_by_persona[pname] = tuple(s for s in collect_persona_surfaces(pname) if s)
            if pname in rec.raw_content:
                return True
            surfaces = surfaces_by_persona[pname]
            if surfaces and any(s in rec.raw_content for s in surfaces):
                return True
        if rec.at_targets and any(_is_known_bot_speaker(aid, "") for aid in rec.at_targets):
            return True
    return False


_NEGATION_MARKERS = ("不", "没", "无", "非", "别", "讨厌", "拒绝", "反对", "停止")
_NEGATION_RE_EN = re.compile(
    r"\b(never|not|no|none|without|refuse[sd]?|den(?:y|ies|ied)|stopped)\b|n't\b",
    re.IGNORECASE,
)


def _fact_polarity(fact: str) -> bool:
    """粗判 fact 的否定极性：含奇数个否定标记 → True（否定句）。"""
    hits = sum(fact.count(m) for m in _NEGATION_MARKERS)
    hits += len(_NEGATION_RE_EN.findall(fact))
    return hits % 2 == 1


EdgeKind = Literal["attribute", "relation", "milestone"]
EdgeOp = Literal["ADD", "MERGE", "SUPERSEDE", "CONTRADICT", "RETRACT"]
ResolutionState = Literal["active", "staging", "superseded", "contradicted", "retracted"]
EntityKind = Literal["speaker", "person", "project", "item", "location", "org"]


class ExtractedEntitySpec(TypedDict):
    name: str
    kind: EntityKind
    user_id: str | None
    summary: str


class ExtractedEdgeOpSpec(TypedDict):
    op: EdgeOp
    target_edge_id: str | None
    source: str
    target: str
    edge_kind: EdgeKind
    slot_key: str
    fact: str
    subject_user_id: str | None
    speaker_user_id: str
    line_idx: int
    event_date: str | None
    ttl_days: int | None


class ExtractedAliasSpec(TypedDict):
    user_id: str
    alias: str
    evidence: AliasTrack


class ExtractedPreferenceSpec(TypedDict):
    user_id: str
    target_context: str
    preference_rule: str
    polarity: Literal["do", "dont"]
    is_correction: bool


class AlgebraExtractOutput(TypedDict):
    entities: list[ExtractedEntitySpec]
    edges: list[ExtractedEdgeOpSpec]
    preferences: list[ExtractedPreferenceSpec]
    aliases: list[ExtractedAliasSpec]
    thread_summary: str


class AlgebraStats(TypedDict):
    entities: int
    edges: int
    preferences: int
    thread_id: str


def _short_edge(edge_id: str) -> str:
    return f"E{edge_id[:8]}"


def _parse_date(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip()[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_entity_kind(raw: object) -> EntityKind:
    if raw == "speaker":
        return "speaker"
    if raw == "project":
        return "project"
    if raw == "item":
        return "item"
    if raw == "location":
        return "location"
    if raw == "org":
        return "org"
    return "person"


def _as_alias_track(raw: object) -> AliasTrack:
    if raw == "self_claim":
        return "self_claim"
    if raw == "at_mention":
        return "at_mention"
    return "corroboration"


def _parse_output(raw: str) -> AlgebraExtractOutput | None:
    data = extract_json_from_text(raw)
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict)), None)
    if not isinstance(data, dict):
        return None

    entities: list[ExtractedEntitySpec] = []
    raw_entities = data["entities"] if "entities" in data else None
    if isinstance(raw_entities, list):
        for item in raw_entities:
            if not isinstance(item, dict):
                continue
            raw_name = item["name"] if "name" in item else item["n"] if "n" in item else ""
            name = raw_name if isinstance(raw_name, str) else ""
            if not name.strip():
                continue
            raw_uid = item["user_id"] if "user_id" in item else item["u"] if "u" in item else None
            uid = raw_uid.strip() if isinstance(raw_uid, str) and raw_uid.strip() else None
            raw_sum = item["summary"] if "summary" in item else item["s"] if "s" in item else ""
            summary = raw_sum if isinstance(raw_sum, str) else ""
            raw_kind = item["kind"] if "kind" in item else item["k"] if "k" in item else None
            entities.append(
                {
                    "name": name.strip(),
                    "kind": _as_entity_kind(raw_kind),
                    "user_id": uid,
                    "summary": summary[:200],
                }
            )

    edges: list[ExtractedEdgeOpSpec] = []
    raw_edges = data["edges"] if "edges" in data else None
    if isinstance(raw_edges, list):
        for item in raw_edges:
            if not isinstance(item, dict):
                continue
            raw_op = item["op"] if "op" in item else "ADD"
            op: EdgeOp = raw_op if raw_op in ("ADD", "MERGE", "SUPERSEDE", "CONTRADICT", "RETRACT") else "ADD"
            raw_target_edge = item["target"] if "target" in item and isinstance(item["target"], str) else ""
            target_edge_id = raw_target_edge.strip() if raw_target_edge.strip().startswith("E") else None
            fact = item["fact"] if "fact" in item and isinstance(item["fact"], str) else ""
            source = item["source"] if "source" in item and isinstance(item["source"], str) else ""
            slot = item["slot"] if "slot" in item and isinstance(item["slot"], str) else ""
            if op == "RETRACT":
                if not target_edge_id and not (source.strip() and slot.strip()):
                    continue
            else:
                if not fact.strip() or not source.strip() or _is_incomplete_fact(fact):
                    continue
            edge_kind = item["kind"] if "kind" in item else "attribute"
            if edge_kind not in ("attribute", "relation", "milestone"):
                edge_kind = "attribute"
            raw_tname = item["target_name"] if "target_name" in item else (None if target_edge_id else raw_target_edge)
            target = raw_tname.strip() if isinstance(raw_tname, str) else ""
            if not target:
                if edge_kind in ("attribute", "milestone") or op == "RETRACT":
                    target = source.strip()
                else:
                    continue
            line = item["line"] if "line" in item and isinstance(item["line"], int) else 0
            raw_ttl = item["ttl_days"] if "ttl_days" in item else None
            ttl_days = raw_ttl if isinstance(raw_ttl, int) and not isinstance(raw_ttl, bool) and raw_ttl > 0 else None
            edges.append(
                {
                    "op": op,
                    "target_edge_id": target_edge_id,
                    "source": source.strip(),
                    "target": target,
                    "edge_kind": edge_kind,
                    "slot_key": slot.strip()[:96],
                    "fact": fact.strip(),
                    "subject_user_id": (
                        item["subject"] if "subject" in item and isinstance(item["subject"], str) else None
                    ),
                    "speaker_user_id": (
                        item["speaker"] if "speaker" in item and isinstance(item["speaker"], str) else ""
                    ),
                    "line_idx": line,
                    "event_date": item["event_date"] if "event_date" in item else None,
                    "ttl_days": ttl_days,
                }
            )

    preferences: list[ExtractedPreferenceSpec] = []
    raw_prefs = data["preferences"] if "preferences" in data else None
    if isinstance(raw_prefs, list):
        for item in raw_prefs:
            if not isinstance(item, dict):
                continue
            uid = item["user_id"] if "user_id" in item and isinstance(item["user_id"], str) else ""
            rule = item["rule"] if "rule" in item and isinstance(item["rule"], str) else ""
            if not uid or not rule.strip():
                continue
            ctx_raw = item["context"] if "context" in item and isinstance(item["context"], str) else "general"
            target_ctx = ctx_raw.strip()[:128] or "general"
            if target_ctx in ("possession", "location"):
                continue
            polarity = "dont" if (item["polarity"] if "polarity" in item else "do") == "dont" else "do"
            preferences.append(
                {
                    "user_id": uid,
                    "target_context": target_ctx,
                    "preference_rule": rule.strip()[:200],
                    "polarity": polarity,
                    "is_correction": bool(item["correction"]) if "correction" in item else False,
                }
            )

    aliases: list[ExtractedAliasSpec] = []
    raw_aliases = data["aliases"] if "aliases" in data else None
    if isinstance(raw_aliases, list):
        for item in raw_aliases:
            if not isinstance(item, dict):
                continue
            uid = item["user_id"] if "user_id" in item and isinstance(item["user_id"], str) else ""
            alias = item["alias"] if "alias" in item and isinstance(item["alias"], str) else ""
            if not uid.strip() or not alias.strip():
                continue
            ev = _as_alias_track(item["evidence"] if "evidence" in item else "corroboration")
            aliases.append({"user_id": uid.strip(), "alias": alias.strip()[:32], "evidence": ev})

    raw_tsum = data["thread_summary"] if "thread_summary" in data and isinstance(data["thread_summary"], str) else ""
    thread_summary = raw_tsum.strip()[:120]

    if not entities and not edges and not preferences and not aliases and not thread_summary:
        return None
    return {
        "entities": entities,
        "edges": edges,
        "preferences": preferences,
        "aliases": aliases,
        "thread_summary": thread_summary,
    }


async def _prefetch(
    scope_key: str, records: list["ObservationRecord"]
) -> tuple[dict[str, str], list[AIMemEdge], dict[str, str]]:
    speaker_ids = list({r.speaker_id for r in records if r.speaker_id and is_human_speaker_id(r.speaker_id)})
    speaker_names = list({r.speaker_name for r in records if r.speaker_name and not r.is_assistant_context})
    lookup_names = list(set(speaker_ids + speaker_names))
    async with async_maker() as session:
        entity_rows = await session.execute(
            select(AIMemEntity).where(
                col(AIMemEntity.scope_key) == scope_key,
                or_(
                    col(AIMemEntity.user_id).in_(speaker_ids),
                    col(AIMemEntity.name).in_(lookup_names),
                )
                if lookup_names
                else False,
            )
        )
        entities = list(entity_rows.scalars().all())
        entity_ids = [e.id for e in entities]
        edges = await AIMemEdge.prefetch_active_edges(scope_key, entity_ids, limit=80)
        names = await AIMemEntity.get_names_by_ids(list({e.source_entity_id for e in edges}))
    return {e.name: e.id for e in entities}, edges, names


def _format_known_edges(edges: list[AIMemEdge], names: dict[str, str]) -> str:
    if not edges:
        return "（无）"
    lines: list[str] = []
    for edge in edges:
        name = names[edge.source_entity_id] if edge.source_entity_id in names else edge.source_entity_id[:8]
        stamp = edge.event_at or edge.valid_at
        date = stamp.strftime("%Y-%m-%d") if stamp is not None else ""
        st = f" [{edge.resolution_state}]" if edge.resolution_state != "active" else ""
        lines.append(
            f"[{_short_edge(edge.id)}] {name} | slot={edge.slot_key or edge.edge_kind}{st} | {edge.fact} ({date})"
        )
    return "\n".join(lines)


def _format_dialogue(records: list["ObservationRecord"], lines: list[int]) -> str:
    from gsuid_core.ai_core.memory.ingestion.window_router import format_window_line

    return "\n".join(format_window_line(i, records[i]) for i in lines if 0 <= i < len(records))


def _resolve_preference_user_id(
    raw_uid: str,
    records: list["ObservationRecord"],
    scope_key: str,
) -> str:
    """校验偏好 user_id 必须为本窗真实群友/私聊用户，拒收助手与群号。"""
    uid = raw_uid.strip()
    if uid.startswith("user_global:"):
        uid = uid[len("user_global:") :].strip()
    if not uid or uid.startswith(("__assistant_", "self:", "group:")):
        return ""
    if scope_key.startswith("group:") and uid == scope_key[len("group:") :]:
        return ""
    human_ids = {
        r.speaker_id
        for r in records
        if r.speaker_id
        and not r.is_assistant_context
        and is_human_speaker_id(r.speaker_id)
        and (not r.group_id or r.speaker_id != r.group_id)
    }
    if uid in human_ids:
        return uid
    name_to_human = {r.speaker_name: r.speaker_id for r in records if r.speaker_name and r.speaker_id in human_ids}
    if uid in name_to_human:
        return name_to_human[uid]
    return ""


async def _resolve_thread(
    session: "AsyncSession",
    scope_key: str,
    threads: list[AIMemThread],
    thread_ref: str | None,
    topic_title: str,
) -> str | None:
    """解耦日常抽边与建长线故事线：仅 thread_ref='T<k>' 续写或 thread_ref='new' 才建线。"""
    if thread_ref is not None and thread_ref.startswith("T"):
        try:
            index = int(thread_ref[1:]) - 1
        except ValueError:
            index = -1
        if 0 <= index < len(threads):
            return threads[index].id
    if thread_ref != "new" or not topic_title.strip():
        return None
    now = datetime.now(timezone.utc)
    thread = AIMemThread(
        id=str(uuid.uuid4()),
        scope_key=scope_key,
        title=topic_title.strip()[:120],
        aliases=[],
        summary="",
        status="active",
        first_at=now,
        last_at=now,
        n_sessions=1,
        milestone_count=0,
        episode_count=0,
        participant_uids_json="[]",
        qdrant_id="",
        last_mentioned_at=now,
        title_source="window",
    )
    session.add(thread)
    await session.flush()
    threads.append(thread)
    return thread.id


async def _resolve_target_edge(
    session: "AsyncSession",
    scope_key: str,
    old: AIMemEdge | None,
    source_id: str,
    target_id: str,
    slot_key: str,
    batch_by_slot: dict[tuple[str, str], AIMemEdge],
    batch_by_pair: dict[tuple[str, str], AIMemEdge],
    *,
    states: Sequence[str] = ("active", "staging"),
) -> AIMemEdge | None:
    """目标边缺失时按同窗/同槽顺序回查（解决同窗 ADD 后立刻 RETRACT/CONTRADICT）。"""
    if old is not None:
        return old
    if source_id and slot_key and (source_id, slot_key) in batch_by_slot:
        cand = batch_by_slot[(source_id, slot_key)]
        if cand.invalid_at is None and cand.resolution_state in states:
            return cand
    if source_id and target_id and (source_id, target_id) in batch_by_pair:
        cand = batch_by_pair[(source_id, target_id)]
        if cand.invalid_at is None and cand.resolution_state in states:
            return cand
    if source_id and slot_key:
        stmt = (
            select(AIMemEdge)
            .where(
                col(AIMemEdge.scope_key) == scope_key,
                col(AIMemEdge.source_entity_id) == source_id,
                col(AIMemEdge.slot_key) == slot_key,
                col(AIMemEdge.invalid_at).is_(None),
                col(AIMemEdge.resolution_state).in_(list(states)),
            )
            .order_by(col(AIMemEdge.valid_at).desc())
            .limit(1)
        )
        found = (await session.execute(stmt)).scalar_one_or_none()
        if found is not None:
            return found
    return None


async def apply_algebra_output(
    scope_key: str,
    records: list["ObservationRecord"],
    lines: list[int],
    output: AlgebraExtractOutput,
    *,
    known_edges: list[AIMemEdge],
    thread_ref: str | None,
    topic_title: str,
    episode_id: str,
    threads: list[AIMemThread] | None = None,
) -> AlgebraStats:
    """确定性执行五态记忆代数落库与全部生产防抖/隔离护栏（无 LLM 依赖，可供离线单测直测）。"""
    from gsuid_core.ai_core.memory.observer import _is_known_bot_speaker
    from gsuid_core.ai_core.memory.vector.ops import (
        EdgeLifecycleUpdate,
        upsert_edge_vectors_batch,
        upsert_entity_vectors_batch,
        update_edge_lifecycle_payloads,
    )

    thread_list = threads if threads is not None else []
    by_short = {_short_edge(edge.id): edge for edge in known_edges}
    entity_vector_payloads: list[dict[str, object]] = []
    edge_vector_payloads: list[dict[str, object]] = []
    lifecycle_updates: list[EdgeLifecycleUpdate] = []
    affected_threads: set[str] = set()
    written_edges = 0
    written_entities = 0
    latest_valid_at = datetime.now(timezone.utc)
    is_group_scope = scope_key.startswith("group:")
    cluster_recs = [records[i] for i in lines if 0 <= i < len(records)]
    cluster_persona = next((r.persona_name for r in cluster_recs if r.persona_name), "")
    recent_thread_facts: list[str] = []

    async with sqlite_write_gate, async_maker() as session:
        thread_id = await _resolve_thread(session, scope_key, thread_list, thread_ref, topic_title)
        window_speaker_ids = {
            r.speaker_id
            for r in cluster_recs
            if not r.is_assistant_context
            and is_human_speaker_id(r.speaker_id)
            and not _is_known_bot_speaker(r.speaker_id, "")
        }
        name_to_uid_candidates: dict[str, set[str]] = {}
        for r in cluster_recs:
            if r.speaker_id not in window_speaker_ids:
                continue
            if r.speaker_name:
                name_to_uid_candidates.setdefault(r.speaker_name, set()).add(r.speaker_id)
        name_to_uid: dict[str, str] = {
            n: next(iter(uids)) for n, uids in name_to_uid_candidates.items() if len(uids) == 1
        }

        entity_data: list[dict[str, object]] = []
        for spec in output["entities"]:
            uid = spec["user_id"]
            if uid is not None and (not is_human_speaker_id(uid) or _is_known_bot_speaker(uid, "")):
                uid = None
            if uid is None and spec["name"] in name_to_uid and are_entity_kinds_compatible("speaker", spec["kind"]):
                uid = name_to_uid[spec["name"]]
            # 强护栏：仅当实体为人物类且 uid 在本窗真实人类名册中，才允许标记 is_speaker=True
            person_kind = are_entity_kinds_compatible("speaker", spec["kind"])
            is_speaker = bool(person_kind and uid is not None and uid in window_speaker_ids)
            kind: EntityKind = spec["kind"]
            if kind == "speaker" and not is_speaker:
                kind = "person"
            elif is_speaker and kind == "person":
                kind = "speaker"
            entity_data.append(
                {
                    "name": spec["name"],
                    "summary": spec["summary"],
                    "tag": [kind],
                    "user_id": uid if person_kind else None,
                    "is_speaker": is_speaker,
                    "kind": kind,
                }
            )

        requested_names = {spec["source"] for spec in output["edges"] if spec["source"]} | {
            spec["target"] for spec in output["edges"] if spec["target"]
        }
        existing_names = {item["name"] for item in entity_data}
        for name in sorted(requested_names - existing_names):
            implicit_uid = name_to_uid[name] if name in name_to_uid else None
            implicit_is_speaker = implicit_uid is not None and implicit_uid in window_speaker_ids
            entity_data.append(
                {
                    "name": name,
                    "summary": "",
                    "tag": ["speaker"] if implicit_is_speaker else ["Topic"],
                    "user_id": implicit_uid,
                    "is_speaker": implicit_is_speaker,
                    "kind": "speaker" if implicit_is_speaker else "person",
                }
            )

        hybrid_ids = await AIMemEntity.prefetch_hybrid_name_ids(scope_key, entity_data)
        name_to_id, payloads, new_count = await AIMemEntity.extract_and_upsert(
            session,
            scope_key,
            entity_data,
            episode_id,
            list(window_speaker_ids),
            hybrid_name_ids=hybrid_ids,
        )
        entity_vector_payloads.extend(payloads)
        written_entities = new_count

        entity_by_id: dict[str, AIMemEntity] = {}
        if name_to_id:
            ent_rows = (
                (await session.execute(select(AIMemEntity).where(col(AIMemEntity.id).in_(list(name_to_id.values())))))
                .scalars()
                .all()
            )
            entity_by_id = {e.id: e for e in ent_rows}

        batch_edges_by_slot: dict[tuple[str, str], AIMemEdge] = {}
        batch_edges_by_pair: dict[tuple[str, str], AIMemEdge] = {}
        edge_payload_by_id: dict[str, dict[str, object]] = {}

        for spec in output["edges"]:
            line_idx = spec["line_idx"] if 0 <= spec["line_idx"] < len(records) else (lines[0] if lines else 0)
            line_rec = records[line_idx] if 0 <= line_idx < len(records) else None
            if line_rec is None:
                continue

            # 护栏 1：物理禁止以 Bot 自身回复（__assistant_* / is_assistant_context / 已知 Bot ID）作事实源
            if (
                line_rec.is_assistant_context
                or not is_human_speaker_id(line_rec.speaker_id)
                or _is_known_bot_speaker(line_rec.speaker_id, "")
                or (spec["speaker_user_id"] and not is_human_speaker_id(spec["speaker_user_id"]))
            ):
                continue

            speaker = line_rec.speaker_id
            valid_at = line_rec.timestamp
            old_ref = by_short[spec["target_edge_id"]] if spec["target_edge_id"] in by_short else None
            old = await session.get(AIMemEdge, old_ref.id) if old_ref is not None else None
            op: EdgeOp = spec["op"]
            source_id = name_to_id[spec["source"]] if spec["source"] in name_to_id else ""
            target_id = name_to_id[spec["target"]] if spec["target"] in name_to_id else ""

            # 护栏 2：RETRACT 第五态（撤回/取消旧边且无替代新值，支持同窗无 target_id 按槽回查）
            if op == "RETRACT":
                old = await _resolve_target_edge(
                    session,
                    scope_key,
                    old,
                    source_id,
                    target_id,
                    spec["slot_key"],
                    batch_edges_by_slot,
                    batch_edges_by_pair,
                    states=("active", "staging"),
                )
                if old is None:
                    continue
                old_src = await session.get(AIMemEntity, old.source_entity_id)
                old_owner_uid = (old_src.user_id or "").strip() if old_src is not None else ""
                if is_group_scope and old_owner_uid and old_owner_uid != speaker and old.resolution_state == "active":
                    # 群聊中禁止第三方直接撤回他人的已证实事实
                    continue
                old.resolution_state = "retracted"
                old.invalid_at = valid_at
                session.add(old)
                if old.id in edge_payload_by_id:
                    edge_payload_by_id[old.id]["resolution_state"] = "retracted"
                    edge_payload_by_id[old.id]["invalid_at_ts"] = valid_at.timestamp()
                elif old.qdrant_id:
                    lifecycle_updates.append(
                        {
                            "edge_id": old.qdrant_id,
                            "invalid_at_ts": valid_at.timestamp(),
                            "resolution_state": "retracted",
                        }
                    )
                if old.thread_id:
                    affected_threads.add(old.thread_id)
                written_edges += 1
                continue

            # 护栏 3：未提及数字群号防脑补与空洞同义反复拦截
            if is_context_omitted_group_hallucination(spec["fact"], line_rec.raw_content, scope_key):
                continue
            if spec["edge_kind"] == "milestone" and _is_tautology_fact(spec["fact"], spec["source"], spec["target"]):
                continue

            if not source_id or not target_id:
                continue

            if op in ("SUPERSEDE", "CONTRADICT") and old is None:
                old = await _resolve_target_edge(
                    session,
                    scope_key,
                    None,
                    source_id,
                    target_id,
                    spec["slot_key"],
                    batch_edges_by_slot,
                    batch_edges_by_pair,
                    states=("active", "staging"),
                )

            # 护栏 4：禁止对累加式 milestone 边执行 SUPERSEDE（始终降级为 ADD 保留历程）
            if op == "SUPERSEDE" and (
                spec["edge_kind"] == "milestone" or (old is not None and old.edge_kind == "milestone")
            ):
                op = "ADD"

            source_ent = entity_by_id[source_id] if source_id in entity_by_id else None
            subject = spec["subject_user_id"]
            if not subject and source_ent is not None and source_ent.entity_kind in ("speaker", "person"):
                subject = source_ent.user_id

            # 护栏 5：群聊第三方事实（含 subject=None 但主语是其他人物实体）100% 强制落 staging
            is_other_person = False
            if is_group_scope:
                if subject and subject != speaker:
                    is_other_person = True
                elif (
                    subject is None
                    and source_ent is not None
                    and source_ent.entity_kind in ("speaker", "person")
                    and source_ent.name != (line_rec.speaker_name or speaker)
                    and source_ent.name != speaker
                ):
                    is_other_person = True

            state: ResolutionState = "staging" if is_other_person else "active"
            effective_kind: EdgeKind = (
                "attribute" if (spec["edge_kind"] == "milestone" and not thread_id) else spec["edge_kind"]
            )
            event_at = _parse_date(spec["event_date"])
            ttl_days = infer_short_term_ttl_days(spec["ttl_days"])
            expired_at = (valid_at + timedelta(days=ttl_days)) if (ttl_days and effective_kind == "attribute") else None

            if op == "MERGE" and old is not None:
                if len(spec["fact"]) > len(old.fact):
                    old.fact = spec["fact"]
                old.mention_count += 1
                old.last_accessed = datetime.now(timezone.utc)
                if old.resolution_state == "staging" and state == "active":
                    old.resolution_state = "active"
                session.add(old)
                merge_payload: dict[str, object] = {
                    "edge_id": old.qdrant_id,
                    "fact": old.fact,
                    "scope_key": scope_key,
                    "valid_at_ts": old.valid_at.timestamp(),
                    "invalid_at_ts": old.invalid_at.timestamp() if old.invalid_at else None,
                    "source_entity_id": old.source_entity_id,
                    "target_entity_id": old.target_entity_id,
                    "resolution_state": old.resolution_state,
                }
                edge_vector_payloads.append(merge_payload)
                continue

            edge_id = str(uuid.uuid4())
            new_state: ResolutionState = state
            if op == "CONTRADICT":
                new_state = "contradicted"
            new_edge = AIMemEdge(
                id=edge_id,
                scope_key=scope_key,
                fact=spec["fact"],
                source_entity_id=source_id,
                target_entity_id=target_id,
                edge_kind=effective_kind,
                slot_key=spec["slot_key"],
                resolution_state=new_state,
                thread_id=thread_id,
                source_episode_id=episode_id or None,
                turn_index=line_idx,
                event_at=event_at,
                valid_at=valid_at,
                expired_at=expired_at,
                qdrant_id=edge_id,
                mention_count=1,
            )
            if op == "SUPERSEDE" and old is not None:
                if is_other_person and old.resolution_state == "active":
                    # 群聊中第三方传闻不能直接覆盖本人已证实的 active 边，转为 staging 留待本人证实
                    new_edge.resolution_state = "staging"
                else:
                    old.resolution_state = "superseded"
                    old.invalid_at = valid_at
                    old.superseded_by_id = edge_id
                    session.add(old)
                    if old.id in edge_payload_by_id:
                        edge_payload_by_id[old.id]["resolution_state"] = "superseded"
                        edge_payload_by_id[old.id]["invalid_at_ts"] = valid_at.timestamp()
                    elif old.qdrant_id:
                        lifecycle_updates.append(
                            {
                                "edge_id": old.qdrant_id,
                                "invalid_at_ts": valid_at.timestamp(),
                                "resolution_state": "superseded",
                            }
                        )
                    if old.thread_id:
                        affected_threads.add(old.thread_id)
            elif op == "CONTRADICT" and old is not None:
                old.resolution_state = "contradicted"
                old.contradicts_edge_id = edge_id
                new_edge.contradicts_edge_id = old.id
                session.add(old)
                if old.id in edge_payload_by_id:
                    edge_payload_by_id[old.id]["resolution_state"] = "contradicted"
                elif old.qdrant_id:
                    lifecycle_updates.append(
                        {
                            "edge_id": old.qdrant_id,
                            "invalid_at_ts": old.invalid_at.timestamp() if old.invalid_at else None,
                            "resolution_state": "contradicted",
                        }
                    )
            session.add(new_edge)
            new_payload: dict[str, object] = {
                "edge_id": edge_id,
                "fact": spec["fact"],
                "scope_key": scope_key,
                "valid_at_ts": valid_at.timestamp(),
                "invalid_at_ts": None,
                "source_entity_id": source_id,
                "target_entity_id": target_id,
                "resolution_state": new_edge.resolution_state,
            }
            edge_vector_payloads.append(new_payload)
            edge_payload_by_id[edge_id] = new_payload
            if new_edge.resolution_state in ("active", "staging"):
                if spec["slot_key"]:
                    batch_edges_by_slot[(source_id, spec["slot_key"])] = new_edge
                batch_edges_by_pair[(source_id, target_id)] = new_edge
            written_edges += 1
            if thread_id:
                recent_thread_facts.append(spec["fact"])
            if valid_at > latest_valid_at:
                latest_valid_at = valid_at

        if thread_id and (written_edges or output["thread_summary"]):
            affected_threads.add(thread_id)
        for tid in affected_threads:
            cnt_res = await session.execute(
                select(func.count())
                .select_from(AIMemEdge)
                .where(
                    col(AIMemEdge.thread_id) == tid,
                    col(AIMemEdge.edge_kind) == "milestone",
                    col(AIMemEdge.invalid_at).is_(None),
                    col(AIMemEdge.resolution_state).notin_(["superseded", "retracted"]),
                )
            )
            actual = int(cnt_res.scalar_one() or 0)
            th_row = await session.get(AIMemThread, tid)
            if th_row is not None:
                th_row.milestone_count = actual
                if tid == thread_id:
                    merged_uids = list(th_row.get_participant_uids())
                    for uid in sorted(window_speaker_ids):
                        if uid not in merged_uids:
                            merged_uids.append(uid)
                    th_row.set_participant_uids(merged_uids)
                    th_row.status = "active"
                    th_row.last_at = latest_valid_at
                    th_row.last_mentioned_at = datetime.now(timezone.utc)
                    if output["thread_summary"]:
                        th_row.summary = output["thread_summary"][:120]
                    elif not th_row.summary.strip() and recent_thread_facts:
                        th_row.summary = "；".join(recent_thread_facts[:2])[:120]
                session.add(th_row)
            else:
                await session.execute(
                    update(AIMemThread).where(col(AIMemThread.id) == tid).values(milestone_count=actual)
                )
        await session.commit()

    if entity_vector_payloads:
        try:
            await upsert_entity_vectors_batch(entity_vector_payloads)
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(t("log.memory.algebra_entity_vector_failed", scope_key=scope_key, e=e))
    if edge_vector_payloads:
        try:
            await upsert_edge_vectors_batch(edge_vector_payloads)
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(t("log.memory.algebra_edge_vector_failed", scope_key=scope_key, e=e))
    if lifecycle_updates:
        await update_edge_lifecycle_payloads(lifecycle_updates)

    # 双轨外号佐证与晋升（后台抽取一律按 caller_pm=6 严防受保护权力称谓提权）
    win_tag = episode_id or str(int(latest_valid_at.timestamp()))
    for alias_spec in output["aliases"]:
        if not is_human_speaker_id(alias_spec["user_id"]):
            continue
        await record_entity_alias(
            scope_key=scope_key,
            user_id=alias_spec["user_id"],
            alias=alias_spec["alias"],
            track=alias_spec["evidence"],
            window_id=win_tag,
            persona_name=cluster_persona,
            caller_pm=6,
            sync_group_profile=is_group_scope,
        )

    addresses_bot = (not is_group_scope) or _cluster_addresses_bot(records, lines)
    written_prefs = 0
    for pref in output["preferences"]:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
        from gsuid_core.ai_core.persona_reset.guard import is_scope_frozen

        if not addresses_bot or pref["target_context"] in ("possession", "location"):
            continue
        if len(pref["preference_rule"].strip()) < 4:
            continue
        pref_uid = _resolve_preference_user_id(pref["user_id"], records, scope_key)
        if not pref_uid:
            continue
        pref_scope = make_scope_key(ScopeType.USER_GLOBAL, pref_uid)
        if is_scope_frozen(pref_scope):
            continue
        await AIMemPreference.upsert(
            scope_key=pref_scope,
            user_id=pref_uid,
            target_context=pref["target_context"],
            preference_rule=pref["preference_rule"],
            polarity=pref["polarity"],
            is_correction=pref["is_correction"],
            source_episode_id=episode_id or None,
        )
        written_prefs += 1

    return {
        "entities": written_entities,
        "edges": written_edges,
        "preferences": written_prefs,
        "thread_id": thread_id or "",
    }


async def extract_cluster(
    scope_key: str,
    records: list["ObservationRecord"],
    lines: list[int],
    *,
    thread_ref: str | None,
    topic_title: str,
    episode_id: str,
    threads: list[AIMemThread] | None = None,
) -> AlgebraStats:
    """对 ``extract`` 聚类执行一次 Pass 2（保持 low 档模型）；返回写入统计。"""
    if not lines:
        return {"entities": 0, "edges": 0, "preferences": 0, "thread_id": ""}
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.memory.prompts import ALGEBRA_USER, ALGEBRA_SYSTEM

    thread_list = threads if threads is not None else []
    _, known_edges, names = await _prefetch(scope_key, records)

    thread_card = f"[新故事线] {topic_title}" if (thread_ref == "new" and topic_title) else "（未关联故事线）"
    if thread_ref is not None and thread_ref.startswith("T"):
        try:
            index = int(thread_ref[1:]) - 1
        except ValueError:
            index = -1
        if 0 <= index < len(thread_list):
            th = thread_list[index]
            thread_card = f"[{thread_ref}] {th.title} | {th.summary}"

    prompt = ALGEBRA_USER.format(
        thread_card=thread_card,
        known_edges=_format_known_edges(known_edges, names),
        dialogue=_format_dialogue(records, lines),
    )
    try:
        agent = GsCoreAIAgent(
            system_prompt=ALGEBRA_SYSTEM,
            max_tokens=8192,
            create_by="MemAlgebra",
            task_kind=TASK_MEMORY_ALGEBRA,
            task_level="low",
            is_subagent=True,
            dynamic_tools=False,
            thinking=False,
            scope_key=scope_key,
        )
        try:
            raw = await asyncio.wait_for(agent.run(user_message=prompt, return_mode="return"), timeout=120.0)
        finally:
            agent._session_logger.close()
        output = _parse_output(raw if isinstance(raw, str) else str(raw))
    except (asyncio.TimeoutError, OSError, RuntimeError, ValueError) as e:
        logger.warning(t("log.memory.algebra_extract_failed", scope_key=scope_key, e=e))
        return {"entities": 0, "edges": 0, "preferences": 0, "thread_id": ""}
    if output is None:
        return {"entities": 0, "edges": 0, "preferences": 0, "thread_id": ""}

    return await apply_algebra_output(
        scope_key,
        records,
        lines,
        output,
        known_edges=known_edges,
        thread_ref=thread_ref,
        topic_title=topic_title,
        episode_id=episode_id,
        threads=thread_list,
    )
