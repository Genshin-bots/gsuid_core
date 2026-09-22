"""睡眠 tick 补 session / thread。检索路径不写这些表。"""

from __future__ import annotations

import math
import uuid
from typing import TypedDict
from datetime import datetime, timezone
from collections import deque

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import DatabaseWriteTimeout

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
    """未挂 thread 的 session 聚成线程。先补空 session_id，零 LLM。"""
    if not scope_key:
        return 0
    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemEvent, AIMemThread, AIMemEpisode, AIMemSession

    await AIMemEpisode.ensure_sessions(scope_key)
    from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard

    pending = await AIMemSession.untitled_for_scope(scope_key, limit=80)
    if not pending:
        return 0
    openers = await AIMemSession.openers_for([s.id for s in pending])
    opener_by_id = {ep.id: ep for ep in openers}
    rows: list[_OpenerRow] = []
    for sess in pending:
        ep = opener_by_id[sess.opener_episode_id] if sess.opener_episode_id in opener_by_id else None
        title = _title_from_opener(ep.content if ep is not None else "")
        rows.append(
            {
                "session_id": sess.id,
                "opener_id": sess.opener_episode_id,
                "start_at": sess.start_at,
                "title": title,
            }
        )
    if not rows:
        return 0

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
        oid = th.qdrant_id
        if oid and oid in vecs:
            thread_rep[th.id] = vecs[oid]

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

    written = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with db_write_guard(), async_maker() as session:
        for tid, row in created:
            session.add(
                AIMemThread(
                    id=tid,
                    scope_key=scope_key,
                    title=row["title"],
                    aliases=[],
                    first_at=row["start_at"],
                    last_at=row["start_at"],
                    n_sessions=0,
                    qdrant_id=row["opener_id"],
                    mention_count=1,
                    last_mentioned_at=now,
                    title_source="opener",
                )
            )
        from sqlmodel import col
        from sqlalchemy import update as _update

        for row in rows:
            tid = assigned[row["session_id"]]
            await session.execute(
                _update(AIMemSession)
                .where(col(AIMemSession.id) == row["session_id"])
                .values(title=row["title"], thread_id=tid)
            )
            session.add(
                AIMemEvent(
                    scope_key=scope_key,
                    episode_id=row["opener_id"],
                    summary=row["title"],
                    start_at=row["start_at"],
                    stated_at=row["start_at"],
                    event_at=None,
                    thread_id=tid,
                    turn_episode_id=row["opener_id"],
                    mention_count=1,
                    last_mentioned_at=now,
                    qdrant_id=row["opener_id"],
                    entities=[],
                    aliases=[],
                )
            )
            written += 1
        counts: dict[str, int] = {}
        last_at: dict[str, datetime] = {}
        for row in rows:
            tid = assigned[row["session_id"]]
            counts[tid] = (counts[tid] if tid in counts else 0) + 1
            prev = last_at[tid] if tid in last_at else row["start_at"]
            last_at[tid] = row["start_at"] if row["start_at"] > prev else prev
        for tid, n in counts.items():
            await session.execute(
                _update(AIMemThread)
                .where(col(AIMemThread.id) == tid)
                .values(n_sessions=col(AIMemThread.n_sessions) + n, last_at=last_at[tid], last_mentioned_at=now)
            )
        await session.commit()
    logger.info(i18n_t("log.memory.sleep_extract", scope_key=scope_key, n=written))
    return written


async def extract_aspects_for_scope(scope_key: str, limit: int = 8) -> int:
    """旧 extract-light 入口：改走 gist rule 回填，不再抽 aspect。"""
    if not scope_key or limit <= 0:
        return 0
    from gsuid_core.ai_core.memory.lifecycle.gist_backfill import backfill_rule_scope

    return await backfill_rule_scope(scope_key, limit=max(limit, 4000))


async def run_sleep_extract_tick(limit: int = _SLEEP_BATCH) -> int:
    """浅睡：先消化 backlog，再扫未挂 thread 的 session。永不阻塞热路径。"""
    from gsuid_core.ai_core.memory.database.models import AIMemSession

    done = 0
    seen_scope: set[str] = set()
    while done < limit and _backlog:
        sid = _backlog.popleft()
        _seen.discard(sid)
        sess = await AIMemSession.get_by_ids([sid])
        if not sess:
            continue
        seen_scope.add(sess[0].scope_key)
        try:
            done += await ensure_scope_threads(sess[0].scope_key)
        except (OSError, SQLAlchemyError) as e:
            logger.debug(i18n_t("log.memory.sleep_extract_fail", scope_key=sess[0].scope_key, e=e))
    if done >= limit:
        return done
    leftover = await AIMemSession.list_untitled(limit=limit - done)
    for sess in leftover:
        if sess.scope_key in seen_scope:
            continue
        seen_scope.add(sess.scope_key)
        try:
            done += await ensure_scope_threads(sess.scope_key)
        except (OSError, SQLAlchemyError) as e:
            logger.debug(i18n_t("log.memory.sleep_extract_fail", scope_key=sess.scope_key, e=e))
        if done >= limit:
            break
    if done >= limit:
        return done
    from gsuid_core.ai_core.memory.config import memory_config

    if memory_config.eval_mode:
        return done
    if done < limit:
        from gsuid_core.ai_core.memory.lifecycle.gist_backfill import run_gist_backfill_tick

        try:
            done += await run_gist_backfill_tick(limit=limit - done)
        except (OSError, SQLAlchemyError, DatabaseWriteTimeout) as e:
            logger.debug(i18n_t("log.memory.gist_backfill_fail", scope_key="tick", e=e))
    return done
