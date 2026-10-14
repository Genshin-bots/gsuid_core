"""长任务开场句：派发成功后由 create_subagent 代发，一轮最多对用户开一次口。

背景：群聊一轮里模型可能多次回复（先查再答），用户却只该听到一句；
重任务例外，委派开工前先应一声，结论稍后回灌。
"""

import asyncio

import pytest

from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.agent_run.opening_line import (
    OpeningResult,
    arm_opening_line,
    disarm_opening_line,
    take_opening_result,
    release_opening_line,
)
from gsuid_core.ai_core.capability_agents.delegation_contracts import format_deferred_subagent_ack

_CLEAN = "好，等我一会儿。"


class _Outbox:
    """send_chat_result 的替身：只记下真正出站的正文。"""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def send(self, bot: Bot, text: str, **_kwargs: object) -> None:
        self.texts.append(text)


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> _Outbox:
    box = _Outbox()
    monkeypatch.setattr("gsuid_core.ai_core.utils.send_chat_result", box.send)
    return box


def _tool_ctx() -> ToolContext:
    ev = Event(user_id="u1", group_id="g1", raw_text="帮我做一份研报")
    sent: set[str] = set()
    return ToolContext(bot=Bot(_Bot("TEST"), ev), ev=ev, extra={"run_sent_texts": sent})


def test_parallel_delegations_speak_the_opening_once(outbox: _Outbox) -> None:
    ctx = _tool_ctx()
    arm_opening_line(ctx.extra, text=_CLEAN, deliver=True)

    async def _two_calls() -> list[OpeningResult | None]:
        return list(await asyncio.gather(release_opening_line(ctx), release_opening_line(ctx)))

    first, second = asyncio.run(_two_calls())
    assert outbox.texts == [_CLEAN]
    assert first == OpeningResult(text=_CLEAN, delivered=True)
    assert second is None
    assert _CLEAN in ctx.extra["run_sent_texts"]
    assert take_opening_result(ctx.extra) == OpeningResult(text=_CLEAN, delivered=True)
    ack = format_deferred_subagent_ack(ordinal=1, pid="research_agent", handle="dlg_x", spoken=_CLEAN)
    assert _CLEAN in ack and "<SILENCE>" in ack


def test_line_from_an_earlier_reply_is_not_released(outbox: _Outbox) -> None:
    ctx = _tool_ctx()
    arm_opening_line(ctx.extra, text=_CLEAN, deliver=True)
    disarm_opening_line(ctx.extra)
    assert asyncio.run(release_opening_line(ctx)) is None
    assert outbox.texts == []


def test_blocked_opening_is_dropped_and_receipt_lets_persona_speak(outbox: _Outbox) -> None:
    ctx = _tool_ctx()
    arm_opening_line(ctx.extra, text="已委派 research_agent 处理，task#12 句柄 dlg_x", deliver=True)
    result = asyncio.run(release_opening_line(ctx))
    assert outbox.texts == []
    assert result == OpeningResult(text="", delivered=False)
    ack = format_deferred_subagent_ack(ordinal=12, pid="research_agent", handle="dlg_x", opening_missed=True)
    assert "<SILENCE>" not in ack


def test_return_mode_records_opening_without_sending(outbox: _Outbox) -> None:
    ctx = _tool_ctx()
    arm_opening_line(ctx.extra, text=_CLEAN, deliver=False)
    result = asyncio.run(release_opening_line(ctx))
    assert outbox.texts == []
    assert result == OpeningResult(text=_CLEAN, delivered=False)
    assert _CLEAN in ctx.extra["run_sent_texts"]
