"""全量工具/子代理落盘 FileOS（SQL 真身）。"""

from __future__ import annotations

from typing import List, Optional, Sequence
from datetime import datetime

from sqlmodel import Field, SQLModel, UniqueConstraint, col, select
from sqlalchemy import Text, Column
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import with_session


class _ToolOutputCRUD:
    """UUID 主键 CRUD（与 Kanban ``_PlanCRUD`` 同模式）。"""

    @classmethod
    @with_session
    async def batch_insert_data(cls, session: AsyncSession, rows: List["AIToolOutputRecord"]) -> None:
        if rows:
            session.add_all(rows)


class AIToolOutputRecord(_ToolOutputCRUD, SQLModel, table=True):
    """全量落盘：达标 ToolReturn / 子代理终态原文。"""

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "scope_key",
            "content_hash",
            "tool_name",
            name="uq_tool_output_owner_scope_hash_tool",
        ),
    )

    id: str = Field(primary_key=True, max_length=64)
    root_task_id: str = Field(index=True, max_length=36)
    task_id: str = Field(index=True, max_length=36)
    session_id: str = Field(index=True, max_length=256)
    owner_user_id: str = Field(index=True, max_length=64)
    scope_key: str = Field(index=True, max_length=64)
    tool_name: str = Field(index=True, max_length=64)
    profile: str = Field(default="", max_length=64)
    summary: str = Field(default="", max_length=512)
    date_str: str = Field(index=True, max_length=16)
    res_handle: str = Field(default="", max_length=64)
    # sha256 去重：同 owner+scope+hash 复用旧记录
    content_hash: str = Field(default="", index=True, max_length=64)
    payload_inline: Optional[str] = Field(default=None, sa_column=Column(Text))
    payload_path: str = Field(default="", max_length=512)
    size_bytes: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = Field(default=None, index=True)

    @classmethod
    @with_session
    async def get_by_id(
        cls,
        session: AsyncSession,
        rid: str,
    ) -> Optional["AIToolOutputRecord"]:
        result = await session.execute(select(cls).where(col(cls.id) == rid))
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def get_by_hash(
        cls,
        session: AsyncSession,
        content_hash: str,
        owner_user_id: str = "",
        scope_key: str = "",
        tool_name: str = "",
    ) -> Optional["AIToolOutputRecord"]:
        """同 owner+scope+hash+tool_name 去重。"""
        if not content_hash:
            return None
        stmt = select(cls).where(col(cls.content_hash) == content_hash)
        if owner_user_id:
            stmt = stmt.where(col(cls.owner_user_id) == owner_user_id)
        if scope_key:
            stmt = stmt.where(col(cls.scope_key) == scope_key)
        stmt = stmt.where(col(cls.tool_name) == (tool_name or ""))
        stmt = stmt.order_by(col(cls.created_at).desc()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def search(
        cls,
        session: AsyncSession,
        owner_user_id: Optional[str] = None,
        scope_key: Optional[str] = None,
        tool_name: Optional[str] = None,
        keyword: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        root_task_id: Optional[str] = None,
        limit: int = 20,
    ) -> List["AIToolOutputRecord"]:
        stmt = select(cls)
        if owner_user_id:
            stmt = stmt.where(col(cls.owner_user_id) == owner_user_id)
        if scope_key:
            stmt = stmt.where(col(cls.scope_key) == scope_key)
        if tool_name:
            stmt = stmt.where(col(cls.tool_name) == tool_name)
        if session_id:
            stmt = stmt.where(col(cls.session_id) == session_id)
        if task_id:
            stmt = stmt.where(col(cls.task_id) == task_id)
        if root_task_id:
            stmt = stmt.where(col(cls.root_task_id) == root_task_id)
        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(col(cls.summary).like(like))
        stmt = stmt.order_by(col(cls.created_at).desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    async def list_recent(
        cls,
        *,
        owner_user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        root_task_id: Optional[str] = None,
        limit: int = 20,
    ) -> List["AIToolOutputRecord"]:
        return await cls.search(
            owner_user_id=owner_user_id,
            session_id=session_id,
            task_id=task_id,
            root_task_id=root_task_id,
            limit=limit,
        )

    @classmethod
    @with_session
    async def delete_by_root_task_ids(
        cls,
        session: AsyncSession,
        root_task_ids: Sequence[str],
    ) -> tuple[int, List[str], List[str]]:
        """按 root_task_id 硬删 FileOS 行；返回 (条数, payload 路径, 记录 id)。"""
        from pathlib import Path

        from sqlmodel import delete as sql_delete

        ids = [r for r in root_task_ids if r]
        if not ids:
            return 0, [], []
        stmt = select(cls).where(col(cls.root_task_id).in_(ids))
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        if not rows:
            return 0, [], []
        paths: list[str] = []
        rids: list[str] = []
        seen: set[str] = set()
        for rec in rows:
            rids.append(rec.id)
            if rec.payload_path and rec.payload_path not in seen:
                seen.add(rec.payload_path)
                paths.append(rec.payload_path)
                p = Path(rec.payload_path)
                if p.exists():
                    p.unlink(missing_ok=True)
        await session.execute(sql_delete(cls).where(col(cls.root_task_id).in_(ids)))
        return len(rows), paths, rids

    @classmethod
    @with_session
    async def delete_expired(
        cls,
        session: AsyncSession,
        now: Optional[datetime] = None,
    ) -> tuple[int, List[str]]:
        """删除过期行与 payload 文件；返回 (条数, 记录 id 列表) 供索引清理。"""
        from pathlib import Path

        from sqlmodel import delete as sql_delete

        cut = now or datetime.now()
        stmt = select(cls).where(col(cls.expires_at).is_not(None)).where(col(cls.expires_at) < cut)
        result = await session.execute(stmt)
        expired = list(result.scalars().all())
        if not expired:
            return 0, []
        seen: set[str] = set()
        rids: list[str] = []
        for rec in expired:
            rids.append(rec.id)
            if rec.payload_path and rec.payload_path not in seen:
                seen.add(rec.payload_path)
                p = Path(rec.payload_path)
                if p.exists():
                    p.unlink(missing_ok=True)
        del_stmt = sql_delete(cls).where(col(cls.expires_at).is_not(None)).where(col(cls.expires_at) < cut)
        await session.execute(del_stmt)
        return len(expired), rids

    @classmethod
    @with_session
    async def delete_by_ids(
        cls,
        session: AsyncSession,
        record_ids: Sequence[str],
    ) -> tuple[int, List[str]]:
        """按主键批量硬删；返回 (条数, 已删 id) 供 Qdrant 清理。"""
        from pathlib import Path

        from sqlmodel import delete as sql_delete

        ids = [r for r in record_ids if r]
        if not ids:
            return 0, []
        stmt = select(cls).where(col(cls.id).in_(ids))
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        if not rows:
            return 0, []
        seen: set[str] = set()
        rids: list[str] = []
        for rec in rows:
            rids.append(rec.id)
            if rec.payload_path and rec.payload_path not in seen:
                seen.add(rec.payload_path)
                p = Path(rec.payload_path)
                if p.exists():
                    p.unlink(missing_ok=True)
        await session.execute(sql_delete(cls).where(col(cls.id).in_(rids)))
        return len(rids), rids

    @classmethod
    @with_session
    async def list_admin(
        cls,
        session: AsyncSession,
        *,
        tool_name: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        scope_key: Optional[str] = None,
        session_id: Optional[str] = None,
        keyword: Optional[str] = None,
        include_expired: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List["AIToolOutputRecord"], int]:
        """控制台列表：过滤 + 分页，返回 (rows, total)。"""
        from sqlalchemy import func

        now = datetime.now()
        filters = []
        if tool_name:
            filters.append(col(cls.tool_name) == tool_name)
        if owner_user_id:
            filters.append(col(cls.owner_user_id) == owner_user_id)
        if scope_key:
            filters.append(col(cls.scope_key) == scope_key)
        if session_id:
            filters.append(col(cls.session_id) == session_id)
        if keyword:
            from sqlalchemy import or_

            like = f"%{keyword}%"
            filters.append(
                or_(
                    col(cls.summary).like(like),
                    col(cls.id).like(like),
                    col(cls.tool_name).like(like),
                    col(cls.session_id).like(like),
                )
            )
        if not include_expired:
            from sqlalchemy import or_

            filters.append(or_(col(cls.expires_at).is_(None), col(cls.expires_at) >= now))

        count_stmt = select(func.count()).select_from(cls)
        list_stmt = select(cls)
        for f in filters:
            count_stmt = count_stmt.where(f)
            list_stmt = list_stmt.where(f)
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        list_stmt = list_stmt.order_by(col(cls.created_at).desc()).offset(max(0, offset)).limit(limit)
        result = await session.execute(list_stmt)
        return list(result.scalars().all()), total

    @classmethod
    @with_session
    async def distinct_tool_names(cls, session: AsyncSession, limit: int = 200) -> List[str]:
        """筛选用：出现过的 tool_name（非空）。"""
        stmt = (
            select(col(cls.tool_name))
            .where(col(cls.tool_name) != "")
            .distinct()
            .order_by(col(cls.tool_name))
            .limit(limit)
        )
        result = await session.execute(stmt)
        return [str(r[0]) for r in result.all() if r[0]]
