"""交付回灌帧委派限制行为测试。"""

import pytest

from gsuid_core.models import Event
from gsuid_core.ai_core.buildin_tools.subagent import _delivery_wake_delegate_block


def test_delivery_wake_delegate_block_normal_turn() -> None:
    """非回灌轮次不作任何限制。"""
    extra: dict[str, object] = {"delivery_wake": False}
    ev = Event(bot_id="b", user_id="u", user_type="group", group_id="g")
    assert _delivery_wake_delegate_block(extra, ev) is None


def test_delivery_wake_delegate_block_unprocessed_image() -> None:
    """回灌帧带图片产物且未发送时，提示先发图。"""
    extra: dict[str, object] = {
        "delivery_wake": True,
        "delivery_has_image": True,
        "image_sent_this_run": False,
    }
    ev = Event(bot_id="b", user_id="u", user_type="group", group_id="g")
    res = _delivery_wake_delegate_block(extra, ev)
    assert res is not None
    assert "有待交付的图片产物" in res


def test_delivery_wake_delegate_block_image_already_sent() -> None:
    """回灌帧带图片但本轮已发出时，解除限制允许继续委派。"""
    extra: dict[str, object] = {
        "delivery_wake": True,
        "delivery_has_image": True,
        "image_sent_this_run": True,
    }
    ev = Event(bot_id="b", user_id="u", user_type="group", group_id="g")
    assert _delivery_wake_delegate_block(extra, ev) is None


def test_delivery_wake_delegate_block_no_image_allows_delegate() -> None:
    """回灌帧无图片产物时，允许正常委派后续代理推进多阶段任务。"""
    extra: dict[str, object] = {
        "delivery_wake": True,
        "delivery_has_image": False,
    }
    ev = Event(bot_id="b", user_id="u", user_type="group", group_id="g")
    assert _delivery_wake_delegate_block(extra, ev) is None


def test_delivery_wake_delegate_block_hits_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """回灌帧达到嵌套委派上限时拦截。"""
    extra: dict[str, object] = {"delivery_wake": True}
    ev = Event(bot_id="b", user_id="u", user_type="group", group_id="g")
    monkeypatch.setattr(
        "gsuid_core.ai_core.planning.kanban_executor.delivery_wake_blocks_new_delegate",
        lambda sid: True,
    )
    res = _delivery_wake_delegate_block(extra, ev)
    assert res is not None
    assert "已达到连续嵌套委派上限" in res
