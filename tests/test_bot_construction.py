"""_Bot / Bot / MockBot 构造契约：禁止把底层连接当成高层包装器。"""

from __future__ import annotations

from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.trigger_bridge import MockBot


def test_high_level_bot_wraps_low_level() -> None:
    low = _Bot("ws-alpha")
    ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g1",
        user_id="u1",
        WS_BOT_ID="ws-alpha",
    )
    high = Bot(low, ev)
    assert high.bot is low
    assert high.ev is ev
    assert not hasattr(low, "ev")
    assert high.bot_id == ev.bot_id
    assert hasattr(high, "send")


def test_mockbot_requires_high_level_bot() -> None:
    low = _Bot("ws-alpha")
    ev = Event(bot_id="onebot", bot_self_id="self1", user_type="direct", user_id="u1", WS_BOT_ID="ws-alpha")
    high = Bot(low, ev)
    mock = MockBot(high, {"image_ids": []})
    assert object.__getattribute__(mock, "_real_bot") is high
