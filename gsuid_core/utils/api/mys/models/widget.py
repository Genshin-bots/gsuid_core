from __future__ import annotations

from typing import List, TypedDict

from .daily_note import DayilyTask, Expedition, Transformer, ArchonProgress


class WidgetResin(TypedDict):
    current_resin: int
    max_resin: int
    resin_recovery_time: str
    finished_task_num: int
    total_task_num: int
    is_extra_task_reward_received: bool
    current_expedition_num: int
    max_expedition_num: int
    expeditions: List[Expedition]
    current_home_coin: int
    max_home_coin: int
    has_signed: bool
    sign_url: str
    home_url: str
    note_url: str


class FakeResin(WidgetResin):
    remain_resin_discount_num: int
    resin_discount_num_limit: int
    transformer: Transformer
    daily_task: DayilyTask
    archon_quest_progress: ArchonProgress
