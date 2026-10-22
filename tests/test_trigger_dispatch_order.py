"""dispatch_sort_key：SV 优先级仍高于类型排序。"""

from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.trigger import Trigger, dispatch_sort_key


async def _noop(bot: Bot, ev: Event) -> None:
    return None


def test_priority_still_precedes_type_rank() -> None:
    regex = Trigger("regex", r"^刷新面板$", _noop, prefix="yh")
    full = Trigger("fullmatch", "刷新面板", _noop, prefix="yh")
    ordered = sorted(
        [(full, 5), (regex, 1)],
        key=lambda item: dispatch_sort_key(item[0], item[1]),
    )
    assert ordered[0][0] is regex
