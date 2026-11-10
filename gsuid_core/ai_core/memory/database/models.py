"""记忆系统 SQLModel 数据模型（schema_v2）。

核心四表：
- ``AIMemThread``  : 跨时间故事线 + 追加式里程碑主轴
- ``AIMemEpisode`` : 原始对话片段（带 session / thread / turn 索引列）
- ``AIMemEntity``  : 具名实体节点
- ``AIMemEdge``    : 属性 / 关系 / 里程碑，四态代数（ADD/MERGE/SUPERSEDE/CONTRADICT）

``AIMemPreference`` 原样保留；旧版 Session / TurnGist / Event / Conflict /
Category / HierarchicalGraphMeta 已由 schema_v2 迁移物理移除。
"""

import uuid
import asyncio
from typing import List, TypeVar, Optional
from datetime import datetime, timezone, timedelta

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
    exists,
    insert,
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
from gsuid_core.ai_core.memory.database.fts import index_episode, index_episodes
from gsuid_core.ai_core.memory.database.session_split import SessionCursor, continue_session


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


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


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
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard

        async with db_write_guard(), async_maker() as session:
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
            conds.append(_as_bool_expr(~c.content.like("assistant:%")))
            conds.append(_as_bool_expr(~c.content.like("[我此前说过]%")))
        if speaker:
            conds.append(
                _as_bool_expr(
                    or_(
                        c.content.like(_like_contains(f"{speaker}:"), escape="\\"),
                        c.content.like(_like_contains(f"[{speaker}]:"), escape="\\"),
                    )
                )
            )
        tie = literal_column("rowid") if _db_type == "sqlite" else c.id
        order = (c.valid_at.asc(), tie.asc()) if ascending else (c.valid_at.desc(), tie.desc())
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
            conds.append(_as_bool_expr(~c.content.like("assistant:%")))
            conds.append(_as_bool_expr(~c.content.like("[我此前说过]%")))
        tie = literal_column("rowid") if _db_type == "sqlite" else c.id
        order = (c.valid_at.asc(), tie.asc()) if ascending else (c.valid_at.desc(), tie.desc())
        stmt = select(cls).where(*conds).order_by(*order)
        if offset > 0:
            stmt = stmt.offset(offset)
        result = await session.execute(stmt.limit(limit))
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
        if not scope_key:
            return 0
        c = _model_table(cls).c
        conds: list[ColumnElement[bool]] = [
            _as_bool_expr(c.scope_key == scope_key),
            _as_bool_expr(c.valid_at >= _naive_utc(start)),
            _as_bool_expr(c.valid_at < _naive_utc(end)),
        ]
        if user_only:
            conds.append(_as_bool_expr(~c.content.like("assistant:%")))
            conds.append(_as_bool_expr(~c.content.like("[我此前说过]%")))
        result = await session.execute(select(func.count()).select_from(cls).where(*conds))
        return int(result.scalar_one() or 0)

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
            _as_bool_expr(~c.content.like("assistant:%")),
            _as_bool_expr(~c.content.like("[我此前说过]%")),
        ]
        day_col = func.date(c.valid_at)
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
        if before > 0:
            stmt_b = (
                select(cls)
                .where(col(cls.scope_key) == scope_key, col(cls.valid_at) <= valid_at)
                .order_by(col(cls.valid_at).desc())
                .limit(before + 1)
            )
            for row in reversed(list((await session.execute(stmt_b)).scalars().all())):
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
            for row in (await session.execute(stmt_a)).scalars().all():
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
        head: list[AIMemEpisode] = []
        for row in (
            (await session.execute(select(cls).where(where).order_by(col(cls.valid_at).asc()).limit(head_n)))
            .scalars()
            .all()
        ):
            head.append(row)
        tail_n = limit - head_n
        if tail_n <= 0:
            return head
        tail: list[AIMemEpisode] = []
        for row in (
            (await session.execute(select(cls).where(where).order_by(col(cls.valid_at).desc()).limit(tail_n)))
            .scalars()
            .all()
        ):
            tail.append(row)
        seen = {row.id for row in head}
        for row in reversed(tail):
            if row.id in seen:
                continue
            seen.add(row.id)
            head.append(row)
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
        if _db_type == "sqlite":
            firsts = (
                select(c.session_id.label("sid"), func.min(literal_column("rowid")).label("rid"))
                .where(*conds)
                .group_by(c.session_id)
                .subquery()
            )
            stmt = (
                select(cls)
                .where(literal_column("aimemepisode.rowid").in_(select(firsts.c.rid)), *conds)
                .order_by(c.valid_at.asc())
                .limit(limit)
            )
        else:
            firsts = (
                select(c.session_id.label("sid"), func.min(c.valid_at).label("t0"))
                .where(*conds)
                .group_by(c.session_id)
                .subquery()
            )
            stmt = (
                select(cls).where(c.valid_at.in_(select(firsts.c.t0)), *conds).order_by(c.valid_at.asc()).limit(limit)
            )
        rows = list((await session.execute(stmt)).scalars().all())
        out: list[tuple[str, datetime, str]] = []
        seen: set[str] = set()
        for row in rows:
            sid = row.session_id or ""
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append((sid, row.valid_at, row.id))
        return out

    @classmethod
    async def assign_session_thread(cls, session_id: str, thread_id: str) -> None:
        if not session_id or not thread_id:
            return
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        async def _write() -> None:
            async with async_maker() as session:
                await session.execute(update(cls).where(col(cls.session_id) == session_id).values(thread_id=thread_id))
                await session.commit()

        await under_db_write(_write)

    @classmethod
    async def purge_episodes_by_ids(cls, episode_ids: list[str]) -> int:
        if not episode_ids:
            return 0
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        return await under_db_write(lambda: cls._purge_episodes_by_ids_db(episode_ids))

    @classmethod
    @with_session
    async def _purge_episodes_by_ids_db(cls, session: AsyncSession, episode_ids: list[str]) -> int:
        from sqlalchemy import delete as _delete

        from gsuid_core.ai_core.memory.database.fts import delete_episode_ids

        await session.execute(
            mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.episode_id.in_(episode_ids))
        )
        await delete_episode_ids(session, episode_ids)
        await session.execute(_delete(cls).where(col(cls.id).in_(episode_ids)))
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
    def _normalize_entity_data_names(cls, entities_data: list[dict]) -> None:
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
        from gsuid_core.ai_core.memory.config import memory_config as _mc

        if _mc.eval_mode:
            return {}
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
        name_to_id: dict[str, str] = {}
        vector_payloads: list[dict] = []
        new_entity_count = 0
        cls._normalize_entity_data_names(entities_data)

        existing_map: dict[str, AIMemEntity] = {}
        all_names = [
            (ed["name"] if "name" in ed else "").strip() for ed in entities_data if (ed["name"] if "name" in ed else "")
        ]
        if all_names:
            result = await session.execute(select(cls).where(cls.scope_key == scope_key, col(cls.name).in_(all_names)))
            existing_map = {e.name: e for e in result.scalars().all()}

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
                similar = await _hybrid_search_entities(query=uname, scope_keys=[scope_key], top_k=3)
                return uname, similar

            search_results = await asyncio.gather(*[_hybrid_search_for_name(n) for n in unmatched_names])
            for uname, similar in search_results:
                if similar and similar[0]["score"] >= _mc.dedup_similarity_threshold:
                    sid = similar[0]["id"]
                    r = await session.execute(select(cls).where(cls.id == sid, cls.scope_key == scope_key))
                    matched = r.scalar_one_or_none()
                    if matched:
                        existing_map[uname] = matched

        _MAX_SUMMARY_CHARS = 2000
        speaker_id_set = {s for s in speaker_ids if s}
        for entity_data in entities_data:
            name = (entity_data["name"] if "name" in entity_data else "").strip()
            if not name:
                continue
            existing = existing_map[name] if name in existing_map else None
            tag_list = entity_data["tag"] if "tag" in entity_data else []
            raw_uid = entity_data["user_id"] if "user_id" in entity_data else None
            uid = raw_uid if isinstance(raw_uid, str) and raw_uid.strip() else None
            is_spk = bool(entity_data["is_speaker"]) if "is_speaker" in entity_data else False
            if not is_spk and uid is not None and uid in speaker_id_set:
                is_spk = True
            raw_kind = entity_data["kind"] if "kind" in entity_data else "person"
            kind = raw_kind if isinstance(raw_kind, str) else "person"
            if is_spk:
                if kind == "person":
                    kind = "speaker"
                if "speaker" not in tag_list:
                    tag_list = list(tag_list) + ["speaker"]
            if existing:
                if is_spk and not existing.is_speaker:
                    existing.is_speaker = True
                if is_spk and existing.entity_kind == "person":
                    existing.entity_kind = "speaker"
                if uid and not existing.user_id:
                    existing.user_id = uid
                new_summary = entity_data["summary"] if "summary" in entity_data else ""
                if new_summary and new_summary not in existing.summary:
                    combined = f"{existing.summary}\n{new_summary}".strip()
                    if len(combined) > _MAX_SUMMARY_CHARS:
                        combined = combined[:_MAX_SUMMARY_CHARS] + "\n[...早期记忆已截断...]"
                    existing.summary = combined
                existing_tags = existing.tag if isinstance(existing.tag, list) else []
                merged_tags = list(set(existing_tags) | set(tag_list))
                if is_spk and "speaker" not in merged_tags:
                    merged_tags.append("speaker")
                existing.tag = merged_tags
                existing.updated_at = datetime.now(timezone.utc)
                session.add(existing)
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
                new_entity_count += 1
                new_entity = cls(
                    id=entity_id,
                    scope_key=scope_key,
                    name=name,
                    summary=entity_data["summary"] if "summary" in entity_data else "",
                    tag=tag_list,
                    is_speaker=is_spk,
                    entity_kind=kind,
                    user_id=uid,
                    qdrant_id=entity_id,
                )
                session.add(new_entity)
                vector_payloads.append(
                    {
                        "entity_id": entity_id,
                        "name": name,
                        "summary": entity_data["summary"] if "summary" in entity_data else "",
                        "scope_key": scope_key,
                        "is_speaker": is_spk,
                        "user_id": uid,
                        "tag": tag_list,
                    }
                )
                name_to_id[name] = entity_id

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
    @with_session
    async def collect_orphans(
        cls,
        session: AsyncSession,
        ttl_days: int = 10,
    ) -> list[tuple[str, str, str]]:
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
        if not entity_ids:
            return 0
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        return await under_db_write(lambda: cls._purge_orphans_by_ids_db(entity_ids))

    @classmethod
    @with_session
    async def _purge_orphans_by_ids_db(cls, session: AsyncSession, entity_ids: list[str]) -> int:
        from sqlalchemy import delete as _delete

        await session.execute(
            mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.entity_id.in_(entity_ids))
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
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        await under_db_write(lambda: cls._touch_accessed_db(edge_ids))

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
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        return await under_db_write(
            lambda: cls._apply_decay_db(
                stale_days=stale_days,
                decay_factor=decay_factor,
                protect_mention_count=protect_mention_count,
                scope_prefix=scope_prefix,
            )
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
    ) -> list["AIMemPreference"]:
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
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    async def touch_applied(cls, pref_ids: list[str]) -> None:
        if not pref_ids:
            return
        from gsuid_core.ai_core.memory.ingestion.eval_write_lock import under_db_write

        await under_db_write(lambda: cls._touch_applied_db(pref_ids))

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
        from sqlalchemy import delete as _delete

        result = await session.execute(_delete(cls).where(col(cls.scope_key).in_(scope_keys)))
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    async def cleanup_polluted_scopes_in_session(cls, session: AsyncSession) -> int:
        """清理误写进 user_global 的助手自 ID 与群号偏好行（幂等）。"""
        from sqlalchemy import or_ as _or, delete as _delete, inspect as _inspect

        from gsuid_core.utils.database.models import CoreGroup

        conn = await session.connection()
        table_names = await conn.run_sync(lambda sync_conn: set(_inspect(sync_conn).get_table_names()))
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
            bogus_scopes = [f"user_global:{gid}" for gid in known_gids]
            conds.append(col(cls.scope_key).in_(bogus_scopes))

        result = await session.execute(_delete(cls).where(_or(*conds)))
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    async def prune_per_context(cls, max_per_context: int) -> int:
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


__all__ = [
    "AIMemEpisode",
    "AIMemEntity",
    "AIMemEdge",
    "AIMemThread",
    "AIMemPreference",
    "mem_episode_entity_mentions",
]
