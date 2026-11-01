"""关系温度表的迁移：去重 + 唯一约束升级到 (user_id, bot_id, persona_name)。

加列走 ``utils/database/startup.exec_list``（幂等 DDL，与其它 AI 表同一机制）。
去重必须在建唯一索引之前跑，且要有日志——静默失败会让日预算被重复行绕过。

旧 (user_id,bot_id) 两列唯一约束必须按方言拆除：SQLite 内联 UNIQUE 只能重建表；
迁移幂等，失败仅告警不阻断启动。

迁移挂 ``on_core_start_before``（阻塞阶段），不是 ``on_core_start``：
后者在 WS 启动后的后台阶段，期间已可能有聊天进来写这张表。
"""

import re

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.server import on_core_start_before

_TABLE = "userfavorability"
_LEGACY_TABLE = "userfavorability_legacy_v1"
_OLD_UNIQUE_COLS = ("user_id", "bot_id")
_NEW_UNIQUE_NAME = "ux_userfavorability_user_bot_persona"
_NEW_UNIQUE_COLS = ("user_id", "bot_id", "persona_name")


def _sqlite_unique_indexes(conn: Connection, table: str) -> list[tuple[str, str, list[str]]]:
    """(name, origin, columns)，仅 UNIQUE；origin 'c'=CREATE INDEX 可 DROP，'u'=内联只能重建表。"""
    found: list[tuple[str, str, list[str]]] = []
    for row in conn.execute(text(f"PRAGMA index_list('{table}')")).fetchall():
        name, unique, origin = str(row[1]), row[2], str(row[3])
        if not unique:
            continue
        cols = [str(r[2]) for r in conn.execute(text(f"PRAGMA index_info('{name}')")).fetchall()]
        found.append((name, origin, cols))
    return found


def _sqlite_all_explicit_indexes(conn: Connection, table: str) -> list[str]:
    """显式 CREATE INDEX 的名字（含非唯一）。SQLite 索引名全局唯一，重建表时必须先删。"""
    names: list[str] = []
    for row in conn.execute(text(f"PRAGMA index_list('{table}')")).fetchall():
        if str(row[3]) == "c":
            names.append(str(row[1]))
    return names


def _sqlite_rebuild_table(conn: Connection) -> None:
    """内联 UNIQUE 不可 DROP：rename 旧表 → 按模型重建 → 复制共有列 → 删旧表。"""
    from gsuid_core.ai_core.database.models import UserFavorability

    conn.execute(text(f"ALTER TABLE {_TABLE} RENAME TO {_LEGACY_TABLE}"))
    # SQLite 索引名全局唯一：旧表上的显式索引（含非唯一）必须先删，否则与新表索引名冲突
    for name in _sqlite_all_explicit_indexes(conn, _LEGACY_TABLE):
        conn.execute(text(f'DROP INDEX "{name}"'))
    # §3.1.1 全小写表名索引 metadata.tables（__table__ 对 pyright 不可见）
    table = UserFavorability.metadata.tables[_TABLE]
    table.create(conn)
    legacy_cols = {str(r[1]) for r in conn.execute(text(f"PRAGMA table_info('{_LEGACY_TABLE}')")).fetchall()}
    common = [c.name for c in table.columns if c.name in legacy_cols]
    collist = ", ".join(f'"{c}"' for c in common)
    conn.execute(text(f"INSERT INTO {_TABLE} ({collist}) SELECT {collist} FROM {_LEGACY_TABLE}"))
    conn.execute(text(f"DROP TABLE {_LEGACY_TABLE}"))


def _has_unique_on(conn: Connection, table: str, cols: tuple[str, ...], dialect: str) -> bool:
    wanted = sorted(cols)
    if dialect == "sqlite":
        return any(sorted(c) == wanted for _n, _o, c in _sqlite_unique_indexes(conn, table))
    if dialect == "mysql":
        rows = conn.execute(
            text(
                "SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS cols "
                "FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND NON_UNIQUE = 0 "
                "GROUP BY INDEX_NAME"
            ),
            {"t": table},
        ).fetchall()
        return any(sorted(str(r[1]).split(",")) == wanted for r in rows)
    if dialect == "postgresql":
        rows = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexdef ILIKE '%UNIQUE%'"),
            {"t": table},
        ).fetchall()
        for row in rows:
            match = re.search(r"\(([^)]+)\)", str(row[0]))
            if match and sorted(c.strip().strip('"') for c in match.group(1).split(",")) == wanted:
                return True
    return False


def _migrate_schema_sync(conn: Connection, dialect: str) -> None:
    if dialect == "sqlite":
        for name, origin, cols in _sqlite_unique_indexes(conn, _TABLE):
            if sorted(cols) != sorted(_OLD_UNIQUE_COLS):
                continue
            if origin == "c":
                conn.execute(text(f'DROP INDEX "{name}"'))
            elif origin == "u":
                _sqlite_rebuild_table(conn)
                break
    elif dialect == "postgresql":
        conn.execute(text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS ux_userfavorability_user_bot"))
        conn.execute(text("DROP INDEX IF EXISTS ux_userfavorability_user_bot"))
    elif dialect == "mysql":
        exists = conn.execute(
            text(
                "SELECT COUNT(1) FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
                "AND INDEX_NAME = 'ux_userfavorability_user_bot'"
            ),
            {"t": _TABLE},
        ).scalar()
        if exists:
            conn.execute(text(f"ALTER TABLE {_TABLE} DROP INDEX ux_userfavorability_user_bot"))
    if not _has_unique_on(conn, _TABLE, _NEW_UNIQUE_COLS, dialect):
        conn.execute(text(f"CREATE UNIQUE INDEX {_NEW_UNIQUE_NAME} ON {_TABLE} (user_id, bot_id, persona_name)"))


async def migrate_userfavorability_schema(engine: AsyncEngine) -> None:
    """去重 → 拆旧两列唯一 → 补三列唯一。幂等；失败仅告警。"""
    from gsuid_core.ai_core.database.models import UserFavorability

    try:
        removed = await UserFavorability.dedupe_user_bot_rows()
    except Exception as e:
        logger.warning(t("log.ai.relationship_dedupe_fail", e=e))
        return
    if removed:
        logger.warning(t("log.ai.relationship_dedupe_done", n=removed))

    try:
        async with engine.begin() as conn:
            await conn.run_sync(_migrate_schema_sync, engine.dialect.name)
    except Exception as e:
        # 建索引失败不阻断启动，但必须吵出来：没有唯一约束 = 日预算可能被绕过
        logger.warning(t("log.ai.relationship_unique_index_fail", e=e))
        return
    logger.debug(t("log.ai.relationship_unique_index_ok"))


@on_core_start_before(priority=-70)
async def migrate_user_favorability() -> None:
    """AI 总开关关闭时整条不跑（表可能都不存在）。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return

    from gsuid_core.utils.database.base_models import engine

    await migrate_userfavorability_schema(engine)
