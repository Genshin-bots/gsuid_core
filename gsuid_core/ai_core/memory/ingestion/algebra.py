"""Pass 2：预取槽位 + 单次四态记忆代数抽取与落库。

四态：``ADD``（多值并存）/ ``MERGE``（同义强化）/ ``SUPERSEDE``（时序覆盖）/
``CONTRADICT``（无解释硬矛盾，双边保留）。
"""

from __future__ import annotations

import re
import uuid
import asyncio
from typing import TYPE_CHECKING, Literal, TypedDict
from datetime import datetime, timezone

from sqlmodel import col, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.utils import extract_json_from_text
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemThread,
    AIMemPreference,
)
from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.observer import ObservationRecord

_DANGLING_FACT_RE = re.compile(
    r"(?:(?:提到|提及|被提及|表示|认为|觉得|提出|指出|强调|透露|说道|谈到|聊到|说起|问到|询问|说)[了过]?"
    r"|(?:讨论|谈论|回复|回应|回答|评价|分享|补充|吐槽|感叹)[了过])"
    r"[。.!！?？…]?$"
)

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
ResolutionState = Literal["active", "staging", "superseded", "contradicted"]
EntityKind = Literal["speaker", "person", "project", "item", "location", "org"]


class ExtractedEntitySpec(TypedDict):
    name: str
    kind: EntityKind
    user_id: str | None
    summary: str


class ExtractedEdgeOpSpec(TypedDict):
    op: Literal["ADD", "MERGE", "SUPERSEDE", "CONTRADICT"]
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
            fact = item["fact"] if "fact" in item and isinstance(item["fact"], str) else ""
            source = item["source"] if "source" in item and isinstance(item["source"], str) else ""
            if not fact.strip() or not source.strip() or _DANGLING_FACT_RE.search(fact.strip()):
                continue
            op = item["op"] if "op" in item else "ADD"
            if op not in ("ADD", "MERGE", "SUPERSEDE", "CONTRADICT"):
                op = "ADD"
            edge_kind = item["kind"] if "kind" in item else "attribute"
            if edge_kind not in ("attribute", "relation", "milestone"):
                edge_kind = "attribute"
            raw_target_edge = item["target"] if "target" in item and isinstance(item["target"], str) else ""
            target_edge_id = raw_target_edge.strip() if raw_target_edge.strip().startswith("E") else None
            raw_tname = item["target_name"] if "target_name" in item else (None if target_edge_id else raw_target_edge)
            target = raw_tname.strip() if isinstance(raw_tname, str) else ""
            if not target:
                if edge_kind in ("attribute", "milestone"):
                    target = source.strip()
                else:
                    continue
            slot = item["slot"] if "slot" in item and isinstance(item["slot"], str) else ""
            line = item["line"] if "line" in item and isinstance(item["line"], int) else 0
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
            if target_ctx == "possession":
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

    if not entities and not edges and not preferences:
        return None
    return {"entities": entities, "edges": edges, "preferences": preferences}


async def _prefetch(
    scope_key: str, records: list["ObservationRecord"]
) -> tuple[dict[str, str], list[AIMemEdge], dict[str, str]]:
    speaker_ids = list({r.speaker_id for r in records if r.speaker_id})
    speaker_names = list({r.speaker_name for r in records if r.speaker_name})
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
        lines.append(f"[{_short_edge(edge.id)}] {name} | slot={edge.slot_key or edge.edge_kind} | {edge.fact} ({date})")
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
        and not r.speaker_id.startswith("__assistant_")
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
    if thread_ref is not None and thread_ref.startswith("T"):
        try:
            index = int(thread_ref[1:]) - 1
        except ValueError:
            index = -1
        if 0 <= index < len(threads):
            return threads[index].id
    if not topic_title:
        return None
    now = datetime.now(timezone.utc)
    thread = AIMemThread(
        id=str(uuid.uuid4()),
        scope_key=scope_key,
        title=topic_title[:120],
        aliases=[],
        summary="",
        status="active",
        first_at=now,
        last_at=now,
        n_sessions=1,
        milestone_count=0,
        qdrant_id="",
        last_mentioned_at=now,
        title_source="window",
    )
    session.add(thread)
    await session.flush()
    threads.append(thread)
    return thread.id


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
    """对 ``extract`` 聚类执行一次 Pass 2；返回写入统计。"""
    if not lines:
        return {"entities": 0, "edges": 0, "preferences": 0, "thread_id": ""}
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.configs.models import get_fast_model_for_task
    from gsuid_core.ai_core.memory.vector.ops import (
        upsert_edge_vectors_batch,
        upsert_entity_vectors_batch,
    )
    from gsuid_core.ai_core.memory.prompts.extraction import ALGEBRA_USER, ALGEBRA_SYSTEM

    thread_list = threads if threads is not None else []
    _, known_edges, names = await _prefetch(scope_key, records)
    thread_card = f"[新话题] {topic_title}" if topic_title else "（未关联故事线）"
    if thread_ref is not None:
        index = -1
        if thread_ref.startswith("T"):
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
            openai_chat_model=get_fast_model_for_task("low"),
            system_prompt=ALGEBRA_SYSTEM,
            max_tokens=1400,
            create_by="MemAlgebra",
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

    by_short = {_short_edge(edge.id): edge for edge in known_edges}
    entity_vector_payloads: list[dict[str, object]] = []
    edge_vector_payloads: list[dict[str, object]] = []
    written_edges = 0
    written_milestones = 0
    written_entities = 0
    latest_valid_at = datetime.now(timezone.utc)

    async with db_write_guard(), async_maker() as session:
        thread_id = await _resolve_thread(session, scope_key, thread_list, thread_ref, topic_title)
        window_speaker_ids = {r.speaker_id for r in records if r.speaker_id}
        window_speaker_names = {r.speaker_name for r in records if r.speaker_name}
        edge_speaker_ids = {e["speaker_user_id"] for e in output["edges"] if e["speaker_user_id"]}
        all_speaker_ids = window_speaker_ids | edge_speaker_ids
        name_to_uid: dict[str, str] = {r.speaker_name: r.speaker_id for r in records if r.speaker_name and r.speaker_id}

        entity_data: list[dict[str, object]] = []
        for spec in output["entities"]:
            uid = spec["user_id"]
            if uid is None and spec["name"] in name_to_uid:
                uid = name_to_uid[spec["name"]]
            is_speaker = (
                spec["kind"] == "speaker"
                or (uid is not None and uid in all_speaker_ids)
                or spec["name"] in window_speaker_names
            )
            kind: EntityKind = "speaker" if is_speaker and spec["kind"] == "person" else spec["kind"]
            entity_data.append(
                {
                    "name": spec["name"],
                    "summary": spec["summary"],
                    "tag": [kind],
                    "user_id": uid,
                    "is_speaker": is_speaker,
                    "kind": kind,
                }
            )
        requested_names = {spec["source"] for spec in output["edges"]} | {spec["target"] for spec in output["edges"]}
        existing_names = {item["name"] for item in entity_data}
        for name in sorted(requested_names - existing_names):
            entity_data.append(
                {
                    "name": name,
                    "summary": "",
                    "tag": ["Topic"],
                    "user_id": None,
                    "is_speaker": False,
                    "kind": "person",
                }
            )
        hybrid_ids = await AIMemEntity.prefetch_hybrid_name_ids(scope_key, entity_data)
        name_to_id, payloads, new_count = await AIMemEntity.extract_and_upsert(
            session,
            scope_key,
            entity_data,
            episode_id,
            list(all_speaker_ids),
            hybrid_name_ids=hybrid_ids,
        )
        entity_vector_payloads.extend(payloads)
        written_entities = new_count

        for spec in output["edges"]:
            source_id = name_to_id[spec["source"]] if spec["source"] in name_to_id else ""
            target_id = name_to_id[spec["target"]] if spec["target"] in name_to_id else ""
            if not source_id or not target_id:
                continue
            line_idx = spec["line_idx"] if 0 <= spec["line_idx"] < len(records) else 0
            line_rec = records[line_idx]
            valid_at = line_rec.timestamp
            event_at = _parse_date(spec["event_date"])
            speaker = spec["speaker_user_id"] or line_rec.speaker_id
            subject = spec["subject_user_id"]
            state: ResolutionState = (
                "active" if not subject or subject == speaker or spec["edge_kind"] == "milestone" else "staging"
            )
            old = by_short[spec["target_edge_id"]] if spec["target_edge_id"] in by_short else None

            if spec["op"] == "MERGE" and old is not None:
                if len(spec["fact"]) > len(old.fact):
                    old.fact = spec["fact"]
                old.mention_count += 1
                old.last_accessed = datetime.now(timezone.utc)
                if old.resolution_state == "staging" and state == "active":
                    old.resolution_state = "active"
                session.add(old)
                edge_vector_payloads.append(
                    {
                        "edge_id": old.qdrant_id,
                        "fact": old.fact,
                        "scope_key": scope_key,
                        "valid_at_ts": old.valid_at.timestamp(),
                        "invalid_at_ts": old.invalid_at.timestamp() if old.invalid_at else None,
                        "source_entity_id": old.source_entity_id,
                        "target_entity_id": old.target_entity_id,
                    }
                )
                continue

            edge_id = str(uuid.uuid4())
            new_state: ResolutionState = state
            if spec["op"] == "CONTRADICT":
                new_state = "contradicted"
            new_edge = AIMemEdge(
                id=edge_id,
                scope_key=scope_key,
                fact=spec["fact"],
                source_entity_id=source_id,
                target_entity_id=target_id,
                edge_kind=spec["edge_kind"],
                slot_key=spec["slot_key"],
                resolution_state=new_state,
                thread_id=thread_id if spec["edge_kind"] == "milestone" else None,
                source_episode_id=episode_id or None,
                turn_index=line_idx,
                event_at=event_at,
                valid_at=valid_at,
                qdrant_id=edge_id,
                mention_count=1,
            )
            if spec["op"] == "SUPERSEDE" and old is not None:
                old.resolution_state = "superseded"
                old.invalid_at = valid_at
                old.superseded_by_id = edge_id
                session.add(old)
            elif spec["op"] == "CONTRADICT" and old is not None:
                old.resolution_state = "contradicted"
                old.contradicts_edge_id = edge_id
                new_edge.contradicts_edge_id = old.id
                session.add(old)
            session.add(new_edge)
            edge_vector_payloads.append(
                {
                    "edge_id": edge_id,
                    "fact": spec["fact"],
                    "scope_key": scope_key,
                    "valid_at_ts": valid_at.timestamp(),
                    "invalid_at_ts": None,
                    "source_entity_id": source_id,
                    "target_entity_id": target_id,
                }
            )
            written_edges += 1
            if spec["edge_kind"] == "milestone":
                written_milestones += 1
            if valid_at > latest_valid_at:
                latest_valid_at = valid_at

        if thread_id and written_edges:
            await session.execute(
                update(AIMemThread)
                .where(col(AIMemThread.id) == thread_id)
                .values(
                    milestone_count=col(AIMemThread.milestone_count) + written_milestones,
                    last_at=latest_valid_at,
                    last_mentioned_at=datetime.now(timezone.utc),
                )
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

    written_prefs = 0
    for pref in output["preferences"]:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

        pref_uid = _resolve_preference_user_id(pref["user_id"], records, scope_key)
        if not pref_uid:
            continue
        await AIMemPreference.upsert(
            scope_key=make_scope_key(ScopeType.USER_GLOBAL, pref_uid),
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
