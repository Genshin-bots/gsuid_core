"""程序性 / 偏好记忆规则表（SQL-only，不写向量）。"""

import uuid
from typing import Optional
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, col, select, update
from sqlalchemy import Text, Index, Column, or_, desc, func, delete, inspect as sa_inspect
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.write_gate import sqlite_write_gate
from gsuid_core.utils.database.base_models import with_session, with_read_session


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


class AIMemPreference(SQLModel, table=True):
    """程序性/偏好记忆规则。主存 USER_GLOBAL scope；SQL-only，不写向量。"""

    __table_args__ = (
        Index("ix_mem_pref_scope_target", "scope_key", "target_context"),
        Index("ix_mem_pref_scope_user", "scope_key", "user_id"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    user_id: Optional[str] = Field(default=None, index=True, max_length=64)
    target_context: str = Field(default="general", index=True, max_length=128)
    preference_rule: str = Field(sa_column=Column(Text, nullable=False))
    polarity: str = Field(default="do", max_length=8)
    is_correction: bool = Field(default=False)
    is_active: bool = Field(default=True)
    source_episode_id: Optional[str] = Field(default=None, max_length=36)
    mention_count: int = Field(default=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_applied_at: Optional[datetime] = Field(default=None)

    @staticmethod
    def _norm_rule(rule: str) -> str:
        return rule.strip().lower().replace(" ", "")[:64]

    @classmethod
    async def upsert(
        cls,
        scope_key: str,
        user_id: Optional[str],
        target_context: str,
        preference_rule: str,
        polarity: str,
        is_correction: bool,
        source_episode_id: Optional[str] = None,
    ) -> tuple[str, bool]:
        async with sqlite_write_gate:
            return await cls._upsert_db(
                scope_key=scope_key,
                user_id=user_id,
                target_context=target_context,
                preference_rule=preference_rule,
                polarity=polarity,
                is_correction=is_correction,
                source_episode_id=source_episode_id,
            )

    @classmethod
    @with_session
    async def _upsert_db(
        cls,
        session: AsyncSession,
        scope_key: str,
        user_id: Optional[str],
        target_context: str,
        preference_rule: str,
        polarity: str,
        is_correction: bool,
        source_episode_id: Optional[str] = None,
    ) -> tuple[str, bool]:
        polarity = "dont" if polarity == "dont" else "do"
        rule = (preference_rule or "").strip()
        if not rule:
            return "", False
        sig = cls._norm_rule(rule)
        result = await session.execute(
            select(cls).where(
                cls.scope_key == scope_key,
                cls.target_context == target_context,
                col(cls.is_active).is_(True),
            )
        )
        existing = list(result.scalars().all())
        for row in existing:
            if row.polarity == polarity and cls._norm_rule(row.preference_rule) == sig:
                row.mention_count += 1
                row.updated_at = datetime.now(timezone.utc)
                if is_correction:
                    row.is_correction = True
                session.add(row)
                return row.id, False
        for row in existing:
            if row.polarity != polarity and cls._norm_rule(row.preference_rule) == sig:
                row.is_active = False
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
        new_id = str(uuid.uuid4())
        session.add(
            cls(
                id=new_id,
                scope_key=scope_key,
                user_id=user_id,
                target_context=target_context or "general",
                preference_rule=rule,
                polarity=polarity,
                is_correction=is_correction,
                source_episode_id=source_episode_id,
            )
        )
        return new_id, True

    @classmethod
    @with_read_session
    async def get_active(
        cls,
        session: AsyncSession,
        scope_keys: list[str],
        target_contexts: Optional[list[str]] = None,
        limit: int = 12,
        self_min_created_at: Optional[datetime] = None,
    ) -> list["AIMemPreference"]:
        if not scope_keys:
            return []
        stmt = select(cls).where(col(cls.scope_key).in_(scope_keys), col(cls.is_active).is_(True))
        if target_contexts:
            stmt = stmt.where(col(cls.target_context).in_(target_contexts))
        if self_min_created_at is not None:
            cutoff = _naive_utc(self_min_created_at)
            stmt = stmt.where(or_(~col(cls.scope_key).startswith("self:"), col(cls.created_at) >= cutoff))
        stmt = stmt.order_by(
            desc(col(cls.is_correction)),
            desc(col(cls.mention_count)),
            desc(col(cls.updated_at)),
        ).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    async def touch_applied(cls, pref_ids: list[str]) -> None:
        if not pref_ids:
            return
        async with sqlite_write_gate:
            await cls._touch_applied_db(pref_ids)

    @classmethod
    @with_session
    async def _touch_applied_db(cls, session: AsyncSession, pref_ids: list[str]) -> None:
        await session.execute(
            update(cls).where(col(cls.id).in_(pref_ids)).values(last_applied_at=datetime.now(timezone.utc))
        )

    @classmethod
    @with_read_session
    async def count_active(cls, session: AsyncSession, scope_keys: Optional[list[str]] = None) -> int:
        stmt = select(func.count()).select_from(cls).where(col(cls.is_active).is_(True))
        if scope_keys:
            stmt = stmt.where(col(cls.scope_key).in_(scope_keys))
        return (await session.execute(stmt)).scalar() or 0

    @classmethod
    @with_session
    async def delete_by_scope_keys(cls, session: AsyncSession, scope_keys: list[str]) -> int:
        if not scope_keys:
            return 0
        result = await session.execute(delete(cls).where(col(cls.scope_key).in_(scope_keys)))
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    async def cleanup_polluted_scopes_in_session(cls, session: AsyncSession) -> int:
        """清理误写进 user_global 的助手自 ID 与群号偏好行（幂等）。"""
        from gsuid_core.utils.database.models import CoreGroup
        from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity, AIMemThread, AIMemEpisode

        conn = await session.connection()
        table_names = await conn.run_sync(lambda sync_conn: set(sa_inspect(sync_conn).get_table_names()))
        if "aimempreference" not in table_names:
            return 0
        known_gids: set[str] = set()
        if "coregroup" in table_names:
            cg_rows = await session.execute(select(CoreGroup.group_id).distinct())
            for row in cg_rows.fetchall():
                gid = str(row[0] or "").strip()
                if gid and gid != "1":
                    known_gids.add(gid)
        for table_name, model in (
            ("aimemepisode", AIMemEpisode),
            ("aimementity", AIMemEntity),
            ("aimemedge", AIMemEdge),
            ("aimemthread", AIMemThread),
        ):
            if table_name not in table_names:
                continue
            sk_rows = await session.execute(
                select(model.scope_key).where(col(model.scope_key).startswith("group:")).distinct()
            )
            for row in sk_rows.fetchall():
                sk = str(row[0] or "").strip()
                if sk.startswith("group:"):
                    gid = sk[len("group:") :].strip()
                    if gid:
                        known_gids.add(gid)

        conds = [
            col(cls.user_id).startswith("__assistant_"),
            col(cls.scope_key).startswith("user_global:__assistant_"),
            col(cls.scope_key).startswith("user_global:group:"),
            col(cls.scope_key).startswith("user_global:self:"),
        ]
        if known_gids:
            conds.append(col(cls.scope_key).in_([f"user_global:{gid}" for gid in known_gids]))

        result = await session.execute(delete(cls).where(or_(*conds)))
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    async def prune_per_context(cls, max_per_context: int) -> int:
        if max_per_context <= 0:
            return 0
        async with sqlite_write_gate:
            return await cls._prune_per_context_db(max_per_context)

    @classmethod
    @with_session
    async def _prune_per_context_db(cls, session: AsyncSession, max_per_context: int) -> int:
        result = await session.execute(
            select(cls)
            .where(col(cls.is_active).is_(True))
            .order_by(
                col(cls.scope_key),
                col(cls.target_context),
                desc(col(cls.is_correction)),
                desc(col(cls.mention_count)),
                desc(col(cls.updated_at)),
            )
        )
        rows = list(result.scalars().all())
        seen: dict[tuple[str, Optional[str], str], int] = {}
        deactivated = 0
        for row in rows:
            key = (row.scope_key, row.user_id, row.target_context)
            rank = seen[key] if key in seen else 0
            seen[key] = rank + 1
            if rank >= max_per_context and not row.is_correction:
                row.is_active = False
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                deactivated += 1
        return deactivated


__all__ = ["AIMemPreference"]
