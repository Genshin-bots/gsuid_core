"""记忆系统 SQLModel 数据模型（schema_v2）。

核心四表：
- ``AIMemThread``  : 跨时间故事线 + 追加式里程碑主轴
- ``AIMemEpisode`` : 原始对话片段（带 session / thread / turn 索引列）
- ``AIMemEntity``  : 具名实体节点
- ``AIMemEdge``    : 属性 / 关系 / 里程碑，四态代数（ADD/MERGE/SUPERSEDE/CONTRADICT）

偏好表 ``AIMemPreference`` 独立位于 ``gsuid_core.ai_core.memory.database.preference``。
"""

import uuid
import asyncio
from typing import List, TypeVar, Optional, Sequence
from datetime import datetime, timezone, timedelta
from collections.abc import Mapping

from sqlmodel import Field, SQLModel, Relationship, col, select, update, literal_column
from sqlalchemy import (
    Text,
    Index,
    Table,
    Column,
    String,
    ForeignKey,
    UniqueConstraint,
    or_,
    and_,
    desc,
    func,
    not_,
    union as sql_union,
    delete,
    exists,
    insert,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement, UnaryExpression
from sqlalchemy.sql.selectable import Select
from sqlalchemy.dialects.postgresql import JSON

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.vector.ops import upsert_episode_vector
from gsuid_core.utils.database.write_gate import sqlite_write_gate
from gsuid_core.utils.database.base_models import (
    _db_type,
    async_maker,
    with_session,
    with_read_session,
)
from gsuid_core.ai_core.memory.database.fts import index_episode, index_episodes
from gsuid_core.ai_core.memory.database.session_split import SessionCursor, continue_session


def _like_contains(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _dict_str(data: Mapping[str, object], key: str, default: str = "") -> str:
    if key in data:
        raw = data[key]
        if isinstance(raw, str):
            return raw
    return default


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


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _user_only_like_conds(content: ColumnElement[str]) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return (_as_bool_expr(~content.like("assistant:%")), _as_bool_expr(~content.like("[我此前说过]%")))


_TCol = TypeVar("_TCol")


def _time_order(
    at_col: ColumnElement[datetime],
    id_col: ColumnElement[str],
    *,
    ascending: bool,
) -> tuple[UnaryExpression[datetime], UnaryExpression[object]] | tuple[UnaryExpression[datetime], UnaryExpression[str]]:
    if _db_type == "sqlite":
        tie = literal_column("rowid")
        return (at_col.asc(), tie.asc()) if ascending else (at_col.desc(), tie.desc())
    return (at_col.asc(), id_col.asc()) if ascending else (at_col.desc(), id_col.desc())


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


_TScopeModel = TypeVar("_TScopeModel", bound=SQLModel)


def _exclude_assistant_turns(content: ColumnElement[str]) -> ColumnElement[bool]:
    trimmed = func.ltrim(content)
    return and_(
        not_(func.lower(trimmed).startswith("assistant:")),
        not_(trimmed.startswith("[我此前说过]")),
    )


async def _scope_rows_uniform(
    session: AsyncSession,
    model: type[_TScopeModel],
    key: str,
    scope_key: str,
    limit: int,
    ties: tuple[str, ...] = (),
    extra: ColumnElement[bool] | None = None,
) -> list[_TScopeModel]:
    """同一 scope 跨全程等距取至多 ``limit`` 行，按 valid_at 升序返回。"""
    t = _model_table(model)
    c = t.c
    id_col = _as_str_col(c[key])
    scope_col = _as_str_col(c.scope_key)
    at_col = _as_dt_col(c.valid_at)
    tie_cols = [_as_dt_col(c[name]) if name == "valid_at" else c[name] for name in ties]
    order = [at_col.asc(), *[col.asc() for col in tie_cols]]

    scope_eq = scope_col == scope_key
    filt = scope_eq if extra is None else and_(scope_eq, extra)
    total = int(await session.scalar(select(func.count()).select_from(t).where(filt)) or 0)
    if total <= limit:
        stmt = select(model).where(_as_bool_expr(filt)).order_by(*order)
        return list((await session.execute(stmt)).scalars().all())

    inner = select(id_col.label("rid"), func.row_number().over(order_by=order).label("rn")).where(filt).subquery()
    step = max(0, limit - 1)
    cur_scaled = (inner.c.rn - 1) * step // (total - 1)
    prev_scaled = (inner.c.rn - 2) * step // (total - 1)
    keep = select(inner.c.rid).where(or_(inner.c.rn == 1, cur_scaled > prev_scaled))
    stmt = select(model).where(_as_bool_expr(id_col.in_(keep))).order_by(*order)
    return list((await session.execute(stmt)).scalars().all())


mem_episode_entity_mentions = Table(
    "mem_episode_entity_mentions",
    SQLModel.metadata,
    Column("episode_id", String(36), ForeignKey("aimemepisode.id"), primary_key=True),
    Column("entity_id", String(36), ForeignKey("aimementity.id"), primary_key=True),
)


class AIMemEpisode(SQLModel, table=True):
    """经聚合的对话片段，是所有记忆的原始素材。"""

    __table_args__ = (
        Index("ix_mem_episode_scope_valid_at", "scope_key", "valid_at"),
        Index("ix_mem_episode_scope_session", "scope_key", "session_id", "valid_at"),
        Index("ix_mem_episode_scope_thread", "scope_key", "thread_id", "valid_at"),
        # 控制台跨 scope 按时间倒序取首页，复合索引前导列是 scope_key，用不上
        Index("ix_mem_episode_valid_at", "valid_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    content: str = Field(sa_column=Column(Text, nullable=False))
    speaker_ids: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    valid_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    qdrant_id: str = Field(index=True, max_length=36)
    session_id: Optional[str] = Field(default=None, max_length=36, index=True)
    thread_id: Optional[str] = Field(default=None, max_length=36, index=True)
    turn_index: int = Field(default=0)

    mentioned_entities: List["AIMemEntity"] = Relationship(
        back_populates="episodes",
        sa_relationship_kwargs={
            "secondary": mem_episode_entity_mentions,
            "back_populates": "episodes",
            "lazy": "noload",
        },
    )

    @classmethod
    async def create_episode(
        cls,
        scope_key: str,
        content: str,
        speaker_ids: list[str],
        valid_at: datetime,
        *,
        thread_id: str | None = None,
    ) -> "AIMemEpisode":
        episode_id = str(uuid.uuid4())
        async with sqlite_write_gate, async_maker() as session:
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
                thread_id=thread_id,
                turn_index=assignment.turn_index,
            )
            session.add(episode)
            await index_episode(session, episode)
            await session.commit()
        try:
            await upsert_episode_vector(
                episode_id=episode_id,
                content=content,
                scope_key=scope_key,
                valid_at_ts=valid_at.timestamp(),
                speaker_ids=speaker_ids,
                session_id=assignment.session_id,
                turn_index=assignment.turn_index,
                thread_id=thread_id,
            )
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(t("log.memory.episode_vector_upsert_fail", episode_id=episode_id, error=str(e)))
        return episode

    @classmethod
    async def create_episodes_bulk(
        cls,
        scope_key: str,
        items: list[dict],
        *,
        vector_chunk: int = 512,
        thread_id: str | None = None,
    ) -> int:
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

        gap = _session_gap_seconds()
        episodes: list["AIMemEpisode"] = []
        async with sqlite_write_gate, async_maker() as session:
            cursor = await _cursor_in_tx(session, scope_key)
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
                        thread_id=thread_id,
                        turn_index=assignment.turn_index,
                    )
                )
                cursor = SessionCursor(assignment.session_id, assignment.turn_index, valid_at)
            session.add_all(episodes)
            await index_episodes(session, episodes)
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
                    "thread_id": ep.thread_id,
                    "turn_index": ep.turn_index,
                }
                for ep in chunk
            ]
            await upsert_episode_vectors_batch(payload)
            written += len(chunk)
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
        if not tokens or not scope_key:
            return []
        extra: list[ColumnElement[bool]] = []
        c = _model_table(cls).c
        if start is not None:
            extra.append(_as_bool_expr(c.valid_at >= _naive_utc(start)))
        if end is not None:
            extra.append(_as_bool_expr(c.valid_at < _naive_utc(end)))
        if user_only:
            extra.extend(_user_only_like_conds(_as_str_col(c.content)))
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
        result = await session.execute(select(cls).where(c.id.in_(id_q)))
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
        offset: int = 0,
    ) -> list["AIMemEpisode"]:
        if not scope_key or limit <= 0 or not tokens:
            return []
        c = _model_table(cls).c
        conds: list[ColumnElement[bool]] = [_as_bool_expr(c.scope_key == scope_key)]
        for tok in tokens[:4]:
            if not tok:
                return []
            conds.append(_as_bool_expr(c.content.like(_like_contains(tok), escape="\\")))
        if user_only:
            conds.extend(_user_only_like_conds(_as_str_col(c.content)))
        if speaker:
            conds.append(
                _as_bool_expr(
                    or_(
                        c.content.like(_like_contains(f"{speaker}:"), escape="\\"),
                        c.content.like(_like_contains(f"[{speaker}]:"), escape="\\"),
                    )
                )
            )
        order = _time_order(_as_dt_col(c.valid_at), _as_str_col(c.id), ascending=ascending)
        stmt = select(cls).where(*conds).order_by(*order)
        if offset > 0:
            stmt = stmt.offset(offset)
        result = await session.execute(stmt.limit(limit))
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
        if not scope_key or limit <= 0:
            return []
        c = _model_table(cls).c
        conds: list[ColumnElement[bool]] = [
            _as_bool_expr(c.scope_key == scope_key),
            _as_bool_expr(c.valid_at >= _naive_utc(start)),
            _as_bool_expr(c.valid_at < _naive_utc(end)),
        ]
        if user_only:
            conds.extend(_user_only_like_conds(_as_str_col(c.content)))
        order = _time_order(_as_dt_col(c.valid_at), _as_str_col(c.id), ascending=ascending)
        stmt = select(cls).where(*conds).order_by(*order)
        if offset > 0:
            stmt = stmt.offset(offset)
        result = await session.execute(stmt.limit(limit))
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def search_by_valid_at_range_scopes(
        cls,
        session: AsyncSession,
        scope_keys: Sequence[str],
        start: datetime,
        end: datetime,
        limit: int = 80,
        ascending: bool = False,
    ) -> list["AIMemEpisode"]:
        keys = [sk for sk in scope_keys if sk]
        if not keys or limit <= 0:
            return []
        c = _model_table(cls).c
        conds: list[ColumnElement[bool]] = [
            _as_bool_expr(c.scope_key.in_(keys)),
            _as_bool_expr(c.valid_at >= _naive_utc(start)),
            _as_bool_expr(c.valid_at < _naive_utc(end)),
        ]
        order = _time_order(_as_dt_col(c.valid_at), _as_str_col(c.id), ascending=ascending)
        stmt = select(cls).where(*conds).order_by(*order).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def get_speaker_map_by_ids(
        cls,
        session: AsyncSession,
        episode_ids: Sequence[str],
    ) -> dict[str, list[str]]:
        ids = [eid for eid in episode_ids if eid]
        if not ids:
            return {}
        stmt = select(col(cls.id), col(cls.speaker_ids)).where(col(cls.id).in_(ids))
        rows = (await session.execute(stmt)).all()
        return {
            eid_raw: [str(s) for s in spks_raw]
            for eid_raw, spks_raw in rows
            if isinstance(eid_raw, str) and isinstance(spks_raw, list)
        }

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
        if not scope_key:
            return 0
        c = _model_table(cls).c
        conds: list[ColumnElement[bool]] = [
            _as_bool_expr(c.scope_key == scope_key),
            _as_bool_expr(c.valid_at >= _naive_utc(start)),
            _as_bool_expr(c.valid_at < _naive_utc(end)),
        ]
        if user_only:
            conds.extend(_user_only_like_conds(_as_str_col(c.content)))
        result = await session.execute(select(func.count()).select_from(cls).where(*conds))
        return int(result.scalar_one() or 0)

    @classmethod
    async def _query_group_openers(
        cls,
        session: AsyncSession,
        group_expr: ColumnElement[_TCol],
        conds: Sequence[ColumnElement[bool]],
        limit: int,
    ) -> list["AIMemEpisode"]:
        c = _model_table(cls).c
        if _db_type == "sqlite":
            firsts = (
                select(group_expr.label("gk"), func.min(literal_column("rowid")).label("rid"))
                .where(*conds)
                .group_by(group_expr)
                .subquery()
            )
            key_cond = literal_column("aimemepisode.rowid").in_(select(firsts.c.rid))
        else:
            firsts = (
                select(group_expr.label("gk"), func.min(c.valid_at).label("t0"))
                .where(*conds)
                .group_by(group_expr)
                .subquery()
            )
            key_cond = c.valid_at.in_(select(firsts.c.t0))
        stmt = select(cls).where(key_cond, *conds).order_by(c.valid_at.asc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

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
        if not scope_key or limit <= 0:
            return []
        c = _model_table(cls).c
        conds: list[ColumnElement[bool]] = [
            _as_bool_expr(c.scope_key == scope_key),
            _as_bool_expr(c.valid_at >= _naive_utc(start)),
            _as_bool_expr(c.valid_at < _naive_utc(end)),
            *_user_only_like_conds(_as_str_col(c.content)),
        ]
        return await cls._query_group_openers(session, func.date(c.valid_at), conds, limit)

    @classmethod
    @with_read_session
    async def neighbors_by_time(
        cls,
        session: AsyncSession,
        scope_key: str,
        valid_at: datetime,
        before: int = 8,
        after: int = 8,
        mode: str = "window",
        episode_id: str | None = None,
    ) -> list["AIMemEpisode"]:
        if not scope_key:
            return []
        ep: "AIMemEpisode | None" = None
        if episode_id:
            h_id = await session.execute(select(cls).where(col(cls.id) == episode_id).limit(1))
            ep = h_id.scalar_one_or_none()
        if ep is None:
            h_time = await session.execute(
                select(cls).where(col(cls.scope_key) == scope_key, col(cls.valid_at) == valid_at).limit(1)
            )
            ep = h_time.scalar_one_or_none()
        if ep is not None and ep.session_id:
            if mode == "session":
                cond = [col(cls.session_id) == ep.session_id]
            else:
                # 限制在同会话邻近轮次内，防止长会话冲垮上下文，同时跨过助手多段切块
                t_min = max(0, ep.turn_index - before)
                t_max = ep.turn_index + after
                cond = [
                    col(cls.session_id) == ep.session_id,
                    col(cls.turn_index) >= t_min,
                    col(cls.turn_index) <= t_max,
                ]
            res = await session.execute(
                select(cls).where(*cond).order_by(col(cls.turn_index).asc(), col(cls.valid_at).asc())
            )
            return list(res.scalars().all())
        rows: list["AIMemEpisode"] = []
        seen: set[str] = set()

        def _append_unique(items: Sequence["AIMemEpisode"]) -> None:
            for row in items:
                if row.id not in seen:
                    seen.add(row.id)
                    rows.append(row)

        if before > 0:
            stmt_b = (
                select(cls)
                .where(col(cls.scope_key) == scope_key, col(cls.valid_at) <= valid_at)
                .order_by(col(cls.valid_at).desc())
                .limit(before + 1)
            )
            _append_unique(list(reversed(list((await session.execute(stmt_b)).scalars().all()))))
        if after > 0:
            stmt_a = (
                select(cls)
                .where(col(cls.scope_key) == scope_key, col(cls.valid_at) > valid_at)
                .order_by(col(cls.valid_at).asc())
                .limit(after)
            )
            _append_unique(list((await session.execute(stmt_a)).scalars().all()))
        return rows

    @classmethod
    @with_read_session
    async def list_by_scope(
        cls,
        session: AsyncSession,
        scope_key: str,
        limit: int = 2000,
        sample: bool = False,
        user_only: bool = False,
    ) -> list["AIMemEpisode"]:
        if not scope_key or limit <= 0:
            return []
        extra = _exclude_assistant_turns(_as_str_col(_model_table(cls).c.content)) if user_only else None
        if sample:
            return await _scope_rows_uniform(
                session, cls, "id", scope_key, limit, ties=("turn_index", "id"), extra=extra
            )
        stmt = (
            select(cls)
            .where(col(cls.scope_key) == scope_key)
            .order_by(col(cls.valid_at).asc(), col(cls.turn_index).asc(), col(cls.id).asc())
            .limit(limit)
        )
        if extra is not None:
            stmt = stmt.where(extra)
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_session
    async def assign_thread(cls, session: AsyncSession, episode_id: str, thread_id: str) -> bool:
        """Pass 2 新建线程后把当窗 Episode 归到同一条故事线。"""
        result = await session.execute(update(cls).where(col(cls.id) == episode_id).values(thread_id=thread_id))
        return isinstance(result, CursorResult) and result.rowcount > 0

    @classmethod
    @with_read_session
    async def list_by_thread(
        cls,
        session: AsyncSession,
        scope_key: str,
        thread_id: str,
        limit: int = 400,
        ascending: bool = True,
    ) -> list["AIMemEpisode"]:
        if not scope_key or not thread_id or limit <= 0:
            return []
        order = col(cls.valid_at).asc() if ascending else col(cls.valid_at).desc()
        stmt = (
            select(cls)
            .where(col(cls.scope_key) == scope_key, col(cls.thread_id) == thread_id)
            .order_by(order, col(cls.turn_index).asc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def list_ingested_documents(
        cls,
        session: AsyncSession,
        scope_key: str,
        limit: int = 12000,
    ) -> list["AIMemEpisode"]:
        if not scope_key or limit <= 0:
            return []
        stmt = (
            select(cls)
            .where(col(cls.scope_key) == scope_key, col(cls.content).like("%【文档%"))
            .order_by(col(cls.valid_at).asc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def list_ingested_document_heads(
        cls,
        session: AsyncSession,
        scope_key: str,
        limit: int = 4000,
    ) -> list[str]:
        if not scope_key or limit <= 0:
            return []
        stmt = (
            select(func.substr(col(cls.content), 1, 180))
            .where(col(cls.scope_key) == scope_key, col(cls.content).like("%【文档%"))
            .limit(limit)
        )
        result = await session.execute(stmt)
        return [str(row[0]) for row in result.all() if isinstance(row[0], str) and row[0]]

    @classmethod
    @with_read_session
    async def list_document_chunks(
        cls,
        session: AsyncSession,
        scope_key: str,
        title: str,
        limit: int = 40,
    ) -> list["AIMemEpisode"]:
        if not scope_key or not title or limit <= 0:
            return []
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cond = or_(
            col(cls.content).like(f"%【文档】{escaped}\n%", escape="\\"),
            col(cls.content).like(f"%【文档续】{escaped}\n%", escape="\\"),
        )
        where = and_(col(cls.scope_key) == scope_key, cond)
        head_n = min(8, limit)
        head: list[AIMemEpisode] = list(
            (await session.execute(select(cls).where(where).order_by(col(cls.valid_at).asc()).limit(head_n)))
            .scalars()
            .all()
        )
        tail_n = limit - head_n
        if tail_n <= 0:
            return head
        tail: list[AIMemEpisode] = list(
            (await session.execute(select(cls).where(where).order_by(col(cls.valid_at).desc()).limit(tail_n)))
            .scalars()
            .all()
        )
        seen = {row.id for row in head}
        head.extend(row for row in reversed(tail) if row.id not in seen)
        return head

    @classmethod
    @with_read_session
    async def last_in_scope(cls, session: AsyncSession, scope_key: str) -> Optional["AIMemEpisode"]:
        if not scope_key:
            return None
        stmt = select(cls).where(col(cls.scope_key) == scope_key).order_by(col(cls.valid_at).desc()).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()

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
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def get_session_around(
        cls,
        session: AsyncSession,
        session_id: str,
        turn_index: int,
        radius: int = 12,
        seed_id: str = "",
    ) -> list["AIMemEpisode"]:
        if not session_id or radius < 0:
            return []
        window: ColumnElement[bool] = and_(
            col(cls.turn_index) >= turn_index - radius,
            col(cls.turn_index) <= turn_index + radius,
        )
        span = or_(window, col(cls.id) == seed_id) if seed_id else window
        stmt = (
            select(cls)
            .where(col(cls.session_id) == session_id, span)
            .order_by(col(cls.turn_index).asc(), col(cls.valid_at).asc())
            .limit(radius * 2 + 8)
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def get_one(cls, session: AsyncSession, episode_id: str) -> "AIMemEpisode | None":
        if not episode_id:
            return None
        return await session.get(cls, episode_id)

    @classmethod
    @with_read_session
    async def list_by_sessions(
        cls,
        session: AsyncSession,
        session_ids: list[str],
        limit: int = 2000,
    ) -> list["AIMemEpisode"]:
        if not session_ids or limit <= 0:
            return []
        stmt = (
            select(cls)
            .where(col(cls.session_id).in_(session_ids))
            .order_by(col(cls.valid_at).asc(), col(cls.turn_index).asc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def session_map_by_ids(cls, session: AsyncSession, episode_ids: list[str]) -> dict[str, str]:
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
    async def list_threadless_sessions(
        cls,
        session: AsyncSession,
        scope_key: str,
        limit: int = 80,
    ) -> list[tuple[str, datetime, str]]:
        """未挂 thread 的 session：``(session_id, start_at, opener_episode_id)``。"""
        if not scope_key or limit <= 0:
            return []
        c = _model_table(cls).c
        conds = [
            _as_bool_expr(c.scope_key == scope_key),
            _as_bool_expr(c.session_id.is_not(None)),
            _as_bool_expr(c.thread_id.is_(None)),
        ]
        rows = await cls._query_group_openers(session, _as_str_col(c.session_id), conds, limit)
        out: list[tuple[str, datetime, str]] = []
        seen: set[str] = set()
        for row in rows:
            sid = row.session_id or ""
            if sid and sid not in seen:
                seen.add(sid)
                out.append((sid, row.valid_at, row.id))
        return out

    @classmethod
    async def assign_session_thread(cls, session_id: str, thread_id: str) -> None:
        if not session_id or not thread_id:
            return
        async with sqlite_write_gate, async_maker() as session:
            await session.execute(update(cls).where(col(cls.session_id) == session_id).values(thread_id=thread_id))
            await session.commit()

    @classmethod
    async def purge_episodes_by_ids(cls, episode_ids: list[str]) -> int:
        if not episode_ids:
            return 0
        async with sqlite_write_gate:
            return await cls._purge_episodes_by_ids_db(episode_ids)

    @classmethod
    @with_session
    async def _purge_episodes_by_ids_db(cls, session: AsyncSession, episode_ids: list[str]) -> int:
        from gsuid_core.ai_core.memory.database.fts import delete_episode_ids

        await session.execute(
            mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.episode_id.in_(episode_ids))
        )
        await delete_episode_ids(session, episode_ids)
        await session.execute(delete(cls).where(col(cls.id).in_(episode_ids)))
        return len(episode_ids)

    @classmethod
    @with_read_session
    async def get_mentioned_entity_ids(cls, session: AsyncSession, episode_ids: list[str]) -> list[str]:
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
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return SessionCursor(None, 0, None)
    return SessionCursor(row.session_id, row.turn_index, row.valid_at)


class AIMemEntity(SQLModel, table=True):
    """从 Episode 中抽取的实体（发言人 / 人 / 项目 / 物件 / 地点 / 组织）。"""

    __table_args__ = (
        UniqueConstraint("scope_key", "name", name="uq_entity_scope_name"),
        Index("ix_mem_entity_scope_name", "scope_key", "name"),
        Index("ix_mem_entity_scope_speaker", "scope_key", "is_speaker"),
        Index("ix_mem_entity_scope_id", "scope_key", "id"),
        Index("ix_mem_entity_updated_at", "updated_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    name: str = Field(max_length=256)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    tag: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    is_speaker: bool = Field(default=False)
    entity_kind: str = Field(default="person", max_length=24)
    user_id: Optional[str] = Field(default=None, index=True, max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    qdrant_id: str = Field(index=True, max_length=36)

    episodes: List["AIMemEpisode"] = Relationship(
        back_populates="mentioned_entities",
        sa_relationship_kwargs={
            "secondary": mem_episode_entity_mentions,
            "back_populates": "mentioned_entities",
            "lazy": "noload",
        },
    )
    outgoing_edges: List["AIMemEdge"] = Relationship(
        back_populates="source_entity",
        sa_relationship_kwargs={
            "primaryjoin": "AIMemEntity.id == AIMemEdge.source_entity_id",
            "lazy": "noload",
        },
    )
    incoming_edges: List["AIMemEdge"] = Relationship(
        back_populates="target_entity",
        sa_relationship_kwargs={
            "primaryjoin": "AIMemEntity.id == AIMemEdge.target_entity_id",
            "lazy": "noload",
        },
    )

    @classmethod
    @with_session
    async def find_existing(
        cls,
        session: AsyncSession,
        scope_key: str,
        name: str,
    ) -> Optional["AIMemEntity"]:
        result = await session.execute(select(cls).where(cls.scope_key == scope_key, cls.name == name))
        entity = result.scalar_one_or_none()
        if entity:
            return entity
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.memory.vector.ops import _hybrid_search_entities

        similar = await _hybrid_search_entities(query=name, scope_keys=[scope_key], top_k=3)
        threshold = memory_config.dedup_similarity_threshold
        if similar and similar[0]["score"] >= threshold:
            result = await session.execute(select(cls).where(cls.id == similar[0]["id"]))
            return result.scalar_one_or_none()
        return None

    @classmethod
    def _normalize_entity_data_names(cls, entities_data: list[dict[str, object]]) -> None:
        for ed in entities_data:
            raw_tag = ed["tag"] if "tag" in ed and isinstance(ed["tag"], list) else []
            if "is_speaker" in ed or "Speaker" in raw_tag:
                name = _dict_str(ed, "name")
                if name.startswith("user_"):
                    ed["name"] = name[len("user_") :]

    @classmethod
    async def _hybrid_match_names(cls, scope_key: str, names: Sequence[str]) -> list[tuple[str, str]]:
        """并发向量+词法匹配未命中精确名称的实体，返回 ``(name, candidate_entity_id)``。"""
        from gsuid_core.ai_core.memory.config import memory_config as _mc
        from gsuid_core.ai_core.memory.vector.ops import _hybrid_search_entities

        async def _one(uname: str) -> tuple[str, Sequence[Mapping[str, object]]]:
            return uname, await _hybrid_search_entities(query=uname, scope_keys=[scope_key], top_k=3)

        search_results = await asyncio.gather(*(_one(n) for n in names))
        threshold = _mc.dedup_similarity_threshold
        pairs: list[tuple[str, str]] = []
        for uname, similar in search_results:
            if not similar:
                continue
            top = similar[0]
            score = top["score"] if "score" in top else 0.0
            sid = top["id"] if "id" in top else ""
            if isinstance(score, (int, float)) and score >= threshold and isinstance(sid, str) and sid:
                pairs.append((uname, sid))
        return pairs

    @classmethod
    async def prefetch_hybrid_name_ids(
        cls,
        scope_key: str,
        entities_data: list[dict[str, object]],
    ) -> dict[str, str]:
        cls._normalize_entity_data_names(entities_data)
        all_names = list({n for ed in entities_data if (n := _dict_str(ed, "name").strip())})
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

        matched_pairs = await cls._hybrid_match_names(scope_key, unmatched)
        if not matched_pairs:
            return {}
        cand_ids = list({sid for _, sid in matched_pairs})
        async with async_maker() as session:
            rows = (
                await session.execute(select(cls.id).where(col(cls.id).in_(cand_ids), cls.scope_key == scope_key))
            ).all()
            valid_ids = {str(r[0]) for r in rows}
        return {uname: sid for uname, sid in matched_pairs if sid in valid_ids}

    @classmethod
    async def extract_and_upsert(
        cls,
        session: AsyncSession,
        scope_key: str,
        entities_data: list[dict[str, object]],
        episode_id: str,
        speaker_ids: list[str],
        *,
        hybrid_name_ids: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], list[dict[str, object]], int]:
        name_to_id: dict[str, str] = {}
        vector_payloads: list[dict[str, object]] = []
        new_entity_count = 0
        cls._normalize_entity_data_names(entities_data)

        existing_map: dict[str, AIMemEntity] = {}
        all_names = [n for ed in entities_data if (n := _dict_str(ed, "name").strip())]
        if all_names:
            result = await session.execute(select(cls).where(cls.scope_key == scope_key, col(cls.name).in_(all_names)))
            existing_map = {e.name: e for e in result.scalars().all()}

        unmatched_names = [n for n in all_names if n not in existing_map]
        if unmatched_names:
            matched_pairs = (
                [(u, hybrid_name_ids[u]) for u in unmatched_names if u in hybrid_name_ids]
                if hybrid_name_ids is not None
                else await cls._hybrid_match_names(scope_key, unmatched_names)
            )
            if matched_pairs:
                cand_ids = list({sid for _, sid in matched_pairs})
                rows = (
                    (await session.execute(select(cls).where(col(cls.id).in_(cand_ids), cls.scope_key == scope_key)))
                    .scalars()
                    .all()
                )
                by_id = {e.id: e for e in rows}
                for uname, sid in matched_pairs:
                    if sid in by_id:
                        existing_map[uname] = by_id[sid]

        _MAX_SUMMARY_CHARS = 2000
        speaker_id_set = {s for s in speaker_ids if s}
        for entity_data in entities_data:
            name = _dict_str(entity_data, "name").strip()
            if not name:
                continue
            existing = existing_map[name] if name in existing_map else None
            raw_tags = entity_data["tag"] if "tag" in entity_data and isinstance(entity_data["tag"], list) else []
            tag_list: list[object] = list(raw_tags)
            uid = _dict_str(entity_data, "user_id").strip() or None
            is_spk = bool(entity_data["is_speaker"]) if "is_speaker" in entity_data else False
            if not is_spk and uid is not None and uid in speaker_id_set:
                is_spk = True
            kind = _dict_str(entity_data, "kind", "person")
            if is_spk:
                if kind == "person":
                    kind = "speaker"
                if "speaker" not in tag_list:
                    tag_list.append("speaker")
            summary_piece = _dict_str(entity_data, "summary")
            if existing:
                if is_spk and not existing.is_speaker:
                    existing.is_speaker = True
                if is_spk and existing.entity_kind == "person":
                    existing.entity_kind = "speaker"
                if uid and not existing.user_id:
                    existing.user_id = uid
                if summary_piece and summary_piece not in existing.summary:
                    combined = f"{existing.summary}\n{summary_piece}".strip()
                    if len(combined) > _MAX_SUMMARY_CHARS:
                        combined = combined[:_MAX_SUMMARY_CHARS] + "\n[...早期记忆已截断...]"
                    existing.summary = combined
                existing_tags = existing.tag if isinstance(existing.tag, list) else []
                merged_tags = list(set(existing_tags) | set(tag_list))
                if is_spk and "speaker" not in merged_tags:
                    merged_tags.append("speaker")
                existing.tag = merged_tags
                existing.updated_at = datetime.now(timezone.utc)
                target_entity = existing
                payload_tags: list[object] = merged_tags
            else:
                entity_id = str(uuid.uuid4())
                new_entity_count += 1
                target_entity = cls(
                    id=entity_id,
                    scope_key=scope_key,
                    name=name,
                    summary=summary_piece,
                    tag=tag_list,
                    is_speaker=is_spk,
                    entity_kind=kind,
                    user_id=uid,
                    qdrant_id=entity_id,
                )
                payload_tags = tag_list
            session.add(target_entity)
            vector_payloads.append(
                {
                    "entity_id": target_entity.id,
                    "name": target_entity.name,
                    "summary": target_entity.summary,
                    "scope_key": scope_key,
                    "is_speaker": target_entity.is_speaker,
                    "user_id": target_entity.user_id,
                    "tag": payload_tags,
                }
            )
            name_to_id[name] = target_entity.id

        if episode_id and name_to_id:
            existing_mentions = await session.execute(
                select(mem_episode_entity_mentions.c.entity_id).where(
                    mem_episode_entity_mentions.c.episode_id == episode_id
                )
            )
            existing_entity_ids = {row[0] for row in existing_mentions.fetchall()}
            new_relations = [
                {"episode_id": episode_id, "entity_id": entity_id}
                for entity_id in set(name_to_id.values())
                if entity_id not in existing_entity_ids
            ]
            if new_relations:
                await session.execute(insert(mem_episode_entity_mentions), new_relations)

        return name_to_id, vector_payloads, new_entity_count

    @classmethod
    def _has_edge_expr(cls) -> ColumnElement[bool]:
        return exists().where(or_(col(AIMemEdge.source_entity_id) == cls.id, col(AIMemEdge.target_entity_id) == cls.id))

    @classmethod
    @with_session
    async def collect_orphans(
        cls,
        session: AsyncSession,
        ttl_days: int = 10,
    ) -> list[tuple[str, str, str]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        result = await session.execute(
            select(cls.id, cls.scope_key, cls.qdrant_id).where(
                col(cls.is_speaker).is_(False),
                col(cls.updated_at) < cutoff,
                ~cls._has_edge_expr(),
            )
        )
        return [(row[0], row[1], row[2] or row[0]) for row in result.all()]

    @classmethod
    async def purge_orphans_by_ids(cls, entity_ids: list[str]) -> int:
        if not entity_ids:
            return 0
        async with sqlite_write_gate:
            return await cls._purge_orphans_by_ids_db(entity_ids)

    @classmethod
    @with_session
    async def _purge_orphans_by_ids_db(cls, session: AsyncSession, entity_ids: list[str]) -> int:
        await session.execute(
            mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.entity_id.in_(entity_ids))
        )
        await session.execute(delete(cls).where(col(cls.id).in_(entity_ids)))
        return len(entity_ids)

    @classmethod
    @with_session
    async def collect_capacity_overflow(
        cls,
        session: AsyncSession,
        max_per_scope: int,
    ) -> list[tuple[str, str, str]]:
        if max_per_scope <= 0:
            return []
        scope_rows = await session.execute(
            select(cls.scope_key, func.count()).group_by(cls.scope_key).having(func.count() > max_per_scope)
        )
        over_scopes = [(row[0], row[1]) for row in scope_rows.all()]
        if not over_scopes:
            return []
        has_edge = cls._has_edge_expr()
        victims: list[tuple[str, str, str]] = []
        for scope_key, count in over_scopes:
            to_remove = count - max_per_scope
            if to_remove <= 0:
                continue
            result = await session.execute(
                select(cls.id, cls.scope_key, cls.qdrant_id)
                .where(cls.scope_key == scope_key, col(cls.is_speaker).is_(False), ~has_edge)
                .order_by(col(cls.updated_at).asc())
                .limit(to_remove)
            )
            victims.extend((row[0], row[1], row[2] or row[0]) for row in result.all())
        return victims

    @classmethod
    @with_read_session
    async def get_frequent_names(
        cls,
        session: AsyncSession,
        scope_key: str,
        limit: int = 20,
    ) -> list[str]:
        result = await session.execute(
            select(cls.name)
            .where(cls.scope_key == scope_key, col(cls.is_speaker).is_(False))
            .order_by(col(cls.updated_at).desc())
            .limit(limit)
        )
        return [row[0] for row in result.all()]

    @classmethod
    @with_read_session
    async def get_names_by_ids(cls, session: AsyncSession, entity_ids: list[str]) -> dict[str, str]:
        if not entity_ids:
            return {}
        result = await session.execute(select(cls.id, cls.name).where(col(cls.id).in_(entity_ids)))
        return {row[0]: row[1] for row in result.all()}

    @classmethod
    @with_read_session
    async def list_distinct_scope_keys(cls, session: AsyncSession) -> List[str]:
        result = await session.execute(select(col(cls.scope_key)).distinct())
        return [row[0] for row in result.all() if row[0]]

    @classmethod
    @with_read_session
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

    @classmethod
    @with_read_session
    async def get_group_scopes_for_user(
        cls,
        session: AsyncSession,
        user_id: str,
        cap: int = 50,
    ) -> list[str]:
        if not user_id or cap <= 0:
            return []
        stmt = (
            select(col(cls.scope_key))
            .where(
                col(cls.user_id) == user_id,
                col(cls.scope_key).startswith("group:"),
            )
            .distinct()
            .limit(cap)
        )
        rows = (await session.execute(stmt)).all()
        return [sk for (sk,) in rows if isinstance(sk, str) and sk]

    @classmethod
    @with_read_session
    async def get_ids_for_user_in_scopes(
        cls,
        session: AsyncSession,
        user_id: str,
        scope_keys: Sequence[str],
    ) -> frozenset[str]:
        keys = [sk for sk in scope_keys if sk]
        if not user_id or not keys:
            return frozenset()
        stmt = select(col(cls.id)).where(
            col(cls.user_id) == user_id,
            col(cls.scope_key).in_(keys),
        )
        rows = (await session.execute(stmt)).all()
        return frozenset(eid for (eid,) in rows if isinstance(eid, str) and eid)

    @classmethod
    @with_read_session
    async def get_created_ts_map(
        cls,
        session: AsyncSession,
        entity_ids: Sequence[str],
    ) -> dict[str, float]:
        ids = [eid for eid in entity_ids if eid]
        if not ids:
            return {}
        stmt = select(col(cls.id), col(cls.created_at)).where(col(cls.id).in_(ids))
        rows = (await session.execute(stmt)).all()
        out: dict[str, float] = {}
        for eid_raw, dt_raw in rows:
            if isinstance(eid_raw, str) and isinstance(dt_raw, datetime):
                dt = dt_raw if dt_raw.tzinfo is not None else dt_raw.replace(tzinfo=timezone.utc)
                out[eid_raw] = dt.timestamp()
        return out


class AIMemThread(SQLModel, table=True):
    """跨 session 的主题线程；里程碑边按 (valid_at, turn_index) 追加。"""

    __table_args__ = (
        Index("ix_mem_thread_scope_first", "scope_key", "first_at"),
        Index("ix_mem_thread_scope_active", "scope_key", "last_at"),
        Index("ix_mem_thread_last_at", "last_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    title: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    aliases: List[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    status: str = Field(default="active", max_length=16)
    first_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    n_sessions: int = Field(default=0)
    milestone_count: int = Field(default=0)
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
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def list_active(cls, session: AsyncSession, scope_key: str, limit: int = 15) -> list["AIMemThread"]:
        if not scope_key or limit <= 0:
            return []
        stmt = (
            select(cls)
            .where(col(cls.scope_key) == scope_key, col(cls.status) == "active")
            .order_by(col(cls.last_at).desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def list_by_title_source(cls, session: AsyncSession, source: str, limit: int = 8) -> list["AIMemThread"]:
        if not source or limit <= 0:
            return []
        stmt = select(cls).where(col(cls.title_source) == source).order_by(col(cls.last_at).desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

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
        await session.execute(
            update(cls)
            .where(col(cls.id) == thread_id)
            .values(title=title[:240], aliases=aliases[:8], title_source="llm")
        )


class AIMemEdge(SQLModel, table=True):
    """一条可验证事实边。``resolution_state`` 承载四态代数。"""

    __table_args__ = (
        Index("ix_mem_edge_scope_valid", "scope_key", "valid_at", "invalid_at"),
        Index("ix_mem_edge_source", "source_entity_id"),
        Index("ix_mem_edge_target", "target_entity_id"),
        Index("ix_mem_edge_scope_src_slot", "scope_key", "source_entity_id", "slot_key"),
        Index("ix_mem_edge_scope_thread_time", "scope_key", "thread_id", "valid_at", "turn_index"),
        Index("ix_mem_edge_scope_state", "scope_key", "resolution_state"),
        Index("ix_mem_edge_valid_at", "valid_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    fact: str = Field(sa_column=Column(Text, nullable=False))
    source_entity_id: str = Field(foreign_key="aimementity.id", max_length=36)
    target_entity_id: str = Field(foreign_key="aimementity.id", max_length=36)
    edge_kind: str = Field(default="attribute", max_length=24, index=True)
    slot_key: str = Field(default="", max_length=96, index=True)
    resolution_state: str = Field(default="active", max_length=24, index=True)
    superseded_by_id: Optional[str] = Field(default=None, max_length=36, index=True)
    contradicts_edge_id: Optional[str] = Field(default=None, max_length=36, index=True)
    thread_id: Optional[str] = Field(default=None, max_length=36, index=True)
    source_episode_id: Optional[str] = Field(default=None, max_length=36, index=True)
    turn_index: int = Field(default=0)
    event_at: Optional[datetime] = Field(default=None)
    valid_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    invalid_at: Optional[datetime] = Field(default=None)
    expired_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    qdrant_id: str = Field(index=True, max_length=36)
    mention_count: int = Field(default=1)
    decay_score: float = Field(default=1.0)
    last_accessed: Optional[datetime] = Field(default=None)

    source_entity: Optional["AIMemEntity"] = Relationship(
        back_populates="outgoing_edges",
        sa_relationship_kwargs={"foreign_keys": "[AIMemEdge.source_entity_id]", "lazy": "noload"},
    )
    target_entity: Optional["AIMemEntity"] = Relationship(
        back_populates="incoming_edges",
        sa_relationship_kwargs={"foreign_keys": "[AIMemEdge.target_entity_id]", "lazy": "noload"},
    )

    @classmethod
    @with_read_session
    async def get_for_entities(
        cls,
        session: AsyncSession,
        entity_ids: list[str],
        scope_key: str,
        limit: int = 30,
        *,
        include_history: bool = False,
    ) -> list["AIMemEdge"]:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        conds: list[ColumnElement[bool]] = [
            _as_bool_expr(col(cls.scope_key) == scope_key),
            _as_bool_expr(or_(col(cls.invalid_at).is_(None), col(cls.invalid_at) > now)),
            _as_bool_expr(
                or_(
                    col(cls.source_entity_id).in_(entity_ids),
                    col(cls.target_entity_id).in_(entity_ids),
                )
            ),
        ]
        if not include_history:
            conds.append(_as_bool_expr(col(cls.resolution_state).in_(["active", "contradicted"])))
        stmt = select(cls).where(*conds).order_by(col(cls.valid_at).desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def list_pair_versions(
        cls,
        session: AsyncSession,
        scope_key: str,
        pairs: list[tuple[str, str]],
        limit: int = 40,
    ) -> list["AIMemEdge"]:
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
            .limit(max(limit * 3, 16))
        )
        kept: list[AIMemEdge] = []
        for row in (await session.execute(stmt)).scalars().all():
            if (row.source_entity_id, row.target_entity_id) not in want:
                continue
            kept.append(row)
            if len(kept) >= limit:
                break
        return kept

    @classmethod
    @with_read_session
    async def list_slot_versions(
        cls,
        session: AsyncSession,
        scope_key: str,
        source_entity_ids: list[str],
        slot_keys: list[str],
        limit: int = 40,
    ) -> list["AIMemEdge"]:
        if not scope_key or not source_entity_ids or not slot_keys:
            return []
        stmt = (
            select(cls)
            .where(
                col(cls.scope_key) == scope_key,
                col(cls.source_entity_id).in_(source_entity_ids),
                col(cls.slot_key).in_(slot_keys),
            )
            .order_by(col(cls.valid_at).asc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def list_thread_milestones(
        cls,
        session: AsyncSession,
        scope_key: str,
        thread_id: str,
        limit: int = 200,
        ascending: bool = True,
    ) -> list["AIMemEdge"]:
        if not scope_key or not thread_id or limit <= 0:
            return []
        stamp = func.coalesce(col(cls.event_at), col(cls.valid_at))
        order = stamp.asc() if ascending else stamp.desc()
        stmt = (
            select(cls)
            .where(
                col(cls.scope_key) == scope_key,
                col(cls.thread_id) == thread_id,
                col(cls.edge_kind) == "milestone",
            )
            .order_by(order, col(cls.turn_index).asc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def prefetch_active_edges(
        cls,
        session: AsyncSession,
        scope_key: str,
        source_entity_ids: list[str],
        limit: int = 80,
    ) -> list["AIMemEdge"]:
        if not scope_key or not source_entity_ids:
            return []
        stmt = (
            select(cls)
            .where(
                col(cls.scope_key) == scope_key,
                col(cls.source_entity_id).in_(source_entity_ids),
                col(cls.resolution_state).in_(["active", "staging", "contradicted"]),
            )
            .order_by(col(cls.valid_at).desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_read_session
    async def search_by_tokens(
        cls,
        session: AsyncSession,
        scope_key: str,
        tokens: list[str],
        limit: int = 24,
    ) -> list["AIMemEdge"]:
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
        result = await session.execute(select(cls).where(c.id.in_(id_q)))
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def get_confidence_inputs(cls, session: AsyncSession, edge_ids: list[str]) -> dict[str, tuple[int, float]]:
        if not edge_ids:
            return {}
        result = await session.execute(
            select(cls.id, cls.mention_count, cls.decay_score).where(col(cls.id).in_(edge_ids))
        )
        return {row[0]: (row[1], row[2]) for row in result.all()}

    @classmethod
    async def touch_accessed(cls, edge_ids: list[str]) -> None:
        if not edge_ids:
            return
        async with sqlite_write_gate:
            await cls._touch_accessed_db(edge_ids)

    @classmethod
    @with_session
    async def _touch_accessed_db(cls, session: AsyncSession, edge_ids: list[str]) -> None:
        await session.execute(
            update(cls).where(col(cls.id).in_(edge_ids)).values(last_accessed=datetime.now(timezone.utc))
        )

    @classmethod
    async def apply_decay(
        cls,
        stale_days: int = 14,
        decay_factor: float = 0.85,
        protect_mention_count: int = 2,
        scope_prefix: Optional[str] = None,
    ) -> int:
        async with sqlite_write_gate:
            return await cls._apply_decay_db(
                stale_days=stale_days,
                decay_factor=decay_factor,
                protect_mention_count=protect_mention_count,
                scope_prefix=scope_prefix,
            )

    @classmethod
    @with_session
    async def _apply_decay_db(
        cls,
        session: AsyncSession,
        stale_days: int = 14,
        decay_factor: float = 0.85,
        protect_mention_count: int = 2,
        scope_prefix: Optional[str] = None,
    ) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
        conditions = [
            col(cls.invalid_at).is_(None),
            col(cls.mention_count) < protect_mention_count,
            or_(col(cls.last_accessed).is_(None), col(cls.last_accessed) < cutoff),
        ]
        if scope_prefix:
            conditions.append(col(cls.scope_key).startswith(scope_prefix))
        result = await session.execute(
            update(cls).where(*conditions).values(decay_score=func.coalesce(col(cls.decay_score), 1.0) * decay_factor)
        )
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    @with_session
    async def collect_forgotten(
        cls,
        session: AsyncSession,
        threshold: float = 0.1,
        scope_prefix: Optional[str] = None,
    ) -> list[str]:
        conditions = [col(cls.decay_score) < threshold]
        if scope_prefix:
            conditions.append(col(cls.scope_key).startswith(scope_prefix))
        result = await session.execute(select(cls.id, cls.qdrant_id).where(*conditions))
        return [row[1] or row[0] for row in result.all()]

    @classmethod
    async def purge_by_ids(cls, edge_ids: list[str]) -> int:
        if not edge_ids:
            return 0
        async with sqlite_write_gate:
            return await cls._purge_by_ids_db(edge_ids)

    @classmethod
    @with_session
    async def _purge_by_ids_db(cls, session: AsyncSession, edge_ids: list[str]) -> int:
        thread_rows = await session.execute(
            select(cls.thread_id).where(col(cls.qdrant_id).in_(edge_ids), col(cls.thread_id).is_not(None))
        )
        affected_threads = {str(r[0]) for r in thread_rows.all() if r[0]}
        await session.execute(delete(cls).where(col(cls.qdrant_id).in_(edge_ids)))
        for tid in affected_threads:
            cnt_res = await session.execute(
                select(func.count())
                .select_from(cls)
                .where(
                    col(cls.thread_id) == tid,
                    col(cls.edge_kind) == "milestone",
                    col(cls.invalid_at).is_(None),
                    col(cls.resolution_state) != "superseded",
                )
            )
            actual = int(cnt_res.scalar_one() or 0)
            await session.execute(update(AIMemThread).where(col(AIMemThread.id) == tid).values(milestone_count=actual))
        return len(edge_ids)

    @classmethod
    @with_session
    async def collect_capacity_overflow(
        cls,
        session: AsyncSession,
        max_per_scope: int,
    ) -> list[str]:
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


__all__ = [
    "AIMemEpisode",
    "AIMemEntity",
    "AIMemEdge",
    "AIMemThread",
    "mem_episode_entity_mentions",
]
