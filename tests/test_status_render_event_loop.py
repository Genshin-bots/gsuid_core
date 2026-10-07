"""core信息 渲染期间的取数必须留在主事件循环。

对应 §12.3「事件循环 / Windows 平台坑」：不要另起线程事件循环。写闸门与读信号量
是进程级单例，队列里存的是绑定创建者循环的 Future，跨循环排队会让闸门失效。

回归背景：`_draw_status_uncached` 曾被 `pool.to_thread` 装饰。该装饰器对**协程函数**
会 `asyncio.new_event_loop()`，函数体连同里面的 DB 访问一起跑到另一个循环上，
写闸门排队失效，表现为 `WriteGateTimeout` 与「写闸门占用超过 20s」。

测试只打桩取数入口，不连真实数据库、不依赖进程级单例状态。
"""

from __future__ import annotations

import asyncio
import threading
from typing import List, Tuple

import pytest

from gsuid_core.models import Event
from gsuid_core.status import draw_status as ds
from gsuid_core.utils.database.models import CoreUser, CoreGroup
from gsuid_core.utils.database.global_val_models import (
    CoreDataSummary,
    CoreDataAnalysis,
)

# 渲染会碰到的取数入口，全部打桩成「记录当前循环」后返回安全值。
_ZERO_COUNT_STUBS = (
    (CoreGroup, "get_distinct_group_count"),
    (CoreUser, "get_distinct_user_count"),
)
_NONE_RESULT_STUBS = (
    (CoreDataSummary, "get_day_trends"),
    (CoreDataSummary, "get_yesterday_data"),
    (CoreDataAnalysis, "calculate_dashboard_metrics"),
)


def test_render_runs_its_db_access_on_the_caller_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """渲染期间的取数必须发生在调用方事件循环上。"""
    asyncio.run(_render_runs_its_db_access_on_the_caller_loop(monkeypatch))


async def _render_runs_its_db_access_on_the_caller_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_loop_id = id(asyncio.get_running_loop())
    seen: List[Tuple[str, int, str]] = []

    def _record(name: str) -> None:
        seen.append((name, id(asyncio.get_running_loop()), threading.current_thread().name))

    def _make_zero(cls_name: str, attr: str):
        async def stub(*args, **kwargs) -> int:
            _record(f"{cls_name}.{attr}")
            return 0

        return stub

    def _make_none(cls_name: str, attr: str):
        async def stub(*args, **kwargs):
            _record(f"{cls_name}.{attr}")
            return None

        return stub

    for cls, attr in _ZERO_COUNT_STUBS:
        monkeypatch.setattr(cls, attr, _make_zero(cls.__name__, attr))
    for cls, attr in _NONE_RESULT_STUBS:
        monkeypatch.setattr(cls, attr, _make_none(cls.__name__, attr))

    ev = Event(
        bot_id="onebot",
        user_type="group",
        group_id="1",
        user_id="2",
        bot_self_id="1",
        WS_BOT_ID="onebot",
    )
    res = await ds._draw_status_uncached(ev)

    assert seen, "渲染过程应至少访问一次取数入口"
    off_loop = [s for s in seen if s[1] != caller_loop_id]
    assert not off_loop, f"有 {len(off_loop)}/{len(seen)} 次取数不在调用方事件循环上: {off_loop}"
    # 取数被兜底成占位值也要真出图，避免测试因提前失败而「空过」
    assert res[:2] == b"\xff\xd8", "应渲染出 JPEG"
