"""确定性群成员身份脊柱（CoreUser + AIMemEntity + group_profile 双向同步）。

职责：
- 真实人类发言即按 ``(scope_key, user_id)`` 幂等维护唯一 ``speaker`` 实体并同步头像与名片；
- 双轨分级外号晋升（第一轨词形/数字/前缀变体直接并入；第二轨真名/外号需本人自称、显式 @UID
  或跨窗口 >= 2 次直接佐证后晋升）；
- 复用当前人格唤醒词防撞守卫与 ``PROTECTED_ALIASES`` 受保护权力称谓防提权守卫；
- 提供 ``< 5ms`` 纯 SQL 成员解析，供 Anchor Lore、``search_cognition`` 与 ``lookup_person_memory`` 复用。
"""

from __future__ import annotations

import re
import uuid
import unicodedata
from typing import Literal, Optional, Sequence
from datetime import datetime, timezone

from sqlmodel import col, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.models import CoreUser
from gsuid_core.utils.database.write_gate import sqlite_write_gate
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.models import (
    AIMemEntity,
    PendingAliasEntry,
    are_entity_kinds_compatible,
)

AliasTrack = Literal["direct", "self_claim", "at_mention", "corroboration"]
AliasOutcome = Literal["promoted", "pending", "blocked", "unchanged"]

_GENERIC_FORBIDDEN_ALIASES: frozenset[str] = frozenset(
    {
        "我",
        "你",
        "他",
        "她",
        "它",
        "我们",
        "你们",
        "他们",
        "大家",
        "群友",
        "群主",
        "成员",
        "用户",
        "网友",
        "朋友",
        "兄弟",
        "姐妹",
        "机器人",
        "助手",
        "系统",
        "bot",
        "assistant",
        "user",
        "system",
    }
)
_SUFFIX_STRIP_RE = re.compile(r"(?:[\d_\-\.\s~·•!！?？]+|[哥姐佬酱桑子总神宝爷叔姨]+)$")
_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def is_human_speaker_id(user_id: str | None) -> bool:
    """判断 speaker_id 是否属于真实人类账号（排除内部助手与系统哨兵）。"""
    uid = (user_id or "").strip()
    if not uid:
        return False
    low = uid.casefold()
    if low.startswith("__assistant_") or low in ("assistant", "system", "bot", "null", "none"):
        return False
    return True


def is_valid_avatar_url(url: str | None) -> bool:
    """过滤 CoreUser 默认占位值（如 '1' 或空串），仅保留可访问头像地址。"""
    val = (url or "").strip()
    if not val or val == "1":
        return False
    return val.startswith(("http://", "https://", "/api/"))


def _clean_display_name(raw: str | None) -> str:
    val = (raw or "").strip()
    if not val or val == "1":
        return ""
    return val[:120]


def _normalize_surface(text: str) -> str:
    norm = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in norm if not unicodedata.category(ch).startswith("C") and not ch.isspace()).casefold()


def is_protected_or_colliding_alias(
    alias: str,
    *,
    persona_name: str = "",
    caller_pm: int = 6,
) -> bool:
    """校验称呼是否撞当前人格唤醒词、泛指代词，或越权冒充受保护权力称谓。"""
    cleaned = (alias or "").strip()
    if not cleaned or len(cleaned) > 32:
        return True
    if _PUNCT_ONLY_RE.match(cleaned):
        return True
    norm = _normalize_surface(cleaned)
    if not norm or norm in _GENERIC_FORBIDDEN_ALIASES:
        return True

    from gsuid_core.ai_core.persona.settings import get_master_title
    from gsuid_core.ai_core.memory.group_profile import collect_persona_surfaces
    from gsuid_core.ai_core.buildin_tools.identity_tools import (
        _PROTECTED_NORMALIZED,
        _normalize_alias_for_guard,
    )

    guard_norm = _normalize_alias_for_guard(cleaned)
    protected = set(_PROTECTED_NORMALIZED)
    if persona_name:
        custom_title = get_master_title(persona_name)
        if custom_title:
            protected.add(_normalize_alias_for_guard(custom_title))
    if guard_norm in protected and caller_pm != 0:
        return True

    if persona_name:
        surfaces = collect_persona_surfaces(persona_name)
        blocked_surfaces = {_normalize_surface(s) for s in surfaces if s.strip()}
        if norm in blocked_surfaces:
            return True
    return False


def is_track1_morphological_variant(
    alias: str,
    canonical_name: str,
    existing_aliases: Sequence[str] = (),
) -> bool:
    """第一轨：无歧义词形 / 数字转写 / 常见前后缀变体可直接归入外号表。"""
    cand = _normalize_surface(alias)
    if len(cand) < 2:
        return False
    cand_stem = _SUFFIX_STRIP_RE.sub("", cand)
    bases = [canonical_name, *existing_aliases]
    for base_raw in bases:
        base = _normalize_surface(base_raw)
        if len(base) < 2:
            continue
        if cand == base:
            return True
        base_stem = _SUFFIX_STRIP_RE.sub("", base)
        if len(cand_stem) >= 2 and len(base_stem) >= 2 and cand_stem == base_stem:
            return True
        shorter, longer = (cand, base) if len(cand) <= len(base) else (base, cand)
        if len(shorter) >= 2 and shorter in longer and (len(shorter) / len(longer)) >= 0.5:
            return True
    return False


def _group_id_from_scope(scope_key: str) -> str:
    if scope_key.startswith("group:"):
        return scope_key[len("group:") :].strip()
    return ""


async def lookup_core_user_profile(
    user_id: str,
    group_id: str = "",
) -> tuple[str, str]:
    """查询单个用户的 CoreUser 昵称与头像（优先匹配同群记录）。"""
    uid = (user_id or "").strip()
    if not uid or async_maker is None:
        return "", ""
    stmt = select(col(CoreUser.group_id), col(CoreUser.user_name), col(CoreUser.user_icon)).where(
        col(CoreUser.user_id) == uid
    )
    async with async_maker() as session:
        rows = (await session.execute(stmt)).all()
    best_name = ""
    best_avatar = ""
    for gid_raw, name_raw, icon_raw in rows:
        gid = str(gid_raw or "").strip()
        name = _clean_display_name(str(name_raw) if name_raw is not None else "")
        icon = str(icon_raw or "").strip()
        avatar = icon if is_valid_avatar_url(icon) else ""
        if group_id and gid == group_id:
            if name:
                best_name = name
            if avatar:
                best_avatar = avatar
            if best_name and best_avatar:
                break
        else:
            if not best_name and name:
                best_name = name
            if not best_avatar and avatar:
                best_avatar = avatar
    return best_name, best_avatar


async def batch_lookup_core_users(
    user_ids: Sequence[str],
    group_id: str = "",
) -> dict[str, tuple[str, str]]:
    """批量查询 CoreUser 昵称与头像（单次 SQL，供列表/锚点/迁移复用）。"""
    uids = list({u.strip() for u in user_ids if u and u.strip()})
    if not uids or async_maker is None:
        return {}
    stmt = select(
        col(CoreUser.user_id),
        col(CoreUser.group_id),
        col(CoreUser.user_name),
        col(CoreUser.user_icon),
    ).where(col(CoreUser.user_id).in_(uids))
    async with async_maker() as session:
        rows = (await session.execute(stmt)).all()

    exact_map: dict[str, tuple[str, str]] = {}
    fallback_map: dict[str, tuple[str, str]] = {}
    for uid_raw, gid_raw, name_raw, icon_raw in rows:
        uid = str(uid_raw or "").strip()
        if not uid:
            continue
        gid = str(gid_raw or "").strip()
        name = _clean_display_name(str(name_raw) if name_raw is not None else "")
        icon = str(icon_raw or "").strip()
        avatar = icon if is_valid_avatar_url(icon) else ""
        target = exact_map if (group_id and gid == group_id) else fallback_map
        prev_name, prev_avatar = target[uid] if uid in target else ("", "")
        target[uid] = (name or prev_name, avatar or prev_avatar)

    out: dict[str, tuple[str, str]] = {}
    for uid in uids:
        e_name, e_avatar = exact_map[uid] if uid in exact_map else ("", "")
        f_name, f_avatar = fallback_map[uid] if uid in fallback_map else ("", "")
        out[uid] = (e_name or f_name, e_avatar or f_avatar)
    return out


async def _find_available_entity_name(
    session: AsyncSession,
    scope_key: str,
    desired: str,
    user_id: str,
    *,
    exclude_entity_id: str = "",
) -> str:
    """确保 (scope_key, name) 唯一约束不冲突；撞名时追加 UID 后缀。"""
    candidates = [desired, f"{desired}({user_id[-4:]})" if len(user_id) >= 4 else f"{desired}({user_id})", user_id]
    for cand in candidates:
        cand_clean = cand.strip()[:120]
        if not cand_clean:
            continue
        stmt = select(col(AIMemEntity.id)).where(
            col(AIMemEntity.scope_key) == scope_key,
            col(AIMemEntity.name) == cand_clean,
        )
        owner_id = (await session.execute(stmt)).scalar_one_or_none()
        if owner_id is None or (exclude_entity_id and str(owner_id) == exclude_entity_id):
            return cand_clean
    return f"{user_id}_{uuid.uuid4().hex[:6]}"


async def ensure_speaker_entity(
    scope_key: str,
    user_id: str,
    *,
    display_name: str = "",
    avatar_url: str = "",
    persona_name: str = "",
    caller_pm: int = 6,
    sync_vector: bool = False,
) -> Optional[AIMemEntity]:
    """按 ``(scope_key, user_id)`` 幂等维护唯一 speaker 实体并同步名片与头像。"""
    uid = (user_id or "").strip()
    if async_maker is None or not scope_key or not is_human_speaker_id(uid):
        return None

    gid = _group_id_from_scope(scope_key)
    name = _clean_display_name(display_name)
    avatar = avatar_url.strip() if is_valid_avatar_url(avatar_url) else ""
    if not name or not avatar:
        cu_name, cu_avatar = await lookup_core_user_profile(uid, gid)
        if not name:
            name = cu_name
        if not avatar:
            avatar = cu_avatar

    resolved_name = name or uid
    created_or_renamed = False
    payload_dirty = False
    target_id = ""
    target_entity: Optional[AIMemEntity] = None

    async with sqlite_write_gate, async_maker() as session:
        stmt_uid = (
            select(AIMemEntity)
            .where(col(AIMemEntity.scope_key) == scope_key, col(AIMemEntity.user_id) == uid)
            .order_by(desc(col(AIMemEntity.is_speaker)), desc(col(AIMemEntity.updated_at)))
            .limit(1)
        )
        existing = (await session.execute(stmt_uid)).scalar_one_or_none()
        now = datetime.now(timezone.utc)

        if existing is not None:
            changed = False
            if not existing.is_speaker:
                existing.is_speaker = True
                changed = True
                payload_dirty = True
            if existing.entity_kind != "speaker" and are_entity_kinds_compatible("speaker", existing.entity_kind):
                existing.entity_kind = "speaker"
                changed = True
                payload_dirty = True
            if avatar and existing.avatar_url != avatar:
                existing.avatar_url = avatar
                changed = True
                payload_dirty = True
            tags = [str(x) for x in existing.tag if isinstance(x, str)] if isinstance(existing.tag, list) else []
            if "speaker" not in tags:
                tags.append("speaker")
                existing.tag = tags
                changed = True
                payload_dirty = True
            if (
                name
                and name != existing.name
                and name != uid
                and not is_protected_or_colliding_alias(name, persona_name=persona_name, caller_pm=caller_pm)
            ):
                safe_name = await _find_available_entity_name(
                    session, scope_key, name, uid, exclude_entity_id=existing.id
                )
                if safe_name != existing.name:
                    old_name = existing.name.strip()
                    aliases = existing.get_aliases()
                    if (
                        old_name
                        and old_name != uid
                        and old_name not in aliases
                        and not is_protected_or_colliding_alias(
                            old_name, persona_name=persona_name, caller_pm=caller_pm
                        )
                    ):
                        aliases.append(old_name)
                        existing.set_aliases(aliases)
                    existing.name = safe_name
                    changed = True
                    created_or_renamed = True
            if changed:
                existing.updated_at = now
                session.add(existing)
                await session.commit()
                await session.refresh(existing)
            target_entity = existing
            target_id = existing.id
        else:
            stmt_name = (
                select(AIMemEntity)
                .where(col(AIMemEntity.scope_key) == scope_key, col(AIMemEntity.name) == resolved_name)
                .limit(1)
            )
            by_name = (await session.execute(stmt_name)).scalar_one_or_none()
            if (
                by_name is not None
                and by_name.user_id in (None, "", uid)
                and are_entity_kinds_compatible("speaker", by_name.entity_kind)
            ):
                by_name.user_id = uid
                by_name.is_speaker = True
                by_name.entity_kind = "speaker"
                if avatar:
                    by_name.avatar_url = avatar
                tags = [str(x) for x in by_name.tag if isinstance(x, str)] if isinstance(by_name.tag, list) else []
                if "speaker" not in tags:
                    tags.append("speaker")
                by_name.tag = tags
                by_name.updated_at = now
                session.add(by_name)
                await session.commit()
                await session.refresh(by_name)
                target_entity = by_name
                target_id = by_name.id
                created_or_renamed = True
            else:
                safe_name = await _find_available_entity_name(session, scope_key, resolved_name, uid)
                new_id = str(uuid.uuid4())
                entity = AIMemEntity(
                    id=new_id,
                    scope_key=scope_key,
                    name=safe_name,
                    summary="",
                    tag=["speaker"],
                    is_speaker=True,
                    entity_kind="speaker",
                    user_id=uid,
                    avatar_url=avatar,
                    qdrant_id=new_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(entity)
                await session.commit()
                await session.refresh(entity)
                target_entity = entity
                target_id = entity.id
                created_or_renamed = True

    if sync_vector and target_entity is not None and target_id:
        if created_or_renamed:
            from gsuid_core.ai_core.memory.vector.ops import upsert_entity_vector

            await upsert_entity_vector(
                entity_id=target_entity.id,
                name=target_entity.name,
                summary=target_entity.summary,
                scope_key=target_entity.scope_key,
                is_speaker=target_entity.is_speaker,
                user_id=target_entity.user_id,
                tag=[str(x) for x in target_entity.tag if isinstance(x, str)],
                aliases=target_entity.get_aliases(),
                entity_kind=target_entity.entity_kind,
                avatar_url=target_entity.avatar_url,
            )
        elif payload_dirty:
            from gsuid_core.ai_core.memory.vector.ops import set_entity_payload

            await set_entity_payload(
                target_entity.qdrant_id or target_entity.id,
                {
                    "is_speaker": target_entity.is_speaker,
                    "entity_kind": target_entity.entity_kind,
                    "tag": [str(x) for x in target_entity.tag if isinstance(x, str)],
                    "avatar_url": target_entity.avatar_url,
                },
            )
    return target_entity


async def record_entity_alias(
    scope_key: str,
    user_id: str,
    alias: str,
    *,
    track: AliasTrack = "corroboration",
    window_id: str = "",
    persona_name: str = "",
    caller_pm: int = 6,
    sync_group_profile: bool = True,
) -> AliasOutcome:
    """双轨外号登记：过防提权与防撞唤醒词守卫后写入实体表，并双向同步 group_profile。"""
    uid = (user_id or "").strip()
    cleaned = (alias or "").strip()
    if async_maker is None or not scope_key or not is_human_speaker_id(uid):
        return "blocked"
    if is_protected_or_colliding_alias(cleaned, persona_name=persona_name, caller_pm=caller_pm):
        return "blocked"

    entity = await ensure_speaker_entity(
        scope_key,
        uid,
        persona_name=persona_name,
        caller_pm=caller_pm,
        sync_vector=False,
    )
    if entity is None:
        return "blocked"

    outcome: AliasOutcome = "unchanged"
    updated_aliases: list[str] = []
    entity_id = entity.id

    async with sqlite_write_gate, async_maker() as session:
        row = await session.get(AIMemEntity, entity_id)
        if row is None:
            return "blocked"
        aliases = row.get_aliases()
        if cleaned == row.name or cleaned in aliases:
            updated_aliases = aliases
            outcome = "unchanged"
        else:
            direct_promote = track in ("direct", "self_claim", "at_mention") or is_track1_morphological_variant(
                cleaned, row.name, aliases
            )
            pending = row.get_pending_aliases()
            if direct_promote:
                aliases.append(cleaned)
                row.set_aliases(aliases)
                row.set_pending_aliases([p for p in pending if p["alias"] != cleaned])
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                await session.commit()
                updated_aliases = row.get_aliases()
                outcome = "promoted"
            else:
                win_tag = window_id.strip()
                matched: Optional[PendingAliasEntry] = None
                for item in pending:
                    if item["alias"] == cleaned:
                        matched = item
                        break
                if matched is None:
                    new_entry = PendingAliasEntry(alias=cleaned, hits=1, windows=[win_tag] if win_tag else [])
                    pending.append(new_entry)
                    row.set_pending_aliases(pending)
                    row.updated_at = datetime.now(timezone.utc)
                    session.add(row)
                    await session.commit()
                    outcome = "pending"
                else:
                    if win_tag and win_tag not in matched["windows"]:
                        matched["windows"].append(win_tag)
                        matched["hits"] += 1
                    elif not win_tag:
                        matched["hits"] += 1
                    if matched["hits"] >= 2:
                        aliases.append(cleaned)
                        row.set_aliases(aliases)
                        row.set_pending_aliases([p for p in pending if p["alias"] != cleaned])
                        outcome = "promoted"
                    else:
                        row.set_pending_aliases(pending)
                        outcome = "pending"
                    row.updated_at = datetime.now(timezone.utc)
                    session.add(row)
                    await session.commit()
                    updated_aliases = row.get_aliases()

    if outcome == "promoted":
        from gsuid_core.ai_core.memory.vector.ops import set_entity_payload

        await set_entity_payload(entity_id, {"aliases": updated_aliases})
        if sync_group_profile and scope_key.startswith("group:"):
            from gsuid_core.ai_core.memory.group_profile import record_member_alias

            await record_member_alias(scope_key, cleaned, uid)
    elif outcome == "unchanged" and sync_group_profile and scope_key.startswith("group:"):
        from gsuid_core.ai_core.memory.group_profile import get_member_aliases, record_member_alias

        gp_aliases = await get_member_aliases(scope_key)
        existing_uids = gp_aliases[cleaned] if cleaned in gp_aliases else []
        if uid not in existing_uids:
            await record_member_alias(scope_key, cleaned, uid)

    return outcome


async def sync_group_profile_alias_to_entity(
    scope_key: str,
    alias: str,
    user_id: str,
    *,
    persona_name: str = "",
    caller_pm: int = 6,
) -> None:
    """将 ``remember_user_alias`` 写入的确定性称呼实时同步至图谱实体的 aliases_json。"""
    await record_entity_alias(
        scope_key=scope_key,
        user_id=user_id,
        alias=alias,
        track="direct",
        persona_name=persona_name,
        caller_pm=caller_pm,
        sync_group_profile=False,
    )


def match_member_entities_from_list(
    speakers: Sequence[AIMemEntity],
    query_text: str,
    *,
    at_targets: Sequence[str] = (),
    alias_map: dict[str, list[str]] | None = None,
    exclude_user_id: str = "",
    limit: int = 4,
) -> list[AIMemEntity]:
    """纯内存按 @UID → 群称呼表 → 实体名/别名 优先级匹配群成员（供单会话锚点复用）。"""
    if not speakers or limit <= 0:
        return []

    by_uid: dict[str, AIMemEntity] = {}
    for spk in speakers:
        if spk.user_id and spk.user_id not in by_uid:
            by_uid[spk.user_id] = spk

    picked: list[AIMemEntity] = []
    seen_ids: set[str] = set()
    excl = exclude_user_id.strip()

    def _add(ent: AIMemEntity) -> None:
        if ent.id in seen_ids:
            return
        if excl and ent.user_id and ent.user_id == excl:
            return
        seen_ids.add(ent.id)
        picked.append(ent)

    # 1. 显式 @UID 命中
    for raw_uid in at_targets:
        uid = str(raw_uid or "").strip()
        if uid and uid in by_uid:
            _add(by_uid[uid])
            if len(picked) >= limit:
                return picked

    text = (query_text or "").strip()
    if not text:
        return picked
    text_cf = text.casefold()

    # 2. 确定性群称呼表（group_profile.member_alias_ids）命中
    if alias_map:
        for alias in sorted(alias_map.keys(), key=len, reverse=True):
            if len(alias) < 2:
                continue
            if alias.casefold() in text_cf:
                for uid in alias_map[alias]:
                    if uid in by_uid:
                        _add(by_uid[uid])
                        if len(picked) >= limit:
                            return picked

    # 3. 实体名与 aliases_json 命中（长词优先，避免单字误撞）
    surface_pairs: list[tuple[str, AIMemEntity]] = []
    for spk in speakers:
        if spk.name and len(spk.name.strip()) >= 2:
            surface_pairs.append((spk.name.strip(), spk))
        for al in spk.get_aliases():
            if len(al) >= 2:
                surface_pairs.append((al, spk))
    surface_pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    for surface, spk in surface_pairs:
        if surface.casefold() in text_cf:
            _add(spk)
            if len(picked) >= limit:
                return picked

    return picked


async def resolve_member_entities(
    scope_key: str,
    query_text: str,
    *,
    at_targets: Sequence[str] = (),
    exclude_user_id: str = "",
    limit: int = 4,
) -> list[AIMemEntity]:
    """<5ms 纯 SQL 解析消息中被 @ 或点名提及的群成员实体（按匹配优先级排序）。"""
    if not scope_key or limit <= 0:
        return []
    speakers = await AIMemEntity.list_speakers_by_scope(scope_key, limit=400)
    if not speakers:
        return []

    alias_map: dict[str, list[str]] | None = None
    if (query_text or "").strip() and scope_key.startswith("group:"):
        from gsuid_core.ai_core.memory.group_profile import get_member_aliases

        alias_map = await get_member_aliases(scope_key)
    return match_member_entities_from_list(
        speakers,
        query_text,
        at_targets=at_targets,
        alias_map=alias_map,
        exclude_user_id=exclude_user_id,
        limit=limit,
    )


async def resolve_member_by_query(
    scope_key: str,
    person_query: str,
    *,
    limit: int = 5,
) -> list[AIMemEntity]:
    """按昵称 / 外号 / UID 精确或子串解析人物实体（含 speaker 与未绑定 UID 的 person）。"""
    q = (person_query or "").strip()
    if not scope_key or not q or limit <= 0:
        return []
    q_cf = q.casefold()
    q_norm = _normalize_surface(q)

    async with async_maker() as session:
        stmt = (
            select(AIMemEntity)
            .where(
                col(AIMemEntity.scope_key) == scope_key,
                col(AIMemEntity.entity_kind).in_(["speaker", "person"]),
            )
            .order_by(desc(col(AIMemEntity.is_speaker)), desc(col(AIMemEntity.updated_at)))
            .limit(500)
        )
        candidates = list((await session.execute(stmt)).scalars().all())

    if not candidates:
        return []

    gp_uid_hits: set[str] = set()
    if scope_key.startswith("group:"):
        from gsuid_core.ai_core.memory.group_profile import get_member_aliases

        alias_map = await get_member_aliases(scope_key)
        for alias, uids in alias_map.items():
            al_cf = alias.casefold()
            if al_cf == q_cf or (len(alias) >= 2 and (al_cf in q_cf or q_cf in al_cf)):
                gp_uid_hits.update(uids)

    scored: list[tuple[int, AIMemEntity]] = []
    for ent in candidates:
        score = 0
        if ent.user_id and ent.user_id == q:
            score = 100
        elif ent.user_id and ent.user_id in gp_uid_hits:
            score = 95
        elif ent.name.casefold() == q_cf or _normalize_surface(ent.name) == q_norm:
            score = 92
        else:
            aliases = ent.get_aliases()
            if any(a.casefold() == q_cf or _normalize_surface(a) == q_norm for a in aliases):
                score = 88
            elif len(q) >= 2 and (q_cf in ent.name.casefold() or any(q_cf in a.casefold() for a in aliases)):
                score = 72
            elif len(ent.name) >= 2 and ent.name.casefold() in q_cf:
                score = 68
            elif any(len(a) >= 2 and a.casefold() in q_cf for a in aliases):
                score = 65
        if score > 0:
            if ent.is_speaker:
                score += 3
            scored.append((score, ent))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [ent for _, ent in scored[:limit]]


__all__ = [
    "AliasOutcome",
    "AliasTrack",
    "batch_lookup_core_users",
    "ensure_speaker_entity",
    "is_human_speaker_id",
    "is_protected_or_colliding_alias",
    "is_track1_morphological_variant",
    "is_valid_avatar_url",
    "lookup_core_user_profile",
    "match_member_entities_from_list",
    "record_entity_alias",
    "resolve_member_by_query",
    "resolve_member_entities",
    "sync_group_profile_alias_to_entity",
]
