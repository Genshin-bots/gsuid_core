from __future__ import annotations

from typing import List, TypedDict


class DayData(TypedDict):
    current_primogems: int
    current_mora: int
    last_primogems: int
    last_mora: int


class GroupBy(TypedDict):
    action_id: int
    action: str
    num: int
    percent: int


class MonthData(TypedDict):
    current_primogems: int
    current_mora: int
    last_primogems: int
    last_mora: int
    current_primogems_level: int
    primogems_rate: int
    mora_rate: int
    group_by: List[GroupBy]


class MonthlyAward(TypedDict):
    uid: int
    region: str
    account_id: str
    nickname: str
    date: str
    month: str
    optional_month: List[int]
    data_month: int
    data_last_month: int
    day_data: DayData
    month_data: MonthData
    lantern: bool
