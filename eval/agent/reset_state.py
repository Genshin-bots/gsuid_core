"""评测前清副作用：取消 eval_ 定时任务，清空 eval_ 记忆与好感。

不碰公共 RAG / 生产记忆。评测 user_id / group_id 带 eval_ 前缀。
"""

from __future__ import annotations

from sqlmodel import col
from sqlalchemy import or_, delete, update
from sqlalchemy.engine import CursorResult

from gsuid_core.utils.database import base_models
from gsuid_core.ai_core.database.models import UserFavorability
from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

_EVAL_MEM_PREFIXES = (
    "group:eval_grp_",
    "user_global:eval_",
    "user_in_group:eval_",
)


async def reset_eval_side_effects() -> dict[str, int]:
    """取消评测定时任务并清空评测记忆/好感。返回计数。"""
    await base_models.init_database()
    async with base_models.async_maker() as session:
        stmt = (
            update(AIScheduledTask)
            .where(
                or_(
                    col(AIScheduledTask.status).in_(("pending", "paused")),
                    col(AIScheduledTask.user_id).like("eval_%"),
                )
            )
            .values(status="cancelled")
        )
        result = await session.execute(stmt)
        n_sched = result.rowcount if isinstance(result, CursorResult) else 0
        fav_stmt = delete(UserFavorability).where(col(UserFavorability.user_id).like("eval_%"))
        fav_result = await session.execute(fav_stmt)
        n_fav = fav_result.rowcount if isinstance(fav_result, CursorResult) else 0
        await session.commit()

    # clear_ops 在 import 时绑定 async_maker；须在 init_database 之后再导入。
    from gsuid_core.ai_core.memory.database.clear_ops import clear_memories_for_scope_async

    n_ep = 0
    n_scope = 0
    for prefix in _EVAL_MEM_PREFIXES:
        cleared = await clear_memories_for_scope_async(scope_pattern=prefix)
        raw = cleared["data"] if "data" in cleared else None
        if not isinstance(raw, dict):
            continue
        scopes = raw["affected_scope_keys"] if "affected_scope_keys" in raw else []
        n_scope += len(scopes) if isinstance(scopes, list) else 0
        if "deleted_episodes" in raw:
            n_ep += int(raw["deleted_episodes"])
    return {
        "cancelled_schedules": n_sched,
        "cleared_favorability": n_fav,
        "cleared_memory_scopes": n_scope,
        "deleted_episodes": n_ep,
    }


# 全量对话记忆 wipe：只清记忆 SQL + memory_* 向量集合 + 定时任务，不动 knowledge / 插件。
# 必须在 core 未运行时执行。
_DIALOGUE_MEM_TABLES = (
    "mem_episode_entity_mentions",
    "mem_category_entity_members",
    "aimemcategoryedge",
    "aimemconflict",
    "aimemedge",
    "aimemcategory",
    "aimementity",
    "aimemepisode",
    "aimempreference",
    "aimemhierarchicalgraphmeta",
)
_MEMORY_COLLECTIONS = (
    "memory_episodes",
    "memory_episodes_cold",
    "memory_entities",
    "memory_edges",
)


def wipe_dialogue_memory_offline(*, qdrant_url: str = "http://127.0.0.1:6333") -> int:
    """离线清空全部对话记忆与定时任务。返回 0 成功。"""
    import sqlite3
    from pathlib import Path
    from urllib.error import URLError, HTTPError
    from urllib.request import Request, urlopen

    db = Path(__file__).resolve().parents[2] / "data" / "GsData.db"
    if not db.exists():
        print(f"missing db: {db}")
        return 1

    def _qdrant(method: str, path: str) -> None:
        req = Request(f"{qdrant_url}{path}", method=method, headers={})
        with urlopen(req, timeout=120) as resp:
            resp.read()

    conn = sqlite3.connect(str(db))
    print("=== before ===")
    for t in (*_DIALOGUE_MEM_TABLES, "aischeduledtask"):
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            print(f"  {t}: {int(n[0]) if n else 0}")
        except sqlite3.Error as e:
            print(f"  {t}: ERR {e}")
    for t in _DIALOGUE_MEM_TABLES:
        try:
            n = conn.execute(f"DELETE FROM {t}").rowcount
            print(f"deleted {t}: {n}")
        except sqlite3.Error as e:
            print(f"skip {t}: {e}")
    n_sched = conn.execute("DELETE FROM aischeduledtask").rowcount
    print(f"deleted aischeduledtask: {n_sched}")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    for name in _MEMORY_COLLECTIONS:
        try:
            _qdrant("DELETE", f"/collections/{name}")
            print(f"deleted qdrant collection {name}")
        except HTTPError as e:
            print(f"qdrant delete {name}: HTTP {e.code}")
        except (URLError, TimeoutError) as e:
            print(f"qdrant delete {name}: {e}")
    return 0


if __name__ == "__main__":
    import asyncio
    import argparse

    p = argparse.ArgumentParser(description="评测副作用清理")
    p.add_argument(
        "--all-dialogue",
        action="store_true",
        help="离线清空全部对话记忆 SQL + memory_* 集合 + 定时任务（core 必须已停；不动 knowledge）",
    )
    args = p.parse_args()
    if args.all_dialogue:
        raise SystemExit(wipe_dialogue_memory_offline())
    print(asyncio.run(reset_eval_side_effects()))
