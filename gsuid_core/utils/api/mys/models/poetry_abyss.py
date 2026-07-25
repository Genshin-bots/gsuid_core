from __future__ import annotations

from typing import Dict, List, TypedDict

from ._typing import NotRequired


class PoetryAbyssLinks(TypedDict):
    lineup_link: str
    lineup_link_pc: str
    strategy_link: str
    lineup_publish_link: str
    lineup_publish_link_pc: str


class PoetryAbyssAvatar(TypedDict):
    avatar_id: int
    avatar_type: int
    name: str
    element: str
    image: str
    level: int
    rarity: int


class PoetryAbyssChoiceCard(TypedDict):
    icon: str
    name: str
    desc: str
    is_enhanced: bool
    id: int


class PoetryAbyssBuff(TypedDict):
    icon: str
    name: str
    desc: str
    is_enhanced: bool
    id: int


class PoetryAbyssDateTime(TypedDict):
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int


class PoetryAbyssSchedule(TypedDict):
    start_time: int
    end_time: int
    schedule_type: int
    schedule_id: int
    start_date_time: PoetryAbyssDateTime
    end_date_time: PoetryAbyssDateTime


class PoetryAbyssDetailStat(TypedDict):
    difficulty_id: int
    max_round_id: int
    heraldry: int
    get_medal_round_list: List[int]
    medal_num: int
    coin_num: int
    avatar_bonus_num: int
    rent_cnt: int
    tarot_finished_cnt: NotRequired[int]


class PoetryEnemy(TypedDict):
    name: str
    icon: str
    level: int


class StatisticAvatar(TypedDict):
    avatar_id: int
    avatar_icon: str
    value: int | str
    rarity: int


class FightStatistic(TypedDict):
    max_defeat_avatar: StatisticAvatar
    max_damage_avatar: StatisticAvatar
    max_take_damage_avatar: StatisticAvatar
    total_coin_consumed: StatisticAvatar
    shortest_avatar_list: List[StatisticAvatar]
    total_use_time: int
    is_show_battle_stats: bool


class LevelEffectLink(TypedDict):
    id: int
    name: str
    desc: str


class LevelEffect(TypedDict):
    icon: str
    name: str
    desc: str
    links: Dict[str, LevelEffectLink]


class SplendourBuffItem(TypedDict):
    name: str
    icon: str
    level: int
    level_effect: List[LevelEffect]


class SplendourBuffSummary(TypedDict):
    total_level: int
    desc: str


class SplendourBuff(TypedDict):
    summary: SplendourBuffSummary
    buffs: List[SplendourBuffItem]


class RoundData(TypedDict):
    avatars: List[PoetryAbyssAvatar]
    choice_cards: List[PoetryAbyssChoiceCard]
    buffs: List[PoetryAbyssBuff]
    is_get_medal: bool
    round_id: int
    finish_time: int
    finish_date_time: PoetryAbyssDateTime
    detail_stat: NotRequired[PoetryAbyssDetailStat]
    enemies: NotRequired[List[PoetryEnemy]]
    splendour_buff: NotRequired[SplendourBuff]
    is_tarot: NotRequired[bool]
    tarot_serial_no: NotRequired[int]


class PoetryAbyssDetail(TypedDict):
    rounds_data: List[RoundData]
    detail_stat: PoetryAbyssDetailStat
    backup_avatars: List[PoetryAbyssAvatar]
    fight_statisic: NotRequired[FightStatistic]


class PoetryAbyssData(TypedDict):
    detail: PoetryAbyssDetail
    stat: PoetryAbyssDetailStat
    schedule: PoetryAbyssSchedule
    has_data: bool
    has_detail_data: bool


class PoetryAbyssDatas(TypedDict):
    data: List[PoetryAbyssData]
    is_unlock: bool
    links: PoetryAbyssLinks
