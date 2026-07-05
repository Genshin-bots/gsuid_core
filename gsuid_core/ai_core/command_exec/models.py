"""命令执行器数据表：审计（AICommandAudit）+ 审批（AICommandApproval）。

遵循 SKILL §11：继承 base_models、table=True、不写 __tablename__（表名=类名小写）、
方法用 @with_session、DML where 一律 col() 包裹（LLM.md §3.5.1）。表随
AI_DATABASE_MODEL_MODULES 由 create_all 统一建表。
"""

import time
from typing import List, Optional

from sqlmodel import Field, col, desc, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import BaseIDModel, with_session


class AICommandAudit(BaseIDModel, table=True):
    """命令执行审计表：每次决策+执行留痕（先写决策、执行后补结果）。"""

    __table_args__ = {"extend_existing": True}

    request_id: str = Field(default="", index=True, max_length=36)
    session_id: str = Field(default="", index=True, max_length=128)
    operator_user_id: str = Field(default="", index=True, max_length=64)
    bot_id: str = Field(default="", max_length=64)
    raw_command: str = Field(default="")
    argv_json: str = Field(default="[]")
    decision: str = Field(default="", max_length=16)
    reason: str = Field(default="")
    risk: str = Field(default="low", max_length=8)
    touches_network: bool = Field(default=False)
    action: str = Field(default="exec", max_length=16)
    status: str = Field(default="decided", max_length=16)
    returncode: Optional[int] = Field(default=None)
    output_excerpt: str = Field(default="")
    findings: str = Field(default="")
    created_at: int = Field(default=0, index=True)
    finished_at: int = Field(default=0)

    @classmethod
    @with_session
    async def add(cls, session: AsyncSession, **kw) -> "AICommandAudit":
        kw.setdefault("created_at", int(time.time()))
        row = cls(**kw)
        session.add(row)
        await session.flush()
        return row

    @classmethod
    @with_session
    async def finish(
        cls,
        session: AsyncSession,
        row_id: int,
        status: str,
        returncode: Optional[int],
        output_excerpt: str,
    ) -> None:
        row = await session.get(cls, row_id)
        if row is None:
            return
        row.status = status
        row.returncode = returncode
        row.output_excerpt = output_excerpt
        row.finished_at = int(time.time())
        session.add(row)

    @classmethod
    @with_session
    async def delete_expired(cls, session: AsyncSession, ttl_days: int) -> int:
        """删早于 TTL 的低风险审计；永久保留 high risk 与 provision 动作。"""
        if ttl_days <= 0:
            return 0
        before = int(time.time()) - ttl_days * 86400
        stmt = delete(cls).where(
            col(cls.created_at) < before,
            col(cls.risk) != "high",
            col(cls.action) != "provision",
        )
        result = await session.execute(stmt)
        return result.rowcount if isinstance(result, CursorResult) else 0


class AICommandApproval(BaseIDModel, table=True):
    """命令审批表：pending / approved / denied / expired / executed。

    执行的永远是入库时的 argv 快照（argv_json），防「偷梁换柱」。
    """

    __table_args__ = {"extend_existing": True}

    request_id: str = Field(default="", index=True, max_length=36)
    short_id: str = Field(default="", index=True, max_length=8)
    session_id: str = Field(default="", index=True, max_length=128)
    operator_user_id: str = Field(default="", index=True, max_length=64)
    bot_id: str = Field(default="", max_length=64)
    bot_self_id: str = Field(default="", max_length=64)
    user_type: str = Field(default="direct", max_length=16)
    group_id: Optional[str] = Field(default=None, max_length=64)
    raw_command: str = Field(default="")
    argv_json: str = Field(default="[]")
    reason: str = Field(default="")
    risk: str = Field(default="low", max_length=8)
    action: str = Field(default="exec", max_length=16)
    is_batch: bool = Field(default=False)
    status: str = Field(default="pending", index=True, max_length=16)
    note: str = Field(default="")
    decided_via: str = Field(default="", max_length=16)
    created_at: int = Field(default=0, index=True)
    decided_at: int = Field(default=0)

    @classmethod
    @with_session
    async def add(cls, session: AsyncSession, **kw) -> "AICommandApproval":
        kw.setdefault("created_at", int(time.time()))
        row = cls(**kw)
        session.add(row)
        await session.flush()
        return row

    @classmethod
    @with_session
    async def list_pending_by_operator(cls, session: AsyncSession, operator_user_id: str) -> List["AICommandApproval"]:
        stmt = (
            select(cls)
            .where(col(cls.operator_user_id) == operator_user_id)
            .where(col(cls.status) == "pending")
            .order_by(desc(col(cls.created_at)))
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def pending_operator_ids(cls, session: AsyncSession) -> List[str]:
        """所有仍在 pending 的操作者 user_id（去重）——重启后回填内存可见性标记用。"""
        stmt = select(col(cls.operator_user_id)).where(col(cls.status) == "pending").distinct()
        result = await session.execute(stmt)
        return [r for r in result.scalars().all() if r]

    @classmethod
    @with_session
    async def get_by_short_id(cls, session: AsyncSession, short_id: str) -> Optional["AICommandApproval"]:
        stmt = select(cls).where(col(cls.short_id) == short_id)
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def get_by_id(cls, session: AsyncSession, row_id: int) -> Optional["AICommandApproval"]:
        return await session.get(cls, row_id)

    @classmethod
    @with_session
    async def mark(
        cls,
        session: AsyncSession,
        row_id: int,
        status: str,
        note: str = "",
        decided_via: str = "",
    ) -> None:
        row = await session.get(cls, row_id)
        if row is None:
            return
        row.status = status
        row.note = note or row.note
        row.decided_via = decided_via or row.decided_via
        row.decided_at = int(time.time())
        session.add(row)

    @classmethod
    @with_session
    async def expire_stale(cls, session: AsyncSession, ttl_seconds: int) -> int:
        """把超过 TTL 的 pending 批量标记 expired；返回受影响行数。"""
        if ttl_seconds <= 0:
            return 0
        before = int(time.time()) - ttl_seconds
        from sqlmodel import update

        stmt = update(cls).where(col(cls.status) == "pending", col(cls.created_at) < before).values(status="expired")
        result = await session.execute(stmt)
        return result.rowcount if isinstance(result, CursorResult) else 0
