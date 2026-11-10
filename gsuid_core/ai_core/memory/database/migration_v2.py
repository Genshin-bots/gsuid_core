"""schema_v2 一次性净室迁移（幂等）。

只重建记忆表：``AIMemPreference``、``UserFavorability``、``AIPersistentState``
与所有插件业务表原样保留。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import text as sql_text, inspect

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import get_res_path
from gsuid_core.utils.database.base_models import (
    DB_PATH,
    engine,
    _db_type,
    async_maker,
)

_SCHEMA_VERSION = 2
_MEMORY_ROOT = get_res_path("ai_core") / "memory"
_VERSION_FILE = _MEMORY_ROOT / "schema_version.json"
_BACKUP_DIR = _MEMORY_ROOT / "backup_v1"

_LEGACY_TABLES = (
    "mem_category_entity_members",
    "aimemcategoryedge",
    "aimemcategory",
    "aimemhierarchicalgraphmeta",
    "mem_episode_entity_mentions",
    "aimemconflict",
    "aimemevent",
    "aimemturngist",
    "aimemsession",
    "aimemedge",
    "aimemepisode",
    "aimementity",
    "aimemthread",
)

# 控制台列表按时间全局倒序，复合索引前导列是 scope_key，需要独立索引兜底
_QUERY_INDEXES = (
    ("aimemepisode", "ix_mem_episode_valid_at"),
    ("aimementity", "ix_mem_entity_updated_at"),
    ("aimemedge", "ix_mem_edge_valid_at"),
    ("aimemthread", "ix_mem_thread_last_at"),
)


def _read_version() -> int:
    if not _VERSION_FILE.exists():
        return 1
    try:
        data = json.loads(_VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 1
    if not isinstance(data, dict):
        return 1
    version = data["version"] if "version" in data else 0
    return version if isinstance(version, int) else 0


def _write_version() -> None:
    _MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {"version": _SCHEMA_VERSION, "migrated_at": datetime.now(timezone.utc).isoformat()}
    _VERSION_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _backup_sqlite() -> None:
    if _db_type != "sqlite" or not DB_PATH.exists():
        return
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = _BACKUP_DIR / "GsData_pre_v2.db.bak"
    if target.exists():
        return
    shutil.copy2(DB_PATH, target)
    logger.info(t("log.memory.schema_v2_backup", path=str(target)))


def _backup_group_profiles() -> None:
    source = _MEMORY_ROOT / "group_profiles"
    if not source.exists():
        return
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = _BACKUP_DIR / "group_profiles"
    if target.exists():
        return
    shutil.copytree(source, target)


async def _drop_legacy_tables() -> None:
    async with engine.begin() as conn:
        names = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        await conn.execute(sql_text("DROP TABLE IF EXISTS aimemepisode_fts"))
        for table in _LEGACY_TABLES:
            if table in names:
                await conn.execute(sql_text(f"DROP TABLE IF EXISTS {table}"))
        if "aimempreference" in names:
            await conn.execute(sql_text("UPDATE aimempreference SET source_episode_id = NULL"))


async def _create_new_tables() -> None:
    from gsuid_core.ai_core.memory.database import models

    async with engine.begin() as conn:
        await conn.run_sync(models.SQLModel.metadata.create_all)
    from gsuid_core.ai_core.memory.database.fts import ensure_fts_table

    async with async_maker() as session:
        await ensure_fts_table(session)
        await session.commit()


async def _ensure_query_indexes() -> None:
    from gsuid_core.ai_core.memory.database import models

    metadata = models.SQLModel.metadata
    indexes = [
        (table_name, index_name, index)
        for table_name, index_name in _QUERY_INDEXES
        for index in metadata.tables[table_name].indexes
        if index.name == index_name
    ]
    created = 0
    async with engine.begin() as conn:
        for table_name, index_name, index in indexes:
            exists = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_index(table_name, index_name))
            if exists:
                continue
            await conn.run_sync(lambda sync_conn: index.create(sync_conn))
            created += 1
    if created:
        logger.info(t("log.memory.query_indexes_created", count=created))


async def _clear_legacy_qdrant() -> None:
    from gsuid_core.ai_core.memory.vector.collections import LEGACY_MEMORY_COLLECTIONS

    try:
        from gsuid_core.ai_core.rag.base import client
        from gsuid_core.ai_core.rag.collection_migration import (
            remove_payload_backup,
            find_latest_payload_backup,
        )
    except ImportError as e:
        logger.debug(t("log.memory.schema_v2_qdrant_skip", e=e))
        return
    if client is None:
        return
    for name in LEGACY_MEMORY_COLLECTIONS:
        try:
            await client.delete_collection(collection_name=name)
        except Exception:
            pass
        backup = find_latest_payload_backup(name)
        if backup is not None:
            try:
                remove_payload_backup(backup, name)
            except OSError:
                pass


async def ensure_memory_schema_v2() -> None:
    """幂等入口：非 v2 执行净室迁移，v2 确保 FTS 虚表与查询索引存在。"""
    from gsuid_core.ai_core.memory.database.fts import ensure_fts_table
    from gsuid_core.ai_core.memory.database.models import AIMemPreference

    if _read_version() >= _SCHEMA_VERSION:
        async with async_maker() as session:
            await ensure_fts_table(session)
            await AIMemPreference.cleanup_polluted_scopes_in_session(session)
            await session.commit()
        await _ensure_query_indexes()
        return

    logger.info(t("log.memory.schema_v2_migrating"))
    if _db_type != "sqlite":
        # 非 SQLite 无自动备份，删表前必须让运维知道先手动备份
        logger.warning(t("log.memory.schema_v2_non_sqlite_no_backup", db_type=_db_type))
    _backup_sqlite()
    _backup_group_profiles()
    await _drop_legacy_tables()
    await _create_new_tables()
    async with async_maker() as session:
        await AIMemPreference.cleanup_polluted_scopes_in_session(session)
        await session.commit()
    await _ensure_query_indexes()
    await _clear_legacy_qdrant()
    try:
        from gsuid_core.ai_core.memory.group_profile import clear_learned_terms

        await clear_learned_terms()
    except (OSError, RuntimeError, ValueError) as e:
        logger.debug(t("log.memory.schema_v2_profile_cleanup_failed", e=e))
    _write_version()
    logger.info(t("log.memory.schema_v2_done"))


def backup_dir() -> Path:
    return _BACKUP_DIR
