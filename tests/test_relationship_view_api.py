"""控制台「关系温度」查询：bot_id 留空必须跨 bot 命中最近结算的那行。

回归背景：留空分支曾用 ``get_scores_for([user_id], "")`` 再按 ``bot_id=""`` 回查，
而写侧 bot_id 恒为真实值（onebot / HTTP / web），于是实盘 310 行里只有 2 行命中，
面板对任何真实用户都回「无」。这条测试锁的是**读路径跨 bot 找得到行**。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlmodel import SQLModel
from sqlalchemy import inspect
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.utils.database import base_models


async def _seed(maker: async_sessionmaker[AsyncSession]) -> None:
    from gsuid_core.ai_core.database.models import UserFavorability

    async with maker() as session:
        # 同一用户三行：留空分支必须挑「最近一次结算」的那行，而不是空串行
        session.add(
            UserFavorability(
                user_id="u1", bot_id="onebot", favorability=54, last_reason="decay.idle", last_eval_at=1000
            )
        )
        session.add(
            UserFavorability(
                user_id="u1", bot_id="web", favorability=12, last_reason="none.no_signal", last_eval_at=5000
            )
        )
        session.add(
            UserFavorability(user_id="u1", bot_id="", favorability=30, last_reason="decay.idle", last_eval_at=0)
        )
        # last_eval_at 并列（大量历史行都是 0），靠 id 兜底保证可重复
        session.add(UserFavorability(user_id="u2", bot_id="onebot", favorability=7, last_eval_at=0))
        session.add(UserFavorability(user_id="u2", bot_id="web", favorability=9, last_eval_at=0))
        # 闲置衰减只写 last_reason 不碰 last_eval_at，
        # 于是陈旧的 bot_id='' 行 last_eval_at=0 却 id 更大，只按 id 兜底会选错行。
        session.add(
            UserFavorability(
                user_id="u3",
                bot_id="onebot",
                favorability=54,
                last_reason="decay.idle",
                last_eval_at=0,
                last_interaction_time=1785221543,
            )
        )
        session.add(
            UserFavorability(
                user_id="u3",
                bot_id="",
                favorability=30,
                last_reason="decay.idle",
                last_eval_at=0,
                last_interaction_time=1779057025,
            )
        )
        await session.commit()


def test_blank_bot_id_resolves_across_bots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_blank_bot_id_resolves_across_bots(tmp_path, monkeypatch))


async def _blank_bot_id_resolves_across_bots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.webconsole.agent_kits_api import relationshipView

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'favor.db').as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        await _seed(maker)

        blank = await relationshipView(user_id="u1", bot_id="", _user={"username": "t"})
        assert blank["data"]["scored"] is True, "留空 bot_id 不该落回未打分"
        assert blank["data"]["bot_id"] == "web", "应取 last_eval_at 最大的一行"
        assert blank["data"]["score"] == 12
        assert blank["data"]["last_reason"] == "none.no_signal"

        explicit = await relationshipView(user_id="u1", bot_id="onebot", _user={"username": "t"})
        assert explicit["data"]["score"] == 54, "指定 bot_id 仍是精确查"

        # 同一查询重复调用必须给同一行，否则排障时数字会自己跳
        again = await relationshipView(user_id="u2", bot_id="", _user={"username": "t"})
        once_more = await relationshipView(user_id="u2", bot_id="", _user={"username": "t"})
        assert again["data"]["bot_id"] == once_more["data"]["bot_id"] == "web"
        assert again["data"]["score"] == once_more["data"]["score"] == 9

        missing = await relationshipView(user_id="nobody", bot_id="", _user={"username": "t"})
        assert missing["data"]["scored"] is False

        # 陈旧的 bot_id='' 行 id 更大但没活动，绝不能被选中
        legacy = await relationshipView(user_id="u3", bot_id="", _user={"username": "t"})
        assert legacy["data"]["bot_id"] == "onebot", "空串陈旧行不是「最近有活动」"
        assert legacy["data"]["score"] == 54

        assert inspect(UserFavorability).local_table is not None
    finally:
        await engine.dispose()
