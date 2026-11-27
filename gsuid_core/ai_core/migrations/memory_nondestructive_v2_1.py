"""AI 记忆系统 v2.1 无损存量纠偏迁移（零物理删除真实记忆表）。

核心约束：
1. 五张真实记忆表（aimemepisode / aimementity / aimemedge / aimemthread / aimempreference）、
   Qdrant 三集合与 journals/ 手账 100% 零物理删除；
2. 唯一物理清理项仅限于 aicognode 派生索引中 2026-11-08 前已废止的旧版
   ('episode', 'observe') 悬空死行，以及指向已不存在实体的 env:<deleted_id> 悬空行
   （及其孤立 aicogedge / aicogattachment）；
3. 启动期仅执行原子备份 + 秒级纯 SQLite 原位纠偏（< 5s），不阻塞 WS 连接；
   约数百个补建说话人的向量嵌入在 Core 就绪（wait_ai_core_ready）后低优后台分批慢刷。
"""

from __future__ import annotations

import re
import json
import time
import uuid
import asyncio
import sqlite3
from typing import Optional, TypedDict
from pathlib import Path
from datetime import datetime, timezone, timedelta

from httpx import HTTPError as HttpxError
from sqlmodel import col, or_, func, delete, select
from sqlalchemy import select as sa_select, inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import CursorResult

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.database import base_models
from gsuid_core.ai_core.rag.qhttp import UnexpectedResponse, ResponseHandlingException
from gsuid_core.ai_core.cognition.hub import ENV_REF_PREFIX
from gsuid_core.utils.database.models import CoreUser
from gsuid_core.ai_core.cognition.nodes import AICogEdge, AICogNode, AICogAttachment
from gsuid_core.ai_core.memory.identity import is_human_speaker_id, is_valid_avatar_url
from gsuid_core.utils.database.write_gate import sqlite_write_gate
from gsuid_core.ai_core.memory.database.fts import index_episodes
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemThread,
    AIMemEpisode,
    _dump_str_list_json,
)
from gsuid_core.ai_core.memory.database.preference import AIMemPreference
from gsuid_core.ai_core.memory.lifecycle.sleep_extract import (
    THREAD_ARCHIVE_DAYS,
    THREAD_DORMANT_HOURS,
)

_MARKER_SCOPE = "system:migrations"
_MARKER_KEY = "memory_nondestructive_v2_1"
_LEGACY_EPISODE_CUTOFF_TS = int(datetime(2026, 11, 8, 0, 0, 0, tzinfo=timezone.utc).timestamp())
_VECTOR_BATCH_SIZE = 16
_PATCH_BATCH_SIZE = 64
_VECTOR_BATCH_SLEEP = 0.3
_DELETE_CHUNK = 500
_SCAN_BATCH = 500
_BACKFILL_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    SQLAlchemyError,
    HttpxError,
    UnexpectedResponse,
    ResponseHandlingException,
)

_NON_HUMAN_ENTITY_KINDS: frozenset[str] = frozenset({"item", "org", "location", "project", "concept", "event"})
_NON_HUMAN_TAGS: frozenset[str] = frozenset(
    {
        "item",
        "org",
        "organization",
        "location",
        "place",
        "project",
        "concept",
        "event",
        "tool",
        "game",
        "product",
        "物品",
        "机构",
        "组织",
        "地点",
        "项目",
        "概念",
        "事件",
        "工具",
        "游戏",
        "软件",
    }
)
_SPEAKER_HINT_RE = re.compile(
    r"(?:^|\n)(?:\[([^\[\]\n()]{1,40})\(([^()\s]{2,40})\)\]"
    r"|([^\[\]\n:：()]{1,40})\(([^()\s]{2,40})\))"
)

_backfill_task: Optional[asyncio.Task[None]] = None


class MigrationV21Report(TypedDict):
    """v2.1 无损迁移审计报告，持久化到 state_store 供 WebConsole 展示。"""

    version: str
    status: str
    migrated_at: str
    backup_path: str
    audit_log_path: str
    duration_ms: int
    malformed_self_scopes_fixed: int
    speaker_tags_cleaned: int
    speaker_avatars_backfilled: int
    non_human_speakers_demoted: int
    missing_speakers_created: int
    threads_backfilled: int
    threads_archived: int
    threads_dormant: int
    threads_active: int
    dangling_cog_episodes_cleaned: int
    dangling_cog_env_cleaned: int
    dangling_cog_edges_cleaned: int
    pending_vector_backfill_ids: list[str]
    pending_payload_patch_entity_ids: list[str]
    vector_backfill_done: int
    vector_backfill_total: int


def _parse_report(raw: object) -> Optional[MigrationV21Report]:
    if not isinstance(raw, dict):
        return None
    ver = raw["version"] if "version" in raw and isinstance(raw["version"], str) else ""
    if not ver:
        return None

    def _s(key: str) -> str:
        val = raw[key] if key in raw else ""
        return val if isinstance(val, str) else ""

    def _i(key: str) -> int:
        val = raw[key] if key in raw else 0
        return int(val) if isinstance(val, int) and not isinstance(val, bool) else 0

    raw_ids = raw["pending_vector_backfill_ids"] if "pending_vector_backfill_ids" in raw else []
    pending_ids = [str(x) for x in raw_ids if isinstance(x, str) and x] if isinstance(raw_ids, list) else []
    raw_patch_ids = raw["pending_payload_patch_entity_ids"] if "pending_payload_patch_entity_ids" in raw else []
    pending_patch_ids = (
        [str(x) for x in raw_patch_ids if isinstance(x, str) and x] if isinstance(raw_patch_ids, list) else []
    )
    return MigrationV21Report(
        version=ver,
        status=_s("status") or "completed",
        migrated_at=_s("migrated_at"),
        backup_path=_s("backup_path"),
        audit_log_path=_s("audit_log_path"),
        duration_ms=_i("duration_ms"),
        malformed_self_scopes_fixed=_i("malformed_self_scopes_fixed"),
        speaker_tags_cleaned=_i("speaker_tags_cleaned"),
        speaker_avatars_backfilled=_i("speaker_avatars_backfilled"),
        non_human_speakers_demoted=_i("non_human_speakers_demoted"),
        missing_speakers_created=_i("missing_speakers_created"),
        threads_backfilled=_i("threads_backfilled"),
        threads_archived=_i("threads_archived"),
        threads_dormant=_i("threads_dormant"),
        threads_active=_i("threads_active"),
        dangling_cog_episodes_cleaned=_i("dangling_cog_episodes_cleaned"),
        dangling_cog_env_cleaned=_i("dangling_cog_env_cleaned"),
        dangling_cog_edges_cleaned=_i("dangling_cog_edges_cleaned"),
        pending_vector_backfill_ids=pending_ids,
        pending_payload_patch_entity_ids=pending_patch_ids,
        vector_backfill_done=_i("vector_backfill_done"),
        vector_backfill_total=_i("vector_backfill_total"),
    )


async def get_migration_v2_1_report() -> Optional[MigrationV21Report]:
    """读取最近一次 v2.1 无损迁移审计报告（供 WebConsole 展示）。"""
    from gsuid_core.ai_core.state_store import state_get_value

    raw = await state_get_value(_MARKER_SCOPE, _MARKER_KEY)
    return _parse_report(raw)


@to_thread
def _backup_sqlite_atomic(src_path: Path, dst_path: Path) -> str:
    if dst_path.exists():
        return str(dst_path)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dst_path.with_suffix(".db.bak.tmp")
    src_conn = sqlite3.connect(str(src_path))
    dst_conn = sqlite3.connect(str(tmp_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    tmp_path.replace(dst_path)
    return str(dst_path)


@to_thread
def _write_audit_jsonl(path: Path, entries: list[dict[str, object]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return str(path)


def _fix_malformed_self_scope(scope_key: str) -> str:
    if not scope_key.startswith("self::"):
        return scope_key
    rest = scope_key[len("self::") :].strip()
    return f"self:default:{rest}" if rest else "self:default"


def _aware_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _extract_speaker_hints(content: str) -> dict[str, str]:
    hints: dict[str, str] = {}
    for m in _SPEAKER_HINT_RE.finditer(content):
        name = (m.group(1) or m.group(3) or "").strip()
        uid = (m.group(2) or m.group(4) or "").strip()
        if uid and name and name != uid and uid not in hints:
            hints[uid] = name[:80]
    return hints


def _clean_human_tags(tags: list[object]) -> tuple[list[str], bool]:
    cleaned: list[str] = []
    dirty = False
    for raw in tags:
        if not isinstance(raw, str):
            dirty = True
            continue
        val = raw.strip()
        if not val:
            dirty = True
            continue
        if val.casefold() in _NON_HUMAN_TAGS:
            dirty = True
            continue
        if val not in cleaned:
            cleaned.append(val)
    if "speaker" not in cleaned:
        cleaned.append("speaker")
    return cleaned, dirty


def _has_non_human_tag(tags: list[object]) -> bool:
    for raw in tags:
        if isinstance(raw, str) and raw.strip().casefold() in _NON_HUMAN_TAGS:
            return True
    return False


def _entity_qdrant_payload(row: AIMemEntity) -> dict[str, object]:
    return {
        "name": row.name,
        "summary": row.summary,
        "scope_key": row.scope_key,
        "is_speaker": row.is_speaker,
        "user_id": row.user_id,
        "tag": [str(x) for x in row.tag if isinstance(x, str)],
        "aliases": row.get_aliases(),
        "entity_kind": row.entity_kind,
        "avatar_url": row.avatar_url,
    }


async def _run_vector_backfill_background(
    entity_ids: list[str],
    patch_entity_ids: list[str],
) -> None:
    """Core 就绪后低优后台分批慢刷缺失说话人向量与 SQL 派生 payload 纠偏（可断点续跑）。"""
    from gsuid_core.ai_core.rag import base as rag_base
    from gsuid_core.ai_core.startup import wait_ai_core_ready
    from gsuid_core.ai_core.state_store import state_set_value
    from gsuid_core.ai_core.memory.vector.ops import (
        set_entity_payloads_batch,
        upsert_entity_vectors_batch,
    )

    try:
        await wait_ai_core_ready(timeout=600.0)
        logger.info(
            t(
                "log.memory.v2_1_vector_backfill_start",
                count=len(entity_ids),
                patches=len(patch_entity_ids),
            )
        )
        remaining_patches = list(patch_entity_ids)
        while remaining_patches:
            batch_ids = remaining_patches[:_PATCH_BATCH_SIZE]
            remaining_patches = remaining_patches[_PATCH_BATCH_SIZE:]
            async with base_models.async_maker() as session:
                rows = list(
                    (await session.execute(select(AIMemEntity).where(col(AIMemEntity.id).in_(batch_ids))))
                    .scalars()
                    .all()
                )
            if rows:
                batch_updates = [(row.qdrant_id or row.id, _entity_qdrant_payload(row)) for row in rows]
                ok = await set_entity_payloads_batch(batch_updates)
                if rag_base.client is not None and ok < len(batch_updates):
                    raise RuntimeError(f"entity payload patch incomplete: {ok}/{len(batch_updates)}")

            report = await get_migration_v2_1_report()
            if report is not None:
                report["pending_payload_patch_entity_ids"] = list(remaining_patches)
                if not remaining_patches and not report["pending_vector_backfill_ids"]:
                    report["status"] = "completed"
                await state_set_value(_MARKER_SCOPE, _MARKER_KEY, dict(report))

            if remaining_patches:
                await asyncio.sleep(_VECTOR_BATCH_SLEEP)

        done = 0
        remaining = list(entity_ids)
        while remaining:
            batch_ids = remaining[:_VECTOR_BATCH_SIZE]
            remaining = remaining[_VECTOR_BATCH_SIZE:]
            async with base_models.async_maker() as session:
                rows = list(
                    (await session.execute(select(AIMemEntity).where(col(AIMemEntity.id).in_(batch_ids))))
                    .scalars()
                    .all()
                )
            if rows:
                payloads: list[dict[str, object]] = [
                    {
                        "entity_id": row.qdrant_id or row.id,
                        **_entity_qdrant_payload(row),
                        "created_at_ts": _aware_utc(row.created_at).timestamp(),
                    }
                    for row in rows
                ]
                await upsert_entity_vectors_batch(payloads)
                done += len(rows)

            report = await get_migration_v2_1_report()
            if report is not None:
                report["pending_vector_backfill_ids"] = list(remaining)
                report["vector_backfill_done"] = report["vector_backfill_total"] - len(remaining)
                if not remaining and not report["pending_payload_patch_entity_ids"]:
                    report["status"] = "completed"
                await state_set_value(_MARKER_SCOPE, _MARKER_KEY, dict(report))

            if remaining:
                await asyncio.sleep(_VECTOR_BATCH_SLEEP)

        if not entity_ids and not patch_entity_ids:
            report = await get_migration_v2_1_report()
            if report is not None and report["status"] != "completed":
                report["status"] = "completed"
                await state_set_value(_MARKER_SCOPE, _MARKER_KEY, dict(report))
        logger.info(t("log.memory.v2_1_vector_backfill_done", done=done))
    except asyncio.CancelledError:
        raise
    except _BACKFILL_ERRORS as e:
        logger.warning(t("log.memory.v2_1_vector_backfill_fail", e=e))


def _spawn_backfill_task(
    entity_ids: list[str],
    patch_entity_ids: list[str],
) -> None:
    global _backfill_task
    if not entity_ids and not patch_entity_ids:
        return
    if _backfill_task is not None and not _backfill_task.done():
        return
    _backfill_task = asyncio.create_task(_run_vector_backfill_background(entity_ids, patch_entity_ids))


async def migrate_memory_nondestructive_v2_1(
    *,
    backup_db_path: Path | None = None,
    audit_log_path: Path | None = None,
    spawn_vector_backfill: bool = True,
) -> MigrationV21Report:
    """幂等执行 v2.1 无损存量纠偏迁移（启动期秒级完成 SQL，后台慢刷向量）。"""
    from gsuid_core.ai_core.state_store import state_set_value

    existing_report = await get_migration_v2_1_report()
    if existing_report is not None:
        if spawn_vector_backfill and (
            existing_report["pending_vector_backfill_ids"] or existing_report["pending_payload_patch_entity_ids"]
        ):
            _spawn_backfill_task(
                list(existing_report["pending_vector_backfill_ids"]),
                list(existing_report["pending_payload_patch_entity_ids"]),
            )
        return existing_report

    t0 = time.perf_counter()
    now_utc = datetime.now(timezone.utc)
    backup_str = ""
    if base_models._db_type == "sqlite" and ":memory:" not in str(base_models.db_url):
        src_db = base_models.DB_PATH
        if src_db.exists():
            dst_db = backup_db_path or (src_db.parent / "GsData_pre_v2_1.db.bak")
            backup_str = await _backup_sqlite_atomic(src_db, dst_db)
            logger.info(t("log.memory.v2_1_backup_done", path=backup_str))

    audit_path = audit_log_path or (get_res_path("ai_core") / "repair_audit_v2_1.jsonl")
    audit_entries: list[dict[str, object]] = []
    pending_patch_entity_ids: list[str] = []
    seen_patch_entity_ids: set[str] = set()
    pending_vector_ids: list[str] = []

    def _mark_payload_patch(entity_id: str) -> None:
        if entity_id and entity_id not in seen_patch_entity_ids:
            seen_patch_entity_ids.add(entity_id)
            pending_patch_entity_ids.append(entity_id)

    malformed_fixed = 0
    tags_cleaned = 0
    avatars_backfilled = 0
    non_human_demoted = 0
    speakers_created = 0
    threads_backfilled = 0
    threads_archived = 0
    threads_dormant = 0
    threads_active = 0
    cog_ep_cleaned = 0
    cog_env_cleaned = 0
    cog_edges_cleaned = 0

    async with sqlite_write_gate, base_models.async_maker() as session:
        conn = await session.connection()
        table_names = await conn.run_sync(lambda sc: set(sa_inspect(sc).get_table_names()))

        # 0. 加载 CoreUser 画像索引（同群优先，跨群回退）
        core_by_gid_uid: dict[tuple[str, str], tuple[str, str]] = {}
        core_by_uid: dict[str, tuple[str, str]] = {}
        core_known_names: set[str] = set()
        if "coreuser" in table_names:
            cu_rows = (
                await session.execute(
                    select(
                        col(CoreUser.user_id),
                        col(CoreUser.group_id),
                        col(CoreUser.user_name),
                        col(CoreUser.user_icon),
                    )
                )
            ).all()
            for uid_raw, gid_raw, name_raw, icon_raw in cu_rows:
                uid = str(uid_raw or "").strip()
                if not uid:
                    continue
                gid = str(gid_raw or "").strip()
                name = str(name_raw or "").strip()
                if name == "1":
                    name = ""
                icon = str(icon_raw or "").strip()
                avatar = icon if is_valid_avatar_url(icon) else ""
                if name:
                    core_known_names.add(name.casefold())
                if gid:
                    prev_n, prev_a = core_by_gid_uid[(gid, uid)] if (gid, uid) in core_by_gid_uid else ("", "")
                    core_by_gid_uid[(gid, uid)] = (name or prev_n, avatar or prev_a)
                prev_n2, prev_a2 = core_by_uid[uid] if uid in core_by_uid else ("", "")
                core_by_uid[uid] = (name or prev_n2, avatar or prev_a2)

        def _lookup_cu(scope_key: str, uid: str) -> tuple[str, str]:
            gid = scope_key[len("group:") :].strip() if scope_key.startswith("group:") else ""
            g_n, g_a = core_by_gid_uid[(gid, uid)] if (gid and (gid, uid) in core_by_gid_uid) else ("", "")
            u_n, u_a = core_by_uid[uid] if uid in core_by_uid else ("", "")
            return g_n or u_n, g_a or u_a

        # 1. 修复畸形 self::* scope_key（五表零删除）
        fixed_episodes: list[AIMemEpisode] = []
        if "aimemepisode" in table_names:
            bad_eps = list(
                (await session.execute(select(AIMemEpisode).where(col(AIMemEpisode.scope_key).startswith("self::"))))
                .scalars()
                .all()
            )
            for ep in bad_eps:
                old_sk = ep.scope_key
                new_sk = _fix_malformed_self_scope(old_sk)
                if new_sk != old_sk:
                    ep.scope_key = new_sk
                    session.add(ep)
                    fixed_episodes.append(ep)
                    malformed_fixed += 1
                    audit_entries.append(
                        {
                            "table": "aimemepisode",
                            "id": ep.id,
                            "action": "fix_self_scope",
                            "before": old_sk,
                            "after": new_sk,
                        }
                    )
            if fixed_episodes and "aimemepisode_fts" in table_names:
                await index_episodes(session, fixed_episodes)

        if "aimementity" in table_names:
            bad_ents = list(
                (await session.execute(select(AIMemEntity).where(col(AIMemEntity.scope_key).startswith("self::"))))
                .scalars()
                .all()
            )
            for ent in bad_ents:
                old_sk = ent.scope_key
                new_sk = _fix_malformed_self_scope(old_sk)
                if new_sk != old_sk:
                    clash = (
                        await session.execute(
                            select(col(AIMemEntity.id)).where(
                                col(AIMemEntity.scope_key) == new_sk, col(AIMemEntity.name) == ent.name
                            )
                        )
                    ).scalar_one_or_none()
                    if clash is None:
                        ent.scope_key = new_sk
                        session.add(ent)
                        malformed_fixed += 1
                        _mark_payload_patch(ent.id)
                        audit_entries.append(
                            {
                                "table": "aimementity",
                                "id": ent.id,
                                "action": "fix_self_scope",
                                "before": old_sk,
                                "after": new_sk,
                            }
                        )

        for tbl_name, model_cls in (
            ("aimemedge", AIMemEdge),
            ("aimemthread", AIMemThread),
            ("aimempreference", AIMemPreference),
        ):
            if tbl_name not in table_names:
                continue
            bad_rows = list(
                (await session.execute(select(model_cls).where(col(model_cls.scope_key).startswith("self::"))))
                .scalars()
                .all()
            )
            for row in bad_rows:
                old_sk = row.scope_key
                new_sk = _fix_malformed_self_scope(old_sk)
                if new_sk != old_sk:
                    row.scope_key = new_sk
                    session.add(row)
                    malformed_fixed += 1
                    audit_entries.append(
                        {"table": tbl_name, "id": row.id, "action": "fix_self_scope", "before": old_sk, "after": new_sk}
                    )

        # 2. 主键游标分批扫描 aimemepisode（仅投影轻字段，正文仅按需抽样查少量缺失昵称的 UID）
        live_episode_ids: set[str] = set()
        scope_speaker_uids: dict[str, set[str]] = {}
        all_episode_uids: set[str] = set()
        hint_names: dict[tuple[str, str], str] = {}
        sample_ep_for_hint: dict[tuple[str, str], str] = {}
        thread_ep_count: dict[str, int] = {}
        thread_ep_last: dict[str, datetime] = {}
        thread_participants: dict[str, set[str]] = {}

        if "aimemepisode" in table_names:
            last_ep_id = ""
            while True:
                ep_chunk = (
                    await session.execute(
                        sa_select(
                            col(AIMemEpisode.id),
                            col(AIMemEpisode.scope_key),
                            col(AIMemEpisode.speaker_ids),
                            col(AIMemEpisode.thread_id),
                            col(AIMemEpisode.valid_at),
                        )
                        .where(col(AIMemEpisode.id) > last_ep_id)
                        .order_by(col(AIMemEpisode.id).asc())
                        .limit(_SCAN_BATCH)
                    )
                ).all()
                if not ep_chunk:
                    break
                for ep_id, sk, spk_ids_raw, tid, valid_at in ep_chunk:
                    if not isinstance(ep_id, str):
                        continue
                    last_ep_id = ep_id
                    live_episode_ids.add(ep_id)
                    scope_str = str(sk or "")
                    human_uids: list[str] = []
                    if isinstance(spk_ids_raw, list):
                        for raw_uid in spk_ids_raw:
                            uid = str(raw_uid or "").strip()
                            if is_human_speaker_id(uid):
                                human_uids.append(uid)
                                all_episode_uids.add(uid)
                                if scope_str.startswith(("group:", "user_global:")):
                                    if scope_str not in scope_speaker_uids:
                                        scope_speaker_uids[scope_str] = set()
                                    scope_speaker_uids[scope_str].add(uid)
                                    if uid not in core_by_uid and (scope_str, uid) not in sample_ep_for_hint:
                                        sample_ep_for_hint[(scope_str, uid)] = ep_id
                    if isinstance(tid, str) and tid:
                        thread_ep_count[tid] = (thread_ep_count[tid] if tid in thread_ep_count else 0) + 1
                        if isinstance(valid_at, datetime):
                            aware_v = _aware_utc(valid_at)
                            if tid not in thread_ep_last or aware_v > thread_ep_last[tid]:
                                thread_ep_last[tid] = aware_v
                        if human_uids:
                            if tid not in thread_participants:
                                thread_participants[tid] = set()
                            thread_participants[tid].update(human_uids)

            sample_ids = sorted(set(sample_ep_for_hint.values()))
            for i in range(0, len(sample_ids), _DELETE_CHUNK):
                id_chunk = sample_ids[i : i + _DELETE_CHUNK]
                content_rows = (
                    await session.execute(
                        sa_select(col(AIMemEpisode.scope_key), col(AIMemEpisode.content)).where(
                            col(AIMemEpisode.id).in_(id_chunk)
                        )
                    )
                ).all()
                for sk_raw, content in content_rows:
                    scope_str = str(sk_raw or "")
                    if isinstance(content, str) and content:
                        extracted = _extract_speaker_hints(content)
                        for h_uid, h_name in extracted.items():
                            if (scope_str, h_uid) not in hint_names:
                                hint_names[(scope_str, h_uid)] = h_name

        # 3. 主键游标分批核验实体（原位纠偏异常说话人 + 回填头像 + 补建缺失说话人）
        live_entity_ids: set[str] = set()
        names_by_scope: dict[str, set[str]] = {}
        existing_speaker_pairs: set[tuple[str, str]] = set()
        unbind_candidate_ids: dict[tuple[str, str], str] = {}

        if "aimementity" in table_names:
            last_ent_id = ""
            while True:
                ent_chunk = list(
                    (
                        await session.execute(
                            select(AIMemEntity)
                            .where(col(AIMemEntity.id) > last_ent_id)
                            .order_by(col(AIMemEntity.id).asc())
                            .limit(_SCAN_BATCH)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not ent_chunk:
                    break
                last_ent_id = ent_chunk[-1].id
                for ent in ent_chunk:
                    live_entity_ids.add(ent.id)
                    if ent.scope_key not in names_by_scope:
                        names_by_scope[ent.scope_key] = set()
                    names_by_scope[ent.scope_key].add(ent.name)
                    uid = (ent.user_id or "").strip()
                    if uid:
                        existing_speaker_pairs.add((ent.scope_key, uid))
                    elif ent.entity_kind in ("speaker", "person"):
                        unbind_candidate_ids[(ent.scope_key, ent.name)] = ent.id

                    if not ent.is_speaker:
                        continue
                    raw_tags = list(ent.tag) if isinstance(ent.tag, list) else []
                    is_bad_uid = bool(uid) and not is_human_speaker_id(uid)
                    is_verified_human = (
                        bool(uid)
                        and is_human_speaker_id(uid)
                        and (uid in core_by_uid or uid in all_episode_uids or ent.entity_kind in ("speaker", "person"))
                    )
                    is_non_human_shape = (
                        ent.entity_kind in _NON_HUMAN_ENTITY_KINDS or _has_non_human_tag(raw_tags)
                    ) and ent.name.casefold() not in core_known_names

                    if is_bad_uid or (not uid and is_non_human_shape):
                        before_state = {"is_speaker": ent.is_speaker, "entity_kind": ent.entity_kind, "tag": raw_tags}
                        ent.is_speaker = False
                        if ent.entity_kind == "speaker" and _has_non_human_tag(raw_tags):
                            ent.entity_kind = "item"
                        ent.tag = [
                            t_item
                            for t_item in raw_tags
                            if not (isinstance(t_item, str) and t_item.lower() == "speaker")
                        ]
                        session.add(ent)
                        non_human_demoted += 1
                        if (ent.scope_key, ent.name) in unbind_candidate_ids:
                            del unbind_candidate_ids[(ent.scope_key, ent.name)]
                        _mark_payload_patch(ent.id)
                        audit_entries.append(
                            {
                                "table": "aimementity",
                                "id": ent.id,
                                "action": "demote_non_human_speaker",
                                "before": before_state,
                                "after": {"is_speaker": False, "entity_kind": ent.entity_kind, "tag": ent.tag},
                            }
                        )
                        continue

                    if is_verified_human or not is_non_human_shape:
                        patch: dict[str, object] = {}
                        cleaned_tags, was_dirty = _clean_human_tags(raw_tags)
                        if was_dirty or ent.entity_kind != "speaker":
                            before_state = {"entity_kind": ent.entity_kind, "tag": raw_tags}
                            ent.entity_kind = "speaker"
                            ent.tag = cleaned_tags
                            tags_cleaned += 1
                            patch["entity_kind"] = "speaker"
                            patch["tag"] = cleaned_tags
                            audit_entries.append(
                                {
                                    "table": "aimementity",
                                    "id": ent.id,
                                    "action": "clean_speaker_tags",
                                    "before": before_state,
                                    "after": {"entity_kind": "speaker", "tag": cleaned_tags},
                                }
                            )
                        if uid and not is_valid_avatar_url(ent.avatar_url):
                            _, cu_avatar = _lookup_cu(ent.scope_key, uid)
                            if cu_avatar:
                                ent.avatar_url = cu_avatar
                                avatars_backfilled += 1
                                patch["avatar_url"] = cu_avatar
                        if patch:
                            session.add(ent)
                            _mark_payload_patch(ent.id)
                await session.flush()
                session.expunge_all()

            # 补建缺失说话人（仅建 SQL 行，向量放到就绪后后台低优队列）
            for scope_str, uids in scope_speaker_uids.items():
                if scope_str not in names_by_scope:
                    names_by_scope[scope_str] = set()
                scope_names = names_by_scope[scope_str]
                for uid in sorted(uids):
                    if (scope_str, uid) in existing_speaker_pairs:
                        continue
                    cu_name, cu_avatar = _lookup_cu(scope_str, uid)
                    hint_name = hint_names[(scope_str, uid)] if (scope_str, uid) in hint_names else ""
                    base_name = (cu_name or hint_name or uid).strip()[:120]

                    # 若同 scope 已存在同名未绑定 UID 的人物节点，则原位绑定而非新建重复行
                    same_name_id = (
                        unbind_candidate_ids[(scope_str, base_name)]
                        if (scope_str, base_name) in unbind_candidate_ids
                        else None
                    )
                    same_name_ent = await session.get(AIMemEntity, same_name_id) if same_name_id else None
                    if (
                        same_name_ent is not None
                        and same_name_ent.user_id in (None, "")
                        and same_name_ent.entity_kind in ("speaker", "person")
                    ):
                        same_name_ent.user_id = uid
                        same_name_ent.is_speaker = True
                        same_name_ent.entity_kind = "speaker"
                        cleaned_tags, _ = _clean_human_tags(
                            list(same_name_ent.tag) if isinstance(same_name_ent.tag, list) else []
                        )
                        same_name_ent.tag = cleaned_tags
                        if cu_avatar and not is_valid_avatar_url(same_name_ent.avatar_url):
                            same_name_ent.avatar_url = cu_avatar
                            avatars_backfilled += 1
                        session.add(same_name_ent)
                        existing_speaker_pairs.add((scope_str, uid))
                        del unbind_candidate_ids[(scope_str, base_name)]
                        _mark_payload_patch(same_name_ent.id)
                        audit_entries.append(
                            {
                                "table": "aimementity",
                                "id": same_name_ent.id,
                                "action": "bind_existing_speaker_uid",
                                "before": {"user_id": None, "is_speaker": False},
                                "after": {"user_id": uid, "is_speaker": True},
                            }
                        )
                        continue

                    final_name = base_name
                    if final_name in scope_names:
                        suffix = uid[-4:] if len(uid) >= 4 else uid
                        cand = f"{base_name}({suffix})"
                        final_name = cand if cand not in scope_names else uid
                        if final_name in scope_names:
                            final_name = f"{uid}_{uuid.uuid4().hex[:6]}"
                    scope_names.add(final_name)
                    new_id = str(uuid.uuid4())
                    new_ent = AIMemEntity(
                        id=new_id,
                        scope_key=scope_str,
                        name=final_name,
                        summary="",
                        tag=["speaker"],
                        is_speaker=True,
                        entity_kind="speaker",
                        user_id=uid,
                        avatar_url=cu_avatar,
                        qdrant_id=new_id,
                        created_at=now_utc,
                        updated_at=now_utc,
                    )
                    session.add(new_ent)
                    live_entity_ids.add(new_id)
                    existing_speaker_pairs.add((scope_str, uid))
                    pending_vector_ids.append(new_id)
                    speakers_created += 1
                    if cu_avatar:
                        avatars_backfilled += 1
                    audit_entries.append(
                        {
                            "table": "aimementity",
                            "id": new_id,
                            "action": "create_missing_speaker",
                            "before": None,
                            "after": {"scope_key": scope_str, "name": final_name, "user_id": uid},
                        }
                    )
            await session.flush()
            session.expunge_all()

        # 4. 故事线主键游标分批回填 episode_count / participant_uids_json 与状态机流转（零删除、零解绑）
        thread_edge_count: dict[str, int] = {}
        if "aimemedge" in table_names:
            edge_cnt_rows = (
                await session.execute(
                    select(col(AIMemEdge.thread_id), func.count())
                    .where(col(AIMemEdge.thread_id).is_not(None))
                    .group_by(col(AIMemEdge.thread_id))
                )
            ).all()
            for tid_raw, cnt_raw in edge_cnt_rows:
                if isinstance(tid_raw, str) and tid_raw and isinstance(cnt_raw, int):
                    thread_edge_count[tid_raw] = cnt_raw

        if "aimemthread" in table_names:
            dormant_cutoff = now_utc - timedelta(hours=THREAD_DORMANT_HOURS)
            archive_cutoff = now_utc - timedelta(days=THREAD_ARCHIVE_DAYS)
            last_th_id = ""
            while True:
                th_chunk = list(
                    (
                        await session.execute(
                            select(AIMemThread)
                            .where(col(AIMemThread.id) > last_th_id)
                            .order_by(col(AIMemThread.id).asc())
                            .limit(_SCAN_BATCH)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not th_chunk:
                    break
                last_th_id = th_chunk[-1].id
                for th in th_chunk:
                    ep_cnt = thread_ep_count[th.id] if th.id in thread_ep_count else 0
                    edge_cnt = thread_edge_count[th.id] if th.id in thread_edge_count else 0
                    p_uids = sorted(thread_participants[th.id]) if th.id in thread_participants else []
                    last_active = thread_ep_last[th.id] if th.id in thread_ep_last else _aware_utc(th.last_at)

                    is_opener_waste = th.title_source == "opener" and (
                        ep_cnt <= 2 or edge_cnt == 0 or not (th.title or "").strip()
                    )
                    is_empty_shell = ep_cnt == 0 and edge_cnt == 0
                    if is_opener_waste or is_empty_shell or last_active < archive_cutoff:
                        new_status = "archived"
                        threads_archived += 1
                    elif last_active < dormant_cutoff:
                        new_status = "dormant"
                        threads_dormant += 1
                    else:
                        new_status = "active"
                        threads_active += 1

                    old_status = th.status
                    th.episode_count = ep_cnt
                    th.participant_uids_json = _dump_str_list_json(p_uids)
                    th.status = new_status
                    session.add(th)
                    threads_backfilled += 1
                    if old_status != new_status:
                        audit_entries.append(
                            {
                                "table": "aimemthread",
                                "id": th.id,
                                "action": "transition_thread_status",
                                "before": {"status": old_status, "title_source": th.title_source},
                                "after": {"status": new_status, "episode_count": ep_cnt, "participants": len(p_uids)},
                            }
                        )
                await session.flush()
                session.expunge_all()

        # 5. 派生索引 aicognode 精确清理：分批清理 11-08 前悬空 ('episode','observe') 与悬空 ent:/env:
        if "aicognode" in table_names:
            dead_ep_node_ids: list[int] = []
            last_nid = 0
            while True:
                ep_node_rows = (
                    await session.execute(
                        select(col(AICogNode.id), col(AICogNode.ref))
                        .where(
                            col(AICogNode.kind) == "episode",
                            col(AICogNode.source) == "observe",
                            col(AICogNode.created_at) < _LEGACY_EPISODE_CUTOFF_TS,
                            col(AICogNode.id) > last_nid,
                        )
                        .order_by(col(AICogNode.id).asc())
                        .limit(_SCAN_BATCH * 2)
                    )
                ).all()
                if not ep_node_rows:
                    break
                for nid_raw, ref_raw in ep_node_rows:
                    if isinstance(nid_raw, int):
                        last_nid = nid_raw
                        if isinstance(ref_raw, str) and ref_raw not in live_episode_ids:
                            dead_ep_node_ids.append(nid_raw)

            dead_env_node_ids: list[int] = []
            env_prefixes = (ENV_REF_PREFIX,) if ENV_REF_PREFIX == "env:" else (ENV_REF_PREFIX, "env:")
            last_nid = 0
            while True:
                env_node_rows = (
                    await session.execute(
                        select(col(AICogNode.id), col(AICogNode.ref))
                        .where(
                            col(AICogNode.kind) == "entity",
                            or_(
                                col(AICogNode.ref).startswith(ENV_REF_PREFIX),
                                col(AICogNode.ref).startswith("env:"),
                            ),
                            col(AICogNode.id) > last_nid,
                        )
                        .order_by(col(AICogNode.id).asc())
                        .limit(_SCAN_BATCH * 2)
                    )
                ).all()
                if not env_node_rows:
                    break
                for nid_raw, ref_raw in env_node_rows:
                    if isinstance(nid_raw, int):
                        last_nid = nid_raw
                        if isinstance(ref_raw, str):
                            matched_prefix = next((p for p in env_prefixes if ref_raw.startswith(p)), "")
                            target_eid = ref_raw[len(matched_prefix) :].strip() if matched_prefix else ""
                            if target_eid and target_eid not in live_entity_ids:
                                dead_env_node_ids.append(nid_raw)

            cog_ep_cleaned = len(dead_ep_node_ids)
            cog_env_cleaned = len(dead_env_node_ids)
            all_dead_cog_ids = dead_ep_node_ids + dead_env_node_ids

            if all_dead_cog_ids:
                for i in range(0, len(all_dead_cog_ids), _DELETE_CHUNK):
                    chunk = all_dead_cog_ids[i : i + _DELETE_CHUNK]
                    if "aicogedge" in table_names:
                        edge_res = await session.execute(
                            delete(AICogEdge).where(
                                (col(AICogEdge.src_id).in_(chunk)) | (col(AICogEdge.dst_id).in_(chunk))
                            )
                        )
                        cog_edges_cleaned += edge_res.rowcount if isinstance(edge_res, CursorResult) else 0
                    if "aicogattachment" in table_names:
                        await session.execute(delete(AICogAttachment).where(col(AICogAttachment.node_id).in_(chunk)))
                    await session.execute(delete(AICogNode).where(col(AICogNode.id).in_(chunk)))
                audit_entries.append(
                    {
                        "table": "aicognode",
                        "id": "batch",
                        "action": "clean_dangling_derived_index",
                        "before": {"dangling_episodes": cog_ep_cleaned, "dangling_env": cog_env_cleaned},
                        "after": {"deleted_nodes": len(all_dead_cog_ids), "deleted_edges": cog_edges_cleaned},
                    }
                )

        await session.commit()

    audit_log_str = await _write_audit_jsonl(audit_path, audit_entries)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    has_bg_work = bool(pending_vector_ids or pending_patch_entity_ids)
    status = "running_vector_backfill" if (has_bg_work and spawn_vector_backfill) else "completed"
    report = MigrationV21Report(
        version="2.1",
        status=status,
        migrated_at=now_utc.isoformat(),
        backup_path=backup_str,
        audit_log_path=audit_log_str,
        duration_ms=duration_ms,
        malformed_self_scopes_fixed=malformed_fixed,
        speaker_tags_cleaned=tags_cleaned,
        speaker_avatars_backfilled=avatars_backfilled,
        non_human_speakers_demoted=non_human_demoted,
        missing_speakers_created=speakers_created,
        threads_backfilled=threads_backfilled,
        threads_archived=threads_archived,
        threads_dormant=threads_dormant,
        threads_active=threads_active,
        dangling_cog_episodes_cleaned=cog_ep_cleaned,
        dangling_cog_env_cleaned=cog_env_cleaned,
        dangling_cog_edges_cleaned=cog_edges_cleaned,
        pending_vector_backfill_ids=list(pending_vector_ids),
        pending_payload_patch_entity_ids=list(pending_patch_entity_ids),
        vector_backfill_done=0,
        vector_backfill_total=len(pending_vector_ids),
    )
    await state_set_value(_MARKER_SCOPE, _MARKER_KEY, dict(report))

    logger.info(
        t(
            "log.memory.v2_1_sql_done",
            ms=duration_ms,
            speakers=speakers_created,
            demoted=non_human_demoted,
            threads=threads_backfilled,
            cog_cleaned=cog_ep_cleaned + cog_env_cleaned,
        )
    )

    if spawn_vector_backfill and has_bg_work:
        _spawn_backfill_task(list(pending_vector_ids), list(pending_patch_entity_ids))

    return report


__all__ = [
    "MigrationV21Report",
    "get_migration_v2_1_report",
    "migrate_memory_nondestructive_v2_1",
]
