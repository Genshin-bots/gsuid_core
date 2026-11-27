"""WebConsole AI 记忆无损编排辅助（四栏实体详情、改名/外号/合并/按边拆分、全量游标对账与参与者头像解析）。"""

from __future__ import annotations

import time as _time
import uuid
from typing import Optional, Sequence, TypedDict
from datetime import datetime, timezone

from sqlmodel import col, or_, desc, func, delete, select, update
from sqlalchemy import text as sql_text, insert, inspect as sa_inspect
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.identity import (
    is_valid_avatar_url,
    batch_lookup_core_users,
)
from gsuid_core.utils.database.write_gate import sqlite_write_gate
from gsuid_core.utils.database.base_models import _db_type, async_maker
from gsuid_core.ai_core.memory.database.fts import index_episode, ensure_fts_table
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemThread,
    AIMemEpisode,
    PendingAliasEntry,
    mem_episode_entity_mentions,
)


class ParticipantInfo(TypedDict):
    user_id: str
    name: str
    avatar_url: str


class EntityEdgeCounts(TypedDict):
    self_edge_count: int
    relation_edge_count: int


class ReconcileResult(TypedDict):
    scope_key: str
    fts_rows: int
    vectors_repaired: int
    elapsed_ms: float


async def batch_entity_edge_counts(entity_ids: Sequence[str]) -> dict[str, EntityEdgeCounts]:
    """批量统计每个实体的自环属性边数与跨实体关系边数（单次往返，避免 N+1）。"""
    ids = [eid for eid in entity_ids if eid]
    if not ids or async_maker is None:
        return {}
    counts: dict[str, EntityEdgeCounts] = {eid: {"self_edge_count": 0, "relation_edge_count": 0} for eid in ids}
    async with async_maker() as session:
        self_stmt = (
            select(col(AIMemEdge.source_entity_id), func.count())
            .where(
                col(AIMemEdge.source_entity_id).in_(ids),
                col(AIMemEdge.source_entity_id) == col(AIMemEdge.target_entity_id),
            )
            .group_by(col(AIMemEdge.source_entity_id))
        )
        for eid, cnt in (await session.execute(self_stmt)).all():
            if isinstance(eid, str) and eid in counts:
                counts[eid]["self_edge_count"] = int(cnt or 0)

        out_rel = (
            select(col(AIMemEdge.source_entity_id), func.count())
            .where(
                col(AIMemEdge.source_entity_id).in_(ids),
                col(AIMemEdge.source_entity_id) != col(AIMemEdge.target_entity_id),
            )
            .group_by(col(AIMemEdge.source_entity_id))
        )
        for eid, cnt in (await session.execute(out_rel)).all():
            if isinstance(eid, str) and eid in counts:
                counts[eid]["relation_edge_count"] += int(cnt or 0)

        in_rel = (
            select(col(AIMemEdge.target_entity_id), func.count())
            .where(
                col(AIMemEdge.target_entity_id).in_(ids),
                col(AIMemEdge.source_entity_id) != col(AIMemEdge.target_entity_id),
            )
            .group_by(col(AIMemEdge.target_entity_id))
        )
        for eid, cnt in (await session.execute(in_rel)).all():
            if isinstance(eid, str) and eid in counts:
                counts[eid]["relation_edge_count"] += int(cnt or 0)
    return counts


async def resolve_thread_participants_batch(
    threads: Sequence[AIMemThread],
) -> dict[str, list[ParticipantInfo]]:
    """批量解析故事线参与者的名称与真实头像（优先同 scope 实体，其次 CoreUser）。"""
    if not threads or async_maker is None:
        return {}
    all_uids: set[str] = set()
    scopes: set[str] = set()
    thread_uids: dict[str, list[str]] = {}
    for th in threads:
        uids = th.get_participant_uids()
        thread_uids[th.id] = uids
        all_uids.update(uids)
        if th.scope_key:
            scopes.add(th.scope_key)
    if not all_uids:
        return {th.id: [] for th in threads}

    entity_map: dict[tuple[str, str], tuple[str, str]] = {}
    async with async_maker() as session:
        stmt = (
            select(
                col(AIMemEntity.scope_key),
                col(AIMemEntity.user_id),
                col(AIMemEntity.name),
                col(AIMemEntity.avatar_url),
            )
            .where(
                col(AIMemEntity.scope_key).in_(list(scopes)),
                col(AIMemEntity.user_id).in_(list(all_uids)),
            )
            .order_by(desc(col(AIMemEntity.is_speaker)), desc(col(AIMemEntity.updated_at)))
        )
        for sk, uid, name, av in (await session.execute(stmt)).all():
            if isinstance(sk, str) and isinstance(uid, str) and (sk, uid) not in entity_map:
                avatar = str(av or "").strip()
                entity_map[(sk, uid)] = (str(name or "").strip(), avatar if is_valid_avatar_url(avatar) else "")

    core_map = await batch_lookup_core_users(list(all_uids))
    out: dict[str, list[ParticipantInfo]] = {}
    for th in threads:
        plist: list[ParticipantInfo] = []
        for uid in thread_uids[th.id]:
            ename, eav = entity_map[(th.scope_key, uid)] if (th.scope_key, uid) in entity_map else ("", "")
            cname, cav = core_map[uid] if uid in core_map else ("", "")
            plist.append(
                {
                    "user_id": uid,
                    "name": ename or cname or uid,
                    "avatar_url": eav or cav,
                }
            )
        out[th.id] = plist
    return out


def _serialize_edge_for_drawer(
    edge: AIMemEdge,
    entity_id: str,
    names: dict[str, str],
) -> dict[str, object]:
    is_self_loop = edge.source_entity_id == edge.target_entity_id
    direction = "self" if is_self_loop else ("outgoing" if edge.source_entity_id == entity_id else "incoming")
    return {
        "id": edge.id,
        "scope_key": edge.scope_key,
        "fact": edge.fact,
        "source_entity_id": edge.source_entity_id,
        "target_entity_id": edge.target_entity_id,
        "source_entity_name": names[edge.source_entity_id] if edge.source_entity_id in names else edge.source_entity_id,
        "target_entity_name": names[edge.target_entity_id] if edge.target_entity_id in names else edge.target_entity_id,
        "edge_kind": edge.edge_kind,
        "slot_key": edge.slot_key,
        "resolution_state": edge.resolution_state,
        "superseded_by_id": edge.superseded_by_id,
        "contradicts_edge_id": edge.contradicts_edge_id,
        "thread_id": edge.thread_id,
        "event_at": str(edge.event_at) if edge.event_at else None,
        "valid_at": str(edge.valid_at) if edge.valid_at else None,
        "invalid_at": str(edge.invalid_at) if edge.invalid_at else None,
        "expired_at": str(edge.expired_at) if edge.expired_at else None,
        "mention_count": edge.mention_count,
        "decay_score": edge.decay_score,
        "direction": direction,
    }


async def load_entity_detail_payload(entity_id: str) -> dict[str, object] | None:
    """加载四栏实体抽屉所需的全部关联数据（显式 selectinload + 全量边分类）。"""
    if async_maker is None:
        return None
    async with async_maker() as session:
        entity = await session.get(AIMemEntity, entity_id)
        if entity is None:
            return None

        ep_stmt = (
            select(AIMemEpisode)
            .join(
                mem_episode_entity_mentions,
                col(AIMemEpisode.id) == mem_episode_entity_mentions.c.episode_id,
            )
            .where(mem_episode_entity_mentions.c.entity_id == entity.id)
            .order_by(desc(col(AIMemEpisode.valid_at)))
            .limit(20)
        )
        ep_rows = list((await session.execute(ep_stmt)).scalars().all())

        edge_stmt = (
            select(AIMemEdge)
            .where(
                or_(
                    col(AIMemEdge.source_entity_id) == entity.id,
                    col(AIMemEdge.target_entity_id) == entity.id,
                )
            )
            .order_by(desc(col(AIMemEdge.valid_at)))
            .limit(300)
        )
        edge_rows = list((await session.execute(edge_stmt)).scalars().all())

        other_ids = list(
            {e.source_entity_id for e in edge_rows} | {e.target_entity_id for e in edge_rows} | {entity.id}
        )
        names = await AIMemEntity.get_names_by_ids(other_ids)

        episodes_out: list[dict[str, object]] = [
            {
                "id": ep.id,
                "content": ep.content[:240],
                "valid_at": str(ep.valid_at) if ep.valid_at else None,
            }
            for ep in ep_rows
        ]

    self_attributes: list[dict[str, object]] = []
    relations: list[dict[str, object]] = []
    rumors_and_conflicts: list[dict[str, object]] = []
    history_edges: list[dict[str, object]] = []
    flat_edges: list[dict[str, object]] = []

    for edge in edge_rows:
        item = _serialize_edge_for_drawer(edge, entity.id, names)
        flat_edges.append(item)
        state = edge.resolution_state
        if state in ("superseded", "retracted") or edge.invalid_at is not None:
            history_edges.append(item)
        elif state in ("staging", "contradicted"):
            rumors_and_conflicts.append(item)
        elif edge.source_entity_id == edge.target_entity_id:
            self_attributes.append(item)
        else:
            relations.append(item)

    return {
        "id": entity.id,
        "scope_key": entity.scope_key,
        "name": entity.name,
        "summary": entity.summary,
        "tag": entity.tag,
        "entity_kind": entity.entity_kind,
        "is_speaker": entity.is_speaker,
        "user_id": entity.user_id,
        "avatar_url": entity.avatar_url,
        "aliases": entity.get_aliases(),
        "pending_aliases": entity.get_pending_aliases(),
        "created_at": str(entity.created_at) if entity.created_at else None,
        "updated_at": str(entity.updated_at) if entity.updated_at else None,
        "qdrant_id": entity.qdrant_id,
        "episodes": episodes_out,
        "edges": flat_edges,
        "self_attributes": self_attributes,
        "relations": relations,
        "rumors_and_conflicts": rumors_and_conflicts,
        "history_edges": history_edges,
    }


async def _sync_entity_vector_safe(entity: AIMemEntity) -> None:
    from gsuid_core.ai_core.memory.vector.ops import upsert_entity_vector

    try:
        await upsert_entity_vector(
            entity_id=entity.qdrant_id or entity.id,
            name=entity.name,
            summary=entity.summary,
            scope_key=entity.scope_key,
            is_speaker=entity.is_speaker,
            user_id=entity.user_id,
            tag=[str(x) for x in entity.tag if isinstance(x, str)],
            aliases=entity.get_aliases(),
            entity_kind=entity.entity_kind,
            avatar_url=entity.avatar_url,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning(t("log.webconsole.memory_reconcile_vectors_fail", e=exc))


async def _sync_edges_vectors_safe(edges: Sequence[AIMemEdge]) -> None:
    if not edges:
        return
    from gsuid_core.ai_core.memory.vector.ops import upsert_edge_vectors_batch

    items: list[dict[str, str | float | None]] = [
        {
            "edge_id": e.qdrant_id or e.id,
            "fact": e.fact,
            "scope_key": e.scope_key,
            "valid_at_ts": e.valid_at.timestamp() if e.valid_at else None,
            "invalid_at_ts": e.invalid_at.timestamp() if e.invalid_at else None,
            "source_entity_id": e.source_entity_id,
            "target_entity_id": e.target_entity_id,
            "resolution_state": e.resolution_state,
        }
        for e in edges
    ]
    try:
        await upsert_edge_vectors_batch(items)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning(t("log.webconsole.memory_reconcile_vectors_fail", e=exc))


async def patch_entity_record(
    entity_id: str,
    *,
    name: Optional[str] = None,
    summary: Optional[str] = None,
    entity_kind: Optional[str] = None,
    aliases: Optional[list[str]] = None,
    promote_pending_alias: Optional[str] = None,
    dismiss_pending_alias: Optional[str] = None,
) -> tuple[bool, str, dict[str, object] | None]:
    """在线编辑实体：改名、改简介、增删外号、晋升/驳回待证实外号，同步 SQL + Qdrant + 群画像。"""
    if async_maker is None:
        return False, "数据库未初始化", None
    promoted_aliases: list[str] = []
    updated_entity: AIMemEntity | None = None

    async with sqlite_write_gate, async_maker() as session:
        entity = await session.get(AIMemEntity, entity_id)
        if entity is None:
            return False, f"未找到 Entity: {entity_id}", None

        changed = False
        if name is not None:
            new_name = name.strip()[:120]
            if not new_name:
                return False, "实体名称不能为空", None
            if new_name != entity.name:
                dup = (
                    await session.execute(
                        select(col(AIMemEntity.id)).where(
                            col(AIMemEntity.scope_key) == entity.scope_key,
                            col(AIMemEntity.name) == new_name,
                            col(AIMemEntity.id) != entity.id,
                        )
                    )
                ).scalar_one_or_none()
                if dup is not None:
                    return False, f"同范围内已存在同名实体「{new_name}」，请使用合并功能", None
                old_name = entity.name.strip()
                cur_aliases = entity.get_aliases()
                if old_name and old_name != entity.user_id and old_name not in cur_aliases:
                    cur_aliases.append(old_name)
                    entity.set_aliases(cur_aliases)
                entity.name = new_name
                changed = True

        if summary is not None:
            clean_sum = summary.strip()[:2000]
            if clean_sum != entity.summary:
                entity.summary = clean_sum
                changed = True

        if entity_kind is not None:
            kind_val = entity_kind.strip().lower()
            if kind_val in ("speaker", "person", "project", "item", "location", "org", "concept", "event"):
                is_human_kind = kind_val in ("speaker", "person")
                bound_uid = (entity.user_id or "").strip()
                if not is_human_kind and bound_uid:
                    return (
                        False,
                        f"已绑定发言人 UID({bound_uid})，不可直接改为非人物类型「{kind_val}」；请使用按边拆分",
                        None,
                    )
                entity.entity_kind = kind_val
                if is_human_kind:
                    entity.is_speaker = kind_val == "speaker" or bool(bound_uid)
                else:
                    entity.is_speaker = False
                    entity.user_id = None
                    raw_tags = list(entity.tag) if isinstance(entity.tag, list) else []
                    entity.tag = [
                        t_item for t_item in raw_tags if not (isinstance(t_item, str) and t_item.lower() == "speaker")
                    ]
                changed = True

        if aliases is not None:
            cleaned = [a.strip()[:32] for a in aliases if a.strip() and a.strip() != entity.name]
            prev = set(entity.get_aliases())
            entity.set_aliases(cleaned)
            promoted_aliases.extend(a for a in cleaned if a not in prev)
            changed = True

        if promote_pending_alias is not None:
            target_al = promote_pending_alias.strip()[:32]
            if target_al:
                cur_al = entity.get_aliases()
                if target_al != entity.name and target_al not in cur_al:
                    cur_al.append(target_al)
                    entity.set_aliases(cur_al)
                    promoted_aliases.append(target_al)
                remaining = [p for p in entity.get_pending_aliases() if p["alias"] != target_al]
                entity.set_pending_aliases(remaining)
                changed = True

        if dismiss_pending_alias is not None:
            target_al = dismiss_pending_alias.strip()
            if target_al:
                remaining = [p for p in entity.get_pending_aliases() if p["alias"] != target_al]
                entity.set_pending_aliases(remaining)
                changed = True

        if changed:
            entity.updated_at = datetime.now(timezone.utc)
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
        updated_entity = entity

    if updated_entity is not None:
        await _sync_entity_vector_safe(updated_entity)
        if promoted_aliases and updated_entity.scope_key.startswith("group:") and updated_entity.user_id:
            from gsuid_core.ai_core.memory.group_profile import record_member_alias

            for al in promoted_aliases:
                await record_member_alias(updated_entity.scope_key, al, updated_entity.user_id)

    payload = await load_entity_detail_payload(entity_id)
    return True, "ok", payload


async def _retarget_entity_cognition_nodes(
    session: AsyncSession,
    src_id: str,
    tgt: AIMemEntity,
) -> None:
    """实体合并时原子重指/合并派生认知索引节点（ent:/env:），防止世界枢纽链路悬空。"""
    from gsuid_core.ai_core.cognition.hub import ENV_REF_PREFIX
    from gsuid_core.ai_core.cognition.nodes import AICogEdge, AICogNode, AICogAttachment
    from gsuid_core.ai_core.cognition.types import CogKind

    def _table_names(sync_session: Session) -> set[str]:
        conn = sync_session.connection()
        return set(sa_inspect(conn).get_table_names())

    tables = await session.run_sync(_table_names)
    if "aicognode" not in tables:
        return
    has_edge_tbl = "aicogedge" in tables
    has_att_tbl = "aicogattachment" in tables
    now_ts = int(_time.time())
    prefixes = (ENV_REF_PREFIX,) if ENV_REF_PREFIX == "env:" else (ENV_REF_PREFIX, "env:")

    for prefix in prefixes:
        src_ref = f"{prefix}{src_id}"
        tgt_ref = f"{prefix}{tgt.id}"
        src_cog = (
            await session.execute(
                select(AICogNode).where(
                    col(AICogNode.kind) == CogKind.ENTITY.value,
                    col(AICogNode.ref) == src_ref,
                )
            )
        ).scalar_one_or_none()
        if src_cog is None or not isinstance(src_cog.id, int):
            continue

        if tgt.is_speaker:
            # 说话人不接入世界枢纽（见 cognition/hub.py:maybe_link_entity_to_world），合入时移除旧索引。
            if has_edge_tbl:
                await session.execute(
                    delete(AICogEdge).where(
                        or_(col(AICogEdge.src_id) == src_cog.id, col(AICogEdge.dst_id) == src_cog.id)
                    )
                )
            if has_att_tbl:
                await session.execute(delete(AICogAttachment).where(col(AICogAttachment.node_id) == src_cog.id))
            await session.delete(src_cog)
            continue

        tgt_cog = (
            await session.execute(
                select(AICogNode).where(
                    col(AICogNode.kind) == CogKind.ENTITY.value,
                    col(AICogNode.ref) == tgt_ref,
                )
            )
        ).scalar_one_or_none()

        if tgt_cog is None:
            src_cog.ref = tgt_ref
            src_cog.title = tgt.name
            src_cog.scope_key = tgt.scope_key
            src_cog.updated_at = now_ts
            session.add(src_cog)
            continue

        if not isinstance(tgt_cog.id, int):
            continue

        src_nid = src_cog.id
        tgt_nid = tgt_cog.id
        if has_edge_tbl:
            src_edges = list(
                (
                    await session.execute(
                        select(AICogEdge).where(or_(col(AICogEdge.src_id) == src_nid, col(AICogEdge.dst_id) == src_nid))
                    )
                )
                .scalars()
                .all()
            )
            existing_tgt_edges = {
                (e.src_id, e.dst_id, e.edge_kind)
                for e in (
                    await session.execute(
                        select(AICogEdge).where(or_(col(AICogEdge.src_id) == tgt_nid, col(AICogEdge.dst_id) == tgt_nid))
                    )
                )
                .scalars()
                .all()
            }
            for ce in src_edges:
                new_src = tgt_nid if ce.src_id == src_nid else ce.src_id
                new_dst = tgt_nid if ce.dst_id == src_nid else ce.dst_id
                key = (new_src, new_dst, ce.edge_kind)
                if new_src == new_dst or key in existing_tgt_edges:
                    await session.delete(ce)
                else:
                    ce.src_id = new_src
                    ce.dst_id = new_dst
                    session.add(ce)
                    existing_tgt_edges.add(key)

        if has_att_tbl:
            src_atts = list(
                (await session.execute(select(AICogAttachment).where(col(AICogAttachment.node_id) == src_nid)))
                .scalars()
                .all()
            )
            existing_att_refs = {
                str(r[0])
                for r in (
                    await session.execute(
                        select(col(AICogAttachment.ref)).where(col(AICogAttachment.node_id) == tgt_nid)
                    )
                ).all()
            }
            for att in src_atts:
                if att.ref in existing_att_refs:
                    await session.delete(att)
                else:
                    att.node_id = tgt_nid
                    session.add(att)
                    existing_att_refs.add(att.ref)

        if not tgt_cog.canon and src_cog.canon:
            tgt_cog.canon = src_cog.canon
        tgt_cog.title = tgt.name
        tgt_cog.updated_at = now_ts
        session.add(tgt_cog)
        await session.flush()
        await session.delete(src_cog)


async def merge_entity_records(
    source_entity_id: str,
    target_entity_id: str,
) -> tuple[bool, str, dict[str, object] | None]:
    """将重复实体无损并入目标实体：全部边与片段提及重指、外号并集、同步 Qdrant。"""
    if async_maker is None:
        return False, "数据库未初始化", None
    if source_entity_id == target_entity_id:
        return False, "源实体与目标实体不能相同", None

    source_qdrant_id = ""
    target_entity: AIMemEntity | None = None
    repointed_edges: list[AIMemEdge] = []

    async with sqlite_write_gate, async_maker() as session:
        src = await session.get(AIMemEntity, source_entity_id)
        tgt = await session.get(AIMemEntity, target_entity_id)
        if src is None:
            return False, f"未找到源实体: {source_entity_id}", None
        if tgt is None:
            return False, f"未找到目标实体: {target_entity_id}", None
        if src.scope_key != tgt.scope_key:
            return False, "只能合并同一记忆范围（scope）内的实体", None

        source_qdrant_id = src.qdrant_id or src.id
        # 1. 重指全部出边与入边
        await session.execute(
            update(AIMemEdge).where(col(AIMemEdge.source_entity_id) == src.id).values(source_entity_id=tgt.id)
        )
        await session.execute(
            update(AIMemEdge).where(col(AIMemEdge.target_entity_id) == src.id).values(target_entity_id=tgt.id)
        )
        # 2. 迁移片段提及表并去重
        src_ep_ids = [
            str(r[0])
            for r in (
                await session.execute(
                    select(mem_episode_entity_mentions.c.episode_id).where(
                        mem_episode_entity_mentions.c.entity_id == src.id
                    )
                )
            ).all()
        ]
        if src_ep_ids:
            existing_tgt_eps = {
                str(r[0])
                for r in (
                    await session.execute(
                        select(mem_episode_entity_mentions.c.episode_id).where(
                            mem_episode_entity_mentions.c.entity_id == tgt.id,
                            mem_episode_entity_mentions.c.episode_id.in_(src_ep_ids),
                        )
                    )
                ).all()
            }
            for ep_id in src_ep_ids:
                if ep_id not in existing_tgt_eps:
                    await session.execute(
                        insert(mem_episode_entity_mentions).values(episode_id=ep_id, entity_id=tgt.id)
                    )
            await session.execute(
                delete(mem_episode_entity_mentions).where(mem_episode_entity_mentions.c.entity_id == src.id)
            )

        # 3. 合并外号、待证外号、简介与头像
        merged_aliases = tgt.get_aliases()
        for cand in [src.name, *src.get_aliases()]:
            c = cand.strip()
            if c and c != tgt.name and c != tgt.user_id and c not in merged_aliases:
                merged_aliases.append(c)
        tgt.set_aliases(merged_aliases)

        pending_by_name: dict[str, PendingAliasEntry] = {p["alias"]: p for p in tgt.get_pending_aliases()}
        for p in src.get_pending_aliases():
            if p["alias"] not in pending_by_name and p["alias"] not in merged_aliases and p["alias"] != tgt.name:
                pending_by_name[p["alias"]] = p
        tgt.set_pending_aliases(list(pending_by_name.values()))

        if not tgt.is_speaker and src.is_speaker:
            tgt.is_speaker = True
            tgt.entity_kind = "speaker"
        if not tgt.user_id and src.user_id and tgt.entity_kind in ("speaker", "person"):
            tgt.user_id = src.user_id
        if not is_valid_avatar_url(tgt.avatar_url) and is_valid_avatar_url(src.avatar_url):
            tgt.avatar_url = src.avatar_url
        if not tgt.summary.strip() and src.summary.strip():
            tgt.summary = src.summary.strip()

        tgt.updated_at = datetime.now(timezone.utc)
        session.add(tgt)
        await _retarget_entity_cognition_nodes(session, src.id, tgt)
        await session.delete(src)
        await session.commit()
        await session.refresh(tgt)
        target_entity = tgt

        repointed_edges = list(
            (
                await session.execute(
                    select(AIMemEdge).where(
                        or_(col(AIMemEdge.source_entity_id) == tgt.id, col(AIMemEdge.target_entity_id) == tgt.id)
                    )
                )
            )
            .scalars()
            .all()
        )

    if source_qdrant_id:
        try:
            from gsuid_core.ai_core.rag.base import client
            from gsuid_core.ai_core.memory.vector.ops import MEMORY_ENTITIES_COLLECTION

            if client is not None:
                await client.delete(
                    collection_name=MEMORY_ENTITIES_COLLECTION,
                    points_selector=[source_qdrant_id],
                )
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(t("log.webconsole.memory_reconcile_vectors_fail", e=exc))

    if target_entity is not None:
        await _sync_entity_vector_safe(target_entity)
        await _sync_edges_vectors_safe(repointed_edges)

    payload = await load_entity_detail_payload(target_entity_id)
    return True, "ok", payload


async def split_entity_records(
    entity_id: str,
    *,
    edge_ids: Sequence[str],
    new_name: str,
    entity_kind: str = "person",
    summary: str = "",
) -> tuple[bool, str, dict[str, object] | None]:
    """按勾选边无损拆出独立新实体：仅重指所选边，零物理删除历史边。"""
    if async_maker is None:
        return False, "数据库未初始化", None
    clean_name = new_name.strip()[:120]
    if not clean_name:
        return False, "拆分后的新实体名称不能为空", None
    selected_ids = list({eid.strip() for eid in edge_ids if eid and eid.strip()})
    if not selected_ids:
        return False, "请至少勾选一条需要拆分迁移的记忆边", None

    kind_val = entity_kind.strip().lower()
    if kind_val not in ("speaker", "person", "project", "item", "location", "org", "concept", "event"):
        kind_val = "person"

    new_entity: AIMemEntity | None = None
    moved_edges: list[AIMemEdge] = []

    async with sqlite_write_gate, async_maker() as session:
        src = await session.get(AIMemEntity, entity_id)
        if src is None:
            return False, f"未找到源实体: {entity_id}", None
        dup = (
            await session.execute(
                select(col(AIMemEntity.id)).where(
                    col(AIMemEntity.scope_key) == src.scope_key,
                    col(AIMemEntity.name) == clean_name,
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            return False, f"同范围内已存在同名实体「{clean_name}」，请换一个名称", None

        edge_rows = list(
            (
                await session.execute(
                    select(AIMemEdge).where(
                        col(AIMemEdge.scope_key) == src.scope_key,
                        col(AIMemEdge.id).in_(selected_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not edge_rows:
            return False, "勾选的边均不属于当前范围或不存在", None

        now = datetime.now(timezone.utc)
        new_id = str(uuid.uuid4())
        created = AIMemEntity(
            id=new_id,
            scope_key=src.scope_key,
            name=clean_name,
            summary=summary.strip()[:2000],
            tag=[kind_val],
            is_speaker=kind_val == "speaker",
            entity_kind=kind_val,
            user_id=None,
            avatar_url="",
            qdrant_id=new_id,
            created_at=now,
            updated_at=now,
        )
        session.add(created)
        await session.flush()

        for edge in edge_rows:
            touched = False
            if edge.source_entity_id == src.id:
                edge.source_entity_id = created.id
                touched = True
            if edge.target_entity_id == src.id:
                edge.target_entity_id = created.id
                touched = True
            if touched:
                session.add(edge)
                moved_edges.append(edge)

        await session.commit()
        await session.refresh(created)
        new_entity = created

    if new_entity is not None:
        await _sync_entity_vector_safe(new_entity)
        await _sync_edges_vectors_safe(moved_edges)
        new_payload = await load_entity_detail_payload(new_entity.id)
        return True, "ok", {"new_entity": new_payload, "moved_edges": len(moved_edges)}
    return False, "拆分实体失败", None


async def reconcile_scope_index_full(scope_key: str) -> ReconcileResult:
    """全量主键游标对账（修复原先 .limit(200) 与 [:512] 截断）：FTS5 全建 + Qdrant 全量补齐。"""
    started = _time.perf_counter()
    if async_maker is None:
        return {"scope_key": scope_key, "fts_rows": 0, "vectors_repaired": 0, "elapsed_ms": 0.0}
    fts_rows = 0
    batch_size = 256

    async with async_maker() as session:
        await ensure_fts_table(session)
        if _db_type == "sqlite":
            await session.execute(
                sql_text("DELETE FROM aimemepisode_fts WHERE scope_key = :sk"),
                {"sk": scope_key},
            )
        last_ep_id = ""
        while True:
            ep_stmt = (
                select(AIMemEpisode)
                .where(col(AIMemEpisode.scope_key) == scope_key, col(AIMemEpisode.id) > last_ep_id)
                .order_by(col(AIMemEpisode.id).asc())
                .limit(batch_size)
            )
            chunk = list((await session.execute(ep_stmt)).scalars().all())
            if not chunk:
                break
            for episode in chunk:
                await index_episode(session, episode)
                fts_rows += 1
            last_ep_id = chunk[-1].id
        await session.commit()

    repaired = 0
    try:
        from gsuid_core.ai_core.rag.base import client
        from gsuid_core.ai_core.memory.vector.ops import (
            _QDRANT_WRITE_ERRORS,
            MEMORY_EDGES_COLLECTION,
            MEMORY_ENTITIES_COLLECTION,
            MEMORY_EPISODES_COLLECTION,
            EdgeLifecycleUpdate,
            set_entity_payloads_batch,
            upsert_edge_vectors_batch,
            upsert_entity_vectors_batch,
            upsert_episode_vectors_batch,
            update_edge_lifecycle_payloads,
        )

        if client is None:
            return {
                "scope_key": scope_key,
                "fts_rows": fts_rows,
                "vectors_repaired": 0,
                "elapsed_ms": round((_time.perf_counter() - started) * 1000, 2),
            }

        # 1. Episode 全量游标补偿
        last_id = ""
        while True:
            async with async_maker() as session:
                ep_rows = list(
                    (
                        await session.execute(
                            select(AIMemEpisode)
                            .where(col(AIMemEpisode.scope_key) == scope_key, col(AIMemEpisode.id) > last_id)
                            .order_by(col(AIMemEpisode.id).asc())
                            .limit(batch_size)
                        )
                    )
                    .scalars()
                    .all()
                )
            if not ep_rows:
                break
            last_id = ep_rows[-1].id
            ids = [r.qdrant_id or r.id for r in ep_rows]
            points = await client.retrieve(
                collection_name=MEMORY_EPISODES_COLLECTION,
                ids=ids,
                with_payload=False,
                with_vectors=False,
            )
            existing = {str(p.id) for p in points}
            missing_eps = [
                {
                    "episode_id": r.qdrant_id or r.id,
                    "content": r.content,
                    "scope_key": r.scope_key,
                    "valid_at_ts": r.valid_at.timestamp() if r.valid_at else 0.0,
                    "speaker_ids": r.speaker_ids,
                    "session_id": r.session_id,
                    "thread_id": r.thread_id,
                    "turn_index": r.turn_index,
                }
                for r in ep_rows
                if (r.qdrant_id or r.id) not in existing
            ]
            if missing_eps:
                await upsert_episode_vectors_batch(missing_eps)
                repaired += len(missing_eps)

        # 2. Entity 全量游标补偿（补缺 + 修复 aliases / entity_kind / avatar_url 等 payload 漂移）
        last_id = ""
        while True:
            async with async_maker() as session:
                ent_rows = list(
                    (
                        await session.execute(
                            select(AIMemEntity)
                            .where(col(AIMemEntity.scope_key) == scope_key, col(AIMemEntity.id) > last_id)
                            .order_by(col(AIMemEntity.id).asc())
                            .limit(batch_size)
                        )
                    )
                    .scalars()
                    .all()
                )
            if not ent_rows:
                break
            last_id = ent_rows[-1].id
            points = await client.retrieve(
                collection_name=MEMORY_ENTITIES_COLLECTION,
                ids=[e.qdrant_id or e.id for e in ent_rows],
                with_payload=True,
                with_vectors=False,
            )
            existing_ent_payloads: dict[str, dict[str, object]] = {
                str(p.id): (p.payload if isinstance(p.payload, dict) else {}) for p in points
            }
            missing_ents: list[dict[str, object]] = []
            drifted_ent_patches: list[tuple[str, dict[str, object]]] = []
            for e in ent_rows:
                qid = e.qdrant_id or e.id
                expected_pl: dict[str, object] = {
                    "name": e.name,
                    "summary": e.summary,
                    "scope_key": e.scope_key,
                    "is_speaker": e.is_speaker,
                    "user_id": e.user_id,
                    "tag": [str(x) for x in e.tag if isinstance(x, str)],
                    "aliases": e.get_aliases(),
                    "entity_kind": e.entity_kind,
                    "avatar_url": e.avatar_url,
                }
                if qid not in existing_ent_payloads:
                    missing_ents.append({"entity_id": qid, **expected_pl})
                else:
                    cur_pl = existing_ent_payloads[qid]
                    if any(k not in cur_pl or cur_pl[k] != v for k, v in expected_pl.items()):
                        drifted_ent_patches.append((qid, expected_pl))
            if missing_ents:
                await upsert_entity_vectors_batch(missing_ents)
                repaired += len(missing_ents)
            if drifted_ent_patches:
                repaired += await set_entity_payloads_batch(drifted_ent_patches)

        # 3. Edge 全量游标补偿
        last_id = ""
        while True:
            async with async_maker() as session:
                edge_rows = list(
                    (
                        await session.execute(
                            select(AIMemEdge)
                            .where(col(AIMemEdge.scope_key) == scope_key, col(AIMemEdge.id) > last_id)
                            .order_by(col(AIMemEdge.id).asc())
                            .limit(batch_size)
                        )
                    )
                    .scalars()
                    .all()
                )
            if not edge_rows:
                break
            last_id = edge_rows[-1].id
            points = await client.retrieve(
                collection_name=MEMORY_EDGES_COLLECTION,
                ids=[e.qdrant_id or e.id for e in edge_rows],
                with_payload=True,
                with_vectors=False,
            )
            existing_meta: dict[str, tuple[str, float | None]] = {}
            for pt in points:
                pl = pt.payload if isinstance(pt.payload, dict) else {}
                st_raw = pl["resolution_state"] if "resolution_state" in pl else ""
                inv_raw = pl["invalid_at_ts"] if "invalid_at_ts" in pl else None
                inv_val = float(inv_raw) if isinstance(inv_raw, (int, float)) else None
                existing_meta[str(pt.id)] = (str(st_raw) if isinstance(st_raw, str) else "", inv_val)
            missing_edges: list[dict[str, str | float | None]] = []
            drifted_updates: list[EdgeLifecycleUpdate] = []
            for e in edge_rows:
                qid = e.qdrant_id or e.id
                exp_inv = e.invalid_at.timestamp() if e.invalid_at else None
                if qid not in existing_meta:
                    missing_edges.append(
                        {
                            "edge_id": qid,
                            "fact": e.fact,
                            "scope_key": e.scope_key,
                            "valid_at_ts": e.valid_at.timestamp() if e.valid_at else None,
                            "invalid_at_ts": exp_inv,
                            "source_entity_id": e.source_entity_id,
                            "target_entity_id": e.target_entity_id,
                            "resolution_state": e.resolution_state,
                        }
                    )
                else:
                    cur_state, cur_inv = existing_meta[qid]
                    inv_drifted = (cur_inv is None) != (exp_inv is None) or (
                        cur_inv is not None and exp_inv is not None and abs(cur_inv - exp_inv) > 1.0
                    )
                    if cur_state != e.resolution_state or inv_drifted:
                        drifted_updates.append(
                            {
                                "edge_id": qid,
                                "invalid_at_ts": exp_inv,
                                "resolution_state": e.resolution_state,
                            }
                        )
            if missing_edges:
                await upsert_edge_vectors_batch(missing_edges)
                repaired += len(missing_edges)
            if drifted_updates:
                await update_edge_lifecycle_payloads(drifted_updates)
                repaired += len(drifted_updates)
    except _QDRANT_WRITE_ERRORS as exc:
        logger.warning(t("log.webconsole.memory_reconcile_vectors_fail", e=exc))

    return {
        "scope_key": scope_key,
        "fts_rows": fts_rows,
        "vectors_repaired": repaired,
        "elapsed_ms": round((_time.perf_counter() - started) * 1000, 2),
    }


__all__ = [
    "EntityEdgeCounts",
    "ParticipantInfo",
    "ReconcileResult",
    "batch_entity_edge_counts",
    "load_entity_detail_payload",
    "merge_entity_records",
    "patch_entity_record",
    "reconcile_scope_index_full",
    "resolve_thread_participants_batch",
    "split_entity_records",
]
