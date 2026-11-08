"""睡眠 tick：把尚未挂 Thread 的 session 聚成故事线。"""

from __future__ import annotations

import math
import uuid
from typing import TypedDict
from datetime import datetime, timezone
from collections import deque

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger

_THREAD_COSINE = 0.92
_SLEEP_BATCH = 8
_backlog: deque[str] = deque()
_seen: set[str] = set()


class _OpenerRow(TypedDict):
    session_id: str
    opener_id: str
    start_at: datetime
    title: str


def cosine_dense(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def enqueue_session(session_id: str) -> None:
    if not session_id or session_id in _seen:
        return
    _seen.add(session_id)
    _backlog.append(session_id)


def _title_from_opener(raw: str) -> str:
    from gsuid_core.ai_core.memory.retrieval.lexical import _speaker_stripped, _prose_without_markup

    text = _prose_without_markup(_speaker_stripped(raw or ""))
    text = " ".join(text.split())
    if len(text) > 80:
        return text[:80].rstrip(" ,.;:") + "…"
    return text or "session"


def _title_key(title: str) -> str:
    return " ".join((title or "").lower().split())[:64]


async def ensure_scope_threads(scope_key: str) -> int:
    """未挂 thread 的 session 聚成线程。零 LLM。"""
    if not scope_key:
        return 0
    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemThread, AIMemEpisode

    pending = await AIMemEpisode.list_threadless_sessions(scope_key, limit=80)
    if not pending:
        return 0
    rows: list[_OpenerRow] = []
    opener_ids = [opener_id for _sid, _at, opener_id in pending]
    openers = {}
    for eid in opener_ids:
        row = await AIMemEpisode.get_one(eid)
        if row is not None:
            openers[eid] = row
    for sid, start_at, opener_id in pending:
        op = openers[opener_id] if opener_id in openers else None
        rows.append(
            {
                "session_id": sid,
                "opener_id": opener_id,
                "start_at": start_at,
                "title": _title_from_opener(op.content if op is not None else ""),
            }
        )

    existing = await AIMemThread.list_by_scope(scope_key, limit=120)
    title_to_tid = {_title_key(th.title): th.id for th in existing if th.title}
    for th in existing:
        for alias in th.aliases:
            key = _title_key(alias)
            if key and key not in title_to_tid:
                title_to_tid[key] = th.id

    vecs: dict[str, list[float]] = {}
    try:
        from gsuid_core.ai_core.memory.vector.ops import retrieve_episode_dense_vectors

        vecs = await retrieve_episode_dense_vectors([r["opener_id"] for r in rows if r["opener_id"]])
    except (TimeoutError, OSError, RuntimeError, SQLAlchemyError) as e:
        logger.debug(i18n_t("log.memory.sleep_extract_fail", scope_key=scope_key, e=e))

    thread_rep: dict[str, list[float]] = {}
    for th in existing:
        if th.qdrant_id and th.qdrant_id in vecs:
            thread_rep[th.id] = vecs[th.qdrant_id]

    assigned: dict[str, str] = {}
    created: list[tuple[str, _OpenerRow]] = []
    for row in rows:
        key = _title_key(row["title"])
        tid = title_to_tid[key] if key in title_to_tid else ""
        vec = vecs[row["opener_id"]] if row["opener_id"] in vecs else []
        if not tid and vec:
            best_id = ""
            best = 0.0
            for oid, ov in thread_rep.items():
                sim = cosine_dense(vec, ov)
                if sim > best:
                    best = sim
                    best_id = oid
            if best >= _THREAD_COSINE and best_id:
                tid = best_id
        if not tid:
            tid = str(uuid.uuid4())
            created.append((tid, row))
            title_to_tid[key] = tid
            if vec:
                thread_rep[tid] = vec
        assigned[row["session_id"]] = tid

    now = datetime.now(timezone.utc)
    from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard

    async with db_write_guard(), async_maker() as session:
        for tid, row in created:
            session.add(
                AIMemThread(
                    id=tid,
                    scope_key=scope_key,
                    title=row["title"],
                    aliases=[],
                    summary="",
                    status="active",
                    first_at=row["start_at"],
                    last_at=row["start_at"],
                    n_sessions=0,
                    milestone_count=0,
                    qdrant_id=row["opener_id"],
                    mention_count=1,
                    last_mentioned_at=now,
                    title_source="opener",
                )
            )
        await session.commit()
    for sid, tid in assigned.items():
        await AIMemEpisode.assign_session_thread(sid, tid)
    logger.info(i18n_t("log.memory.sleep_extract", scope_key=scope_key, n=len(assigned)))
    return len(assigned)


async def extract_aspects_for_scope(scope_key: str, limit: int = 8) -> int:
    """兼容评测入口：schema_v2 无独立 gist 抽取，返回 0 表示无需处理。"""
    del scope_key, limit
    return 0


async def run_sleep_extract_tick(limit: int = _SLEEP_BATCH) -> int:
    """先消化 backlog，再扫未挂 thread 的 session。永不阻塞热路径。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    done = 0
    seen_scope: set[str] = set()
    while done < limit and _backlog:
        sid = _backlog.popleft()
        _seen.discard(sid)
        rows = await AIMemEpisode.get_session(sid)
        if not rows:
            continue
        seen_scope.add(rows[0].scope_key)
        try:
            done += await ensure_scope_threads(rows[0].scope_key)
        except (OSError, SQLAlchemyError) as e:
            logger.debug(i18n_t("log.memory.sleep_extract_fail", scope_key=rows[0].scope_key, e=e))
    if done >= limit:
        return done
    from gsuid_core.ai_core.memory.database.models import AIMemEntity

    for scope_key in await AIMemEntity.list_distinct_scope_keys():
        if scope_key in seen_scope:
            continue
        if await AIMemEpisode.list_threadless_sessions(scope_key, limit=1):
            try:
                done += await ensure_scope_threads(scope_key)
            except (OSError, SQLAlchemyError) as e:
                logger.debug(i18n_t("log.memory.sleep_extract_fail", scope_key=scope_key, e=e))
        if done >= limit:
            break
    return done
