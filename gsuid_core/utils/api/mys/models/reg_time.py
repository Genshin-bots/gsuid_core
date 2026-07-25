from __future__ import annotations

from typing import List, TypedDict


class CardOpts(TypedDict):
    adjs: List[int]
    titles: List[int]
    items: List[int]
    data_version: str


Props = TypedDict(
    "Props",
    {
        "66a": str,
        "50a": str,
        "53b": str,
        "pre_69b": str,
        "49a": str,
        "52b": str,
        "pre_71b": str,
        "37": str,
        "48a": str,
        "57": str,
    },
)


class RegTime(TypedDict):
    data: str
    card_opts: CardOpts
    props: Props
    data_version: int
    prop_version: int
