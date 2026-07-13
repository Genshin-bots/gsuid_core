"""Kanban 周期触发：cron 星期字段的编号翻译单测。

背景：标准 crontab 是 0/7=周日、1=周一 … 6=周六，APScheduler 的 CronTrigger 却是
0=周一 … 6=周日。主人格 / 插件都按常识写 `1-5` 表示周一至周五，不翻译就会跑成
周二至周六——周一永远不触发（2026-07-13 papertrade 全天停摆即此因）。

本测试锁住两件事：
- _normalize_cron_dow 的编号映射（含范围 / 步长 / 跨周 / 英文名透传）；
- parse_trigger_spec 出来的 kwargs 直接喂 CronTrigger 后，实际落点是周一至周五。
"""

from datetime import datetime

import pytest
from apscheduler.triggers.cron import CronTrigger

from gsuid_core.ai_core.planning.recurring import (
    parse_trigger_spec,
    _normalize_cron_dow,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1-5", "0,1,2,3,4"),  # 周一~周五
        ("1", "0"),  # 周一
        ("5", "4"),  # 周五
        ("6", "5"),  # 周六
        ("0", "6"),  # 周日
        ("7", "6"),  # 周日的另一种写法
        ("0,6", "5,6"),  # 周末
        ("1,3,5", "0,2,4"),  # 一三五
        ("1-5/2", "0,2,4"),  # 带步长的范围
        ("5-1", "0,4,5,6"),  # 跨周末的范围：周五~周一
        ("*", "*"),
        ("0-6", "*"),  # 全周 → 收敛回 *
        ("mon-fri", "mon-fri"),  # 英文名两套编号一致，透传
    ],
)
def test_normalize_cron_dow(raw: str, expected: str):
    assert _normalize_cron_dow(raw) == expected


@pytest.mark.parametrize("raw", ["8", "abc-", "1-5/0", "1-5/x"])
def test_normalize_cron_dow_rejects_bad_input(raw: str):
    with pytest.raises(ValueError):
        _normalize_cron_dow(raw)


def test_papertrade_triggers_fire_on_monday():
    """papertrade 的三个周期模板在周一必须开火（回归 2026-07-13 停摆）。"""
    monday_dawn = datetime(2026, 7, 13, 8, 0, 0)
    expected = {
        "cron:0,30 9-11,13-15 * * 1-5": (9, 0),  # 决策心跳
        "cron:5 15 * * 1-5": (15, 5),  # 收盘快照
        "cron:15 10,14 * * 1-5": (10, 15),  # 候选池轮换
    }
    for spec, (hour, minute) in expected.items():
        trigger_type, kwargs = parse_trigger_spec(spec)
        assert trigger_type == "cron"
        fire_at = CronTrigger(**kwargs).get_next_fire_time(None, monday_dawn)
        assert fire_at is not None
        assert (fire_at.month, fire_at.day) == (7, 13), spec
        assert (fire_at.hour, fire_at.minute) == (hour, minute), spec


def test_weekday_trigger_skips_saturday():
    """`1-5` 不得把周六算进来（旧行为会在周六唤醒一次）。"""
    _, kwargs = parse_trigger_spec("cron:0 9 * * 1-5")
    friday_evening = datetime(2026, 7, 10, 20, 0, 0)
    fire_at = CronTrigger(**kwargs).get_next_fire_time(None, friday_evening)
    assert fire_at is not None
    assert fire_at.strftime("%A") == "Monday"
