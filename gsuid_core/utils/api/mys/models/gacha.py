from __future__ import annotations

from typing import List, TypedDict


class SingleGachaLog(TypedDict):
    uid: str
    gacha_type: str
    item_id: str
    count: str
    time: str
    name: str
    lang: str
    item_type: str
    rank_type: str
    id: str


class GachaLog(TypedDict):
    page: str
    size: str
    total: str
    list: List[SingleGachaLog]
    region: str
