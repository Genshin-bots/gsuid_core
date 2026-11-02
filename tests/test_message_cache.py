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
    fetch_latest_rows,
    payload_from_record,
    query_session_stats,
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

        # 往上翻页：before 只要比它更早的
        older = await CoreMessageCache.select_recent("s1", 10, before=1000.0)
        assert [row.content for row in older] == ["旧的"]
        assert await query_messages("s1", 10, before=900.0) == []

        messages = await query_messages("s1", 10)
        assert messages[-1]["content"] == "你好"
        assert messages[-1]["segments"] == []

        stats = await query_session_stats()
        assert stats["s1"]["count"] == 2
        assert (await fetch_latest_rows(["s1"]))["s1"].content == "你好"

        # 只清 24 小时之前的
        removed = await CoreMessageCache.purge_before(now_ms - 24 * 3600 * 1000)
        assert removed == 1
        left = await CoreMessageCache.select_recent("s1", 10)
        assert [row.content for row in left] == ["你好"]

        assert await CoreMessageCache.clear_all() == 1
        assert await CoreMessageCache.select_recent("s1", 10) == []

    asyncio.run(scenario())


def test_session_stats_then_fetch_page_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """会话列表先只拿「条数 + 最后时间」排序分页，整行只给当前页的会话取。"""

    async def scenario() -> None:
        await _bind_mem_db(monkeypatch)
        now_ms = int(time.time() * 1000)
        for session_id, contents in (("s1", ["a", "b", "c"]), ("s2", ["x"])):
            rows = [
                _row(
                    MessageRecord(role="user", content=text, user_id="1", timestamp=100.0 * (i + 1)),
                    session_id,
                    now_ms,
                )
                for i, text in enumerate(contents)
            ]
            await CoreMessageCache.batch_insert(rows)

        stats = await query_session_stats()
        assert stats["s1"] == {"count": 3, "last_time": 300.0}
        assert stats["s2"] == {"count": 1, "last_time": 100.0}

        # 只给这一页要显示的会话取整行
        page = await fetch_latest_rows(["s1"])
        assert list(page) == ["s1"]
        assert page["s1"].content == "c"
        # 一页多个会话一次查完，别按会话逐个点查
        both = await fetch_latest_rows(["s1", "s2"])
        assert {key: row.content for key, row in both.items()} == {"s1": "c", "s2": "x"}
        # 空列表不该查库
        assert await fetch_latest_rows([]) == {}

    asyncio.run(scenario())


def test_session_stats_maintained_by_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """会话汇总跟着写入累加，不是每次读的时候现算。"""

    async def scenario() -> None:
        await _bind_mem_db(monkeypatch)
        now_ms = int(time.time() * 1000)
        for text, timestamp in (("a", 100.0), ("b", 200.0)):
            await CoreMessageCache.batch_insert(
                [_row(MessageRecord(role="user", content=text, user_id="1", timestamp=timestamp), "s1", now_ms)]
            )
        assert await query_session_stats() == {"s1": {"count": 2, "last_time": 200.0}}

        # 分批写入要累加，不能覆盖
        await CoreMessageCache.batch_insert(
            [_row(MessageRecord(role="user", content="c", user_id="1", timestamp=300.0), "s1", now_ms)]
        )
        assert (await query_session_stats())["s1"] == {"count": 3, "last_time": 300.0}

        # 只取指定会话时不碰其他会话
        assert await query_session_stats(["s1"]) == {"s1": {"count": 3, "last_time": 300.0}}
        assert await query_session_stats(["nope"]) == {}

    asyncio.run(scenario())


def test_purge_keeps_session_stats_in_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理删掉的可能就是某个会话的最后一条，汇总必须跟着回退；会话删空就整行消失。"""

    async def scenario() -> None:
        import gsuid_core.message_history.cache as cache_module

        await _bind_mem_db(monkeypatch)
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 48 * 3600 * 1000
        # s1：一条过期、一条新的；s2：只有一条过期的
        await CoreMessageCache.batch_insert(
            [
                _row(MessageRecord(role="user", content="老", user_id="1", timestamp=100.0), "s1", old_ms),
                _row(MessageRecord(role="user", content="新", user_id="1", timestamp=200.0), "s1", now_ms),
            ]
        )
        await CoreMessageCache.batch_insert(
            [_row(MessageRecord(role="user", content="过期", user_id="1", timestamp=50.0), "s2", old_ms)]
        )
        assert (await query_session_stats())["s2"]["count"] == 1

        monkeypatch.setattr(cache_module, "cache_hours", lambda: 24)
        assert await cache_module.purge_expired() == 2

        stats = await query_session_stats()
        assert stats == {"s1": {"count": 1, "last_time": 200.0}}

        # 清空缓存连汇总一起清，否则列表里会留下一堆空会话
        assert await CoreMessageCache.clear_all() == 1
        assert await query_session_stats() == {}

    asyncio.run(scenario())


def test_legacy_rows_do_not_need_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    """升级前写入的消息没有汇总行 —— 不迁移，等保留窗口到期自然清掉。

    升级后老会话要等它再说一句话（`bump` 建行）或旧消息过期后才进列表，
    这是有意的取舍：不为了几天内的老数据跑一次全表补齐。
    """

    async def scenario() -> None:
        import gsuid_core.utils.database.base_models as base_models

        await _bind_mem_db(monkeypatch)
        now_ms = int(time.time() * 1000)
        async with base_models.async_maker() as session:
            session.add(
                CoreMessageCache(
                    **_row(MessageRecord(role="user", content="x", user_id="1", timestamp=10.0), "s9", now_ms)
                )
            )
            await session.commit()

        # 老数据没有汇总行，读统计不会为它做任何补偿
        assert await query_session_stats() == {}
        # 一旦它再说一句话，汇总行由写入路径建出来，条数从这一刻开始算
        await CoreMessageCache.batch_insert(
            [_row(MessageRecord(role="user", content="y", user_id="1", timestamp=20.0), "s9", now_ms)]
        )
        assert (await query_session_stats())["s9"] == {"count": 1, "last_time": 20.0}

    asyncio.run(scenario())


def test_session_stats_read_does_not_scan_message_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """列表分页读汇总表就够。

    排序键就是聚合结果，如果每页都去消息表 ``group by session_id``，翻页次数就是
    全表聚合次数。这条守住「读统计不碰消息表」。
    """

    async def scenario() -> None:
        from sqlalchemy import event

        engine = await _bind_mem_db(monkeypatch)
        now_ms = int(time.time() * 1000)
        await CoreMessageCache.batch_insert(
            [
                _row(MessageRecord(role="user", content="a", user_id="1", timestamp=100.0), "s1", now_ms),
                _row(MessageRecord(role="user", content="b", user_id="1", timestamp=200.0), "s2", now_ms),
            ]
        )

        statements: list[str] = []

        def _capture(*args: object) -> None:
            statements.append(str(args[2]).lower())

        event.listen(engine.sync_engine, "before_cursor_execute", _capture)
        try:
            assert (await query_session_stats())["s1"] == {"count": 1, "last_time": 100.0}
            assert (await query_session_stats(["s2"]))["s2"] == {"count": 1, "last_time": 200.0}
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _capture)

        selects = [item for item in statements if item.startswith("select")]
        assert selects, "应该真的查了库"
        assert all("coremessagecachesession" in item for item in selects), selects

    asyncio.run(scenario())


def test_cached_message_key_is_primary_key() -> None:
    """机器人连发两条一模一样的话：msg_id 都是空，只有主键能把它们分开。

    出站记录是在 send 之前构造的，平台还没返回消息 ID，所以 assistant 的
    ``msg_id`` 永远是空串 —— 前端拿它当 React key 会直接撞。
    """
    rows = [
        CoreMessageCache(id=7, session_id="s1", role="assistant", content="在的", msg_id=""),
        CoreMessageCache(id=8, session_id="s1", role="assistant", content="在的", msg_id=""),
    ]
    keys = [row.to_chat_message()["key"] for row in rows]
    assert keys == ["c7", "c8"]
    assert len(set(keys)) == 2
    # 还没落库（没有主键）的记录留空，交给前端兜底
    assert CoreMessageCache(session_id="s1", role="assistant").to_chat_message()["key"] == ""


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
