"""群聊排队超过 STALE_CHAT_REQUEST_TTL 必须仍处理；私聊超时才丢。

回归：同群 A 占锁调研时 B 已等 22s，旧 TTL 把 B 静默丢掉。
"""

from __future__ import annotations

import time

import pytest

from gsuid_core.models import Event
from gsuid_core.ai_core.const import STALE_CHAT_REQUEST_TTL
from gsuid_core.ai_core.turn_pipeline import stale_request


def _aged(seconds: float) -> float:
    return time.time() - seconds


@pytest.mark.parametrize(
    ("event", "aged", "injection", "expect_drop"),
    [
        (Event(user_type="group", group_id="g1", user_id="u1"), STALE_CHAT_REQUEST_TTL + 20, False, False),
        (Event(user_type="channel", group_id="c1", user_id="u1"), 30.0, False, False),
        (Event(user_type="sub_channel", group_id="s1", user_id="u1"), 30.0, False, False),
        (Event(user_type="direct", user_id="u1"), STALE_CHAT_REQUEST_TTL + 5, False, True),
        (Event(user_type="direct", user_id="u1"), 1.0, False, False),
        (Event(user_type="direct", user_id="u1"), 60.0, True, False),
    ],
)
def test_stale_request_ttl_policy(
    event: Event,
    aged: float,
    injection: bool,
    expect_drop: bool,
) -> None:
    dropped = stale_request(
        _aged(aged),
        STALE_CHAT_REQUEST_TTL,
        event=event,
        is_framework_injection=injection,
    )
    assert dropped is expect_drop


def test_stale_request_keeps_missing_enqueue_ts() -> None:
    ev = Event(user_type="direct", user_id="u1")
    assert not stale_request(None, STALE_CHAT_REQUEST_TTL, event=ev)
