"""跨 scope 读取必须等距抽样，不能取 valid_at 升序前 N 条。

取前 N 会让长对话的后半程完全进不来，而依赖「末尾若干行不裁」的尾部保护只能在
已载入的前缀里抽——前缀一截断，尾部保护等于失效。这里用临时 SQLite 打真 SQL。
"""

from __future__ import annotations

import asyncio
from typing import TypeVar
from pathlib import Path
from datetime import datetime, timedelta
from collections.abc import Coroutine

import pytest
from sqlmodel import SQLModel, select
from sqlalchemy import insert
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.ai_core.memory.database.models import (
    AIMemEpisode,
    AIMemTurnGist,
    _model_table,
    _scope_rows_uniform,
)

T = TypeVar("T")

_BASE = datetime(2024, 3, 1)
_N = 500

# (总量, limit)：含用户点名的两个近边界，以及整数倍与稀疏两极。
_CASES = [(501, 500), (1001, 1000), (200, 100), (500, 100), (2000, 1000)]


def _run(coro: Coroutine[object, object, T]) -> T:
    return asyncio.run(coro)


def _ep_rows(n: int) -> list[dict[str, object]]:
    return [
        {
            "id": f"e{i}",
            "scope_key": "sk",
            "content": f"turn {i}",
            "speaker_ids": [],
            "valid_at": _BASE + timedelta(minutes=i),
            "created_at": _BASE,
            "qdrant_id": f"q{i}",
            "is_archived": False,
            "session_id": "s1",
            "turn_index": i,
        }
        for i in range(n)
    ]


def _sample(tmp_path: Path, n: int, limit: int) -> list[str]:
    url = f"sqlite+aiosqlite:///{tmp_path / f'span_{n}_{limit}.db'}"
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    # 走 _model_table 而不是 cls.__table__：后者不在 stub 的 type[cls] 上。
    ep_table = _model_table(AIMemEpisode)

    async def run() -> list[str]:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all, tables=[ep_table])
        async with maker() as s:
            await s.execute(insert(ep_table), _ep_rows(n))
            await s.commit()
        async with maker() as s:
            rows = await _scope_rows_uniform(s, AIMemEpisode, "id", "sk", limit)
            assert [r.valid_at for r in rows] == sorted(r.valid_at for r in rows), "必须按 valid_at 升序"
            return [r.id for r in rows]

    try:
        return _run(run())
    finally:
        _run(engine.dispose())


@pytest.mark.parametrize(("n", "limit"), _CASES)
def test_episode_sampling_returns_exactly_limit_and_keeps_both_ends(tmp_path: Path, n: int, limit: int) -> None:
    """样本数**恰好** limit，首行与末行都在。

    旧实现用 ceil(total/limit) 当步长：总量刚过上限时步长变 2，返回数直接腰斩
    （20001/20000 只剩约 1 万行、4001/4000 只剩约 2001 行）。
    """
    ids = _sample(tmp_path, n, limit)
    assert len(ids) == limit, f"{n} 行取 {limit} 却回了 {len(ids)} 行"
    assert ids[0] == "e0", "最早那条必须在场"
    assert ids[-1] == f"e{n - 1}", "最新那条必须在场——旧实现取前 N 时它整段读不进来"
    assert len(set(ids)) == len(ids), "抽样不该重复命中同一行"


def test_episode_sampling_below_limit_returns_everything(tmp_path: Path) -> None:
    """总量没超上限时不该抽样，原样全取。"""
    ids = _sample(tmp_path, 40, 100)
    assert len(ids) == 40
    assert ids[-1] == "e39"


def test_gist_sampling_uses_its_own_key_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gist 表没有 id 列（主键是 episode_id），抽样必须走各自的列名。"""
    from gsuid_core.utils.database import base_models

    url = f"sqlite+aiosqlite:///{tmp_path / 'gist.db'}"
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    gist_table = _model_table(AIMemTurnGist)

    rows = [
        {
            "episode_id": f"e{i}",
            "scope_key": "sk",
            "session_id": "s1",
            "turn_index": i,
            "valid_at": _BASE + timedelta(minutes=i),
            "gist": f"g{i}",
            "gist_source": "rule",
            "is_new_aspect": False,
            "code_digest": "",
            "source_tag": "",
            "model": "",
            "created_at": _BASE,
        }
        for i in range(_N)
    ]

    async def run() -> list[tuple[datetime, int]]:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all, tables=[gist_table])
        async with maker() as s:
            await s.execute(insert(gist_table), rows)
            await s.commit()
        got = await AIMemTurnGist.list_by_scope("sk", 60, sample=True)
        return [(g.valid_at, g.turn_index) for g in got]

    try:
        keys = _run(run())
    finally:
        _run(engine.dispose())

    assert len(keys) == 60, f"limit=60 却回了 {len(keys)} 行"
    assert keys[0] == (_BASE, 0), "最早那条必须在场"
    assert keys[-1] == (_BASE + timedelta(minutes=_N - 1), _N - 1), "最新那条必须在场"
    assert keys == sorted(keys), "valid_at 相同也要按 turn_index 稳定排序"


def test_list_by_scope_defaults_to_contiguous_rows(tmp_path, monkeypatch) -> None:
    """默认不抽样。评测还原 haystack、oracle 列表要的是整个 scope。

    抽样一旦套在这些读者上，金标 turn 会直接从列表里消失，命中率按残缺样本计。
    """
    from gsuid_core.utils.database import base_models

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'contig.db'}", poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    ep_table = _model_table(AIMemEpisode)

    rows = [
        {
            "id": f"e{i}",
            "scope_key": "sk",
            "session_id": "s1",
            "turn_index": i,
            "valid_at": _BASE + timedelta(minutes=i),
            "content": f"turn {i}",
            "speaker_ids": "",
            "qdrant_id": f"q{i}",
        }
        for i in range(_N)
    ]

    async def run() -> tuple[list[int], list[int]]:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all, tables=[ep_table])
        async with maker() as s:
            await s.execute(insert(ep_table), rows)
            await s.commit()
        contig = await AIMemEpisode.list_by_scope("sk", 60)
        sampled = await AIMemEpisode.list_by_scope("sk", 60, sample=True)
        return [e.turn_index for e in contig], [e.turn_index for e in sampled]

    try:
        contig_idx, sampled_idx = _run(run())
    finally:
        _run(engine.dispose())

    assert contig_idx == list(range(60)), f"默认必须取时间上连续的前 60 行，实得 {contig_idx[:5]}..."
    assert contig_idx != sampled_idx, "抽样与连续取数必须不同，否则这条开关没生效"


def test_backfill_keeps_llm_gist_the_other_sample_would_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """另抽一份 gist 会漏掉被抽中的 llm 行，回填再把它覆盖成 rule。"""
    from gsuid_core.utils.database import base_models
    from gsuid_core.ai_core.memory.lifecycle.gist_backfill import backfill_rule_scope

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'backfill.db'}", poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    ep_table = _model_table(AIMemEpisode)
    gist_table = _model_table(AIMemTurnGist)
    episodes: list[dict[str, object]] = []
    for i in range(8):
        episodes.append(
            {
                "id": f"a{i}",
                "scope_key": "sk",
                "content": f"assistant: note {i}",
                "speaker_ids": [],
                "valid_at": _BASE + timedelta(minutes=i),
                "created_at": _BASE,
                "qdrant_id": f"qa{i}",
                "is_archived": False,
                "session_id": "s1",
                "turn_index": i,
            }
        )
    for i in range(12):
        episodes.append(
            {
                "id": f"u{i}",
                "scope_key": "sk",
                "content": f"user fact {i}",
                "speaker_ids": [],
                "valid_at": _BASE + timedelta(minutes=8 + i),
                "created_at": _BASE,
                "qdrant_id": f"qu{i}",
                "is_archived": False,
                "session_id": "s1",
                "turn_index": 8 + i,
            }
        )
    gists: list[dict[str, object]] = []
    for i in range(40):
        gists.append(
            {
                "episode_id": f"d{i}",
                "scope_key": "sk",
                "session_id": "s0",
                "turn_index": i,
                "valid_at": _BASE - timedelta(days=2, minutes=40 - i),
                "gist": f"decoy {i}",
                "gist_source": "rule",
                "is_new_aspect": False,
                "code_digest": "",
                "source_tag": "",
                "model": "",
                "created_at": _BASE,
            }
        )
    for i in range(12):
        source = "llm" if i in (4, 5) else "rule"
        text = f"KEEP{i}" if i in (4, 5) else f"old {i}"
        gists.append(
            {
                "episode_id": f"u{i}",
                "scope_key": "sk",
                "session_id": "s1",
                "turn_index": 8 + i,
                "valid_at": _BASE + timedelta(minutes=8 + i),
                "gist": text,
                "gist_source": source,
                "is_new_aspect": False,
                "code_digest": "",
                "source_tag": "",
                "model": "",
                "created_at": _BASE,
            }
        )

    async def run() -> dict[str, tuple[str, str]]:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all, tables=[ep_table, gist_table])
        async with maker() as s:
            await s.execute(insert(ep_table), episodes)
            await s.execute(insert(gist_table), gists)
            await s.commit()
        await backfill_rule_scope("sk", limit=4)
        async with maker() as s:
            rows = list((await s.execute(select(AIMemTurnGist))).scalars().all())
        return {row.episode_id: (row.gist_source, row.gist) for row in rows}

    try:
        got = _run(run())
    finally:
        _run(engine.dispose())

    assert got["u5"] == ("llm", "KEEP5")
    assert got["u4"] == ("llm", "KEEP4")
    assert got["u0"][0] == "rule" and got["u0"][1] != "old 0"


def _budget_engine(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    from gsuid_core.utils.database import base_models

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}", poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    return engine, maker


def test_extract_aspects_keeps_caller_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正数 limit 必须封顶。丢掉它时评测入口会按默认窗口 upsert。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode, AIMemTurnGist, _model_table
    from gsuid_core.ai_core.memory.lifecycle.sleep_extract import extract_aspects_for_scope

    engine, maker = _budget_engine(tmp_path, "extract_budget.db", monkeypatch)
    ep_table = _model_table(AIMemEpisode)
    gist_table = _model_table(AIMemTurnGist)

    async def run() -> tuple[int, int]:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all, tables=[ep_table, gist_table])
        async with maker() as s:
            await s.execute(insert(ep_table), _ep_rows(24))
            await s.commit()
        written = await extract_aspects_for_scope("sk", limit=3)
        async with maker() as s:
            stored = list((await s.execute(select(AIMemTurnGist))).scalars().all())
        return written, len(stored)

    try:
        got = _run(run())
    finally:
        _run(engine.dispose())
    assert got == (3, 3)


def test_gist_backfill_tick_keeps_row_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """浅睡的 limit 是行预算。单个 scope 不能改走全量扫描。"""
    from gsuid_core.ai_core.memory.database.models import (
        AIMemEpisode,
        AIMemSession,
        AIMemTurnGist,
        _model_table,
    )
    from gsuid_core.ai_core.memory.lifecycle.gist_backfill import run_gist_backfill_tick

    engine, maker = _budget_engine(tmp_path, "tick_budget.db", monkeypatch)
    ep_table = _model_table(AIMemEpisode)
    gist_table = _model_table(AIMemTurnGist)
    sess_table = _model_table(AIMemSession)

    async def run() -> tuple[int, int]:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all, tables=[ep_table, gist_table, sess_table])
        async with maker() as s:
            await s.execute(insert(ep_table), _ep_rows(24))
            await s.execute(
                insert(sess_table),
                [
                    {
                        "id": "s1",
                        "scope_key": "sk",
                        "start_at": _BASE,
                        "end_at": _BASE,
                        "n_turns": 24,
                        "opener_episode_id": "e0",
                        "title": None,
                        "title_qdrant_id": None,
                        "thread_id": None,
                        "title_source": "opener",
                    }
                ],
            )
            await s.commit()
        written = await run_gist_backfill_tick(limit=3)
        async with maker() as s:
            stored = list((await s.execute(select(AIMemTurnGist))).scalars().all())
        return written, len(stored)

    try:
        got = _run(run())
    finally:
        _run(engine.dispose())
    assert got == (3, 3)


def test_gist_backfill_tick_moves_on_after_a_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下一轮要写还没有 gist 的行。同一小预算不能每轮重写同一批。"""
    from gsuid_core.ai_core.memory.database.models import (
        AIMemEpisode,
        AIMemSession,
        AIMemTurnGist,
        _model_table,
    )
    from gsuid_core.ai_core.memory.lifecycle.gist_backfill import run_gist_backfill_tick

    engine, maker = _budget_engine(tmp_path, "tick_advance.db", monkeypatch)
    ep_table = _model_table(AIMemEpisode)
    gist_table = _model_table(AIMemTurnGist)
    sess_table = _model_table(AIMemSession)

    async def run() -> tuple[list[str], list[str], tuple[str, str]]:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all, tables=[ep_table, gist_table, sess_table])
        async with maker() as s:
            await s.execute(insert(ep_table), _ep_rows(24))
            await s.execute(
                insert(sess_table),
                [
                    {
                        "id": "s1",
                        "scope_key": "sk",
                        "start_at": _BASE,
                        "end_at": _BASE,
                        "n_turns": 24,
                        "opener_episode_id": "e0",
                        "title": None,
                        "title_qdrant_id": None,
                        "thread_id": None,
                        "title_source": "opener",
                    }
                ],
            )
            await s.execute(
                insert(gist_table),
                [
                    {
                        "episode_id": "e0",
                        "scope_key": "sk",
                        "session_id": "s1",
                        "turn_index": 0,
                        "valid_at": _BASE,
                        "gist": "KEEP",
                        "gist_source": "llm",
                        "is_new_aspect": False,
                        "code_digest": "",
                        "source_tag": "",
                        "model": "",
                        "created_at": _BASE,
                    }
                ],
            )
            await s.commit()
        first_n = await run_gist_backfill_tick(limit=2)
        async with maker() as s:
            first_rows = list((await s.execute(select(AIMemTurnGist))).scalars().all())
        first_rule = sorted(row.episode_id for row in first_rows if row.gist_source == "rule")
        kept = next(row for row in first_rows if row.episode_id == "e0")
        assert first_n == 2
        second_n = await run_gist_backfill_tick(limit=2)
        async with maker() as s:
            second_rows = list((await s.execute(select(AIMemTurnGist))).scalars().all())
        rule_ids = sorted(row.episode_id for row in second_rows if row.gist_source == "rule")
        assert second_n == 2
        return first_rule, rule_ids, (kept.gist_source, kept.gist)

    try:
        first_rule, rule_ids, kept = _run(run())
    finally:
        _run(engine.dispose())
    assert first_rule == ["e1", "e2"]
    assert rule_ids == ["e1", "e2", "e3", "e4"]
    assert kept == ("llm", "KEEP")
