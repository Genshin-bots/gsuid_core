"""消息缓存：配置默认关闭、写入/读取/过期清理。"""

from __future__ import annotations

import time
import asyncio

import pytest
from sqlmodel import SQLModel
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.models import Event
from gsuid_core.message_history.cache import (
    CoreMessageCache,
    query_messages,
    query_sessions,
    payload_from_record,
)
from gsuid_core.message_history.manager import MessageRecord


async def _bind_mem_db(monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    import gsuid_core.utils.database.base_models as base_models

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "sqlite_semaphore", None)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


def _event() -> Event:
    return Event(
        bot_id="QQBot",
        real_bot_id="QQBot",
        bot_self_id="10000",
        user_id="20000",
        group_id="30000",
        user_type="group",
        WS_BOT_ID="ws-1",
    )


def _row(record: MessageRecord, session_id: str, created_at: int) -> dict:
    payload = payload_from_record(_event(), record)
    payload["session_id"] = session_id
    payload["created_at"] = created_at
    return payload


def test_default_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """模板默认值必须是 0（不缓存）；配置为 0 时 enqueue 直接丢掉不报错。"""
    import gsuid_core.message_history.cache as cache_module
    from gsuid_core.utils.plugins_config.message_cache_config import MESSAGE_CACHE_CONIFG

    assert MESSAGE_CACHE_CONIFG["CacheHours"].data == 0

    # 不读本地配置文件，避免被用户自己改过的值影响
    monkeypatch.setattr(cache_module, "cache_hours", lambda: 0)
    assert cache_module.cache_enabled() is False
    cache_module.enqueue({"session_id": "x"})  # 没绑循环也不该抛


def test_payload_keeps_raw_segments() -> None:
    """段数组由调用方直接递进来 —— 它不进内存里的 MessageRecord。"""
    segments = [{"type": "text", "text": "按钮"}]
    record = MessageRecord(
        role="assistant",
        content="按钮",
        user_id="10000",
        metadata={"sender": {"nickname": "机器人"}},
    )
    payload = payload_from_record(_event(), record, segments)
    assert payload["session_id"] == _event().session_id
    assert payload["role"] == "assistant"
    assert '"type": "text"' in payload["segments"]
    assert '"nickname"' in payload["sender"]


def test_payload_without_segments_is_empty() -> None:
    """没传段数组就存空，不去 metadata 里翻。"""
    record = MessageRecord(role="user", content="旧", user_id="10000", metadata={})
    payload = payload_from_record(_event(), record)
    assert payload["segments"] == "[]"


def test_insert_query_and_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        await _bind_mem_db(monkeypatch)
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 48 * 3600 * 1000
        record = MessageRecord(role="user", content="你好", user_id="20000", timestamp=1000.0)

        await CoreMessageCache.batch_insert([_row(record, "s1", now_ms)])
        await CoreMessageCache.batch_insert(
            [_row(MessageRecord(role="user", content="旧的", user_id="20000", timestamp=900.0), "s1", old_ms)]
        )

        rows = await CoreMessageCache.select_recent("s1", 10)
        # 时间正序返回，前端直接追加
        assert [row.content for row in rows] == ["旧的", "你好"]

        messages = await query_messages("s1", 10)
        assert messages[-1]["content"] == "你好"
        assert messages[-1]["segments"] == []

        sessions = await query_sessions()
        assert sessions["s1"]["count"] == 2
        assert sessions["s1"]["row"].content == "你好"

        # 只清 24 小时之前的
        removed = await CoreMessageCache.purge_before(now_ms - 24 * 3600 * 1000)
        assert removed == 1
        left = await CoreMessageCache.select_recent("s1", 10)
        assert [row.content for row in left] == ["你好"]

        assert await CoreMessageCache.clear_all() == 1
        assert await CoreMessageCache.select_recent("s1", 10) == []

    asyncio.run(scenario())


def test_cached_assistant_uses_bot_identity() -> None:
    """机器人自己发的那条，缓存里的 user_id 也是提问的人，读出来要换回 bot。"""
    row = CoreMessageCache(
        session_id="s1",
        role="assistant",
        user_id="20000",
        sender='{"user_id": "10000", "nickname": "机器人"}',
        segments="[]",
    )
    message = row.to_chat_message()
    assert message["user_id"] == "10000"
    assert message["user_name"] == "机器人"
    assert message["segments"] == []


def test_at_segment_gets_user_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """官机的 at 只有 openid，读消息时要用 coreuser 里的昵称补上。

    这里真查一次库：`@with_read_session` 只认类方法，一旦挂到模块级函数上
    整条补全会因为外层吞异常而静默失效，光测结构测不出来。
    """
    import gsuid_core.utils.database.base_models as base_models
    from gsuid_core.utils.database.models import CoreUser
    from gsuid_core.webconsole.session_chat_api import _fill_at_names

    async def scenario() -> None:
        await _bind_mem_db(monkeypatch)
        async with base_models.async_maker() as session:
            session.add(CoreUser(bot_id="qqgroup", user_id="openid-1", user_name="猫猫"))
            # 没采到昵称时表里存的是 "1"，不能当名字显示
            session.add(CoreUser(bot_id="qqgroup", user_id="openid-2", user_name="1"))
            await session.commit()

        messages = [
            {
                "segments": [
                    {"type": "at", "qq": "openid-1", "name": ""},
                    {"type": "at", "qq": "openid-2", "name": ""},
                    {"type": "at", "qq": "openid-3", "name": ""},
                ]
            }
        ]
        await _fill_at_names(messages, "qqgroup")

        segments = messages[0]["segments"]
        assert segments[0]["name"] == "猫猫"
        assert segments[1]["name"] == ""
        # 查不到就留空，前端退回显示 id
        assert segments[2]["name"] == ""

    asyncio.run(scenario())


def test_cached_sender_falls_back_to_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程重启后内存历史是空的，信息卡要靠缓存里的 sender 快照，否则只剩一串 ID。"""
    from gsuid_core.webconsole.session_chat_api import _cached_sender

    async def scenario() -> None:
        await _bind_mem_db(monkeypatch)
        now_ms = int(time.time() * 1000)
        record = MessageRecord(
            role="user",
            content="你好",
            user_id="20000",
            timestamp=1000.0,
            metadata={"sender": {"user_id": "20000", "nickname": "小明", "avatar": "https://a"}},
        )
        await CoreMessageCache.batch_insert([_row(record, "s1", now_ms)])

        sender = await _cached_sender("s1", "20000")
        assert sender["nickname"] == "小明"
        assert sender["avatar"] == "https://a"
        # 指定别的用户就找不到
        assert await _cached_sender("s1", "99999") == {}

    asyncio.run(scenario())


def test_target_info_falls_back_to_core_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """信息卡昵称要查早柚用户库（和 @ 补昵称同一份数据），不能只认消息里的 sender 快照。"""
    import gsuid_core.utils.database.base_models as base_models
    from gsuid_core.utils.database.models import CoreUser
    from gsuid_core.webconsole.session_chat_api import get_chat_target_info

    async def scenario() -> None:
        await _bind_mem_db(monkeypatch)
        async with base_models.async_maker() as session:
            session.add(CoreUser(bot_id="qqgroup", user_id="openid-1", user_name="猫猫", user_icon="https://i"))
            await session.commit()

        data = (await get_chat_target_info("QQBotUID:qqgroup:10000:private:openid-1"))["data"]
        assert data["name"] == "猫猫"
        assert data["avatar"] == "https://i"

    asyncio.run(scenario())


def test_bind_loop_recreates_queue_on_new_loop():
    """事件循环变化时必须重新绑定并新建队列，避免绑定在旧 loop 上。"""
    import asyncio

    import gsuid_core.message_history.cache as cache_module

    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()
    try:
        cache_module.bind_loop(loop1)
        q1 = cache_module._queue
        assert q1 is not None

        cache_module.bind_loop(loop2)
        q2 = cache_module._queue
        assert q2 is not None
        assert q1 is not q2
    finally:
        loop1.close()
        loop2.close()
