"""CaptureBot：Bot 类型；无 send_bytes；主人 DM 不入队；出站 observe；omitted。"""

from __future__ import annotations

import base64
import asyncio

from pydantic_ai.messages import (
    TextPart,
    ThinkingPart,
    ToolCallPart,
    TextPartDelta,
    PartDeltaEvent,
    PartStartEvent,
    ThinkingPartDelta,
)

from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import ThinkTagSplitter
from gsuid_core.ai_core.agent_run.loop import LoopPhase
from gsuid_core.ai_core.agent_run.state import RunOnceState
from gsuid_core.ai_core.agent_run.support import TraceKind
from gsuid_core.ai_core.http_agent.config import ATTACHMENT_FRAME_MAX
from gsuid_core.ai_core.http_agent.capture_bot import CaptureBot, CaptureItem


def _bot_pair() -> tuple[CaptureBot, asyncio.Queue[CaptureItem]]:
    q: asyncio.Queue[CaptureItem] = asyncio.Queue()
    ev = Event(bot_id="bot", bot_self_id="self", user_type="direct", user_id="u1", WS_BOT_ID="HTTP_AGENT")
    cap = CaptureBot(_Bot("HTTP_AGENT"), ev, q)
    return cap, q


def test_is_high_level_bot() -> None:
    cap, _q = _bot_pair()
    assert isinstance(cap, Bot)
    assert not hasattr(cap, "send_bytes")


def test_master_dm_via_target_send_not_queued() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        await cap.target_send("master-report", "direct", target_id="master-1")
        assert q.empty()
        await cap.send("visible")
        item = q.get_nowait()
        assert item.kind == "text"
        assert item.text == "visible"

    asyncio.run(_run())


def test_outbound_observe_called(monkeypatch) -> None:
    seen: list[str] = []

    async def _obs(**kwargs: object) -> None:
        content = kwargs["content"] if "content" in kwargs else ""
        seen.append(str(content))

    class _Mem:
        memory_mode = ["主动会话"]
        observer_blacklist: list[str] = []

    class _Flag:
        data = True

        def get_config(self, _k: str) -> _Flag:
            return self

    monkeypatch.setattr("gsuid_core.ai_core.memory.observer.observe", _obs)
    monkeypatch.setattr("gsuid_core.ai_core.memory.config.memory_config", _Mem())
    monkeypatch.setattr("gsuid_core.ai_core.configs.ai_config.ai_config", _Flag())
    cap, _q = _bot_pair()

    async def _run() -> None:
        await cap.send("hello-observe")

    asyncio.run(_run())
    assert seen == ["hello-observe"]


def test_receive_resp_fails_fast() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        try:
            await cap.receive_resp("choose")
        except RuntimeError as e:
            assert "interactive wait" in str(e)
        else:
            raise AssertionError("expected RuntimeError")
        item = q.get_nowait()
        assert item.kind == "text"
        assert item.text == "choose"

    asyncio.run(_run())


def test_queue_overflow_sets_flag() -> None:
    q: asyncio.Queue[CaptureItem] = asyncio.Queue(maxsize=1)
    ev = Event(bot_id="bot", bot_self_id="self", user_type="direct", user_id="u1", WS_BOT_ID="HTTP_AGENT")
    cap = CaptureBot(_Bot("HTTP_AGENT"), ev, q)

    async def _run() -> None:
        await cap.send("one")
        await cap.send("two")
        assert cap._overflow is True
        assert q.qsize() == 1

    asyncio.run(_run())


def test_text_delta_coalesces_and_skips_duplicate_send() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        cap.enqueue_text_delta("Hel")
        cap.enqueue_text_delta("lo, ")
        cap.enqueue_text_delta("world")
        await cap.flush_text_delta()
        frames = []
        while not q.empty():
            frames.append(q.get_nowait().text)
        assert "".join(frames) == "Hello, world"
        assert cap.consume_streamed_text("Hello, world") is True
        await cap.commit_streamed_history("Hello, world")
        assert q.empty()

    asyncio.run(_run())


def test_text_delta_consume_prefix_then_remainder() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        cap.enqueue_text_delta("aaa")
        cap.enqueue_text_delta("bbb")
        await cap.flush_text_delta()
        assert cap.consume_streamed_text("aaa") is True
        assert cap.consume_streamed_text("bbb") is True
        assert cap.consume_streamed_text("ccc") is False

    asyncio.run(_run())


def test_text_delta_consume_rejects_unrelated_suffix() -> None:
    """CallTools 段若只是已推流的后缀，不得当成已覆盖（避免跳过闸门）。"""
    cap, q = _bot_pair()

    async def _run() -> None:
        cap.enqueue_text_delta("用户打招呼了。保持简短。")
        cap.enqueue_text_delta("你好！这里什么都没有。")
        await cap.flush_text_delta()
        assert cap.consume_streamed_text("你好！这里什么都没有。") is False
        assert cap.take_unsent_suffix("你好！这里什么都没有。") is None
        assert cap._streamed_text == ""

    asyncio.run(_run())


def test_text_delta_skips_silence_fragments() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        cap.enqueue_text_delta("<SILEN")
        cap.enqueue_text_delta("CE>")
        cap.enqueue_text_delta("<SILENCE")
        cap.enqueue_text_delta(">")
        await cap.flush_text_delta()
        assert q.empty()
        assert cap.has_queued_text() is False
        assert cap.consume_streamed_text("<SILENCE>") is False

    asyncio.run(_run())


def test_text_delta_hold_then_visible() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        cap.enqueue_text_delta("你好")
        cap.enqueue_text_delta("<SIL")
        cap.enqueue_text_delta("ENCE>世界足够长了吧")
        await cap.flush_text_delta()
        frames = []
        while not q.empty():
            frames.append(q.get_nowait().text)
        assert "".join(frames) == "你好世界足够长了吧"
        assert cap.consume_streamed_text("你好世界足够长了吧") is True

    asyncio.run(_run())


def test_take_unsent_suffix_truncated_stream() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        cap.enqueue_text_delta("Hello, wo")
        await cap.flush_text_delta()
        assert cap.take_unsent_suffix("Hello, world") == "rld"
        assert cap.has_queued_text() is False

    asyncio.run(_run())


def test_flush_text_delta_cancel_does_not_mark_streamed() -> None:
    q: asyncio.Queue[CaptureItem] = asyncio.Queue(maxsize=1)
    ev = Event(bot_id="bot", bot_self_id="self", user_type="direct", user_id="u1", WS_BOT_ID="HTTP_AGENT")
    cap = CaptureBot(_Bot("HTTP_AGENT"), ev, q)

    async def _run() -> None:
        cap.enqueue_text_delta("hello world")
        await cap.flush_text_delta()
        assert q.qsize() == 1
        cap.enqueue_text_delta(" more text!!")
        task = asyncio.create_task(cap.flush_text_delta())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert cap.consume_streamed_text("hello world more text!!") is False
        assert cap.take_unsent_suffix("hello world more text!!") == " more text!!"

    asyncio.run(_run())


class _LoopStub(LoopPhase):
    def __init__(self) -> None:
        self._run_sent_texts: set[str] = set()
        self.traces: list[tuple[TraceKind, str]] = []

    def _emit_trace(self, kind: TraceKind, text: str) -> None:
        self.traces.append((kind, text))


def _run_state(
    bot: CaptureBot | None = None,
    *,
    suppress_intermediate_text: bool = True,
    outbound_stream: bool = True,
) -> RunOnceState:
    return RunOnceState(
        user_message="",
        bot=bot,
        ev=None,
        rag_context=None,
        tools=[],
        return_mode="by_bot",
        output_type=None,
        intent=None,
        has_active_task=False,
        budget_gate=False,
        suppress_intermediate_text=suppress_intermediate_text,
        fake_done_retry=False,
        turn_graph=None,
        cheap_gate=None,
        is_framework_injection=False,
        outbound_stream=outbound_stream,
        stats_chat_type="Http_Chat",
    )


def test_emit_stream_events_strips_think_and_enqueues_text() -> None:
    cap, q = _bot_pair()
    st = _run_state(bot=cap)
    loop = _LoopStub()
    splitter = ThinkTagSplitter("<think>", "</think>")
    starts: set[int] = set()

    async def _run() -> None:
        await loop._emit_stream_events(
            st,
            PartStartEvent(index=0, part=TextPart(content="<think>内心")),
            text_starts=starts,
            splitter=splitter,
        )
        await loop._emit_stream_events(
            st,
            PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="独白</think>你好世界啊")),
            text_starts=starts,
            splitter=splitter,
        )
        vis, th = splitter.flush()
        if th:
            loop._emit_trace("thinking_delta", th)
        if vis:
            await loop._enqueue_text_delta(st, vis, None)
        await loop._flush_bot_text_delta(st)

    asyncio.run(_run())
    frames = []
    while not q.empty():
        frames.append(q.get_nowait().text)
    assert "".join(frames) == "你好世界啊"
    assert any(kind == "thinking_delta" and "内心" in text for kind, text in loop.traces)
    assert cap.consume_streamed_text("你好世界啊") is True


def test_emit_stream_events_unclosed_think_not_visible() -> None:
    cap, q = _bot_pair()
    st = _run_state(bot=cap)
    loop = _LoopStub()
    splitter = ThinkTagSplitter("<think>", "</think>")
    starts: set[int] = set()

    async def _run() -> None:
        await loop._emit_stream_events(
            st,
            PartStartEvent(index=0, part=TextPart(content="<think>secret")),
            text_starts=starts,
            splitter=splitter,
        )
        vis, th = splitter.flush()
        if th:
            st.thinking_streamed = True
            loop._emit_trace("thinking_delta", th)
        if vis:
            await loop._enqueue_text_delta(st, vis, None)
        await loop._flush_bot_text_delta(st)

    asyncio.run(_run())
    assert q.empty()
    assert cap.has_queued_text() is False
    assert st.thinking_streamed is True


def test_emit_stream_events_stops_text_after_tool_call() -> None:
    cap, q = _bot_pair()
    st = _run_state(cap, suppress_intermediate_text=True)
    loop = _LoopStub()
    splitter = ThinkTagSplitter("<think>", "</think>")
    starts: set[int] = set()

    async def _run() -> None:
        await loop._emit_stream_events(
            st,
            PartStartEvent(
                index=1,
                part=ToolCallPart(tool_name="find_tools", args="{}", tool_call_id="c1"),
            ),
            text_starts=starts,
            splitter=splitter,
        )
        await loop._emit_stream_events(
            st,
            PartStartEvent(index=2, part=TextPart(content="我先查一下工具池。")),
            text_starts=starts,
            splitter=splitter,
        )
        await loop._flush_bot_text_delta(st)

    asyncio.run(_run())
    assert st.stream_saw_fn_tool is True
    assert q.empty()


def test_emit_native_thinking_delta() -> None:
    cap, _q = _bot_pair()
    st = _run_state(bot=cap)
    loop = _LoopStub()
    splitter = ThinkTagSplitter("<think>", "</think>")
    starts: set[int] = set()

    async def _run() -> None:
        await loop._emit_stream_events(
            st,
            PartStartEvent(index=0, part=ThinkingPart(content="先想")),
            text_starts=starts,
            splitter=splitter,
        )
        await loop._emit_stream_events(
            st,
            PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="一想")),
            text_starts=starts,
            splitter=splitter,
        )

    asyncio.run(_run())
    assert st.thinking_streamed is True
    assert loop.traces == [("thinking_delta", "先想"), ("thinking_delta", "一想")]


def test_send_gated_text_does_not_duplicate_sse() -> None:
    cap, q = _bot_pair()
    st = _run_state(bot=cap)
    loop = _LoopStub()

    async def _run() -> None:
        cap.enqueue_text_delta("Hello, world")
        await cap.flush_text_delta()
        await loop._send_gated_text(st, "Hello, world", at_user_id=None)
        frames = []
        while not q.empty():
            frames.append(q.get_nowait().text)
        assert "".join(frames) == "Hello, world"
        assert "Hello, world" in loop._run_sent_texts
        assert st.main_channel_sends == 1

    asyncio.run(_run())


def test_outbound_full_skips_delta_enqueue() -> None:
    """非流式出站不入 delta；pydantic-ai 仍可 stream 打点，这里只断言出站队列。"""
    cap, q = _bot_pair()
    st = _run_state(bot=cap, outbound_stream=False)
    loop = _LoopStub()
    splitter = ThinkTagSplitter("<think>", "</think>")
    starts: set[int] = set()

    async def _run() -> None:
        await loop._emit_stream_events(
            st,
            PartStartEvent(index=0, part=TextPart(content="Hello, world enough")),
            text_starts=starts,
            splitter=splitter,
        )
        await loop._flush_bot_text_delta(st)
        await loop._send_gated_text(st, "Hello, world enough", at_user_id=None)

    asyncio.run(_run())
    frames = []
    while not q.empty():
        frames.append(q.get_nowait().text)
    assert "".join(frames) == "Hello, world enough"
    assert st.main_channel_sends == 1


def test_bot_stream_hooks_default_to_full_send() -> None:
    ev = Event(bot_id="bot", bot_self_id="self", user_type="direct", user_id="u1", WS_BOT_ID="x")
    bot = Bot(_Bot("x"), ev)
    assert bot.take_unsent_suffix("hello") == "hello"
    bot.enqueue_text_delta("hello")
    bot.reset_text_stream()
    bot.discard_streamed_preview("hello")

    async def _run() -> None:
        await bot.flush_text_delta()
        await bot.commit_streamed_history("hello")

    asyncio.run(_run())


def test_empty_discard_does_not_duplicate_later_part() -> None:
    """空白/剥空 TextPart 不得清掉后续 part 的预览，否则闸门通过后会再 enqueue 一份。"""
    cap, q = _bot_pair()
    st = _run_state(bot=cap)
    loop = _LoopStub()

    async def _run() -> None:
        cap.enqueue_text_delta("\n")
        cap.enqueue_text_delta("Hello, world enough")
        await cap.flush_text_delta()
        loop._discard_stream_preview(st)
        await loop._send_gated_text(st, "Hello, world enough", at_user_id=None)
        frames = []
        while not q.empty():
            frames.append(q.get_nowait().text)
        assert "".join(frames) == "\nHello, world enough"
        assert st.main_channel_sends == 1

    asyncio.run(_run())


def test_discard_mismatch_keeps_later_preview() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        cap.enqueue_text_delta("visible answer enough")
        await cap.flush_text_delta()
        cap.discard_streamed_preview("<SILENCE>")
        assert cap.take_unsent_suffix("visible answer enough") is None
        assert q.qsize() >= 1

    asyncio.run(_run())


def test_send_gated_text_appends_truncated_suffix() -> None:
    cap, q = _bot_pair()
    st = _run_state(bot=cap)
    loop = _LoopStub()

    async def _run() -> None:
        cap.enqueue_text_delta("Hello, wo")
        await cap.flush_text_delta()
        await loop._send_gated_text(st, "Hello, world", at_user_id=None)
        frames = []
        while not q.empty():
            frames.append(q.get_nowait().text)
        assert "".join(frames) == "Hello, world"
        assert "Hello, world" in loop._run_sent_texts
        assert cap.has_queued_text() is False

    asyncio.run(_run())


def test_send_gated_text_roundtrip_trailing_bracket() -> None:
    """流式末尾字面 '[' 与 CallTools 全文对齐后，SSE 必须等于 history。"""
    cap, q = _bot_pair()
    st = _run_state(bot=cap)
    loop = _LoopStub()

    async def _run() -> None:
        cap.enqueue_text_delta("hello[")
        await cap.flush_text_delta()
        await loop._send_gated_text(st, "hello[", at_user_id=None)
        frames = []
        while not q.empty():
            frames.append(q.get_nowait().text)
        assert "".join(frames) == "hello["
        assert "hello[" in loop._run_sent_texts
        assert cap.has_queued_text() is False

    asyncio.run(_run())


def test_send_gated_text_leftover_bracket_not_reheld() -> None:
    """SSE 只推了 'hello'、闸门全文是 'hello[' 时，补 '[' 不得再被 hold 丢掉。"""
    cap, q = _bot_pair()
    st = _run_state(bot=cap)
    loop = _LoopStub()

    async def _run() -> None:
        cap.enqueue_text_delta("hello")
        await cap.flush_text_delta()
        await loop._send_gated_text(st, "hello[", at_user_id=None)
        frames = []
        while not q.empty():
            frames.append(q.get_nowait().text)
        assert "".join(frames) == "hello["
        assert "hello[" in loop._run_sent_texts

    asyncio.run(_run())


def test_send_gated_text_protocol_prefix_at_eos_not_leaked() -> None:
    """未闭合协议开标签 force-drop 后不得因 leftover 漏进 SSE。"""
    cap, q = _bot_pair()
    st = _run_state(bot=cap)
    loop = _LoopStub()

    async def _run() -> None:
        cap.enqueue_text_delta("hello<SILEN")
        await cap.flush_text_delta()
        await loop._send_gated_text(st, "hello<SILEN", at_user_id=None)
        frames = []
        while not q.empty():
            frames.append(q.get_nowait().text)
        assert "".join(frames) == "hello"
        assert "hello<SILEN" in loop._run_sent_texts

    asyncio.run(_run())


def test_text_delta_keeps_protocol_tag_in_code_span() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        cap.enqueue_text_delta("use `<SILENCE>` please enough")
        await cap.flush_text_delta()
        frames = []
        while not q.empty():
            frames.append(q.get_nowait().text)
        assert "".join(frames) == "use `<SILENCE>` please enough"
        assert cap.take_unsent_suffix("use `<SILENCE>` please enough") is None

    asyncio.run(_run())


def test_discard_preview_skips_history_commit() -> None:
    cap, q = _bot_pair()
    st = _run_state(bot=cap)
    loop = _LoopStub()

    async def _run() -> None:
        cap.enqueue_text_delta("规划内心独白足够长")
        await cap.flush_text_delta()
        loop._discard_stream_preview(st, "规划内心独白足够长")
        assert cap.has_queued_text() is False
        assert st.main_channel_sends == 0
        frames = []
        while not q.empty():
            frames.append(q.get_nowait().text)
        assert "".join(frames) == "规划内心独白足够长"

    asyncio.run(_run())


def test_large_attachment_omitted() -> None:
    cap, q = _bot_pair()
    raw = b"x" * (ATTACHMENT_FRAME_MAX + 10)
    b64 = base64.b64encode(raw).decode("ascii")

    async def _run() -> None:
        await cap.send("base64://" + b64)
        item = q.get_nowait()
        assert item.kind == "attachment"
        assert item.encoding == "omitted"
        assert item.data == ""
        assert item.nbytes > ATTACHMENT_FRAME_MAX

    asyncio.run(_run())
