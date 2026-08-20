"""出站审计与交付幂等账本。

``OutboundAudit``：发过什么图/台词/给谁（群维等值读，引用消解用）。
``DeliveryLedger``：``(group_id, res_id)`` 原子占位，拦同句柄二发。
"""

from __future__ import annotations

import time
from typing import List, Optional
from dataclasses import dataclass

from sqlmodel import Field, col, and_, delete, select
from sqlalchemy import Text, Column, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import BaseIDModel, async_maker, with_session

_TTL_SEC = 7 * 24 * 3600


def match_text_exact_or_prefix(bodies: List[str], needle: str) -> int:
    """等值优先；引用 = 出站原文 + 可选 @后缀 才算前缀命中。"""
    n = (needle or "").strip()
    if not n:
        return -1
    stripped = [(i, (b or "").strip()) for i, b in enumerate(bodies)]
    for i, body in stripped:
        if body == n:
            return i
    for i, body in stripped:
        if not body or not n.startswith(body):
            continue
        rest = n[len(body) :].lstrip()
        if rest == "" or rest.startswith("@"):
            return i
    return -1


@dataclass(frozen=True)
class DeliveryHit:
    ts: int
    session_id: str


@dataclass(frozen=True)
class OutboundHit:
    ts: int
    topic: str
    target_user: str
    target_name: str
    owner_user_id: str
    image_handles: str
    text: str
    session_id: str


class OutboundAudit(BaseIDModel, table=True):
    """出站审计：一次成功发送一行。表名 ``outboundaudit``。"""

    __table_args__ = {"extend_existing": True}

    session_id: str = Field(default="", index=True, max_length=256)
    group_id: str = Field(default="", index=True, max_length=64)
    text: str = Field(default="", sa_column=Column(Text, nullable=False))
    image_handles: str = Field(default="", max_length=512)
    topic: str = Field(default="", max_length=32)
    target_user: str = Field(default="", index=True, max_length=64)
    target_name: str = Field(default="", max_length=64)
    owner_user_id: str = Field(default="", index=True, max_length=64)
    ts: int = Field(default=0, index=True)

    @classmethod
    @with_session
    async def record(
        cls,
        session: AsyncSession,
        *,
        session_id: str,
        group_id: str,
        text: str,
        image_handles: str,
        topic: str,
        target_user: str,
        target_name: str,
        owner_user_id: str,
        ts: int | None = None,
    ) -> int:
        now = int(ts) if ts is not None else int(time.time())
        session.add(
            cls(
                session_id=session_id[:256],
                group_id=group_id[:64],
                text=(text or "")[:2000],
                image_handles=(image_handles or "")[:512],
                topic=(topic or "")[:32],
                target_user=(target_user or "")[:64],
                target_name=(target_name or "")[:64],
                owner_user_id=(owner_user_id or "")[:64],
                ts=now,
            )
        )
        return 1

    @classmethod
    @with_session
    async def match_by_text(
        cls,
        session: AsyncSession,
        *,
        group_id: str,
        text: str,
    ) -> Optional["OutboundAudit"]:
        needle = (text or "").strip()
        gid = (group_id or "").strip()
        if not needle or not gid:
            return None
        stmt = select(cls).where(col(cls.group_id) == gid).order_by(col(cls.ts).desc()).limit(40)
        rows = list((await session.execute(stmt)).scalars().all())
        idx = match_text_exact_or_prefix([r.text for r in rows], needle)
        if idx < 0:
            return None
        return rows[idx]

    @classmethod
    @with_session
    async def recent_for_group(
        cls,
        session: AsyncSession,
        group_id: str,
        *,
        limit: int = 8,
    ) -> List["OutboundAudit"]:
        gid = (group_id or "").strip()
        if not gid:
            return []
        n = max(1, min(int(limit), 40))
        stmt = select(cls).where(col(cls.group_id) == gid).order_by(col(cls.ts).desc()).limit(n)
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_session
    async def search_recent(
        cls,
        session: AsyncSession,
        *,
        group_id: str,
        query: str,
        limit: int = 8,
    ) -> List["OutboundAudit"]:
        gid = (group_id or "").strip()
        q = (query or "").strip().lower()
        if not gid or not q:
            return []
        n = max(1, min(int(limit), 20))
        stmt = select(cls).where(col(cls.group_id) == gid).order_by(col(cls.ts).desc()).limit(80)
        rows = list((await session.execute(stmt)).scalars().all())
        hits: List[OutboundAudit] = []
        for row in rows:
            blob = f"{row.text} {row.topic} {row.image_handles} {row.target_name}".lower()
            if q in blob:
                hits.append(row)
            if len(hits) >= n:
                break
        return hits

    @classmethod
    @with_session
    async def purge_expired(cls, session: AsyncSession, *, before_ts: int) -> int:
        result = await session.execute(delete(cls).where(col(cls.ts) < before_ts))
        return result.rowcount if isinstance(result, CursorResult) else 0


class DeliveryLedger(BaseIDModel, table=True):
    """artifact 级交付幂等。表名 ``deliveryledger``。"""

    __table_args__ = (
        UniqueConstraint("group_id", "res_id", name="ux_deliveryledger_group_res"),
        {"extend_existing": True},
    )

    group_id: str = Field(default="", index=True, max_length=80)
    res_id: str = Field(default="", index=True, max_length=64)
    session_id: str = Field(default="", max_length=256)
    ts: int = Field(default=0, index=True)

    @classmethod
    async def check_and_claim(
        cls,
        group_id: str,
        res_id: str,
        *,
        session_id: str = "",
    ) -> Optional[DeliveryHit]:
        """原子占位。已存在返回先前记录；本次占到返回 None。"""
        gid = (group_id or "").strip()
        rid = (res_id or "").strip()
        if not gid or not rid:
            return None
        async with async_maker() as session:
            stmt = select(cls).where(and_(col(cls.group_id) == gid, col(cls.res_id) == rid))
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                return DeliveryHit(ts=int(existing.ts), session_id=existing.session_id or "")
            session.add(
                cls(
                    group_id=gid[:80],
                    res_id=rid[:64],
                    session_id=(session_id or "")[:256],
                    ts=int(time.time()),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raced = (await session.execute(stmt)).scalar_one_or_none()
                if raced is None:
                    return DeliveryHit(ts=int(time.time()), session_id=session_id)
                return DeliveryHit(ts=int(raced.ts), session_id=raced.session_id or "")
            return None

    @classmethod
    @with_session
    async def release(cls, session: AsyncSession, group_id: str, res_id: str) -> int:
        """发送失败时释放本次占位。"""
        gid = (group_id or "").strip()
        rid = (res_id or "").strip()
        if not gid or not rid:
            return 0
        result = await session.execute(delete(cls).where(and_(col(cls.group_id) == gid, col(cls.res_id) == rid)))
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    @with_session
    async def purge_expired(cls, session: AsyncSession, *, before_ts: int) -> int:
        result = await session.execute(delete(cls).where(col(cls.ts) < before_ts))
        return result.rowcount if isinstance(result, CursorResult) else 0


async def purge_outbound_expired() -> None:
    cutoff = int(time.time()) - _TTL_SEC
    await OutboundAudit.purge_expired(before_ts=cutoff)
    await DeliveryLedger.purge_expired(before_ts=cutoff)
