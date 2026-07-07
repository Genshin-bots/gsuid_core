"""命令执行器数据表：审计（AICommandAudit）。

审批表已收编进统一审批中心（``ai_core/approval/models.py::AIApprovalRequest``）；
旧 ``aicommandapproval`` 表在既有部署中保留为孤表、不再读写（pending TTL 仅 30
分钟，升级损失可忽略）。表随 AI_DATABASE_MODEL_MODULES 由 create_all 统一建表。
"""

import time
from typing import Optional

from sqlmodel import Field, col, delete
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
