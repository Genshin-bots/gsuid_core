"""在现存 GsData.db 上按生产路径压测十分钟命令统计落库。

造数用 2099-12-31 + bot_id=__gscore_scale__，不进看板 30 天窗口。
测完删除该切片。用法：

    uv run python eval/manual/bench_analysis_snapshot.py
"""

from __future__ import annotations

import sys
import time
import asyncio
import datetime
from typing import NamedTuple

from sqlmodel import col, func, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.global_val import (
    PlatformVal,
    mark_command_dirty,
    _save_global_val_to_database,
)
from gsuid_core.utils.database.base_models import (
    DB_PATH,
    with_session,
    init_database,
    with_read_session,
)
from gsuid_core.utils.database.global_val_models import (
    DataType,
    CoreDataSummary,
    CoreDataAnalysis,
)

_DAY = datetime.date(2099, 12, 31)
_BOT_ID = "__gscore_scale__"
_BOT_SELF_ID = "scale"


class ScaleCase(NamedTuple):
    dau: int
    cmds_per_user: int
    dag: int
    cmds_per_group: int


CASES: tuple[ScaleCase, ...] = (
    ScaleCase(10_000, 3, 1_000, 3),
    ScaleCase(10_000, 5, 2_000, 5),
    ScaleCase(50_000, 3, 5_000, 3),
)


class _Slice:
    @classmethod
    @with_read_session
    async def count_analysis(cls, session: AsyncSession) -> int:
        stmt = (
            select(func.count())
            .select_from(CoreDataAnalysis)
            .where(
                col(CoreDataAnalysis.date) == _DAY,
                col(CoreDataAnalysis.bot_id) == _BOT_ID,
                col(CoreDataAnalysis.bot_self_id) == _BOT_SELF_ID,
            )
        )
        raw = (await session.execute(stmt)).scalar_one()
        return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else 0

    @classmethod
    @with_read_session
    async def count_users(cls, session: AsyncSession) -> int:
        stmt = select(func.count(func.distinct(col(CoreDataAnalysis.target_id)))).where(
            col(CoreDataAnalysis.date) == _DAY,
            col(CoreDataAnalysis.bot_id) == _BOT_ID,
            col(CoreDataAnalysis.bot_self_id) == _BOT_SELF_ID,
            col(CoreDataAnalysis.data_type) == DataType.USER,
        )
        raw = (await session.execute(stmt)).scalar_one()
        return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else 0

    @classmethod
    @with_read_session
    async def count_summary(cls, session: AsyncSession) -> int:
        stmt = (
            select(func.count())
            .select_from(CoreDataSummary)
            .where(
                col(CoreDataSummary.date) == _DAY,
                col(CoreDataSummary.bot_id) == _BOT_ID,
                col(CoreDataSummary.bot_self_id) == _BOT_SELF_ID,
            )
        )
        raw = (await session.execute(stmt)).scalar_one()
        return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else 0

    @classmethod
    @with_session
    async def wipe(cls, session: AsyncSession) -> tuple[int, int]:
        analysis = await session.execute(
            delete(CoreDataAnalysis).where(
                col(CoreDataAnalysis.date) == _DAY,
                col(CoreDataAnalysis.bot_id) == _BOT_ID,
                col(CoreDataAnalysis.bot_self_id) == _BOT_SELF_ID,
            )
        )
        summary = await session.execute(
            delete(CoreDataSummary).where(
                col(CoreDataSummary.date) == _DAY,
                col(CoreDataSummary.bot_id) == _BOT_ID,
                col(CoreDataSummary.bot_self_id) == _BOT_SELF_ID,
            )
        )
        a_n = analysis.rowcount if isinstance(analysis, CursorResult) else 0
        s_n = summary.rowcount if isinstance(summary, CursorResult) else 0
        return a_n, s_n


def _build_platform_val(case: ScaleCase) -> PlatformVal:
    commands = [f"cmd{i}" for i in range(case.cmds_per_user)]
    group_cmds = [f"gcmd{i}" for i in range(case.cmds_per_group)]
    users: dict[str, dict[str, int]] = {}
    for i in range(case.dau):
        users[f"u{i}"] = {name: 1 + (i % 5) for name in commands}
    groups: dict[str, dict[str, int]] = {}
    for i in range(case.dag):
        groups[f"g{i}"] = {name: 1 + (i % 3) for name in group_cmds}
    command_total = case.dau * case.cmds_per_user
    return {
        "receive": command_total,
        "send": 0,
        "command": command_total,
        "image": 0,
        "user_count": case.dau,
        "group_count": case.dag,
        "user": users,
        "group": groups,
    }


def _expected_rows(case: ScaleCase) -> int:
    return case.dau * case.cmds_per_user + case.dag * case.cmds_per_group


async def _run_case(case: ScaleCase) -> None:
    expect = _expected_rows(case)
    print(
        f"\n== DAU={case.dau} cmds/user={case.cmds_per_user} "
        f"DAG={case.dag} cmds/group={case.cmds_per_group} expect_rows={expect} ==",
        flush=True,
    )
    t0 = time.perf_counter()
    local_val = _build_platform_val(case)
    for user_id, cmds in local_val["user"].items():
        for command_name in cmds:
            mark_command_dirty(_BOT_ID, _BOT_SELF_ID, DataType.USER, user_id, command_name)
    for group_id, cmds in local_val["group"].items():
        for command_name in cmds:
            mark_command_dirty(_BOT_ID, _BOT_SELF_ID, DataType.GROUP, group_id, command_name)
    assemble_s = time.perf_counter() - t0
    print(f"assemble_s={assemble_s:.3f}", flush=True)

    t1 = time.perf_counter()
    await _save_global_val_to_database(local_val, _BOT_ID, _BOT_SELF_ID, _DAY)
    first_s = time.perf_counter() - t1
    print(f"first_write_s={first_s:.3f} rows_per_s={expect / first_s:.0f}", flush=True)

    got = await _Slice.count_analysis()
    users = await _Slice.count_users()
    summaries = await _Slice.count_summary()
    print(f"verify analysis={got} users={users} summary={summaries}", flush=True)
    if got != expect:
        raise RuntimeError(f"analysis row count {got} != {expect}")
    if users != case.dau:
        raise RuntimeError(f"distinct users {users} != {case.dau}")
    if summaries != 1:
        raise RuntimeError(f"summary rows {summaries} != 1")

    local_val["user"]["u0"]["cmd0"] = 99
    mark_command_dirty(_BOT_ID, _BOT_SELF_ID, DataType.USER, "u0", "cmd0")
    t2 = time.perf_counter()
    await _save_global_val_to_database(local_val, _BOT_ID, _BOT_SELF_ID, _DAY)
    second_s = time.perf_counter() - t2
    print(f"second_write_s={second_s:.3f} dirty_rows=1", flush=True)
    got2 = await _Slice.count_analysis()
    if got2 != expect:
        raise RuntimeError(f"second dump analysis {got2} != {expect}")


async def main() -> int:
    print(f"db={DB_PATH} size_mb={DB_PATH.stat().st_size / 1024 / 1024:.1f}", flush=True)
    await init_database()
    await _Slice.wipe()
    try:
        for case in CASES:
            await _run_case(case)
            t3 = time.perf_counter()
            a_n, s_n = await _Slice.wipe()
            print(f"wipe analysis={a_n} summary={s_n} wipe_s={time.perf_counter() - t3:.3f}", flush=True)
    except Exception:
        a_n, s_n = await _Slice.wipe()
        print(f"failed; wiped analysis={a_n} summary={s_n}", flush=True)
        raise
    leftover = await _Slice.count_analysis()
    print(f"leftover_analysis={leftover} size_mb={DB_PATH.stat().st_size / 1024 / 1024:.1f}", flush=True)
    return 0 if leftover == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
