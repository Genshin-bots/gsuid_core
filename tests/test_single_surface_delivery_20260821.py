"""单一表面 / 等待句不置 delivered / compact 不在 iter 中途换 history。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gsuid_core.ai_core.persona.prompts import (
    SYSTEM_CONSTRAINTS,
    CHARACTER_BUILDING_TEMPLATE,
    TOOL_ORCHESTRATION_CONSTRAINTS,
    sayu_persona_prompt,
)
from gsuid_core.ai_core.agent_run.speech_policy import (
    looks_like_wait_comfort,
    has_orchestration_narration,
    should_mark_speech_delivered,
    should_block_user_visible_text,
    looks_like_inflight_quota_speech,
)

_ROOT = Path(__file__).resolve().parent.parent
_AI_CORE = _ROOT / "gsuid_core" / "ai_core"


def test_default_persona_does_not_expose_second_actor() -> None:
    assert "调用子Agent" not in sayu_persona_prompt
    assert "第二个自己" in sayu_persona_prompt or "内部步骤" in sayu_persona_prompt
    assert "调用子Agent" not in CHARACTER_BUILDING_TEMPLATE
    assert "第二个执行者" in CHARACTER_BUILDING_TEMPLATE
    assert "create_subagent" in SYSTEM_CONSTRAINTS
    assert "第二个自己" in SYSTEM_CONSTRAINTS
    assert "一句等待 →" not in TOOL_ORCHESTRATION_CONSTRAINTS


def test_startup_syncs_existing_default_persona() -> None:
    src = (_AI_CORE / "persona" / "startup.py").read_text(encoding="utf-8")
    assert 'Persona("早柚")' in src
    assert "persona.exists()" in src
    assert "save_content(sayu_persona_prompt)" in src
    assert "current != seeded" in src


def test_second_actor_dispatch_is_orchestration() -> None:
    assert has_orchestration_narration("让帮手去查一下")
    assert not looks_like_wait_comfort("让帮手去查一下")
    assert not looks_like_inflight_quota_speech("让帮手去查一下")
    blk, why = should_block_user_visible_text(
        "silence_only",
        "让帮手去查一下",
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
    )
    assert blk and why == "silence_only_or_async"


def test_wait_comfort_does_not_mark_delivered() -> None:
    assert not should_mark_speech_delivered(text="马上好。", has_media=False)
    assert not should_mark_speech_delivered(text="这就去办", has_media=False)
    assert should_mark_speech_delivered(text="查到了，出门带伞。", has_media=False)
    assert should_mark_speech_delivered(text="出门带伞", has_media=True)
    assert not should_mark_speech_delivered(text="", has_media=True)


def test_send_message_wait_does_not_set_delivered() -> None:
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = MagicMock()
    ev.session_id = "s_wait"
    ev.user_id = "u1"
    ev.group_id = "g1"
    ev.raw_text = ""
    extra: dict[str, object] = {"turn_id": "t_wait", "speech_policy": "free", "has_status_tool": False}
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=bot, ev=ev, extra=extra, parent_session_id=None)
    ms.clear_turn_send_throttle("s_wait", "t_wait")
    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()),
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
    ):
        result = asyncio.run(ms.send_message_by_ai(ctx, text="马上好。"))
    assert "消息已发送" in result
    assert "delivered_with_speech" not in extra


def test_send_message_status_ok_refuses_without_status_tool() -> None:
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = MagicMock()
    ev.session_id = "s_st"
    ev.user_id = "u1"
    ev.group_id = "g1"
    ev.raw_text = "图呢"
    extra: dict[str, object] = {"turn_id": "t_st", "speech_policy": "status_ok", "has_status_tool": False}
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=bot, ev=ev, extra=extra, parent_session_id=None)
    ms.clear_turn_send_throttle("s_st", "t_st")
    result = asyncio.run(ms.send_message_by_ai(ctx, text="做完了…zzz"))
    assert "追问进度" in result
    assert bot.send.await_count == 0


def test_iter_does_not_compact_history_mid_run() -> None:
    gs = (_AI_CORE / "gs_agent.py").read_text(encoding="utf-8")
    loop = (_AI_CORE / "agent_run" / "loop.py").read_text(encoding="utf-8")
    assert "self._history_iter_active: bool = False" in gs
    assert "if self._history_iter_active:" in gs
    assert "self._history_iter_active = True" in loop
    assert "self._history_iter_active = False" in loop
    stream_idx = loop.index("async with node.stream")
    assert loop.find("st.cancel_ev is not None and st.cancel_ev.is_set()", stream_idx) > stream_idx
