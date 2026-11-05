"""Tests for context clarity, referent resolution, dual-track history lean-down,
and pending unread media promotion under multi-user group chat.
All IDs and user names are strictly sanitized and anonymized.
"""

from __future__ import annotations

import time
import asyncio

import pytest
from pydantic_ai.messages import (
    TextPart,
    ModelRequest,
    ToolCallPart,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
    SystemPromptPart,
)

from gsuid_core.models import Event
from gsuid_core.ai_core.utils import (
    _relean_user_turn,
    prepare_content_payload,
    _truncate_history_keep_prefix,
)
from gsuid_core.message_history import MessageRecord, get_history_manager
from gsuid_core.ai_core.outbound import resolve_quote, inspect_quote_relation
from gsuid_core.ai_core.control.lines import (
    QUOTE_TOME_MARKER,
    pending_unread_image_guide_line,
)
from gsuid_core.ai_core.turn_pipeline import extract_pending_unread_images
from gsuid_core.ai_core.history_format import compose_group_history
from gsuid_core.ai_core.interaction_scaffold import build_turn_graph


@pytest.fixture(autouse=True)
def clean_history_manager():
    hm = get_history_manager()
    hm._histories.clear()
    hm._msg_id_cache.clear()
    yield
    hm._histories.clear()
    hm._msg_id_cache.clear()


@pytest.mark.asyncio
async def test_pending_unread_images_promotion() -> None:
    """A user sends an image in message 1, then follows up with '你怎么看' in message 2.

    The pending unread image is promoted to this turn's prompt.
    """
    hm = get_history_manager()

    # Turn 1: user_101 posts an image
    ev1 = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="user_101",
        sender={"nickname": "Alice"},
        group_id="group_202",
        msg_id="msg_001",
        image_id="img_test_101",
        image_id_list=["img_test_101"],
        text="[图片]",
        raw_text="[图片]",
    )
    hm.add_message(ev1, role="user", content="[图片]", metadata={"image_id_list": ["img_test_101"]})

    # Turn 2: user_101 follows up with text only
    ev2 = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="user_101",
        sender={"nickname": "Alice"},
        group_id="group_202",
        msg_id="msg_002",
        text="你怎么看这个？",
        raw_text="你怎么看这个？",
    )
    hm.add_message(ev2, role="user", content="你怎么看这个？")

    # When image has not been read yet
    read_images: set[str] = set()
    pending = extract_pending_unread_images(ev2, read_images, window_seconds=180.0)
    assert len(pending) == 1
    assert pending[0].img_id == "img_test_101"

    # Prepare payload with pending images
    user_messages, _ = await prepare_content_payload(
        ev2,
        quoted_tome=False,
        pending_unread_images=pending,
    )
    full_text = "\n".join(str(m) for m in user_messages)
    expected_guide = pending_unread_image_guide_line("img_test_101")
    assert expected_guide in full_text
    assert "你怎么看这个？" in full_text

    # Once image is marked as read, pending list should be empty
    read_images.add("img_test_101")
    pending_after_read = extract_pending_unread_images(ev2, read_images, window_seconds=180.0)
    assert len(pending_after_read) == 0


@pytest.mark.asyncio
async def test_self_quote_with_at_bot_resolution() -> None:
    """User quotes their own earlier image and @Bot.

    quoted_tome must be False, and quote_self_line must be injected.
    """
    hm = get_history_manager()

    # Earlier message from user_101
    ev_early = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="user_101",
        sender={"nickname": "Alice"},
        group_id="group_202",
        msg_id="msg_early_100",
        image_id="img_test_201",
        image_id_list=["img_test_201"],
        text="这是我的画",
        raw_text="这是我的画",
    )
    hm.add_message(
        ev_early,
        role="user",
        content="这是我的画",
        metadata={"msg_id": "msg_early_100", "image_id_list": ["img_test_201"]},
    )

    # Current message: user_101 quotes msg_early_100 and @Bot
    ev_cur = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="user_101",
        sender={"nickname": "Alice"},
        group_id="group_202",
        msg_id="msg_cur_101",
        reply_id="msg_early_100",
        reply="这是我的画",
        at_bot=True,
        is_tome=True,
        text="你看下这个怎么样",
        raw_text="你看下这个怎么样",
    )
    hm.add_message(ev_cur, role="user", content="你看下这个怎么样")

    # 1. Resolve quote
    resolved = await resolve_quote(ev_cur)
    assert resolved is not None
    assert resolved.target == "当前说话人"
    assert resolved.is_bot_reply is False

    # 2. Turn graph: @Bot with quote does not quote Bot
    tg = build_turn_graph(
        ev_cur.raw_text,
        persona_name="TestBot",
        is_tome=True,
        user_type="group",
        primary_speaker="user_101",
        at_bot=True,
        is_reply_to_bot=False,
    )
    assert tg.quoted_tome is False

    # 3. Prepare payload: quote_self_line should be present
    user_messages, _ = await prepare_content_payload(ev_cur, quoted_tome=tg.quoted_tome)
    full_text = "\n".join(str(m) for m in user_messages)
    assert "不是你发的" in full_text
    assert "你看下这个怎么样" in full_text


def test_group_history_partitions_speaker_thread_and_timeline() -> None:
    """Group history partitions speaker thread and others timeline cleanly."""
    now = time.time()
    records: list[MessageRecord] = [
        MessageRecord(
            role="user",
            content="大家今天都在忙什么呢？",
            user_id="user_101",
            user_name="Alice",
            timestamp=now - 50,
            metadata={"time_str": "10:00:10"},
        ),
        MessageRecord(
            role="user",
            content="我在写代码，顺便喝杯咖啡",
            user_id="user_102",
            user_name="Bob",
            timestamp=now - 30,
            metadata={"time_str": "10:00:30"},
        ),
        MessageRecord(
            role="user",
            content="我刚发了一张图",
            user_id="user_103",
            user_name="Charlie",
            timestamp=now - 20,
            metadata={"time_str": "10:00:40", "image_id_list": ["img_charlie_01"]},
        ),
        MessageRecord(
            role="user",
            content="小助手今天天气怎么样呀",
            user_id="user_101",
            user_name="Alice",
            timestamp=now - 5,
            metadata={"time_str": "10:00:55"},
        ),
    ]

    hist_str = compose_group_history(records, current_user_id="user_101")
    assert "[与你的对话]" in hist_str
    assert "[历史对话]" in hist_str
    # Image ID formatting check
    assert "[图片ID: img_charlie_01]" in hist_str
    assert "Bob(用户ID:user_102)" in hist_str
    assert "Charlie(用户ID:user_103)" in hist_str


def test_b_track_lean_down_strips_ephemeral_system_lines() -> None:
    """When a turn settles, B-track UserPromptPart strips ephemeral system instructions."""
    raw_prompt = (
        "Alice: 帮我算一下 2 + 2\n"
        "（系统提示：当前说话人近期发了待讨论图片但尚未被阅读。若该句在承接图片，请优先调用 read_image）\n"
        "（系统校验：请确保格式正确）"
    )
    req = ModelRequest(parts=[UserPromptPart(content=raw_prompt)])
    _relean_user_turn([req], lean_content="Alice: 帮我算一下 2 + 2")
    assert len(req.parts) == 1
    part = req.parts[0]
    assert isinstance(part, UserPromptPart)
    assert isinstance(part.content, str)
    assert "（系统提示：" not in part.content
    assert "（系统校验：" not in part.content
    assert part.content == "Alice: 帮我算一下 2 + 2"


def test_truncate_history_keep_prefix_preserves_closed_tool_pairs() -> None:
    """Middle-cut compaction preserves prefix, suffix, and keeps tool call/return pairs closed."""
    prefix = [
        ModelRequest(parts=[SystemPromptPart(content="System prompt")]),
        ModelRequest(parts=[UserPromptPart(content="Hello")]),
        ModelResponse(parts=[TextPart(content="Hi there")]),
    ]

    middle = []
    # Add several multi-step tool calls
    for i in range(5):
        call_id = f"call_{i}"
        middle.extend(
            [
                ModelRequest(parts=[UserPromptPart(content=f"Query {i}")]),
                ModelResponse(parts=[ToolCallPart(tool_name="test_tool", args={"q": i}, tool_call_id=call_id)]),
                ModelRequest(
                    parts=[ToolReturnPart(tool_name="test_tool", content=f"Result {i}", tool_call_id=call_id)]
                ),
                ModelResponse(parts=[TextPart(content=f"Answer {i}")]),
            ]
        )

    suffix = [
        ModelRequest(parts=[UserPromptPart(content="Latest user question")]),
        ModelResponse(parts=[TextPart(content="Latest answer")]),
    ]

    full_history = prefix + middle + suffix
    # Request a compaction that cuts some middle messages
    compacted = _truncate_history_keep_prefix(full_history, max_keep=10)

    # Prefix is preserved byte-identically
    assert compacted[0] == prefix[0]
    # Suffix latest message is present
    last_msg = compacted[-1]
    assert isinstance(last_msg, ModelResponse)
    assert any(isinstance(p, TextPart) and "Latest answer" in p.content for p in last_msg.parts)

    # Verify tool pairs are completely closed: any ToolCallPart in compacted has its ToolReturnPart
    call_ids = {
        p.tool_call_id
        for m in compacted
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart)
    }
    return_ids = {
        p.tool_call_id
        for m in compacted
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    }
    assert call_ids == return_ids


@pytest.mark.asyncio
async def test_bystander_quote_with_client_auto_at_bot() -> None:
    """When a bystander quotes Bot's message, C-end client auto-injects @Bot.

    The framework detects is_bystander_quote=True, marks quoted_tome=True,
    and resolves the quote with bystander description without asserting direct address.
    """
    hm = get_history_manager()

    # 1. Earlier Bot message replying to user_101 (Alice)
    ev_bot = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="bot_main",
        sender={"nickname": "TestBot"},
        group_id="group_202",
        msg_id="msg_bot_050",
        text="这是为您生成的查询结果",
        raw_text="这是为您生成的查询结果",
    )
    hm.add_message(
        ev_bot,
        role="assistant",
        content="这是为您生成的查询结果",
        metadata={
            "msg_id": "msg_bot_050",
            "reply_to_user_id": "user_101",
            "reply_to_user_name": "Alice",
        },
    )

    # 2. Bystander user_102 (Bob) quotes msg_bot_050.
    # C-end mobile client automatically sets at_bot=True!
    ev_bob = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="user_102",
        sender={"nickname": "Bob"},
        group_id="group_202",
        msg_id="msg_bob_051",
        reply_id="msg_bot_050",
        reply="这是为您生成的查询结果",
        at_bot=True,
        is_tome=True,
        text="笑死我了，这什么鬼",
        raw_text="@TestBot 笑死我了，这什么鬼",
    )

    # 3. Test inspect_quote_relation
    is_rep_bot, is_bystander, is_stale = inspect_quote_relation(ev_bob)
    assert is_rep_bot is True
    assert is_bystander is True
    assert is_stale is False

    # 4. Test build_turn_graph
    tg = build_turn_graph(
        ev_bob.raw_text,
        persona_name="TestBot",
        is_tome=True,
        user_type="group",
        primary_speaker="user_102",
        at_bot=True,
        has_reply=True,
        is_reply_to_bot=is_rep_bot,
        is_bystander_quote=is_bystander,
        is_stale_quote=is_stale,
    )
    # Bystander quote must be quoted_tome=True (not direct call)
    assert tg.quoted_tome is True
    assert tg.is_bystander_quote is True

    # 5. Test prepare_content_payload: must NOT inject DIRECT_MARKER
    user_messages, _ = await prepare_content_payload(ev_bob, quoted_tome=tg.quoted_tome)
    full_text = "\n".join(str(m) for m in user_messages)
    assert "（直接找你说的）" not in full_text
    assert QUOTE_TOME_MARKER in full_text
    assert "当前说话人属于旁观借楼" in full_text


@pytest.mark.asyncio
async def test_stale_quote_marked_as_quoted_tome() -> None:
    """When a user quotes a Bot message from over 180s ago, it is marked as stale and quoted_tome=True."""
    hm = get_history_manager()
    t_old = time.time() - 360  # 6 minutes ago

    ev_bot = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="bot_main",
        sender={"nickname": "TestBot"},
        group_id="group_202",
        msg_id="msg_bot_old_01",
        text="这是早先生成的查询结果",
        raw_text="这是早先生成的查询结果",
    )
    rec_bot = hm.add_message(
        ev_bot,
        role="assistant",
        content="这是早先生成的查询结果",
        metadata={
            "msg_id": "msg_bot_old_01",
            "reply_to_user_id": "user_101",
            "reply_to_user_name": "Alice",
        },
    )
    rec_bot.timestamp = t_old

    ev_alice = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="user_101",
        sender={"nickname": "Alice"},
        group_id="group_202",
        msg_id="msg_cur_stale",
        reply_id="msg_bot_old_01",
        reply="这是早先生成的查询结果",
        at_bot=True,
        is_tome=True,
        text="这个我才看到",
        raw_text="@TestBot 这个我才看到",
    )

    is_rep_bot, is_bystander, is_stale = inspect_quote_relation(ev_alice)
    assert is_rep_bot is True
    assert is_bystander is False
    assert is_stale is True

    tg = build_turn_graph(
        ev_alice.raw_text,
        persona_name="TestBot",
        is_tome=True,
        user_type="group",
        primary_speaker="user_101",
        at_bot=True,
        has_reply=True,
        is_reply_to_bot=is_rep_bot,
        is_bystander_quote=is_bystander,
        is_stale_quote=is_stale,
    )
    assert tg.quoted_tome is True
    assert tg.is_stale_quote is True

    resolved = await resolve_quote(ev_alice)
    assert resolved is not None
    assert resolved.is_bot_reply is True
    assert resolved.is_stale_quote is True
    assert "较早发言" in resolved.line


@pytest.mark.asyncio
async def test_command_card_quote_resolution() -> None:
    """When a user quotes a command output card, resolve_quote marks it as command card."""
    hm = get_history_manager()
    ev_bot_cmd = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="bot_main",
        group_id="group_202",
        msg_id="msg_bot_cmd_01",
        text="[卡片] 今日运势",
        raw_text="[卡片] 今日运势",
    )
    hm.add_message(
        ev_bot_cmd,
        role="assistant",
        content="[卡片] 今日运势",
        metadata={
            "msg_id": "msg_bot_cmd_01",
            "speech_channel": "command",
            "reply_to_user_id": "user_101",
        },
    )

    ev_user = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="user_101",
        group_id="group_202",
        msg_id="msg_user_cmd_quote",
        reply_id="msg_bot_cmd_01",
        reply="[卡片] 今日运势",
        text="这个签不错",
        raw_text="这个签不错",
    )

    resolved = await resolve_quote(ev_user)
    assert resolved is not None
    assert resolved.topic == "命令卡片"
    assert "非你口头发言" in resolved.line


def test_fail_open_when_quoted_record_unresolvable() -> None:
    """When message history cannot find quoted record (no echo), fail-open to quoted_tome=True if not textual."""
    # When is_reply_to_bot is None (unresolvable)
    tg = build_turn_graph(
        "笑死我了，这什么鬼",
        persona_name="TestBot",
        is_tome=True,
        user_type="group",
        primary_speaker="user_102",
        at_bot=True,
        has_reply=True,
        is_reply_to_bot=None,
    )
    assert tg.quoted_tome is True


def test_private_chat_cold_start_replay() -> None:
    """In private chat, is_cold_session=True returns recent history; is_cold_session=False returns empty."""
    from gsuid_core.ai_core.turn_pipeline import build_group_history_block

    hm = get_history_manager()
    ev_dm = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="user_101",
        sender={"nickname": "Alice"},
        user_type="direct",
        msg_id="msg_dm_01",
        text="你好呀",
        raw_text="你好呀",
    )
    # Add past message in private chat
    ev_dm_prev = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="user_101",
        sender={"nickname": "Alice"},
        user_type="direct",
        msg_id="msg_dm_00",
        text="昨天的方案确认了",
        raw_text="昨天的方案确认了",
    )
    hm.add_message(ev_dm_prev, role="user", content="昨天的方案确认了")
    # Current incoming turn
    hm.add_message(ev_dm, role="user", content="你好呀")

    # In cold session (e.g. core restart / new session)
    cold_block = build_group_history_block(ev_dm, is_cold_session=True)
    assert "[近期私聊记录] 旧→新" in cold_block
    assert "昨天的方案确认了" in cold_block

    # In active ongoing session
    warm_block = build_group_history_block(ev_dm, is_cold_session=False)
    assert warm_block == ""


@pytest.mark.asyncio
async def test_outbound_msg_id_backfill_from_recall_echo() -> None:
    """Outbound assistant message records get patched with platform msg_id when recall echoes."""
    from unittest.mock import AsyncMock

    from gsuid_core.bot import _Bot
    from gsuid_core.models import Message, MessageReceive

    bot = _Bot("test_ws_bot")
    bot.bot = AsyncMock()
    bot.start_send_worker()

    # Send outbound message
    await bot.target_send(
        "这是AI助手的回复文本",
        "group",
        "group_202",
        "bot_main",
        "bot_main",
        "user_msg_01",
        at_sender=False,
        sender_id="user_101",
        group_id="group_202",
        extra_metadata={"speech_channel": "persona", "reply_to_user_id": "user_101", "reply_to_user_name": "Alice"},
    )

    assert len(bot._echo_records) >= 1
    echo_key = list(bot._echo_records.keys())[0]

    # Adapter echoes back recall_message_id with platform id
    echo_msg = MessageReceive(content=[Message("recall_message_id", {"echo": echo_key, "id": "platform_mid_888"})])
    assert bot.resolve_recall(echo_msg) is True

    # Quoting this platform message in the next turn
    ev_quote = Event(
        bot_id="bot_main",
        bot_self_id="bot_main",
        user_id="user_101",
        group_id="group_202",
        WS_BOT_ID="test_ws_bot",
        reply_id="platform_mid_888",
        reply="这是AI助手的回复文本",
        text="好的明白",
        raw_text="好的明白",
    )
    is_rep_bot, is_bystander, is_stale = inspect_quote_relation(ev_quote)
    assert is_rep_bot is True
    assert is_bystander is False
    assert is_stale is False

    quote_res = await resolve_quote(ev_quote)
    assert quote_res is not None
    assert quote_res.is_bot_reply is True
    assert "发出的发言" in quote_res.line

    if bot._send_task is not None:
        bot._send_task.cancel()
        try:
            await bot._send_task
        except (asyncio.CancelledError, Exception):
            pass
    bot.clear_send_queue()


def test_read_image_ids_bounded_capacity_fifo() -> None:
    """read_image_ids in ToolContext extra never exceeds 200 entries and evicts FIFO."""
    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage
    from pydantic_ai.models.test import TestModel

    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools.image_reader import _mark_image_id_read

    read_set: set[str] = set()
    ctx_deps = ToolContext(extra={"read_image_ids": read_set})
    ctx = RunContext(deps=ctx_deps, model=TestModel(), usage=RunUsage())

    for i in range(250):
        _mark_image_id_read(ctx, f"img_test_{i:04d}")

    assert len(read_set) == 200
    assert "img_test_0000" not in read_set
    assert "img_test_0049" not in read_set
    assert "img_test_0050" in read_set
    assert "img_test_0249" in read_set


def test_prev_turn_used_tools_multi_iteration_speaker_attribution() -> None:
    """prev_turn_used_tools correctly scans past intermediate ToolReturn ModelRequests."""
    from pydantic_ai.messages import (
        TextPart,
        ModelRequest,
        ToolCallPart,
        ModelResponse,
        ToolReturnPart,
        UserPromptPart,
    )

    from gsuid_core.ai_core.turn_pipeline import prev_turn_used_tools

    history = [
        # Turn 0: User user_101 asks a question
        ModelRequest(parts=[UserPromptPart(content="Alice(用户ID:user_101)：请帮我查天气")]),
        # Assistant iteration 0 makes tool call
        ModelResponse(parts=[ToolCallPart(tool_name="weather_tool", args={"city": "Beijing"}, tool_call_id="call_01")]),
        # Tool return
        ModelRequest(parts=[ToolReturnPart(tool_name="weather_tool", content="Sunny", tool_call_id="call_01")]),
        # Assistant iteration 1 makes second tool call
        ModelResponse(parts=[ToolCallPart(tool_name="format_tool", args={}, tool_call_id="call_02")]),
        # Tool return
        ModelRequest(parts=[ToolReturnPart(tool_name="format_tool", content="Formatted", tool_call_id="call_02")]),
        # Final assistant text response
        ModelResponse(parts=[TextPart(content="北京今天晴朗。")]),
    ]

    # For user_101, should return True
    assert prev_turn_used_tools(history, current_user_id="user_101") is True
    # For a different user (user_999), should return False
    assert prev_turn_used_tools(history, current_user_id="user_999") is False


def test_strip_remote_images_from_history_preserves_local_and_data_uris() -> None:
    """_strip_remote_images_from_history strips http/https but preserves data-URI and BinaryContent."""
    from pydantic_ai.messages import ImageUrl, ModelRequest, BinaryContent, UserPromptPart

    from gsuid_core.ai_core.utils import _strip_remote_images_from_history

    req = ModelRequest(
        parts=[
            UserPromptPart(
                content=[
                    ImageUrl(url="https://example.com/expired_photo.jpg"),
                    ImageUrl(
                        url="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
                    ),
                    BinaryContent(data=b"\x89PNG\r\n\x1a\n", media_type="image/png"),
                ]
            )
        ]
    )

    removed = _strip_remote_images_from_history([req])
    assert removed == 1
    p0 = req.parts[0]
    assert isinstance(p0, UserPromptPart)
    parts = p0.content
    assert isinstance(parts, list)
    assert parts[0] == "[图片已过期，无法再显示]"
    assert isinstance(parts[1], ImageUrl)
    assert isinstance(parts[2], BinaryContent)


def test_find_by_msg_id_multi_frame_split_and_negative_cache() -> None:
    """测试多分帧回执在 msg_id_list 中的多 ID 解析以及未命中负缓存。"""
    from gsuid_core.models import Event
    from gsuid_core.message_history import get_history_manager

    mgr = get_history_manager()
    ev = Event(
        user_type="group",
        group_id="group_test_split",
        user_id="user_test",
        bot_id="test_bot",
        bot_self_id="bot_1",
    )
    rec = mgr.add_message(
        ev,
        role="assistant",
        content="第一段文本；第二段图片",
        metadata={"msg_id": "frame_id_2", "msg_id_list": ["frame_id_1", "frame_id_2"]},
    )

    hit2 = mgr.find_by_msg_id(ev, "frame_id_2")
    assert hit2 is not None
    assert hit2.content == rec.content

    hit1 = mgr.find_by_msg_id(ev, "frame_id_1")
    assert hit1 is not None
    assert hit1.content == rec.content

    miss = mgr.find_by_msg_id(ev, "non_existent_id")
    assert miss is None
    storage_ev = mgr._get_storage_event(ev)
    assert (storage_ev, "non_existent_id") in mgr._msg_id_cache
    assert mgr._msg_id_cache[(storage_ev, "non_existent_id")] is None
