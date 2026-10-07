"""管理员花名册：列出全部已打分用户，而不是只查一个人。

锁住的行为：

- 空库返回空列表，total 为 0
- 排序按分数从高到低；同分再按最近结算，再按 id，重复调用不换行
- ``bot_id`` 留空是全部 bot，不是匹配空串 bot_id
- 关键词只打在 user_id / user_name 上，``%`` 和 ``_`` 按字面量
- 档位、原因、当日预算跟该行一起返回；主人标记不改分数档
- 路由依赖管理员，登录用户不够
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlmodel import SQLModel
from fastapi.routing import APIRoute
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.utils.database import base_models


def _calls(route: APIRoute) -> list[object]:
    found: list[object] = []
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        found.append(dep.call)
        stack.extend(dep.dependencies)
    return found


def test_relationship_list_requires_admin() -> None:
    from gsuid_core.webconsole.app_app import app
    from gsuid_core.webconsole.web_api import require_admin
    from gsuid_core.webconsole.agent_kits_api import relationshipList

    matched = [route for route in app.routes if isinstance(route, APIRoute) and route.path == "/api/relationship/list"]
    assert len(matched) == 1
    route = matched[0]
    assert route.endpoint is relationshipList
    methods = route.methods
    assert methods is not None and "GET" in methods
    assert require_admin in _calls(route)


def test_relationship_list_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_relationship_list_roster(tmp_path, monkeypatch))


async def _relationship_list_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.webconsole.agent_kits_api import relationshipList

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'favor.db').as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr(
        "gsuid_core.ai_core.utils._is_master_user",
        lambda user_id: user_id == "closeuser",
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        empty = await relationshipList(bot_id="", keyword="", offset=0, limit=50, _user={"role": "admin"})
        assert empty["data"]["items"] == []
        assert empty["data"]["total"] == 0

        async with maker() as session:
            # 插入顺序决定 id。同分且结算时间相同的两行，id 更大的排前面。
            session.add(
                UserFavorability(
                    user_id="tieold",
                    bot_id="onebot",
                    user_name="旧",
                    favorability=50,
                    last_eval_at=20,
                    last_reason="pos.old",
                    last_delta=1,
                )
            )
            session.add(
                UserFavorability(
                    user_id="closeuser",
                    bot_id="onebot",
                    user_name="阿禾",
                    favorability=80,
                    last_eval_at=100,
                    last_reason="pos.chat",
                    last_delta=2,
                    daily_gain=2,
                    daily_loss=0,
                    daily_ymd="2026-10-08",
                    interaction_count=4,
                    last_positive_interact_at=100,
                )
            )
            session.add(
                UserFavorability(
                    user_id="tienew",
                    bot_id="onebot",
                    user_name="新",
                    favorability=50,
                    last_eval_at=20,
                    last_reason="pos.new",
                    last_delta=1,
                )
            )
            session.add(
                UserFavorability(
                    user_id="wilduser",
                    bot_id="onebot",
                    user_name="100%_ok",
                    favorability=1,
                    last_eval_at=1,
                    last_reason="none.no_signal",
                )
            )
            session.add(
                UserFavorability(
                    user_id="colduser",
                    bot_id="web",
                    user_name="冷",
                    favorability=-40,
                    last_eval_at=50,
                    last_reason="neg.insult",
                    last_delta=-3,
                    daily_loss=3,
                )
            )
            await session.commit()

        listed = await relationshipList(bot_id="", keyword="", offset=0, limit=50, _user={"role": "admin"})
        items = listed["data"]["items"]
        assert listed["data"]["total"] == 5
        assert [row["user_id"] for row in items] == [
            "closeuser",
            "tienew",
            "tieold",
            "wilduser",
            "colduser",
        ]

        again = await relationshipList(bot_id="   ", keyword="", offset=0, limit=50, _user={"role": "admin"})
        assert [row["user_id"] for row in again["data"]["items"]] == [row["user_id"] for row in items]

        close = items[0]
        assert close["score"] == 80
        assert close["zone"] == "close"
        assert close["zone_label"] == "亲近"
        assert close["is_master"] is True
        assert close["user_name"] == "阿禾"
        assert "主人" in close["line"]
        assert "很熟" in close["line"]
        assert close["last_reason"] == "pos.chat"
        assert close["last_delta"] == 2
        assert close["daily_gain"] == 2
        assert close["daily_ymd"] == "2026-10-08"
        assert close["interaction_count"] == 4
        assert close["bot_id"] == "onebot"

        cold = items[-1]
        assert cold["zone"] == "cold"
        assert cold["zone_label"] == "冷淡"
        assert cold["is_master"] is False
        assert "主人" not in cold["line"]
        assert cold["last_reason"] == "neg.insult"
        assert cold["daily_loss"] == 3

        onebot = await relationshipList(bot_id="onebot", keyword="", offset=0, limit=50, _user={"role": "admin"})
        assert onebot["data"]["total"] == 4
        assert "colduser" not in [row["user_id"] for row in onebot["data"]["items"]]

        by_name = await relationshipList(bot_id="", keyword="阿", offset=0, limit=50, _user={"role": "admin"})
        assert [row["user_id"] for row in by_name["data"]["items"]] == ["closeuser"]

        by_id = await relationshipList(bot_id="", keyword="colduser", offset=0, limit=50, _user={"role": "admin"})
        assert [row["user_id"] for row in by_id["data"]["items"]] == ["colduser"]

        # bot_id 不参与关键词，避免把「web」搜成某个 bot 的全表。
        assert (await relationshipList(bot_id="", keyword="web", offset=0, limit=50, _user={"role": "admin"}))["data"][
            "total"
        ] == 0

        literal_pct = await relationshipList(bot_id="", keyword="%", offset=0, limit=50, _user={"role": "admin"})
        assert [row["user_id"] for row in literal_pct["data"]["items"]] == ["wilduser"]

        literal_us = await relationshipList(bot_id="", keyword="_", offset=0, limit=50, _user={"role": "admin"})
        assert [row["user_id"] for row in literal_us["data"]["items"]] == ["wilduser"]

        page = await relationshipList(bot_id="", keyword="", offset=0, limit=2, _user={"role": "admin"})
        assert page["data"]["total"] == 5
        assert [row["user_id"] for row in page["data"]["items"]] == ["closeuser", "tienew"]
        assert page["data"]["offset"] == 0
        assert page["data"]["limit"] == 2

        tail = await relationshipList(bot_id="", keyword="", offset=4, limit=2, _user={"role": "admin"})
        assert [row["user_id"] for row in tail["data"]["items"]] == ["colduser"]
        assert tail["data"]["total"] == 5

        past = await relationshipList(bot_id="", keyword="", offset=10, limit=2, _user={"role": "admin"})
        assert past["data"]["items"] == []
        assert past["data"]["total"] == 5
    finally:
        await engine.dispose()
