"""count_data 统计口径：message / regex 触发器不计入命令明细。

on_message 默认以 uuid4 作 unique_id，on_regex 以正则模式作 keyword，
两者都对每条消息匹配，若计入 per-command 明细会淹没真实命令统计。
"""

from __future__ import annotations

import asyncio

import pytest

from gsuid_core.trigger import Trigger
from gsuid_core.models import Event
from gsuid_core.handler import count_data
from gsuid_core.global_val import get_platform_val


def _noop(bot, ev):  # pragma: no cover - 仅作 Trigger 占位
    raise AssertionError("不应被调用")


def _ev() -> Event:
    return Event(
        bot_id="onebot",
        bot_self_id="self_count",
        user_id="u_count",
        group_id="g_count",
        real_bot_id="onebot",
        raw_text="随便一句话",
    )


@pytest.fixture()
def local_val():
    ev = _ev()
    val = get_platform_val(ev.real_bot_id, ev.bot_self_id)
    before = {
        "command": val["command"],
        "group": dict(val["group"]),
        "user": dict(val["user"]),
    }
    val["group"].pop(ev.group_id, None)
    val["user"].pop(ev.user_id, None)
    yield val
    val["command"] = before["command"]
    val["group"].clear()
    val["group"].update(before["group"])
    val["user"].clear()
    val["user"].update(before["user"])


@pytest.mark.parametrize(
    ("ttype", "keyword"),
    [
        ("message", "4cc218f6-2093-4bc0-ab79-31644fa466ed"),
        ("regex", r"^(?P<lead_space>\s+)?(?P<waves_id>\d{9})?$"),
    ],
)
def test_passive_trigger_not_counted_as_command(local_val, ttype, keyword):
    ev = _ev()
    base = local_val["command"]
    trigger = Trigger(ttype, keyword, _noop)

    asyncio.run(count_data(ev, trigger))

    # 总量与活跃度照常统计
    assert local_val["command"] == base + 1
    assert ev.group_id in local_val["group"]
    assert ev.user_id in local_val["user"]
    # 但不写入命令明细
    assert local_val["group"][ev.group_id] == {}
    assert local_val["user"][ev.user_id] == {}


@pytest.mark.parametrize("ttype", ["command", "fullmatch", "prefix", "keyword"])
def test_real_command_still_counted(local_val, ttype):
    ev = _ev()
    trigger = Trigger(ttype, "今日老婆", _noop)

    asyncio.run(count_data(ev, trigger))
    asyncio.run(count_data(ev, trigger))

    assert local_val["group"][ev.group_id]["今日老婆"] == 2
    assert local_val["user"][ev.user_id]["今日老婆"] == 2
