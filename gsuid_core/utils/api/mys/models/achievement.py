from __future__ import annotations

from typing import TypedDict


class AchievementData(TypedDict):
    name: str
    id: str
    percentage: int
    finish_num: int
    show_percent: bool
    icon: str
