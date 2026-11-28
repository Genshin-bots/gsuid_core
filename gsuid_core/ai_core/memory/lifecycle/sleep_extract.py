"""睡眠 tick：维护已有故事线统计与状态机（已废除首句原话造 opener 故事线）。"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from sqlmodel import col, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.identity import is_human_speaker_id

_SLEEP_BATCH = 20
# 故事线状态机流转阈值（唯一定义源，供 sleep_extract / consolidation_worker / v2.1 迁移共用）
THREAD_DORMANT_HOURS = 72
THREAD_ARCHIVE_DAYS = 14


async def _apply_thread_status_transition(
    session: AsyncSession,
    th: object,
    last_at: datetime,
    *,
    dormant_cutoff: datetime,
    archive_cutoff: datetime,
) -> str:
    """对单条故事线执行 active→dormant(72h)→archived(14d) 状态流转；返回新状态或空串。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemThread

    if not isinstance(th, AIMemThread):
        return ""
    if last_at < archive_cutoff:
        if not (th.summary or "").strip():
            facts = [
                str(f).strip()
                for f in (
                    await session.execute(
                        select(col(AIMemEdge.fact))
                        .where(col(AIMemEdge.thread_id) == th.id, col(AIMemEdge.invalid_at).is_(None))
                        .order_by(col(AIMemEdge.valid_at).desc())
                        .limit(2)
                    )
                )
                .scalars()
                .all()
                if isinstance(f, str) and f.strip()
            ]
            if facts:
                th.summary = "；".join(facts)[:120]
        th.status = "archived"
        return "archived"
    if th.status == "active" and last_at < dormant_cutoff:
        th.status = "dormant"
        return "dormant"
    return ""


async def transition_threads(scope_key: str | None = None) -> tuple[int, int]:
    """统一故事线状态机流转（零物理删除）；返回 ``(to_dormant, to_archived)``。"""
    from gsuid_core.utils.database.write_gate import sqlite_write_gate
    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemThread

    now_utc = datetime.now(timezone.utc)
    dormant_cutoff = now_utc - timedelta(hours=THREAD_DORMANT_HOURS)
    archive_cutoff = now_utc - timedelta(days=THREAD_ARCHIVE_DAYS)
    to_dormant = 0
    to_archived = 0

    async with sqlite_write_gate, async_maker() as session:
        stmt = select(AIMemThread).where(col(AIMemThread.status).in_(["active", "dormant"]))
        if scope_key:
            stmt = stmt.where(col(AIMemThread.scope_key) == scope_key)
        rows = list((await session.execute(stmt)).scalars().all())
        for th in rows:
            last_at = th.last_at if th.last_at.tzinfo is not None else th.last_at.replace(tzinfo=timezone.utc)
            new_state = await _apply_thread_status_transition(
                session,
                th,
                last_at,
                dormant_cutoff=dormant_cutoff,
                archive_cutoff=archive_cutoff,
            )
            if new_state == "archived":
                session.add(th)
                to_archived += 1
            elif new_state == "dormant":
                session.add(th)
                to_dormant += 1
        if to_dormant or to_archived:
            await session.commit()
    return to_dormant, to_archived


async def ensure_scope_threads(scope_key: str) -> int:
    """刷新该 scope 已有故事线的片段计数、参与人与状态机；严禁再造 opener 废线。"""
    if not scope_key:
        return 0
    from gsuid_core.utils.database.write_gate import sqlite_write_gate
    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemThread, AIMemEpisode

    now_utc = datetime.now(timezone.utc)
    dormant_cutoff = now_utc - timedelta(hours=THREAD_DORMANT_HOURS)
    archive_cutoff = now_utc - timedelta(days=THREAD_ARCHIVE_DAYS)
    updated = 0

    async with sqlite_write_gate, async_maker() as session:
        threads = list(
            (
                await session.execute(
                    select(AIMemThread).where(
                        col(AIMemThread.scope_key) == scope_key,
                        col(AIMemThread.status).in_(["active", "dormant"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not threads:
            return 0
        tids = [th.id for th in threads]
        ep_rows = (
            await session.execute(
                select(col(AIMemEpisode.thread_id), col(AIMemEpisode.speaker_ids), col(AIMemEpisode.valid_at)).where(
                    col(AIMemEpisode.scope_key) == scope_key,
                    col(AIMemEpisode.thread_id).in_(tids),
                )
            )
        ).all()
        ep_counts: dict[str, int] = {}
        ep_uids: dict[str, set[str]] = {}
        ep_last: dict[str, datetime] = {}
        for tid_raw, spk_raw, vat in ep_rows:
            if not isinstance(tid_raw, str) or not tid_raw:
                continue
            ep_counts[tid_raw] = (ep_counts[tid_raw] if tid_raw in ep_counts else 0) + 1
            if isinstance(spk_raw, list):
                if tid_raw not in ep_uids:
                    ep_uids[tid_raw] = set()
                for uid_val in spk_raw:
                    uid = str(uid_val or "").strip()
                    if is_human_speaker_id(uid):
                        ep_uids[tid_raw].add(uid)
            if isinstance(vat, datetime):
                aware = vat if vat.tzinfo is not None else vat.replace(tzinfo=timezone.utc)
                if tid_raw not in ep_last or aware > ep_last[tid_raw]:
                    ep_last[tid_raw] = aware

        for th in threads:
            changed = False
            cnt = ep_counts[th.id] if th.id in ep_counts else 0
            if cnt != th.episode_count:
                th.episode_count = cnt
                changed = True
            uids = sorted(ep_uids[th.id]) if th.id in ep_uids else []
            if uids and uids != th.get_participant_uids():
                th.set_participant_uids(uids)
                changed = True
            last_at = (
                ep_last[th.id]
                if th.id in ep_last
                else (th.last_at if th.last_at.tzinfo is not None else th.last_at.replace(tzinfo=timezone.utc))
            )
            if await _apply_thread_status_transition(
                session,
                th,
                last_at,
                dormant_cutoff=dormant_cutoff,
                archive_cutoff=archive_cutoff,
            ):
                changed = True
            if changed:
                session.add(th)
                updated += 1
        if updated:
            await session.commit()
    if updated:
        logger.info(i18n_t("log.memory.sleep_extract", scope_key=scope_key, n=updated))
    return updated


async def extract_aspects_for_scope(scope_key: str, limit: int = 8) -> int:
    """兼容评测入口：schema_v2 无独立 gist 抽取，返回 0 表示无需处理。"""
    del scope_key, limit
    return 0


async def run_sleep_extract_tick(limit: int = _SLEEP_BATCH) -> int:
    """定时维护活跃/休眠故事线统计与状态流转，永不阻塞热路径。"""
    if limit <= 0:
        return 0
    from gsuid_core.ai_core.memory.database.models import AIMemEntity

    done = 0
    for scope_key in await AIMemEntity.list_distinct_scope_keys():
        try:
            done += await ensure_scope_threads(scope_key)
        except (OSError, SQLAlchemyError) as e:
            logger.debug(i18n_t("log.memory.sleep_extract_fail", scope_key=scope_key, e=e))
        if done >= limit:
            break
    return done
