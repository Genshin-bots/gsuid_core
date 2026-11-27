"""类酒馆零延迟人物锚点直注（<5ms 纯 SQL，无 LLM 调用）。

每轮入站按当前会话边界挂载：
- 群聊（group:{gid}）：100% 物理隔离本群，直注当前说话人精简卡 + 本句点名/@群友人物卡
  （含实锤近况、带来源口吻传闻、已否认矛盾对与关联活跃故事线标题）；
- 私聊（group_id=None）：单向只读汇聚「该用户本人」在各群的自身画像与亲口经历
  （附 [群:gid] 来源标签），绝不泄露同群任何其他成员。
"""

from __future__ import annotations

import json
from typing import Sequence
from datetime import datetime, timezone

from sqlmodel import col, or_, desc, select

from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.control.lines import (
    ANCHOR_LORE_HEADER,
    MEMORY_RUMOR_DENIED_TAG,
    MEMORY_RUMOR_UNCONFIRMED_TAG,
)
from gsuid_core.ai_core.memory.privacy import prefix_group_tag
from gsuid_core.ai_core.memory.identity import (
    is_human_speaker_id,
    match_member_entities_from_list,
)
from gsuid_core.ai_core.state_store.models import AIPersistentState
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.group_profile import (
    _PROFILE_SCOPE,
    _coerce_member_alias_ids,
)
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemThread,
)
from gsuid_core.ai_core.memory.ingestion.algebra import _DANGLING_FACT_RE
from gsuid_core.ai_core.memory.retrieval.prompt_render import (
    _sensitive_fact_hit,
    _get_sensitive_extra_terms,
)

_MAX_SPEAKER_FACTS = 4
_MAX_MENTIONED_MEMBERS = 3
_MAX_MENTIONED_FACTS = 3
_MAX_RUMORS_PER_MEMBER = 2
_MAX_THREADS_PER_MEMBER = 2


def _clean_fact(raw: str) -> str:
    text = " ".join((raw or "").split()).strip()
    if not text or _DANGLING_FACT_RE.search(text):
        return ""
    return text[:96]


def _format_member_card(
    entity: AIMemEntity,
    edges: Sequence[AIMemEdge],
    threads: Sequence[AIMemThread],
    *,
    is_current_speaker: bool,
    extra_sensitive: Sequence[str],
    is_private: bool = False,
) -> str:
    """渲染单个人物锚点卡；无有效信息返回空串。"""
    aliases = entity.get_aliases()
    verified: list[str] = []
    rumors: list[str] = []
    conflicts: list[str] = []
    seen_sig: set[str] = set()
    by_id: dict[str, AIMemEdge] = {e.id: e for e in edges}
    emitted_conflict_ids: set[str] = set()
    member_thread_ids: set[str] = set()

    for edge in edges:
        if edge.thread_id:
            member_thread_ids.add(edge.thread_id)
        if edge.invalid_at is not None:
            continue
        fact = _clean_fact(edge.fact)
        if not fact:
            continue
        if not is_current_speaker and _sensitive_fact_hit(fact, extra_sensitive):
            continue
        sig = fact.casefold()[:24]
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        rendered = prefix_group_tag(edge.scope_key, fact) if is_private else fact
        if edge.resolution_state == "active":
            if len(verified) < (_MAX_SPEAKER_FACTS if is_current_speaker else _MAX_MENTIONED_FACTS):
                verified.append(rendered)
        elif edge.resolution_state == "staging":
            if len(rumors) < _MAX_RUMORS_PER_MEMBER:
                rumors.append(f"{MEMORY_RUMOR_UNCONFIRMED_TAG}{rendered}")
        elif edge.resolution_state == "contradicted":
            if edge.id in emitted_conflict_ids:
                continue
            emitted_conflict_ids.add(edge.id)
            pair_text = rendered
            pid = edge.contradicts_edge_id or ""
            if pid and pid in by_id and pid not in emitted_conflict_ids:
                partner = by_id[pid]
                pfact = _clean_fact(partner.fact)
                if pfact and (is_current_speaker or not _sensitive_fact_hit(pfact, extra_sensitive)):
                    emitted_conflict_ids.add(pid)
                    p_rendered = prefix_group_tag(partner.scope_key, pfact) if is_private else pfact
                    pair_text = f"{rendered} ⇄ {p_rendered}"
            if len(conflicts) < _MAX_RUMORS_PER_MEMBER:
                conflicts.append(f"{MEMORY_RUMOR_DENIED_TAG}{pair_text}")

    linked_threads: list[str] = []
    uid = (entity.user_id or "").strip()
    for th in threads:
        if th.id in member_thread_ids or (uid and uid in th.get_participant_uids()):
            title = (th.title or "").strip()[:36]
            if not title:
                continue
            summary = (th.summary or "").strip()[:48]
            linked_threads.append(f"「{title}」({summary})" if summary else f"「{title}」")
            if len(linked_threads) >= _MAX_THREADS_PER_MEMBER:
                break

    if not aliases and not verified and not rumors and not conflicts and not linked_threads:
        return ""

    role_tag = "当前说话人" if is_current_speaker else "本句提及群友"
    uid_part = f"UID:{uid}" if uid else ""
    alias_part = f"别名:{'/'.join(aliases[:4])}" if aliases else ""
    meta = " · ".join(part for part in (uid_part, alias_part) if part)
    head = f"• [{role_tag}] {entity.name}（{meta}）" if meta else f"• [{role_tag}] {entity.name}"

    body_parts: list[str] = []
    if verified:
        body_parts.append("；".join(verified))
    if rumors:
        body_parts.append("；".join(rumors))
    if conflicts:
        body_parts.append("；".join(conflicts))
    if linked_threads:
        body_parts.append(f"相关故事线：{'、'.join(linked_threads)}")
    return f"{head}：{' | '.join(body_parts)}" if body_parts else head


async def _load_group_anchor_cards(
    scope_key: str,
    user_id: str,
    query: str,
    at_targets: Sequence[str],
    extra_sensitive: Sequence[str],
) -> list[str]:
    has_mention_hint = bool((query or "").strip() or at_targets)
    async with async_maker() as session:
        speakers: list[AIMemEntity] = []
        speaker: AIMemEntity | None = None
        if has_mention_hint:
            spk_stmt = (
                select(AIMemEntity)
                .where(col(AIMemEntity.scope_key) == scope_key, col(AIMemEntity.is_speaker).is_(True))
                .order_by(desc(col(AIMemEntity.updated_at)))
                .limit(400)
            )
            speakers = list((await session.execute(spk_stmt)).scalars().all())
            for spk in speakers:
                if spk.user_id == user_id:
                    speaker = spk
                    break
        if speaker is None:
            fb_stmt = (
                select(AIMemEntity)
                .where(col(AIMemEntity.scope_key) == scope_key, col(AIMemEntity.user_id) == user_id)
                .order_by(desc(col(AIMemEntity.is_speaker)), desc(col(AIMemEntity.updated_at)))
                .limit(1)
            )
            speaker = (await session.execute(fb_stmt)).scalar_one_or_none()

        alias_map: dict[str, list[str]] | None = None
        if (query or "").strip() and scope_key.startswith("group:"):
            st_stmt = select(col(AIPersistentState.value)).where(
                col(AIPersistentState.scope) == _PROFILE_SCOPE,
                col(AIPersistentState.state_key) == scope_key,
            )
            raw_json = (await session.execute(st_stmt)).scalar_one_or_none()
            if isinstance(raw_json, str) and raw_json:
                try:
                    parsed = json.loads(raw_json)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict) and "member_alias_ids" in parsed:
                    alias_map = _coerce_member_alias_ids(parsed["member_alias_ids"])

        mentioned = (
            match_member_entities_from_list(
                speakers,
                query,
                at_targets=at_targets,
                alias_map=alias_map,
                exclude_user_id=user_id,
                limit=_MAX_MENTIONED_MEMBERS,
            )
            if has_mention_hint
            else []
        )
        ordered_entities: list[tuple[AIMemEntity, bool]] = []
        seen_eids: set[str] = set()
        if speaker is not None:
            ordered_entities.append((speaker, True))
            seen_eids.add(speaker.id)
        for ent in mentioned:
            if ent.id not in seen_eids:
                ordered_entities.append((ent, False))
                seen_eids.add(ent.id)
        if not ordered_entities:
            return []

        eids = [ent.id for ent, _ in ordered_entities]
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        edge_stmt = (
            select(AIMemEdge)
            .where(
                col(AIMemEdge.scope_key) == scope_key,
                col(AIMemEdge.source_entity_id).in_(eids),
                col(AIMemEdge.invalid_at).is_(None),
                or_(col(AIMemEdge.expired_at).is_(None), col(AIMemEdge.expired_at) > now_utc),
                col(AIMemEdge.resolution_state).in_(["active", "staging", "contradicted"]),
            )
            .order_by(desc(col(AIMemEdge.mention_count)), desc(col(AIMemEdge.valid_at)))
            .limit(80)
        )
        edge_rows = list((await session.execute(edge_stmt)).scalars().all())
        th_stmt = (
            select(AIMemThread)
            .where(
                col(AIMemThread.scope_key) == scope_key,
                col(AIMemThread.status).in_(["active", "dormant"]),
            )
            .order_by(desc(col(AIMemThread.last_at)))
            .limit(20)
        )
        threads = list((await session.execute(th_stmt)).scalars().all())

    edges_by_src: dict[str, list[AIMemEdge]] = {}
    for row in edge_rows:
        edges_by_src.setdefault(row.source_entity_id, []).append(row)

    cards: list[str] = []
    for ent, is_cur in ordered_entities:
        card = _format_member_card(
            ent,
            edges_by_src[ent.id] if ent.id in edges_by_src else (),
            threads,
            is_current_speaker=is_cur,
            extra_sensitive=extra_sensitive,
            is_private=False,
        )
        if card:
            cards.append(card)
    return cards


async def _load_dm_anchor_cards(
    user_id: str,
    extra_sensitive: Sequence[str],
) -> list[str]:
    """私聊单向只读汇聚本人画像与亲口自述，严禁泄露同群他人信息。"""
    ug_scope = make_scope_key(ScopeType.USER_GLOBAL, user_id)

    async with async_maker() as session:
        ent_stmt = (
            select(AIMemEntity)
            .where(
                col(AIMemEntity.user_id) == user_id,
                or_(
                    col(AIMemEntity.scope_key) == ug_scope,
                    col(AIMemEntity.scope_key).startswith("group:"),
                ),
            )
            .order_by(desc(col(AIMemEntity.is_speaker)), desc(col(AIMemEntity.updated_at)))
            .limit(30)
        )
        own_entities = list((await session.execute(ent_stmt)).scalars().all())
        if not own_entities:
            return []
        own_eids = [e.id for e in own_entities]
        own_scopes = list({e.scope_key for e in own_entities})
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        edge_stmt = (
            select(AIMemEdge)
            .where(
                col(AIMemEdge.scope_key).in_(own_scopes),
                col(AIMemEdge.source_entity_id).in_(own_eids),
                col(AIMemEdge.invalid_at).is_(None),
                or_(col(AIMemEdge.expired_at).is_(None), col(AIMemEdge.expired_at) > now_utc),
                col(AIMemEdge.resolution_state).in_(["active", "contradicted"]),
            )
            .order_by(desc(col(AIMemEdge.mention_count)), desc(col(AIMemEdge.valid_at)))
            .limit(40)
        )
        own_edges = list((await session.execute(edge_stmt)).scalars().all())

    primary = own_entities[0]
    merged_aliases: list[str] = []
    seen_al: set[str] = {primary.name}
    for ent in own_entities:
        for cand in [ent.name, *ent.get_aliases()]:
            val = cand.strip()
            if val and val != user_id and val not in seen_al:
                seen_al.add(val)
                merged_aliases.append(val)

    proxy = AIMemEntity(
        id=primary.id,
        scope_key=ug_scope,
        name=primary.name,
        user_id=user_id,
        is_speaker=True,
        entity_kind="speaker",
        qdrant_id=primary.qdrant_id or primary.id,
    )
    proxy.set_aliases(merged_aliases)
    card = _format_member_card(
        proxy,
        own_edges,
        (),
        is_current_speaker=True,
        extra_sensitive=extra_sensitive,
        is_private=True,
    )
    return [card] if card else []


async def build_anchor_lore_block(
    user_id: str,
    group_id: str | None,
    query: str,
    *,
    at_targets: Sequence[str] = (),
    max_chars: int = 900,
) -> str:
    """构造 <5ms 纯 SQL 人物锚点直注块（零 LLM）；无有效档案返回空串。"""
    uid = (user_id or "").strip()
    if async_maker is None or not is_human_speaker_id(uid) or max_chars <= 0:
        return ""
    extra_sensitive = _get_sensitive_extra_terms()
    gid = (group_id or "").strip()
    if gid:
        cards = await _load_group_anchor_cards(
            make_scope_key(ScopeType.GROUP, gid),
            uid,
            query,
            at_targets,
            extra_sensitive,
        )
    else:
        cards = await _load_dm_anchor_cards(uid, extra_sensitive)
    if not cards:
        return ""

    out_lines: list[str] = [ANCHOR_LORE_HEADER]
    used = len(ANCHOR_LORE_HEADER)
    for card in cards:
        if used + len(card) + 1 > max_chars and len(out_lines) > 1:
            break
        room = max(48, max_chars - used - 1)
        clipped = card if len(card) <= room else (card[: room - 1] + "…")
        out_lines.append(clipped)
        used += len(clipped) + 1
    return "\n".join(out_lines) if len(out_lines) > 1 else ""


__all__ = ["build_anchor_lore_block"]
