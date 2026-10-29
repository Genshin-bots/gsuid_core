"""统一审批中心数据表：AIApprovalRequest。

全框架唯一的审批 / 交互请求账本——command_exec 命令审批、Kanban HITL、插件安装、
Agent 主动请求（request_user/master_approval）、工具策略门（tool_call）全部落此表。
payload 是提交时冻结的快照，裁决后 category handler 只作用于快照。
"""

import time
from typing import List, Optional

from sqlmodel import Field, col, desc, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import BaseIDModel, with_session, with_read_session


class AIApprovalRequest(BaseIDModel, table=True):
    """一条审批 / 交互请求。status: pending / approved / rejected / expired / auto_approved。"""

    __table_args__ = {"extend_existing": True}

    request_id: str = Field(default="", index=True, max_length=36)
    short_id: str = Field(default="", index=True, max_length=8)
    interaction: str = Field(default="approval", max_length=16)  # approval | question
    audience: str = Field(default="master", max_length=8)  # user | master
    category: str = Field(default="", index=True, max_length=32)
    ref_key: str = Field(default="", index=True, max_length=128)  # 领域引用键（task_id / 工具名等）
    origin_session_id: str = Field(default="", index=True, max_length=128)
    operator_user_id: str = Field(default="", index=True, max_length=64)
    # 提交时的权限。回灌沿用它；缺省 6，避免旧行被抬成主人。
    operator_user_pm: int = Field(default=6)
    bot_id: str = Field(default="", max_length=64)
    bot_self_id: str = Field(default="", max_length=64)
    user_type: str = Field(default="direct", max_length=16)
    group_id: Optional[str] = Field(default=None, max_length=64)
    title: str = Field(default="")
    payload_json: str = Field(default="{}")
    status: str = Field(default="pending", index=True, max_length=16)
    resolved_by: str = Field(default="", max_length=64)
    resolved_note: str = Field(default="")
    resolved_via: str = Field(default="", max_length=16)  # chat | webconsole | auto
    created_at: int = Field(default=0, index=True)
    resolved_at: int = Field(default=0)
    # 空哈希 = 部署前的旧票：只走控制台，沿用 category TTL，不回灌。
    confirm_code_hash: str = Field(default="", max_length=64, index=True)
    dedupe_key: str = Field(default="", max_length=512, index=True)
    effect: str = Field(default="")
    expires_at: int = Field(default=0, index=True)

    @classmethod
    @with_session
    async def add(cls, session: AsyncSession, **kw) -> "AIApprovalRequest":
        kw.setdefault("created_at", int(time.time()))
        row = cls(**kw)
        session.add(row)
        await session.flush()
        return row

    @classmethod
    @with_read_session
    async def get_by_request_id(cls, session: AsyncSession, request_id: str) -> Optional["AIApprovalRequest"]:
        stmt = select(cls).where(col(cls.request_id) == request_id)
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_read_session
    async def get_by_short_id(cls, session: AsyncSession, short_id: str) -> Optional["AIApprovalRequest"]:
        stmt = select(cls).where(col(cls.short_id) == short_id).order_by(desc(col(cls.created_at)))
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_read_session
    async def get_pending_by_short_id(cls, session: AsyncSession, short_id: str) -> Optional["AIApprovalRequest"]:
        """同短号有多行时，只取仍在等的那条。"""
        if not short_id:
            return None
        stmt = (
            select(cls)
            .where(col(cls.status) == "pending", col(cls.short_id) == short_id)
            .order_by(desc(col(cls.created_at)))
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_read_session
    async def list_pending(
        cls,
        session: AsyncSession,
        operator_user_id: Optional[str] = None,
        audience: Optional[str] = None,
        category: Optional[str] = None,
        ref_key: Optional[str] = None,
    ) -> List["AIApprovalRequest"]:
        stmt = select(cls).where(col(cls.status) == "pending")
        if operator_user_id is not None:
            stmt = stmt.where(col(cls.operator_user_id) == operator_user_id)
        if audience is not None:
            stmt = stmt.where(col(cls.audience) == audience)
        if category is not None:
            stmt = stmt.where(col(cls.category) == category)
        if ref_key is not None:
            stmt = stmt.where(col(cls.ref_key) == ref_key)
        stmt = stmt.order_by(desc(col(cls.created_at)))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def pending_operator_ids(cls, session: AsyncSession) -> List[str]:
        """所有仍在 pending 的操作者 user_id（去重）——重启后回填内存可见性标记用。"""
        stmt = select(col(cls.operator_user_id)).where(col(cls.status) == "pending").distinct()
        result = await session.execute(stmt)
        return [r for r in result.scalars().all() if r]

    @classmethod
    @with_session
    async def mark(
        cls,
        session: AsyncSession,
        row_id: int,
        status: str,
        resolved_by: str = "",
        note: str = "",
        via: str = "",
    ) -> None:
        row = await session.get(cls, row_id)
        if row is None:
            return
        row.status = status
        row.resolved_by = resolved_by or row.resolved_by
        row.resolved_note = note or row.resolved_note
        row.resolved_via = via or row.resolved_via
        row.resolved_at = int(time.time())
        session.add(row)

    @classmethod
    @with_session
    async def expire_stale(cls, session: AsyncSession, category: str, ttl_seconds: int) -> int:
        """把某 category 下超过 TTL 的 pending 批量标记 expired；返回受影响行数。"""
        if ttl_seconds <= 0:
            return 0
        before = int(time.time()) - ttl_seconds
        stmt = (
            update(cls)
            .where(
                col(cls.status) == "pending",
                col(cls.category) == category,
                col(cls.created_at) < before,
            )
            .values(status="expired")
        )
        result = await session.execute(stmt)
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    @with_read_session
    async def get_by_code_hash(cls, session: AsyncSession, digest: str) -> Optional["AIApprovalRequest"]:
        if not digest:
            return None
        stmt = select(cls).where(col(cls.confirm_code_hash) == digest).order_by(desc(col(cls.created_at)))
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_read_session
    async def get_pending_by_dedupe(cls, session: AsyncSession, dedupe_key: str) -> Optional["AIApprovalRequest"]:
        if not dedupe_key:
            return None
        stmt = select(cls).where(col(cls.status) == "pending", col(cls.dedupe_key) == dedupe_key)
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_read_session
    async def list_pending_plugin_installs(cls, session: AsyncSession) -> List["AIApprovalRequest"]:
        stmt = select(cls).where(col(cls.status) == "pending", col(cls.dedupe_key).like("plugin_install:%"))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def list_pending_for_origin(cls, session: AsyncSession, origin_session_id: str) -> List["AIApprovalRequest"]:
        if not origin_session_id:
            return []
        stmt = select(cls).where(col(cls.status) == "pending", col(cls.origin_session_id) == origin_session_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def pending_origin_session_ids(cls, session: AsyncSession) -> List[str]:
        stmt = select(col(cls.origin_session_id)).where(col(cls.status) == "pending").distinct()
        result = await session.execute(stmt)
        return [r for r in result.scalars().all() if r]

    @classmethod
    @with_read_session
    async def list_due_coded(cls, session: AsyncSession, now: int) -> List["AIApprovalRequest"]:
        """新票：expires_at 已到。expires_at=0 的旧票不在这里。"""
        stmt = select(cls).where(
            col(cls.status) == "pending",
            col(cls.expires_at) > 0,
            col(cls.expires_at) <= now,
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def list_legacy_pending(cls, session: AsyncSession) -> List["AIApprovalRequest"]:
        """expires_at 为空的旧票。过期与否由 category 原来的 TTL 决定。"""
        stmt = select(cls).where(col(cls.status) == "pending", col(cls.expires_at) == 0)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def claim_status(
        cls,
        session: AsyncSession,
        row_id: int,
        status: str,
        resolved_by: str = "",
        note: str = "",
        via: str = "",
    ) -> bool:
        """只抢仍是 pending 的行。并发裁决时后到的得到 False。"""
        stmt = (
            update(cls)
            .where(col(cls.id) == row_id, col(cls.status) == "pending")
            .values(
                status=status,
                resolved_by=resolved_by,
                resolved_note=note,
                resolved_via=via,
                resolved_at=int(time.time()),
            )
        )
        result = await session.execute(stmt)
        changed = result.rowcount if isinstance(result, CursorResult) else 0
        return changed == 1

    @classmethod
    @with_read_session
    async def list_recent(cls, session: AsyncSession, limit: int = 50) -> List["AIApprovalRequest"]:
        """最近的请求（含已裁决），webconsole 列表用。"""
        stmt = select(cls).order_by(desc(col(cls.created_at))).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())
