from __future__ import annotations

from typing import List, TypedDict


class MysSign(TypedDict):
    code: str
    risk_code: int
    gt: str
    challenge: str
    success: int
    message: str


class SignInfo(TypedDict):
    total_sign_day: int
    today: str
    is_sign: bool
    first_bind: bool
    is_sub: bool
    month_first: bool
    sign_cnt_missed: int
    month_last_day: bool


class SignAward(TypedDict):
    icon: str
    name: str
    cnt: int


class SignList(TypedDict):
    month: int
    awards: List[SignAward]
    resign: bool
