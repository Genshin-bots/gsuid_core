"""记忆系统 SQLModel 数据模型 + 业务方法

存储模型：
- AIMemEpisode                : 原始对话片段（Base Graph 第一层）
- AIMemEntity                 : 提取出的实体节点（Base Graph 第二层）
- AIMemEdge                   : 实体间的关系（Base Graph 第三层）
- AIMemCategory               : 分层语义图节点（Hierarchical Graph）
- AIMemCategoryEdge           : Category ↔ Category 层次关联
- AIMemHierarchicalGraphMeta  : 分层图构建状态追踪
"""

import uuid
import asyncio
from typing import List, Optional, TypedDict, NotRequired
from datetime import datetime, timezone, timedelta

from sqlmodel import Field, SQLModel, Relationship, col, select, update
from sqlalchemy import (
    Text,
    Index,
    Table,
    Column,
    String,
    ForeignKey,
    UniqueConstraint,
    or_,
    desc,
    func,
    union as sql_union,
    exists,
    insert,
    literal_column,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Select
from sqlalchemy.dialects.postgresql import JSON

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.vector.ops import upsert_episode_vector
from gsuid_core.utils.database.base_models import (
    _db_type,
    async_maker,
    with_session,
    with_read_session,
)
from gsuid_core.ai_core.memory.database.session_split import SessionCursor


def _like_contains(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _model_table(model: type[SQLModel]) -> Table:
    name = model.__name__.lower()
    tables = model.metadata.tables
    assert name in tables, name
    return tables[name]


def _as_str_col(field: object) -> ColumnElement[str]:
    assert isinstance(field, ColumnElement)
    return field


def _as_dt_col(field: object) -> ColumnElement[datetime]:
    assert isinstance(field, ColumnElement)
    return field


def _as_bool_expr(field: object) -> ColumnElement[bool]:
    assert isinstance(field, ColumnElement)
    return field


def _ids_matching_tokens(
    id_col: ColumnElement[str],
    scope_col: ColumnElement[str],
    text_col: ColumnElement[str],
    order_col: ColumnElement[datetime],
    scope_key: str,
    tokens: list[str],
    limit: int,
    extra_conds: tuple[ColumnElement[bool], ...] = (),
) -> Select[tuple[str]]:
    """每个 token 最新+最旧各 LIMIT 后 UNION，避免热词只留下半段。"""
    per = max(6, min(12, limit // max(len(tokens), 1) + 2))
    pieces: list[Select[tuple[str]]] = []
    for tok in tokens:
        conds = (scope_col == scope_key, text_col.like(_like_contains(tok), escape="\\"), *extra_conds)
        inner_new = select(id_col.label("eid")).where(*conds).order_by(order_col.desc()).limit(per).subquery()
        inner_old = select(id_col.label("eid")).where(*conds).order_by(order_col.asc()).limit(per).subquery()
        pieces.append(select(inner_new.c.eid))
        pieces.append(select(inner_old.c.eid))
    if len(pieces) == 1:
        return pieces[0]
    unioned = sql_union(*pieces).subquery()
    return select(unioned.c.eid)


# ─────────────────────────────────────────────
# 多对多关联表
# ─────────────────────────────────────────────
mem_episode_entity_mentions = Table(
    "mem_episode_entity_mentions",
    SQLModel.metadata,
    Column("episode_id", String(36), ForeignKey("aimemepisode.id"), primary_key=True),
    Column("entity_id", String(36), ForeignKey("aimementity.id"), primary_key=True),
)


mem_category_entity_members = Table(
    "mem_category_entity_members",
    SQLModel.metadata,
    Column("category_id", String(36), ForeignKey("aimemcategory.id"), primary_key=True),
    Column("entity_id", String(36), ForeignKey("aimementity.id"), primary_key=True),
    Index("ix_mem_cat_entity_entity_id", "entity_id"),
)


# ─────────────────────────────────────────────
# Episode：原始对话片段（Base Graph 第一层）
# ─────────────────────────────────────────────
class AIMemEpisode(SQLModel, table=True):
    """存储经聚合的对话片段，是所有记忆的原始素材。"""

    __table_args__ = (
        Index("ix_mem_episode_scope_valid_at", "scope_key", "valid_at"),
        # §3.2① 保留策略/冷热分集合：按 (scope, 冷热, 时间) 高效取降级/物理上限候选
        Index("ix_mem_episode_scope_archived_valid", "scope_key", "is_archived", "valid_at"),
        Index("ix_mem_episode_scope_session", "scope_key", "session_id", "valid_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    content: str = Field(sa_column=Column(Text, nullable=False))
    speaker_ids: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    valid_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    qdrant_id: str = Field(index=True, max_length=36)
    # §3.2① 冷热分集合标记：is_archived=True 表示已降级为"冷"——其向量已迁出热集合
    # memory_episodes（迁入冷集合 memory_episodes_cold），SQL 文本保留可审计。System-1
    # 只查热集合，故冷 Episode 退出在线向量暴力扫描、不再抬高交互检索成本（缓解 P0-1/P0-2）。
    # 旧库无此列，由 utils/database/startup.py 的 ALTER 语句补齐（默认 False）。
    is_archived: bool = Field(default=False)
    # 时间 gap 切出的 session；旧库由 ALTER + backfill_sessions 补齐。
    session_id: Optional[str] = Field(default=None, max_length=36, index=True)
    turn_index: int = Field(default=0)

    mentioned_entities: List["AIMemEntity"] = Relationship(
        back_populates="episodes",
        sa_relationship_kwargs={
            "secondary": mem_episode_entity_mentions,
            "back_populates": "episodes",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    @classmethod
    async def create_episode(
        cls,
        scope_key: str,
        content: str,
        speaker_ids: list[str],
        valid_at: datetime,
    ) -> "AIMemEpisode":
        """创建一条 Episode 记录并写入向量库。

        Args:
            scope_key: 作用域标识（如 "group:789012"）
            content: 聚合后的对话文本
            speaker_ids: 参与发言的 user_id 列表
            valid_at: 最早消息的时间戳

        Returns:
            新创建的 AIMemEpisode 对象
        """
        episode_id = str(uuid.uuid4())
        from gsuid_core.ai_core.memory.database.session_split import continue_session
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard

        async with db_write_guard(), async_maker() as session:
            cursor = await _cursor_in_tx(session, scope_key)
            assignment = continue_session(cursor, valid_at, _session_gap_seconds(), str(uuid.uuid4()))
            episode = AIMemEpisode(
                id=episode_id,
                scope_key=scope_key,
                content=content,
                speaker_ids=speaker_ids,
                valid_at=valid_at,
                created_at=datetime.now(timezone.utc),
                qdrant_id=episode_id,
                session_id=assignment.session_id,
                turn_index=assignment.turn_index,
            )
            session.add(episode)
            await _touch_session_row(
                session,
                session_id=assignment.session_id,
                scope_key=scope_key,
                valid_at=valid_at,
                opener_episode_id=episode_id,
                is_new=assignment.is_new_session,
            )
            await session.commit()
        session_id = assignment.session_id
        turn_index = assignment.turn_index
        if assignment.is_new_session:
            from gsuid_core.ai_core.memory.lifecycle.sleep_extract import enqueue_session

            enqueue_session(session_id)

        try:
            await upsert_episode_vector(
                episode_id=episode_id,
                content=content,
                scope_key=scope_key,
                valid_at_ts=valid_at.timestamp(),
                speaker_ids=speaker_ids,
                session_id=session_id,
                turn_index=turn_index,
            )
        except Exception as e:
            logger.warning(t("log.memory.episode_vector_upsert_fail", episode_id=episode_id, error=str(e)))

        return episode

    @classmethod
    async def create_episodes_bulk(
        cls,
        scope_key: str,
        items: list[dict],
        *,
        vector_chunk: int = 64,
    ) -> int:
        """批量创建 granular Episode（评测/回灌专用，每 turn 一条）。

        与 ``create_episode`` 不同：不把整批对话拼成一条巨型 Episode（回灌大语料时
        单条可达数十万字符，向量只覆盖头部 ~512 token、注入又被预算截断 → 召回恒空），
        而是**每条 turn 一个细粒度 Episode**，单独 embedding、可被 System-1 精确召回。

        ``items``：``[{"content": str, "speaker_ids": list[str], "valid_at": datetime}]``。
        SQL 行批量 ``add_all`` 一次提交；向量按 ``vector_chunk`` 分块 batch embed 避免一次
        embedding 上千条触发远程 413 / 本地 OOM。返回成功写入条数。
        """
        if not items:
            return 0

        now = datetime.now(timezone.utc)
        prepared: list[tuple[str, datetime, list[str]]] = []
        for it in items:
            content = str(it["content"])
            valid_at = it["valid_at"] if isinstance(it["valid_at"], datetime) else now
            speakers = list(it["speaker_ids"]) if it["speaker_ids"] else []
            prepared.append((content, valid_at, speakers))
        prepared.sort(key=lambda row: row[1])

        from gsuid_core.ai_core.memory.database.session_split import SessionCursor, continue_session
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard

        gap = _session_gap_seconds()
        episodes: list["AIMemEpisode"] = []
        async with db_write_guard(), async_maker() as session:
            cursor = await _cursor_in_tx(session, scope_key)
            session_touches: list[tuple[str, datetime, str, bool]] = []
            for content, valid_at, speakers in prepared:
                assignment = continue_session(cursor, valid_at, gap, str(uuid.uuid4()))
                episode_id = str(uuid.uuid4())
                episodes.append(
                    cls(
                        id=episode_id,
                        scope_key=scope_key,
                        content=content,
                        speaker_ids=speakers,
                        valid_at=valid_at,
                        created_at=now,
                        qdrant_id=episode_id,
                        session_id=assignment.session_id,
                        turn_index=assignment.turn_index,
                    )
                )
                session_touches.append((assignment.session_id, valid_at, episode_id, assignment.is_new_session))
                cursor = SessionCursor(assignment.session_id, assignment.turn_index, valid_at)
            session.add_all(episodes)
            for sid, at, opener_id, is_new in session_touches:
                await _touch_session_row(
                    session,
                    session_id=sid,
                    scope_key=scope_key,
                    valid_at=at,
                    opener_episode_id=opener_id,
                    is_new=is_new,
                )
            await session.commit()

        from gsuid_core.ai_core.memory.vector.ops import upsert_episode_vectors_batch

        written = 0
        for i in range(0, len(episodes), vector_chunk):
            chunk = episodes[i : i + vector_chunk]
            payload = [
                {
                    "episode_id": ep.id,
                    "content": ep.content,
                    "scope_key": ep.scope_key,
                    "valid_at_ts": ep.valid_at.timestamp(),
                    "speaker_ids": ep.speaker_ids,
                    "session_id": ep.session_id,
                    "turn_index": ep.turn_index,
                }
                for ep in chunk
            ]
            await upsert_episode_vectors_batch(payload)
            written += len(chunk)
        from gsuid_core.ai_core.memory.lifecycle.sleep_extract import enqueue_session

        for sid, _at, _opener, is_new in session_touches:
            if is_new:
                enqueue_session(sid)
        return written

    @classmethod
    @with_read_session
    async def search_by_tokens(
        cls,
        session: AsyncSession,
        scope_key: str,
        tokens: list[str],
        limit: int = 24,
        start: datetime | None = None,
        end: datetime | None = None,
        user_only: bool = False,
    ) -> list["AIMemEpisode"]:
        """按 token 词面召回本 scope 的 Episode（每 token 限条后 UNION，一次往返）。"""
        if not tokens or not scope_key:
            return []
        extra: list[ColumnElement[bool]] = []
        c = _model_table(cls).c
        if start is not None:
            lo = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo is not None else start
            extra.append(_as_bool_expr(c.valid_at >= lo))
        if end is not None:
            hi = end.astimezone(timezone.utc).replace(tzinfo=None) if end.tzinfo is not None else end
            extra.append(_as_bool_expr(c.valid_at < hi))
        if user_only:
            extra.append(_as_bool_expr(~c.content.like("assistant:%")))
            extra.append(_as_bool_expr(~c.content.like("[我此前说过]%")))
        id_q = _ids_matching_tokens(
            _as_str_col(c.id),
            _as_str_col(c.scope_key),
            _as_str_col(c.content),
            _as_dt_col(c.valid_at),
            scope_key,
            tokens[:18],
            limit,
            extra_conds=tuple(extra),
        )
        stmt = select(cls).where(c.id.in_(id_q))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def search_by_all_tokens(
        cls,
        session: AsyncSession,
        scope_key: str,
        tokens: list[str],
        *,
        limit: int = 8,
        ascending: bool = False,
        user_only: bool = False,
        speaker: str | None = None,
    ) -> list["AIMemEpisode"]:
        """实词同时出现。一个话题词也要能取头尾，否则中段那次赋值进不了页。"""
        if not scope_key or limit <= 0 or not tokens:
            return []
        c = _model_table(cls).c
        conds: list[ColumnElement[bool]] = [_as_bool_expr(c.scope_key == scope_key)]
        for tok in tokens[:4]:
            if not tok:
                return []
            conds.append(_as_bool_expr(c.content.like(_like_contains(tok), escape="\\")))
        if user_only:
            conds.append(_as_bool_expr(~c.content.like("assistant:%")))
            conds.append(_as_bool_expr(~c.content.like("[我此前说过]%")))
        if speaker:
            # [id]: 与 id: 两种前缀；只钉本人，避免群里别人的后一次赋值盖过来。
            conds.append(
                _as_bool_expr(
                    or_(
                        c.content.like(_like_contains(f"{speaker}:"), escape="\\"),
                        c.content.like(_like_contains(f"[{speaker}]:"), escape="\\"),
                    )
                )
            )
        tie = literal_column("rowid") if _db_type == "sqlite" else c.id
        if ascending:
            stmt = select(cls).where(*conds).order_by(c.valid_at.asc(), tie.asc()).limit(limit)
        else:
            stmt = select(cls).where(*conds).order_by(c.valid_at.desc(), tie.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def search_by_valid_at_range(
        cls,
        session: AsyncSession,
        scope_key: str,
        start: datetime,
        end: datetime,
        limit: int = 24,
        ascending: bool = False,
        offset: int = 0,
        user_only: bool = False,
    ) -> list["AIMemEpisode"]:
        """本 scope 在 [start, end) 内的片段。相对日问句用，不是全库 dump。"""
        if not scope_key or limit <= 0:
            return []
        lo = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo is not None else start
        hi = end.astimezone(timezone.utc).replace(tzinfo=None) if end.tzinfo is not None else end
        c = _model_table(cls).c
        conds: list[ColumnElement[bool]] = [
            _as_bool_expr(c.scope_key == scope_key),
            _as_bool_expr(c.valid_at >= lo),
            _as_bool_expr(c.valid_at < hi),
        ]
        if user_only:
            conds.append(_as_bool_expr(~c.content.like("assistant:%")))
            conds.append(_as_bool_expr(~c.content.like("[我此前说过]%")))
        tie = literal_column("rowid") if _db_type == "sqlite" else c.id
        if ascending:
            stmt = select(cls).where(*conds).order_by(c.valid_at.asc(), tie.asc())
        else:
            stmt = select(cls).where(*conds).order_by(c.valid_at.desc(), tie.desc())
        if offset > 0:
            stmt = stmt.offset(offset)
        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def count_by_valid_at_range(
        cls,
        session: AsyncSession,
        scope_key: str,
        start: datetime,
        end: datetime,
        user_only: bool = False,
    ) -> int:
        """窗口内条数。user_only 时去掉 assistant 长回复。"""
        if not scope_key:
            return 0
        lo = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo is not None else start
        hi = end.astimezone(timezone.utc).replace(tzinfo=None) if end.tzinfo is not None else end
        c = _model_table(cls).c
        conds: list[ColumnElement[bool]] = [
            _as_bool_expr(c.scope_key == scope_key),
            _as_bool_expr(c.valid_at >= lo),
            _as_bool_expr(c.valid_at < hi),
        ]
        if user_only:
            conds.append(_as_bool_expr(~c.content.like("assistant:%")))
            conds.append(_as_bool_expr(~c.content.like("[我此前说过]%")))
        stmt = select(func.count()).select_from(cls).where(*conds)
        result = await session.execute(stmt)
        val = result.scalar_one()
        return int(val or 0)

    @classmethod
    @with_read_session
    async def search_user_day_openers(
        cls,
        session: AsyncSession,
        scope_key: str,
        start: datetime,
        end: datetime,
        limit: int = 40,
    ) -> list["AIMemEpisode"]:
        """每个日历日最早一条用户话。时间线真源。"""
        if not scope_key or limit <= 0:
            return []
        lo = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo is not None else start
        hi = end.astimezone(timezone.utc).replace(tzinfo=None) if end.tzinfo is not None else end
        c = _model_table(cls).c
        conds: list[ColumnElement[bool]] = [
            _as_bool_expr(c.scope_key == scope_key),
            _as_bool_expr(c.valid_at >= lo),
            _as_bool_expr(c.valid_at < hi),
            _as_bool_expr(~c.content.like("assistant:%")),
            _as_bool_expr(~c.content.like("[我此前说过]%")),
        ]
        day_col = func.date(c.valid_at)
        # SQLite 同日撞 00:00:00 时 rowid 才是摄入序；其它库靠 spread 后的 min(valid_at)。
        if _db_type == "sqlite":
            rowid = literal_column("rowid")
            firsts = select(day_col.label("d"), func.min(rowid).label("rid")).where(*conds).group_by(day_col).subquery()
            stmt = (
                select(cls)
                .where(literal_column("aimemepisode.rowid").in_(select(firsts.c.rid)), *conds)
                .order_by(c.valid_at.asc())
                .limit(limit)
            )
        else:
            firsts = (
                select(day_col.label("d"), func.min(c.valid_at).label("t0")).where(*conds).group_by(day_col).subquery()
            )
            stmt = (
                select(cls).where(c.valid_at.in_(select(firsts.c.t0)), *conds).order_by(c.valid_at.asc()).limit(limit)
            )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def neighbors_by_time(
        cls,
        session: AsyncSession,
        scope_key: str,
        valid_at: datetime,
        before: int = 6,
        after: int = 6,
        mode: str = "window",
    ) -> list["AIMemEpisode"]:
        """同一 scope 按 valid_at 取前后邻条；mode=session 时取整段。"""
        if not scope_key:
            return []
        if mode == "session":
            hit = await session.execute(
                select(cls).where(col(cls.scope_key) == scope_key, col(cls.valid_at) == valid_at).limit(1)
            )
            ep = hit.scalar_one_or_none()
            if ep is not None and ep.session_id:
                full = await session.execute(
                    select(cls)
                    .where(col(cls.session_id) == ep.session_id)
                    .order_by(col(cls.turn_index).asc(), col(cls.valid_at).asc())
                )
                return list(full.scalars().all())
        if before <= 0 and after <= 0:
            return []
        rows: list["AIMemEpisode"] = []
        seen: set[str] = set()
        if before > 0:
            stmt_b = (
                select(cls)
                .where(col(cls.scope_key) == scope_key, col(cls.valid_at) <= valid_at)
                .order_by(col(cls.valid_at).desc())
                .limit(before + 1)
            )
            earlier = list((await session.execute(stmt_b)).scalars().all())
            for row in reversed(earlier):
                if row.id in seen:
                    continue
                seen.add(row.id)
                rows.append(row)
        if after > 0:
            stmt_a = (
                select(cls)
                .where(col(cls.scope_key) == scope_key, col(cls.valid_at) > valid_at)
                .order_by(col(cls.valid_at).asc())
                .limit(after)
            )
            later = list((await session.execute(stmt_a)).scalars().all())
            for row in later:
                if row.id in seen:
                    continue
                seen.add(row.id)
                rows.append(row)
        return rows

    @classmethod
    @with_read_session
    async def list_by_scope(
        cls,
        session: AsyncSession,
        scope_key: str,
        limit: int = 2000,
    ) -> list["AIMemEpisode"]:
        """同一 scope 按时间取出全部 Episode（评测还原 haystack 会话）。"""
        if not scope_key or limit <= 0:
            return []
        stmt = select(cls).where(col(cls.scope_key) == scope_key).order_by(col(cls.valid_at).asc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def last_in_scope(cls, session: AsyncSession, scope_key: str) -> Optional["AIMemEpisode"]:
        if not scope_key:
            return None
        stmt = select(cls).where(col(cls.scope_key) == scope_key).order_by(col(cls.valid_at).desc()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_read_session
    async def recent_ids(cls, session: AsyncSession, scope_key: str, limit: int = 8) -> list[str]:
        if not scope_key or limit <= 0:
            return []
        stmt = (
            select(col(cls.id)).where(col(cls.scope_key) == scope_key).order_by(col(cls.valid_at).desc()).limit(limit)
        )
        result = await session.execute(stmt)
        return [str(eid) for (eid,) in result.all() if isinstance(eid, str)]

    @classmethod
    @with_read_session
    async def get_session(cls, session: AsyncSession, session_id: str) -> list["AIMemEpisode"]:
        if not session_id:
            return []
        stmt = (
            select(cls)
            .where(col(cls.session_id) == session_id)
            .order_by(col(cls.turn_index).asc(), col(cls.valid_at).asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def list_by_sessions(
        cls,
        session: AsyncSession,
        session_ids: list[str],
        limit: int = 2000,
    ) -> list["AIMemEpisode"]:
        """命中 session 后的 RPE：一次取出原文 turn，不再按 hybrid 名次截断。"""
        if not session_ids or limit <= 0:
            return []
        stmt = (
            select(cls)
            .where(col(cls.session_id).in_(session_ids))
            .order_by(col(cls.valid_at).asc(), col(cls.turn_index).asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def session_map_by_ids(cls, session: AsyncSession, episode_ids: list[str]) -> dict[str, str]:
        """{episode_id: session_id}，只含已回填的行。"""
        if not episode_ids:
            return {}
        result = await session.execute(
            select(col(cls.id), col(cls.session_id)).where(
                col(cls.id).in_(episode_ids), col(cls.session_id).is_not(None)
            )
        )
        out: dict[str, str] = {}
        for eid, sid in result.all():
            if isinstance(eid, str) and isinstance(sid, str) and sid:
                out[eid] = sid
        return out

    @classmethod
    @with_read_session
    async def _has_null_session(cls, session: AsyncSession, scope_key: str) -> bool:
        stmt = select(cls.id).where(col(cls.scope_key) == scope_key, col(cls.session_id).is_(None)).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    @classmethod
    async def ensure_sessions(cls, scope_key: str, gap_seconds: int | None = None) -> int:
        """该 scope 仍有空 session_id 时回填一次。"""
        if not scope_key:
            return 0
        if not await cls._has_null_session(scope_key):
            return 0
        return await cls.backfill_sessions(scope_key, gap_seconds=gap_seconds)

    @classmethod
    async def backfill_sessions(cls, scope_key: str, gap_seconds: int | None = None) -> int:
        """只给仍为空的 session_id 补段。不删除、不改写已有 session。"""
        if not scope_key:
            return 0
        gap = gap_seconds if gap_seconds is not None else _session_gap_seconds()
        from sqlalchemy import case, update as _update

        from gsuid_core.ai_core.memory.database.session_split import naive_utc, plan_null_session_backfill
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard

        async with db_write_guard(), async_maker() as session:
            still_null = await session.execute(
                select(col(cls.id)).where(col(cls.scope_key) == scope_key, col(cls.session_id).is_(None)).limit(1)
            )
            if still_null.scalar_one_or_none() is None:
                return 0
            result = await session.execute(
                select(col(cls.id), col(cls.valid_at), col(cls.session_id), col(cls.turn_index)).where(
                    col(cls.scope_key) == scope_key
                )
            )
            stamped: list[tuple[str, datetime, str | None, int]] = []
            for eid, at, sid, turn in result.all():
                if not isinstance(eid, str) or not isinstance(at, datetime):
                    continue
                have = sid if isinstance(sid, str) and sid else None
                stamped.append((eid, naive_utc(at), have, turn if isinstance(turn, int) else 0))
            stamped.sort(key=lambda row: (row[1], row[3], row[0]))
            planned = plan_null_session_backfill(stamped, gap, lambda: str(uuid.uuid4()))
            if not planned:
                return 0
            fresh: dict[str, list[tuple[str, int, datetime]]] = {}
            extend: dict[str, list[tuple[str, int, datetime]]] = {}
            new_ids: set[str] = set()
            for item in planned:
                if item.is_new_session:
                    new_ids.add(item.session_id)
                slot = (item.episode_id, item.turn_index, item.valid_at)
                if item.session_id in new_ids:
                    fresh.setdefault(item.session_id, []).append(slot)
                else:
                    extend.setdefault(item.session_id, []).append(slot)
            for sid, slots in fresh.items():
                session.add(
                    AIMemSession(
                        id=sid,
                        scope_key=scope_key,
                        start_at=slots[0][2],
                        end_at=slots[-1][2],
                        n_turns=len(slots),
                        opener_episode_id=slots[0][0],
                    )
                )
            for sid, slots in extend.items():
                await session.execute(
                    _update(AIMemSession)
                    .where(col(AIMemSession.id) == sid)
                    .values(end_at=slots[-1][2], n_turns=col(AIMemSession.n_turns) + len(slots))
                )
            written = 0
            payload_items: list[tuple[str, str, int]] = []
            assigns = [(item.episode_id, item.session_id, item.turn_index) for item in planned]
            for offset in range(0, len(assigns), 400):
                chunk = assigns[offset : offset + 400]
                ids = [eid for eid, _sid, _turn in chunk]
                upd = await session.execute(
                    _update(cls)
                    .where(col(cls.id).in_(ids), col(cls.session_id).is_(None))
                    .values(
                        session_id=case(*[(col(cls.id) == eid, sid) for eid, sid, _turn in chunk], else_=""),
                        turn_index=case(*[(col(cls.id) == eid, turn) for eid, _sid, turn in chunk], else_=0),
                    )
                )
                if isinstance(upd, CursorResult):
                    written += upd.rowcount
                payload_items.extend(chunk)
            gist_ids = [eid for eid, _sid, _turn in assigns]
            gist_rows = await session.execute(
                select(col(AIMemTurnGist.episode_id)).where(
                    col(AIMemTurnGist.episode_id).in_(gist_ids),
                    col(AIMemTurnGist.session_id) == "",
                )
            )
            empty_gists = {eid for eid in gist_rows.scalars().all() if isinstance(eid, str)}
            gist_chunk = [item for item in assigns if item[0] in empty_gists]
            for offset in range(0, len(gist_chunk), 400):
                chunk = gist_chunk[offset : offset + 400]
                gids = [eid for eid, _sid, _turn in chunk]
                await session.execute(
                    _update(AIMemTurnGist)
                    .where(col(AIMemTurnGist.episode_id).in_(gids), col(AIMemTurnGist.session_id) == "")
                    .values(
                        session_id=case(
                            *[(col(AIMemTurnGist.episode_id) == eid, sid) for eid, sid, _turn in chunk],
                            else_="",
                        ),
                        turn_index=case(
                            *[(col(AIMemTurnGist.episode_id) == eid, turn) for eid, _sid, turn in chunk],
                            else_=0,
                        ),
                    )
                )
            await session.commit()

        try:
            from gsuid_core.ai_core.memory.vector.ops import set_episode_session_payload

            await set_episode_session_payload(payload_items)
        except Exception as e:
            logger.warning(t("log.memory.session_payload_fail", scope_key=scope_key, error=str(e)))
        logger.info(t("log.memory.session_backfill", scope_key=scope_key, n=written, sessions=len(fresh)))
        return written

    # ── §3.2① Episode 保留策略 / 冷热分集合 ──────────
    # Episode 是"每条放行消息都写"的无界增长主力（P0-2）。以下方法为生命周期 Worker
    # 提供"降级（热→冷）"与"每 scope 物理上限"两级裁剪支持，纯规则、零 LLM。

    @classmethod
    @with_session
    async def collect_episodes_to_demote(
        cls,
        session: AsyncSession,
        hot_days: int,
        hot_per_scope: int,
    ) -> list[tuple[str, str]]:
        """收集应从热集合降级为冷的 Episode（保留策略 = 引用 + 年龄 + 每 scope 最近 M 条）。

        一条**热**（is_archived=False）Episode 满足任一即受保护、不降级：
        ① 被存活 Entity 引用（mem_episode_entity_mentions 有关联，孤儿实体被 GC 时其
           mention 行同步清理，故"有 mention"≈"被存活实体引用"）；
        ② 年龄在 hot_days 内（valid_at 较新）；
        ③ 属于本 scope 最近 hot_per_scope 条（按 valid_at 倒序）。
        三者皆不满足的冷 Episode 才降级。返回 [(id, qdrant_id), ...]。

        实现：只处理"非归档条数 > hot_per_scope"的 scope（其余 scope 全部落在最近 M 条内、
        天然受保护）。"不在最近 M 条" ⟺ valid_at < 本 scope 第 M 新的 valid_at(recent_cutoff)；
        与年龄边界 age_cutoff 取较早者 effective_cutoff（两者都是"早于 X"，合取即 < min）。
        """
        if hot_per_scope <= 0:
            return []

        age_cutoff = datetime.now(timezone.utc) - timedelta(days=hot_days)

        scope_rows = await session.execute(
            select(cls.scope_key, func.count())
            .where(col(cls.is_archived).is_(False))
            .group_by(cls.scope_key)
            .having(func.count() > hot_per_scope)
        )
        over_scopes = [row[0] for row in scope_rows.all()]
        if not over_scopes:
            return []

        mention_exists = exists().where(mem_episode_entity_mentions.c.episode_id == cls.id)

        victims: list[tuple[str, str]] = []
        for scope_key in over_scopes:
            # 本 scope 第 hot_per_scope 新的非归档 Episode 的 valid_at（最近 M 条窗口下界）
            boundary_row = await session.execute(
                select(cls.valid_at)
                .where(cls.scope_key == scope_key, col(cls.is_archived).is_(False))
                .order_by(col(cls.valid_at).desc())
                .offset(hot_per_scope - 1)
                .limit(1)
            )
            recent_cutoff = boundary_row.scalar_one_or_none()
            if recent_cutoff is None:
                continue
            # SQLite 取出的 datetime 可能是 naive，需补 UTC 后才能与 aware 的 age_cutoff 比较
            if recent_cutoff.tzinfo is None:
                recent_cutoff = recent_cutoff.replace(tzinfo=timezone.utc)
            effective_cutoff = min(recent_cutoff, age_cutoff)

            result = await session.execute(
                select(cls.id, cls.qdrant_id).where(
                    cls.scope_key == scope_key,
                    col(cls.is_archived).is_(False),
                    col(cls.valid_at) < effective_cutoff,
                    ~mention_exists,
                )
            )
            victims.extend((row[0], row[1] or row[0]) for row in result.all())
        return victims

    @classmethod
    async def mark_archived_by_ids(cls, episode_ids: list[str]) -> int:
        """把一批 Episode 标记为已归档（冷）。集合式单条 UPDATE。返回受影响行数。"""
        if not episode_ids:
            return 0
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        return await under_db_write(lambda: cls._mark_archived_by_ids_db(episode_ids))

    @classmethod
    @with_session
    async def _mark_archived_by_ids_db(cls, session: AsyncSession, episode_ids: list[str]) -> int:
        from sqlalchemy import update as _update

        result = await session.execute(_update(cls).where(col(cls.id).in_(episode_ids)).values(is_archived=True))
        # AGENTS.md §3.5.2: session.execute 静态返回 Result[Any], 无 rowcount;
        # DML 实际返回 CursorResult。用 isinstance 守门取 rowcount, 不依赖 cast。
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    @with_session
    async def collect_episode_overflow(
        cls,
        session: AsyncSession,
        max_per_scope: int,
    ) -> list[tuple[str, str]]:
        """每 scope Episode 物理上限：对总条数超过 max_per_scope 的 scope，物理删除最老的
        "已归档(冷)且无 Entity 引用"的 Episode，把 SQL 体量也钉在可控范围。

        仅删冷且无引用者——热 Episode、被引用 Episode 受保护。返回 [(id, qdrant_id), ...]。
        """
        if max_per_scope <= 0:
            return []
        scope_rows = await session.execute(
            select(cls.scope_key, func.count()).group_by(cls.scope_key).having(func.count() > max_per_scope)
        )
        over_scopes = [(row[0], row[1]) for row in scope_rows.all()]
        if not over_scopes:
            return []

        mention_exists = exists().where(mem_episode_entity_mentions.c.episode_id == cls.id)

        victims: list[tuple[str, str]] = []
        for scope_key, count in over_scopes:
            to_remove = count - max_per_scope
            if to_remove <= 0:
                continue
            result = await session.execute(
                select(cls.id, cls.qdrant_id)
                .where(
                    cls.scope_key == scope_key,
                    col(cls.is_archived).is_(True),
                    ~mention_exists,
                )
                .order_by(col(cls.valid_at).asc())
                .limit(to_remove)
            )
            victims.extend((row[0], row[1] or row[0]) for row in result.all())
        return victims

    @classmethod
    async def purge_episodes_by_ids(cls, episode_ids: list[str]) -> int:
        """物理删除 Episode 并清理其 Episode-Entity 关联表。返回删除行数。"""
        if not episode_ids:
            return 0
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        return await under_db_write(lambda: cls._purge_episodes_by_ids_db(episode_ids))

    @classmethod
    @with_session
    async def _purge_episodes_by_ids_db(cls, session: AsyncSession, episode_ids: list[str]) -> int:
        from sqlalchemy import delete as _delete

        await session.execute(
            mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.episode_id.in_(episode_ids))
        )
        await session.execute(_delete(cls).where(col(cls.id).in_(episode_ids)))
        return len(episode_ids)

    @classmethod
    @with_session
    async def get_mentioned_entity_ids(cls, session: AsyncSession, episode_ids: list[str]) -> list[str]:
        """取一批 Episode 提及的全部 Entity ID（去重）。

        供 RF-Mem 回忆环的"关系投影"使用：向量回忆找到的模糊 Episode 链 → 投影出
        链上提及的实体 → 再反查精准 Edge 事实。见
        plans/rf_mem_dual_process_retrieval_assessment_20260614.md §4.2。
        """
        if not episode_ids:
            return []
        result = await session.execute(
            select(mem_episode_entity_mentions.c.entity_id)
            .where(mem_episode_entity_mentions.c.episode_id.in_(episode_ids))
            .distinct()
        )
        return [row[0] for row in result.all()]


def _session_gap_seconds() -> int:
    from gsuid_core.ai_core.memory.config import memory_config

    gap = memory_config.session_gap_seconds
    return gap if gap > 0 else 1800


async def _cursor_in_tx(session: AsyncSession, scope_key: str) -> SessionCursor:
    """写锁内读同 scope 最后一条，避免 allocate 与 insert 不在同一临界区。"""
    if not scope_key:
        return SessionCursor(None, 0, None)
    stmt = (
        select(AIMemEpisode)
        .where(col(AIMemEpisode.scope_key) == scope_key)
        .order_by(col(AIMemEpisode.valid_at).desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return SessionCursor(None, 0, None)
    return SessionCursor(row.session_id, row.turn_index, row.valid_at)


async def _touch_session_row(
    session: AsyncSession,
    *,
    session_id: str,
    scope_key: str,
    valid_at: datetime,
    opener_episode_id: str,
    is_new: bool,
) -> None:
    from sqlalchemy import update as _update

    from gsuid_core.ai_core.memory.database.session_split import naive_utc

    at = naive_utc(valid_at)
    if is_new:
        session.add(
            AIMemSession(
                id=session_id,
                scope_key=scope_key,
                start_at=at,
                end_at=at,
                n_turns=1,
                opener_episode_id=opener_episode_id,
            )
        )
        return
    await session.execute(
        _update(AIMemSession)
        .where(col(AIMemSession.id) == session_id)
        .values(end_at=at, n_turns=col(AIMemSession.n_turns) + 1)
    )


class AIMemSession(SQLModel, table=True):
    """时间 gap 切出的对话 session；title / thread_id 由睡眠期回填。"""

    __table_args__ = (
        Index("ix_mem_session_scope_start", "scope_key", "start_at"),
        Index("ix_mem_session_thread", "thread_id"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    start_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    end_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    n_turns: int = Field(default=0)
    opener_episode_id: str = Field(default="", max_length=36)
    title: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    title_qdrant_id: Optional[str] = Field(default=None, max_length=36)
    thread_id: Optional[str] = Field(default=None, max_length=36, index=True)
    title_source: str = Field(default="opener", max_length=16)

    @classmethod
    @with_read_session
    async def list_by_scope(
        cls,
        session: AsyncSession,
        scope_key: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 200,
    ) -> list["AIMemSession"]:
        if not scope_key or limit <= 0:
            return []
        conds: list[ColumnElement[bool]] = [_as_bool_expr(col(cls.scope_key) == scope_key)]
        if start is not None:
            lo = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo is not None else start
            conds.append(_as_bool_expr(col(cls.end_at) >= lo))
        if end is not None:
            hi = end.astimezone(timezone.utc).replace(tzinfo=None) if end.tzinfo is not None else end
            conds.append(_as_bool_expr(col(cls.start_at) < hi))
        stmt = select(cls).where(*conds).order_by(col(cls.start_at).asc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def openers_for(cls, session: AsyncSession, session_ids: list[str]) -> list["AIMemEpisode"]:
        if not session_ids:
            return []
        sess_rows = await session.execute(select(cls).where(col(cls.id).in_(session_ids)))
        opener_ids = [row.opener_episode_id for row in sess_rows.scalars().all() if row.opener_episode_id]
        if not opener_ids:
            return []
        ep_rows = await session.execute(
            select(AIMemEpisode).where(col(AIMemEpisode.id).in_(opener_ids)).order_by(col(AIMemEpisode.valid_at).asc())
        )
        return list(ep_rows.scalars().all())

    @classmethod
    @with_read_session
    async def get_by_ids(cls, session: AsyncSession, session_ids: list[str]) -> list["AIMemSession"]:
        if not session_ids:
            return []
        result = await session.execute(select(cls).where(col(cls.id).in_(session_ids)))
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def untitled_for_scope(cls, session: AsyncSession, scope_key: str, limit: int = 80) -> list["AIMemSession"]:
        if not scope_key or limit <= 0:
            return []
        stmt = (
            select(cls)
            .where(col(cls.scope_key) == scope_key, col(cls.thread_id).is_(None))
            .order_by(col(cls.end_at).desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def list_untitled(cls, session: AsyncSession, limit: int = 8) -> list["AIMemSession"]:
        if limit <= 0:
            return []
        stmt = select(cls).where(col(cls.thread_id).is_(None)).order_by(col(cls.end_at).desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def ids_for_threads(cls, session: AsyncSession, thread_ids: list[str]) -> list[str]:
        if not thread_ids:
            return []
        result = await session.execute(select(col(cls.id)).where(col(cls.thread_id).in_(thread_ids)))
        return [str(sid) for (sid,) in result.all() if isinstance(sid, str)]

    @classmethod
    @with_session
    async def set_title(cls, session: AsyncSession, session_id: str, title: str, source: str) -> None:
        if not session_id or not title:
            return
        stmt = update(cls).where(col(cls.id) == session_id).values(title=title[:80], title_source=source[:16])
        await session.execute(stmt)


class AIMemTurnGist(SQLModel, table=True):
    """用户 turn 一行 gist（侧表，不改 Episode）。"""

    __table_args__ = (
        Index("ix_mem_gist_scope_valid", "scope_key", "valid_at", "turn_index"),
        Index("ix_mem_gist_session_turn", "session_id", "turn_index"),
    )

    episode_id: str = Field(primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    session_id: str = Field(default="", max_length=36, index=True)
    turn_index: int = Field(default=0)
    valid_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    gist: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    gist_source: str = Field(default="rule", max_length=8)
    is_new_aspect: Optional[bool] = Field(default=None)
    code_digest: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    source_tag: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    @with_read_session
    async def list_by_scope(
        cls,
        session: AsyncSession,
        scope_key: str,
        limit: int = 4000,
    ) -> list["AIMemTurnGist"]:
        if not scope_key or limit <= 0:
            return []
        stmt = (
            select(cls)
            .where(col(cls.scope_key) == scope_key)
            .order_by(col(cls.valid_at).asc(), col(cls.turn_index).asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def list_by_session(cls, session: AsyncSession, session_id: str) -> list["AIMemTurnGist"]:
        if not session_id:
            return []
        stmt = select(cls).where(col(cls.session_id) == session_id).order_by(col(cls.turn_index).asc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def upsert_rows(cls, session: AsyncSession, rows: list["AIMemTurnGist"]) -> int:
        if not rows:
            return 0
        n = 0
        for row in rows:
            exist = await session.get(cls, row.episode_id)
            if exist is None:
                session.add(row)
            else:
                exist.gist = row.gist
                exist.gist_source = row.gist_source
                exist.is_new_aspect = row.is_new_aspect
                exist.code_digest = row.code_digest
                exist.source_tag = row.source_tag
                exist.model = row.model
                exist.session_id = row.session_id
                exist.turn_index = row.turn_index
                exist.valid_at = row.valid_at
                exist.scope_key = row.scope_key
            n += 1
        return n


class AIMemThread(SQLModel, table=True):
    """跨 session 的主题线程（Graphiti Saga / Phase 5）。"""

    __table_args__ = (Index("ix_mem_thread_scope_first", "scope_key", "first_at"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    title: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    aliases: List[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    first_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    n_sessions: int = Field(default=0)
    qdrant_id: str = Field(default="", max_length=36)
    mention_count: int = Field(default=1)
    last_mentioned_at: Optional[datetime] = Field(default=None)
    title_source: str = Field(default="opener", max_length=16)
    watermark_valid_at: Optional[datetime] = Field(default=None)

    @classmethod
    @with_read_session
    async def list_by_scope(cls, session: AsyncSession, scope_key: str, limit: int = 80) -> list["AIMemThread"]:
        if not scope_key or limit <= 0:
            return []
        stmt = select(cls).where(col(cls.scope_key) == scope_key).order_by(col(cls.first_at).asc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def list_by_title_source(cls, session: AsyncSession, source: str, limit: int = 8) -> list["AIMemThread"]:
        if not source or limit <= 0:
            return []
        stmt = select(cls).where(col(cls.title_source) == source).order_by(col(cls.last_at).desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def apply_llm_title(
        cls,
        session: AsyncSession,
        thread_id: str,
        title: str,
        aliases: list[str],
    ) -> None:
        if not thread_id or not title:
            return
        from sqlalchemy import update as _update

        await session.execute(
            _update(cls)
            .where(col(cls.id) == thread_id)
            .values(title=title[:240], aliases=aliases[:8], title_source="llm")
        )


# ─────────────────────────────────────────────
# Entity：提取出的实体节点（Base Graph 第二层）
# ─────────────────────────────────────────────
class AIMemEntity(SQLModel, table=True):
    """从 Episode 中提取的实体，是知识图谱的核心节点。"""

    __table_args__ = (
        UniqueConstraint("scope_key", "name", name="uq_entity_scope_name"),
        Index("ix_mem_entity_scope_name", "scope_key", "name"),
        Index("ix_mem_entity_scope_speaker", "scope_key", "is_speaker"),
        Index("ix_mem_entity_scope_id", "scope_key", "id"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    name: str = Field(max_length=256)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    tag: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    is_speaker: bool = Field(default=False)
    user_id: Optional[str] = Field(default=None, index=True, max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    qdrant_id: str = Field(index=True, max_length=36)

    episodes: List["AIMemEpisode"] = Relationship(
        back_populates="mentioned_entities",
        sa_relationship_kwargs={
            "secondary": mem_episode_entity_mentions,
            "back_populates": "mentioned_entities",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )
    outgoing_edges: List["AIMemEdge"] = Relationship(
        back_populates="source_entity",
        sa_relationship_kwargs={
            "primaryjoin": "AIMemEntity.id == AIMemEdge.source_entity_id",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    incoming_edges: List["AIMemEdge"] = Relationship(
        back_populates="target_entity",
        sa_relationship_kwargs={
            "primaryjoin": "AIMemEntity.id == AIMemEdge.target_entity_id",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    # ── entity_dedup ──────────────────────────

    @classmethod
    @with_session
    async def find_existing(
        cls,
        session: AsyncSession,
        scope_key: str,
        name: str,
    ) -> Optional["AIMemEntity"]:
        """两阶段去重查找：精确匹配 → 混合检索（BM25+向量）"""
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.memory.vector.ops import _hybrid_search_entities

        # 阶段1：精确匹配
        result = await session.execute(select(cls).where(cls.scope_key == scope_key, cls.name == name))
        entity = result.scalar_one_or_none()
        if entity:
            return entity

        # 阶段2：混合检索（BM25+向量 RRF 融合），比纯向量更可靠
        similar = await _hybrid_search_entities(
            query=name,
            scope_keys=[scope_key],
            top_k=3,
        )
        threshold = memory_config.dedup_similarity_threshold
        if similar and similar[0]["score"] >= threshold:
            result = await session.execute(select(cls).where(cls.id == similar[0]["id"]))
            return result.scalar_one_or_none()

        return None

    @classmethod
    def _normalize_entity_data_names(cls, entities_data: list[dict]) -> None:
        """与 extract_and_upsert 相同：Speaker 的 user_ 前缀剥成裸 id（就地改 dict）。"""
        for ed in entities_data:
            if "is_speaker" in ed or "Speaker" in (ed["tag"] if "tag" in ed else []):
                name = ed["name"] if "name" in ed else ""
                if name.startswith("user_"):
                    ed["name"] = name[len("user_") :]

    @classmethod
    async def prefetch_hybrid_name_ids(
        cls,
        scope_key: str,
        entities_data: list[dict],
    ) -> dict[str, str]:
        """写锁外：对 SQL 未精确命中的名称做混合检索，返回 {name: entity_id}。

        extract_and_upsert 在锁内只消费此 map + 再做一次精确 SQL，避免持锁做 embed/Qdrant。
        名称归一与写路径一致（先剥 speaker 的 user_），避免 hybrid 键对不上。
        """
        from gsuid_core.ai_core.memory.config import memory_config as _mc

        if _mc.eval_mode:
            return {}
        # 与写路径同一套 name 归一，保证 hybrid_name_ids 的 key 与锁内 all_names 一致
        cls._normalize_entity_data_names(entities_data)
        all_names = list(
            {
                (ed["name"] if "name" in ed else "").strip()
                for ed in entities_data
                if (ed["name"] if "name" in ed else "").strip()
            }
        )
        if not all_names:
            return {}
        async with async_maker() as session:
            result = await session.execute(
                select(cls.name).where(cls.scope_key == scope_key, col(cls.name).in_(all_names))
            )
            exact = {row[0] for row in result.all()}
        unmatched = [n for n in all_names if n not in exact]
        if not unmatched:
            return {}

        from gsuid_core.ai_core.memory.vector.ops import _hybrid_search_entities

        async def _hybrid_one(uname: str) -> tuple[str, list]:
            similar = await _hybrid_search_entities(query=uname, scope_keys=[scope_key], top_k=3)
            return uname, similar

        search_results = await asyncio.gather(*[_hybrid_one(n) for n in unmatched])
        hits: dict[str, str] = {}
        async with async_maker() as session:
            for uname, similar in search_results:
                if similar and similar[0]["score"] >= _mc.dedup_similarity_threshold:
                    sid = similar[0]["id"]
                    r = await session.execute(select(cls.id).where(cls.id == sid, cls.scope_key == scope_key))
                    matched_id = r.scalar_one_or_none()
                    if matched_id:
                        hits[uname] = matched_id
        return hits

    @classmethod
    async def extract_and_upsert(
        cls,
        session: AsyncSession,
        scope_key: str,
        entities_data: list[dict],
        episode_id: str,
        speaker_ids: list[str],
        *,
        hybrid_name_ids: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], list[dict], int]:
        """
        Returns:
            name_to_id,
            vector_payloads,
            new_entity_count  ← 新建实体数量（仅新建不包含已更新）

        hybrid_name_ids: 写锁外 prefetch 的语义去重结果；传入则锁内不再跑 Qdrant。
        """

        name_to_id: dict[str, str] = {}
        vector_payloads: list[dict] = []
        new_entity_count: int = 0

        # 与 prefetch 共用归一（prefetch 已剥过则幂等）；直调本方法时仍保证行为一致
        cls._normalize_entity_data_names(entities_data)

        # 再补充 speaker（此时 LLM 已有的会被精确匹配去重）
        """
        existing_speaker_names = {
            ed["name"] if "name" in ed else ""
            for ed in entities_data
            if "Speaker" in (ed["tag"] if "tag" in ed else [])
        }
        """
        existing_map: dict[str, AIMemEntity] = {}
        all_names = [
            (ed["name"] if "name" in ed else "").strip() for ed in entities_data if (ed["name"] if "name" in ed else "")
        ]
        if all_names:
            result = await session.execute(select(cls).where(cls.scope_key == scope_key, col(cls.name).in_(all_names)))
            existing_map = {e.name: e for e in result.scalars().all()}

        # 向量去重：优先用锁外 prefetch；未传 hybrid_name_ids 时回落锁内检索（兼容直调）。
        unmatched_names = [n for n in all_names if n not in existing_map]
        from gsuid_core.ai_core.memory.config import memory_config as _mc

        if unmatched_names and _mc.eval_mode:
            unmatched_names = []

        if unmatched_names and hybrid_name_ids is not None:
            for uname in unmatched_names:
                if uname not in hybrid_name_ids:
                    continue
                sid = hybrid_name_ids[uname]
                r = await session.execute(select(cls).where(cls.id == sid, cls.scope_key == scope_key))
                matched = r.scalar_one_or_none()
                if matched:
                    existing_map[uname] = matched
        elif unmatched_names:
            from gsuid_core.ai_core.memory.vector.ops import _hybrid_search_entities

            async def _hybrid_search_for_name(uname: str) -> tuple[str, list]:
                similar = await _hybrid_search_entities(
                    query=uname,
                    scope_keys=[scope_key],
                    top_k=3,
                )
                return uname, similar

            search_results = await asyncio.gather(*[_hybrid_search_for_name(n) for n in unmatched_names])
            for uname, similar in search_results:
                if similar and similar[0]["score"] >= _mc.dedup_similarity_threshold:
                    sid = similar[0]["id"]
                    r = await session.execute(select(cls).where(cls.id == sid, cls.scope_key == scope_key))
                    matched = r.scalar_one_or_none()
                    if matched:
                        existing_map[uname] = matched

        # Summary 增长上限，防止无限拼接导致 token 超限
        _MAX_SUMMARY_CHARS = 2000

        for entity_data in entities_data:
            name = (entity_data["name"] if "name" in entity_data else "").strip()
            if not name:
                continue

            existing = existing_map[name] if name in existing_map else None

            if existing:
                new_summary = entity_data["summary"] if "summary" in entity_data else ""
                if new_summary and new_summary not in existing.summary:
                    combined = f"{existing.summary}\n{new_summary}".strip()
                    if len(combined) > _MAX_SUMMARY_CHARS:
                        # Bug-06 修复：保留头部（早期上下文通常更重要），添加截断提示
                        combined = combined[:_MAX_SUMMARY_CHARS] + "\n[...早期记忆已截断...]"
                    existing.summary = combined

                existing_tags = existing.tag if isinstance(existing.tag, list) else []
                merged_tags = list(set(existing_tags) | set(entity_data["tag"] if "tag" in entity_data else []))

                existing.tag = merged_tags
                existing.updated_at = datetime.now(timezone.utc)
                session.add(existing)

                # 👉 收集 vector payload
                vector_payloads.append(
                    {
                        "entity_id": existing.id,
                        "name": existing.name,
                        "summary": existing.summary,
                        "scope_key": scope_key,
                        "is_speaker": existing.is_speaker,
                        "user_id": existing.user_id,
                        "tag": merged_tags,
                    }
                )

                name_to_id[name] = existing.id

            else:
                entity_id = str(uuid.uuid4())
                tag_list = entity_data["tag"] if "tag" in entity_data else []
                new_entity_count += 1

                new_entity = cls(
                    id=entity_id,
                    scope_key=scope_key,
                    name=name,
                    summary=entity_data["summary"] if "summary" in entity_data else "",
                    tag=tag_list,
                    is_speaker=entity_data["is_speaker"] if "is_speaker" in entity_data else False,
                    user_id=entity_data["user_id"] if "user_id" in entity_data else None,
                    qdrant_id=entity_id,
                )
                session.add(new_entity)

                vector_payloads.append(
                    {
                        "entity_id": entity_id,
                        "name": name,
                        "summary": entity_data["summary"] if "summary" in entity_data else "",
                        "scope_key": scope_key,
                        "is_speaker": entity_data["is_speaker"] if "is_speaker" in entity_data else False,
                        "user_id": entity_data["user_id"] if "user_id" in entity_data else None,
                        "tag": tag_list,
                    }
                )

                name_to_id[name] = entity_id

        # 写入 Episodic Edge（Episode ↔ Entity 关联），论文 Section 2.1
        if episode_id and name_to_id:
            # 查出已存在的关联，避免重复插入
            existing_mentions = await session.execute(
                select(mem_episode_entity_mentions.c.entity_id).where(
                    mem_episode_entity_mentions.c.episode_id == episode_id
                )
            )
            existing_entity_ids = {row[0] for row in existing_mentions.fetchall()}

            # name_to_id.values() 可能包含重复的 entity_id（不同 name 去重后指向同一 Entity），
            # 需要先 set() 去重，再与已有关联做差集
            new_relations = [
                {"episode_id": episode_id, "entity_id": entity_id}
                for entity_id in set(name_to_id.values())
                if entity_id not in existing_entity_ids
            ]
            if new_relations:
                await session.execute(insert(mem_episode_entity_mentions), new_relations)

        return name_to_id, vector_payloads, new_entity_count

    # ── 孤儿实体回收（C11 扩展：实体级 GC）──────────
    # Edge 会被生命周期 Worker 衰减→遗忘物理删除，但其连接的 Entity 没人回收，
    # 久而久之孤儿实体只增不减，膨胀分层图分类成本。以下两个方法为后台 GC 提供支持。

    @classmethod
    @with_session
    async def collect_orphans(
        cls,
        session: AsyncSession,
        ttl_days: int = 10,
    ) -> list[tuple[str, str, str]]:
        """收集应被回收的孤儿实体：非 speaker、无任何 edge、且超过 ttl_days 未更新。

        - 排除 is_speaker：群成员花名册即使无 edge 也要保留。
        - updated_at 时效护栏：避免误删刚抽出、edge 尚未形成的新实体。
        - 无 edge：孤儿实体不进 prompt、不承载事实，回收不损失召回质量。

        返回 [(id, scope_key, qdrant_id), ...]，供上层删 SQL / 删向量 / 递减计数。
        """
        from sqlalchemy import exists

        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        has_edge = exists().where(
            or_(
                col(AIMemEdge.source_entity_id) == cls.id,
                col(AIMemEdge.target_entity_id) == cls.id,
            )
        )
        result = await session.execute(
            select(cls.id, cls.scope_key, cls.qdrant_id).where(
                col(cls.is_speaker).is_(False),
                col(cls.updated_at) < cutoff,
                ~has_edge,
            )
        )
        return [(row[0], row[1], row[2] or row[0]) for row in result.all()]

    @classmethod
    async def purge_orphans_by_ids(cls, entity_ids: list[str]) -> int:
        """物理删除孤儿实体并清理其 Episode / Category 关联表。返回删除行数。"""
        if not entity_ids:
            return 0
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        return await under_db_write(lambda: cls._purge_orphans_by_ids_db(entity_ids))

    @classmethod
    @with_session
    async def _purge_orphans_by_ids_db(cls, session: AsyncSession, entity_ids: list[str]) -> int:
        from sqlalchemy import delete as _delete

        # 先清两张多对多关联表，再删主表，避免悬空外键
        await session.execute(
            mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.entity_id.in_(entity_ids))
        )
        await session.execute(
            mem_category_entity_members.delete().where(mem_category_entity_members.c.entity_id.in_(entity_ids))
        )
        await session.execute(_delete(cls).where(col(cls.id).in_(entity_ids)))
        return len(entity_ids)

    @classmethod
    @with_session
    async def collect_capacity_overflow(
        cls,
        session: AsyncSession,
        max_per_scope: int,
    ) -> list[tuple[str, str, str]]:
        """每 scope Entity 软上限（§3.2③，容量驱动裁剪）：对实体数超过 max_per_scope 的
        scope，淘汰最弱的"非 speaker、无任何 edge"实体，把单群体量钉在可控范围。

        - 仅回收无 edge 的非 speaker 实体：FK 安全、不牵连任何 fact，与孤儿 GC 同样无损召回，
          区别仅在于由"容量"而非"10 天 TTL"触发，防止超活跃群在两次 GC 之间堆出海量噪声实体。
        - speaker（群成员花名册）受保护——其生命周期由"说话人沉默 TTL"另行处理。
        - 有 edge 的实体受保护——承载 fact，不能在不删边的前提下回收。
        - salience 近似：updated_at 倒序为强（最近活跃），故在可回收集合中淘汰最旧的。

        返回 [(id, scope_key, qdrant_id), ...]，供上层删 SQL / 删向量 / 递减分层图计数。
        """
        if max_per_scope <= 0:
            return []

        scope_rows = await session.execute(
            select(cls.scope_key, func.count()).group_by(cls.scope_key).having(func.count() > max_per_scope)
        )
        over_scopes = [(row[0], row[1]) for row in scope_rows.all()]
        if not over_scopes:
            return []

        has_edge = exists().where(
            or_(
                col(AIMemEdge.source_entity_id) == cls.id,
                col(AIMemEdge.target_entity_id) == cls.id,
            )
        )

        victims: list[tuple[str, str, str]] = []
        for scope_key, count in over_scopes:
            to_remove = count - max_per_scope
            if to_remove <= 0:
                continue
            result = await session.execute(
                select(cls.id, cls.scope_key, cls.qdrant_id)
                .where(
                    cls.scope_key == scope_key,
                    col(cls.is_speaker).is_(False),
                    ~has_edge,
                )
                .order_by(col(cls.updated_at).asc())
                .limit(to_remove)
            )
            victims.extend((row[0], row[1], row[2] or row[0]) for row in result.all())
        return victims

    @classmethod
    @with_session
    async def get_frequent_names(
        cls,
        session: AsyncSession,
        scope_key: str,
        limit: int = 20,
    ) -> list[str]:
        """获取本 scope 最近活跃的非发言者实体名，作为跨批次消歧锚点（C2-b）。

        无独立的提及计数列，故以 updated_at 倒序近似"高频活跃"，
        并排除 Speaker 实体（其 name 为 user_id，对消歧无意义）。
        """
        result = await session.execute(
            select(cls.name)
            .where(cls.scope_key == scope_key, col(cls.is_speaker).is_(False))
            .order_by(col(cls.updated_at).desc())
            .limit(limit)
        )
        return [row[0] for row in result.all()]

    @classmethod
    @with_session
    async def get_names_by_ids(cls, session: AsyncSession, entity_ids: list[str]) -> dict[str, str]:
        """批量取 {entity_id: name}（供 RF-Mem 关系投影补全 Edge 的 source/target 名称）。"""
        if not entity_ids:
            return {}
        result = await session.execute(select(cls.id, cls.name).where(col(cls.id).in_(entity_ids)))
        return {row[0]: row[1] for row in result.all()}

    @classmethod
    @with_session
    async def list_distinct_scope_keys(cls, session: AsyncSession) -> List[str]:
        result = await session.execute(select(col(cls.scope_key)).distinct())
        return [row[0] for row in result.all() if row[0]]

    @classmethod
    @with_session
    async def list_page_by_scope(
        cls,
        session: AsyncSession,
        scope_key: str,
        *,
        offset: int,
        limit: int,
    ) -> List["AIMemEntity"]:
        stmt = (
            select(cls)
            .where(col(cls.scope_key) == scope_key)
            .order_by(col(cls.updated_at).desc())
            .offset(offset)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())


# ─────────────────────────────────────────────
# Edge：实体间的关系（Base Graph 第三层）
# ─────────────────────────────────────────────
class EventWriteRow(TypedDict):
    summary: str
    start_at: datetime | None
    end_at: datetime | None
    entities: list[str]
    aliases: list[str]
    stated_at: NotRequired[datetime | None]
    event_at: NotRequired[datetime | None]
    thread_id: NotRequired[str | None]
    turn_episode_id: NotRequired[str | None]


class AIMemEvent(SQLModel, table=True):
    """抽取出的时间事件（摘要 + 归一化日期 + 别名），供排序/时间线题按时间检索。"""

    __table_args__ = (
        Index("ix_mem_event_scope_start", "scope_key", "start_at"),
        Index("ix_mem_event_thread", "thread_id", "stated_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    episode_id: str = Field(default="", index=True, max_length=36)
    summary: str = Field(sa_column=Column(Text, nullable=False))
    start_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    end_at: Optional[datetime] = Field(default=None)
    entities: List[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    aliases: List[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    stated_at: Optional[datetime] = Field(default=None)
    event_at: Optional[datetime] = Field(default=None)
    thread_id: Optional[str] = Field(default=None, max_length=36, index=True)
    turn_episode_id: Optional[str] = Field(default=None, max_length=36)
    mention_count: int = Field(default=1)
    last_mentioned_at: Optional[datetime] = Field(default=None)
    qdrant_id: Optional[str] = Field(default=None, max_length=36)

    @classmethod
    async def upsert_events_bulk(
        cls,
        scope_key: str,
        rows: list[EventWriteRow],
        *,
        episode_id: str = "",
        fallback_at: Optional[datetime] = None,
    ) -> int:
        """批量写入事件；同 scope 内摘要重复只跳过（抽取窗口重叠时的自然去重）。"""
        if not rows:
            return 0
        inserted = 0
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard

        async with db_write_guard(), async_maker() as session:
            existing = await session.execute(select(col(cls.summary)).where(col(cls.scope_key) == scope_key))
            seen = {str(s) for s in existing.scalars().all()}
            for row in rows:
                summary = str(row["summary"]) if "summary" in row else ""
                summary = summary.strip()
                if not summary or summary in seen:
                    continue
                raw_start = row["start_at"] if "start_at" in row else None
                raw_end = row["end_at"] if "end_at" in row else None
                raw_entities = row["entities"] if "entities" in row else None
                raw_aliases = row["aliases"] if "aliases" in row else None
                start_at = raw_start if isinstance(raw_start, datetime) else (fallback_at or datetime.now(timezone.utc))
                raw_stated = row["stated_at"] if "stated_at" in row else None
                raw_event = row["event_at"] if "event_at" in row else None
                raw_thread = row["thread_id"] if "thread_id" in row else None
                raw_turn = row["turn_episode_id"] if "turn_episode_id" in row else None
                session.add(
                    cls(
                        scope_key=scope_key,
                        episode_id=episode_id,
                        summary=summary,
                        start_at=start_at,
                        end_at=raw_end if isinstance(raw_end, datetime) else None,
                        entities=[str(x) for x in raw_entities] if isinstance(raw_entities, list) else [],
                        aliases=[str(x) for x in raw_aliases] if isinstance(raw_aliases, list) else [],
                        stated_at=raw_stated if isinstance(raw_stated, datetime) else start_at,
                        event_at=raw_event if isinstance(raw_event, datetime) else None,
                        thread_id=str(raw_thread) if isinstance(raw_thread, str) else None,
                        turn_episode_id=str(raw_turn) if isinstance(raw_turn, str) else episode_id,
                    )
                )
                seen.add(summary)
                inserted += 1
            await session.commit()
        return inserted

    @classmethod
    @with_read_session
    async def search_by_scope(cls, session: AsyncSession, scope_key: str, *, limit: int = 240) -> List["AIMemEvent"]:
        """按时间升序取该 scope 的事件（时间线/排序题用）。"""
        stmt = select(cls).where(col(cls.scope_key) == scope_key).order_by(col(cls.start_at)).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def list_by_threads(cls, session: AsyncSession, thread_ids: list[str], limit: int = 64) -> list["AIMemEvent"]:
        if not thread_ids or limit <= 0:
            return []
        stamp = func.coalesce(col(cls.event_at), col(cls.stated_at), col(cls.start_at))
        stmt = select(cls).where(col(cls.thread_id).in_(thread_ids)).order_by(stamp.asc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


class AIMemEdge(SQLModel, table=True):
    """实体之间的有向关系边，存储一条可验证的事实。"""

    __table_args__ = (
        Index("ix_mem_edge_scope_valid", "scope_key", "valid_at", "invalid_at"),
        Index("ix_mem_edge_source", "source_entity_id"),
        Index("ix_mem_edge_target", "target_entity_id"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    fact: str = Field(sa_column=Column(Text, nullable=False))
    source_entity_id: str = Field(foreign_key="aimementity.id", max_length=36)
    target_entity_id: str = Field(foreign_key="aimementity.id", max_length=36)
    valid_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    invalid_at: Optional[datetime] = Field(default=None)
    expired_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    qdrant_id: str = Field(index=True, max_length=36)
    # C1 跨发言者归并：同一 fact 被不同 source 重复陈述时，命中既有 Edge 只累加此计数，
    # 不再写入 N 条重复 Edge。旧库无此列，由 startup.py 的 ALTER 语句补齐（默认 1）。
    mention_count: int = Field(default=1)
    # C11 记忆生命周期：时效衰减分。检索排序按 reranker_score × decay_score 加权；
    # 长期未被检索的 Edge 由衰减 Worker 周期性下调，decay_score < 阈值则被遗忘。
    # 旧库无此列，由 startup.py 的 ALTER 语句补齐（默认 1.0）。
    decay_score: float = Field(default=1.0)
    # C11：最近一次被检索命中的时间，衰减判定的依据。旧库由 ALTER 补齐（默认 NULL）。
    last_accessed: Optional[datetime] = Field(default=None)

    source_entity: Optional["AIMemEntity"] = Relationship(
        back_populates="outgoing_edges",
        sa_relationship_kwargs={
            "foreign_keys": "[AIMemEdge.source_entity_id]",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    target_entity: Optional["AIMemEntity"] = Relationship(
        back_populates="incoming_edges",
        sa_relationship_kwargs={
            "foreign_keys": "[AIMemEdge.target_entity_id]",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    @classmethod
    @with_session
    async def get_for_entities(
        cls,
        session: AsyncSession,
        entity_ids: list[str],
        scope_key: str,
        limit: int = 30,
    ) -> list["AIMemEdge"]:
        """获取与指定 Entity 关联的有效 Edge（invalid_at 空或未到 as-of）。"""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        result = await session.execute(
            select(cls)
            .where(
                cls.scope_key == scope_key,
                or_(col(cls.invalid_at).is_(None), col(cls.invalid_at) > now),
                or_(
                    col(cls.source_entity_id).in_(entity_ids),
                    col(cls.target_entity_id).in_(entity_ids),
                ),
            )
            .order_by(col(cls.valid_at).desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def list_pair_versions(
        cls,
        session: AsyncSession,
        scope_key: str,
        pairs: list[tuple[str, str]],
        limit: int = 40,
    ) -> list["AIMemEdge"]:
        """同 (src,tgt) 全版本，含已失效，按 valid_at 升序。"""
        if not scope_key or not pairs or limit <= 0:
            return []
        src_ids = {a for a, _b in pairs}
        tgt_ids = {b for _a, b in pairs}
        want = set(pairs)
        stmt = (
            select(cls)
            .where(
                col(cls.scope_key) == scope_key,
                col(cls.source_entity_id).in_(src_ids),
                col(cls.target_entity_id).in_(tgt_ids),
            )
            .order_by(col(cls.valid_at).asc())
            .limit(max(limit * 2, 8))
        )
        result = await session.execute(stmt)
        kept: list[AIMemEdge] = []
        for row in result.scalars().all():
            if (row.source_entity_id, row.target_entity_id) not in want:
                continue
            kept.append(row)
            if len(kept) >= limit:
                break
        return kept

    @classmethod
    @with_read_session
    async def search_by_tokens(
        cls,
        session: AsyncSession,
        scope_key: str,
        tokens: list[str],
        limit: int = 24,
    ) -> list["AIMemEdge"]:
        """按 token 词面召回本 scope 的 Edge（每 token 限条后 UNION，一次往返）。"""
        if not tokens or not scope_key:
            return []
        c = _model_table(cls).c
        id_q = _ids_matching_tokens(
            _as_str_col(c.id),
            _as_str_col(c.scope_key),
            _as_str_col(c.fact),
            _as_dt_col(c.valid_at),
            scope_key,
            tokens[:18],
            limit,
        )
        stmt = select(cls).where(c.id.in_(id_q))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ── C11 记忆生命周期 ───────────────────────────

    @classmethod
    @with_session
    async def get_confidence_inputs(cls, session: AsyncSession, edge_ids: list[str]) -> dict[str, tuple[int, float]]:
        """批量取边的置信度输入 {id: (mention_count, decay_score)}。

        检索期 dual_route 据此富集 Edge.weight（置信度=佐证×新鲜度）。decay_score 由衰减
        Worker 周期更新、且只落 DB（Qdrant 载荷会过期），故置信度一律以 DB 实时值为准。
        """
        if not edge_ids:
            return {}
        result = await session.execute(
            select(cls.id, cls.mention_count, cls.decay_score).where(col(cls.id).in_(edge_ids))
        )
        return {row[0]: (row[1], row[2]) for row in result.all()}

    @classmethod
    async def touch_accessed(cls, edge_ids: list[str]) -> None:
        """把一批 Edge 标记为"刚被检索命中"，刷新 last_accessed（C11 Decay 依据）。"""
        if not edge_ids:
            return
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        await under_db_write(lambda: cls._touch_accessed_db(edge_ids))

    @classmethod
    @with_session
    async def _touch_accessed_db(cls, session: AsyncSession, edge_ids: list[str]) -> None:
        from sqlalchemy import update as _update

        await session.execute(
            _update(cls).where(col(cls.id).in_(edge_ids)).values(last_accessed=datetime.now(timezone.utc))
        )

    @classmethod
    async def apply_decay(
        cls,
        stale_days: int = 14,
        decay_factor: float = 0.85,
        protect_mention_count: int = 3,
    ) -> int:
        """时效衰减（C11）：超过 stale_days 未被检索、且非高频提及的有效 Edge，
        ``decay_score *= decay_factor``。返回受影响行数。

        集合式 UPDATE（非逐行）；经 db_write_guard 与热路径写排队，避免维护窗写风暴。
        """
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        return await under_db_write(
            lambda: cls._apply_decay_db(
                stale_days=stale_days,
                decay_factor=decay_factor,
                protect_mention_count=protect_mention_count,
            )
        )

    @classmethod
    @with_session
    async def _apply_decay_db(
        cls,
        session: AsyncSession,
        stale_days: int = 14,
        decay_factor: float = 0.85,
        protect_mention_count: int = 3,
    ) -> int:
        from sqlalchemy import update as _update

        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
        result = await session.execute(
            _update(cls)
            .where(
                col(cls.invalid_at).is_(None),
                col(cls.mention_count) < protect_mention_count,
                or_(col(cls.last_accessed).is_(None), col(cls.last_accessed) < cutoff),
            )
            .values(decay_score=func.coalesce(col(cls.decay_score), 1.0) * decay_factor)
        )
        # AGENTS.md §3.5.2: rowcount 仅对 CursorResult 暴露, 用 isinstance 守门
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    @with_session
    async def collect_forgotten(cls, session: AsyncSession, threshold: float = 0.1) -> list[str]:
        """收集 decay_score 已低于阈值、应被遗忘（物理删除）的 Edge ID 列表（C11）。"""
        result = await session.execute(select(cls.id, cls.qdrant_id).where(col(cls.decay_score) < threshold))
        return [row[1] or row[0] for row in result.all()]

    @classmethod
    async def purge_by_ids(cls, edge_ids: list[str]) -> int:
        """按 ID 物理删除 Edge（C11 遗忘）。返回删除行数。"""
        if not edge_ids:
            return 0
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        return await under_db_write(lambda: cls._purge_by_ids_db(edge_ids))

    @classmethod
    @with_session
    async def _purge_by_ids_db(cls, session: AsyncSession, edge_ids: list[str]) -> int:
        from sqlalchemy import delete as _delete

        await session.execute(_delete(cls).where(col(cls.qdrant_id).in_(edge_ids)))
        return len(edge_ids)

    @classmethod
    @with_session
    async def collect_capacity_overflow(
        cls,
        session: AsyncSession,
        max_per_scope: int,
    ) -> list[str]:
        """每 scope Edge 软上限（§3.2③，容量驱动裁剪）：对边数超过 max_per_scope 的 scope，
        按 salience 降序保留 top-N，返回应被淘汰的长尾 Edge 的 qdrant_id 列表（供 SQL+向量删除）。

        salience 纯由现成列计算、零 LLM，排序依次：有效边（invalid_at 为空）优先 →
        mention_count（跨发言者佐证）→ decay_score（新鲜度）→ 最近访问/最近有效时间。
        offset(max_per_scope) 之后的即为本 scope 最弱的长尾，被淘汰。这能防止一个超活跃群
        把整库拖垮，也让向量检索的候选集与成本可预测。
        """
        if max_per_scope <= 0:
            return []

        scope_rows = await session.execute(
            select(cls.scope_key, func.count()).group_by(cls.scope_key).having(func.count() > max_per_scope)
        )
        over_scopes = [row[0] for row in scope_rows.all()]
        if not over_scopes:
            return []

        salience_order = (
            desc(col(cls.invalid_at).is_(None)),
            desc(col(cls.mention_count)),
            desc(col(cls.decay_score)),
            desc(func.coalesce(col(cls.last_accessed), col(cls.valid_at))),
        )

        victims: list[str] = []
        for scope_key in over_scopes:
            result = await session.execute(
                select(cls.qdrant_id, cls.id)
                .where(cls.scope_key == scope_key)
                .order_by(*salience_order)
                .offset(max_per_scope)
            )
            victims.extend((row[0] or row[1]) for row in result.all())
        return victims


# ─────────────────────────────────────────────
# C11：记忆矛盾记录（Contradiction Engine）
# ─────────────────────────────────────────────
class AIMemConflict(SQLModel, table=True):
    """记录一对语义冲突的 Edge（同实体/关系但事实相反）。

    冲突检测不在普通回复中把新旧矛盾全量丢给 LLM——只保留一条框架生成的
    冲突摘要，并默认以最新有效记录为准（见 dual_route 后置拦截器）。
    """

    __table_args__ = (Index("ix_mem_conflict_scope_sig", "scope_key", "fact_signature"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    fact_signature: str = Field(default="", max_length=256)
    old_edge_id: str = Field(default="", max_length=36)
    new_edge_id: str = Field(default="", max_length=36)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    relation: str = Field(default="contradicts", max_length=16)

    @classmethod
    def attach(
        cls,
        session: AsyncSession,
        *,
        scope_key: str,
        fact_signature: str,
        old_edge_id: str,
        new_edge_id: str,
        summary: str,
        relation: str = "contradicts",
    ) -> None:
        """把矛盾行挂到调用方 session（与 edge 写同事务，避免嵌套 with_session 自锁）。"""
        rel = relation if relation in ("supersedes", "contradicts") else "contradicts"
        session.add(
            cls(
                scope_key=scope_key,
                fact_signature=fact_signature[:256],
                old_edge_id=old_edge_id,
                new_edge_id=new_edge_id,
                summary=summary[:2000],
                relation=rel,
            )
        )

    @classmethod
    @with_session
    async def record(
        cls,
        session: AsyncSession,
        scope_key: str,
        fact_signature: str,
        old_edge_id: str,
        new_edge_id: str,
        summary: str,
    ) -> None:
        """独立 session 记一条矛盾（供非 edge 批写路径）；edge 批写请用 attach。"""
        cls.attach(
            session,
            scope_key=scope_key,
            fact_signature=fact_signature,
            old_edge_id=old_edge_id,
            new_edge_id=new_edge_id,
            summary=summary,
        )

    @classmethod
    @with_session
    async def get_by_signatures(
        cls,
        session: AsyncSession,
        scope_keys: list[str],
        signatures: list[str],
        limit: int = 6,
    ) -> list[str]:
        """按 (scope, fact_signature) 取矛盾摘要，供检索期"矛盾提示"注入。

        矛盾解决类问题（"我到底说过 A 还是 ¬A？"）的旧边已被 C11 软删除，检索只剩
        单侧事实，Agent 无从察觉矛盾；这里把命中边对应的 Conflict 摘要带回，让
        Agent 能"指出矛盾 + 请用户澄清"而不是武断给单一结论。
        """
        if not scope_keys or not signatures:
            return []
        result = await session.execute(
            select(cls.summary)
            .where(col(cls.scope_key).in_(scope_keys))
            .where(col(cls.fact_signature).in_(signatures))
            .order_by(col(cls.created_at).desc())
            .limit(limit)
        )
        return [row[0] for row in result.all() if row[0]]


# ─────────────────────────────────────────────
# Category ↔ Category 层次关联（链接模型）
# ─────────────────────────────────────────────
class AIMemCategoryEdge(SQLModel, table=True):
    """记录父 Category → 子 Category 的归属关系，支持多对多。"""

    parent_category_id: str = Field(foreign_key="aimemcategory.id", primary_key=True, max_length=36)
    child_category_id: str = Field(foreign_key="aimemcategory.id", primary_key=True, max_length=36)


# ─────────────────────────────────────────────
# Category：分层语义图节点（Hierarchical Graph）
# ─────────────────────────────────────────────
class AIMemCategory(SQLModel, table=True):
    """分层语义图的类目节点。Layer=1 最具体，Layer 越大越抽象。"""

    __table_args__ = (
        UniqueConstraint("scope_key", "layer", "name", name="uq_category_scope_layer_name"),
        Index("ix_mem_category_scope_layer", "scope_key", "layer"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    name: str = Field(max_length=256)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    tag: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    layer: int = Field(default=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    child_categories: List["AIMemCategory"] = Relationship(
        back_populates="parent_categories",
        link_model=AIMemCategoryEdge,
        sa_relationship_kwargs={
            "primaryjoin": "AIMemCategory.id == AIMemCategoryEdge.parent_category_id",
            "secondaryjoin": "AIMemCategory.id == AIMemCategoryEdge.child_category_id",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )
    parent_categories: List["AIMemCategory"] = Relationship(
        back_populates="child_categories",
        link_model=AIMemCategoryEdge,
        sa_relationship_kwargs={
            "primaryjoin": "AIMemCategory.id == AIMemCategoryEdge.child_category_id",
            "secondaryjoin": "AIMemCategory.id == AIMemCategoryEdge.parent_category_id",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )
    member_entities: List["AIMemEntity"] = Relationship(
        sa_relationship_kwargs={
            "secondary": mem_category_entity_members,
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    # ── hierarchical_graph ────────────────────

    @classmethod
    @with_session
    async def get_by_layer(
        cls,
        session: AsyncSession,
        scope_key: str,
        layer: int,
    ) -> list["AIMemCategory"]:
        result = await session.execute(select(cls).where(cls.scope_key == scope_key, cls.layer == layer))
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def get_children_of(
        cls,
        session: AsyncSession,
        parent_ids: list[str],
    ) -> list["AIMemCategory"]:
        result = await session.execute(
            select(cls)
            .join(AIMemCategoryEdge, col(AIMemCategoryEdge.child_category_id) == col(cls.id))
            .where(col(AIMemCategoryEdge.parent_category_id).in_(parent_ids))
            .distinct()
        )
        return list(result.scalars().all())


# ─────────────────────────────────────────────
# 程序性 / 偏好记忆（Procedural / Preference Memory）
# 设计：plans/procedural_preference_memory_design_20260614.md §3
# 与 Episode/Entity/Edge 三层陈述性记忆正交：本表承载"针对 Agent 未来行为的
# 程序性指令/纠错规程/偏好规则"（如"调 generate_image 用竖图""按用户时区"），
# SQL-only 结构化真值、不写向量（精确召回比向量更快更准，避免向量模糊性与对账负担）。
# ─────────────────────────────────────────────
class AIMemPreference(SQLModel, table=True):
    """程序性/偏好记忆规则。一条规则 = "在某 target_context 下，该/不该如何做"。

    主存 USER_GLOBAL scope（偏好多为跨群用户特质，应随用户跨群生效）；群内专属
    约定用 USER_IN_GROUP。检索主路径走 (scope_key, target_context) 复合索引精确取，
    O(log n) 索引 seek，无需向量。
    """

    __table_args__ = (
        # 检索主路径：按 (scope, target_context) 精确取规则
        Index("ix_mem_pref_scope_target", "scope_key", "target_context"),
        Index("ix_mem_pref_scope_user", "scope_key", "user_id"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    user_id: Optional[str] = Field(default=None, index=True, max_length=64)
    # 绑定上下文：优先填 capability_domain（能力族，如"定时任务"/"网络搜索"），
    # 其次具体 tool_name（如 generate_image），无可绑定时填 "general"（泛化/风格类）。
    target_context: str = Field(default="general", index=True, max_length=128)
    preference_rule: str = Field(sa_column=Column(Text, nullable=False))  # 规则正文（含触发条件）
    polarity: str = Field(default="do", max_length=8)  # "do" / "dont"
    is_correction: bool = Field(default=False)  # 是否由纠错产生（纠错类受保护、衰减更慢）
    is_active: bool = Field(default=True)  # 软停用：WebConsole 可关掉误抽规则而不删（保留审计）
    source_episode_id: Optional[str] = Field(default=None, max_length=36)  # 溯源
    mention_count: int = Field(default=1)  # 被重复纠正/确认次数（强化用）
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_applied_at: Optional[datetime] = Field(default=None)  # 上次被成功注入/应用

    @staticmethod
    def _norm_rule(rule: str) -> str:
        """规则归一化签名，用于跨批次去重 / 合并强化判定。"""
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
        """合并 / 覆盖 / 强化写入一条偏好规则。返回 ``(id, is_new)``。

        - **语义等价**（同 (scope, user, target_context) + 同极性 + 同归一签名）→
          ``mention_count += 1`` + 刷新 ``updated_at``（强化，复用 Edge 的 C1 归并思路），不新增行。
        - **语义冲突**（同 target_context、极性相反）→ 旧的相反规则软停用（``is_active=False``），
          以新规则为准（复用 Edge 极性反转思路）。
        - **无等价** → 新建。
        """
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        return await under_db_write(
            lambda: cls._upsert_db(
                scope_key=scope_key,
                user_id=user_id,
                target_context=target_context,
                preference_rule=preference_rule,
                polarity=polarity,
                is_correction=is_correction,
                source_episode_id=source_episode_id,
            )
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

        # 1) 语义等价（同极性 + 同签名）→ 强化既有
        for row in existing:
            if row.polarity == polarity and cls._norm_rule(row.preference_rule) == sig:
                row.mention_count += 1
                row.updated_at = datetime.now(timezone.utc)
                if is_correction:
                    row.is_correction = True
                session.add(row)
                return row.id, False

        # 2) 极性相反 → 软停用旧的相反规则（以新为准）
        for row in existing:
            if row.polarity != polarity and cls._norm_rule(row.preference_rule) == sig:
                row.is_active = False
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)

        # 3) 新建
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
    @with_session
    async def get_active(
        cls,
        session: AsyncSession,
        scope_keys: list[str],
        target_contexts: Optional[list[str]] = None,
        limit: int = 12,
    ) -> list["AIMemPreference"]:
        """取若干 scope 下的活跃偏好规则（注入用）。

        排序：纠错类优先 → 高频强化优先 → 最近更新优先。可选按 target_contexts 过滤
        （检索时只取与本轮工具能力域相关的规则）。
        """
        if not scope_keys:
            return []
        stmt = select(cls).where(col(cls.scope_key).in_(scope_keys), col(cls.is_active).is_(True))
        if target_contexts:
            stmt = stmt.where(col(cls.target_context).in_(target_contexts))
        stmt = stmt.order_by(
            desc(col(cls.is_correction)),
            desc(col(cls.mention_count)),
            desc(col(cls.updated_at)),
        ).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    async def touch_applied(cls, pref_ids: list[str]) -> None:
        """把一批偏好标记为"刚被注入/应用"，刷新 last_applied_at（生命周期保护依据）。"""
        if not pref_ids:
            return
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        await under_db_write(lambda: cls._touch_applied_db(pref_ids))

    @classmethod
    @with_session
    async def _touch_applied_db(cls, session: AsyncSession, pref_ids: list[str]) -> None:
        from sqlalchemy import update as _update

        await session.execute(
            _update(cls).where(col(cls.id).in_(pref_ids)).values(last_applied_at=datetime.now(timezone.utc))
        )

    @classmethod
    @with_session
    async def count_active(cls, session: AsyncSession, scope_keys: Optional[list[str]] = None) -> int:
        """统计活跃偏好规则数（WebConsole stats 用）。"""
        stmt = select(func.count()).select_from(cls).where(col(cls.is_active).is_(True))
        if scope_keys:
            stmt = stmt.where(col(cls.scope_key).in_(scope_keys))
        result = await session.execute(stmt)
        return result.scalar() or 0

    @classmethod
    @with_session
    async def delete_by_scope_keys(cls, session: AsyncSession, scope_keys: list[str]) -> int:
        """按 scope 物理删除偏好规则（供 clear_ops 清空联动；偏好无向量，仅删 SQL 行）。"""
        if not scope_keys:
            return 0
        from sqlalchemy import delete as _delete

        result = await session.execute(_delete(cls).where(col(cls.scope_key).in_(scope_keys)))
        # AGENTS.md §3.5.2: rowcount 仅对 CursorResult 暴露, 用 isinstance 守门
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    async def prune_per_context(cls, max_per_context: int) -> int:
        """生命周期裁剪：每个 (scope_key, user_id, target_context) 仅保留 salience 最高的
        max_per_context 条活跃规则，其余**非纠错**规则软停用（纠错类受保护不裁）。返回停用条数。

        salience：纠错类优先 → mention_count → 最近更新。纯规则、零 LLM。
        """
        if max_per_context <= 0:
            return 0
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        return await under_db_write(lambda: cls._prune_per_context_db(max_per_context))

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
