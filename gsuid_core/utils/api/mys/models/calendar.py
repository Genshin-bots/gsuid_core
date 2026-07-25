from __future__ import annotations

from typing import List, Optional, TypedDict

from .poetry_abyss import PoetryAbyssDateTime
from .hard_challenge import HardChallengeDetail


class AvatarPool(TypedDict):
    id: int
    icon: str
    name: str
    element: str
    rarity: int
    is_invisible: bool


class WeaponPool(TypedDict):
    id: int
    icon: str
    rarity: int
    name: str
    wiki_url: str


class RewardItem(TypedDict):
    item_id: int
    name: str
    icon: str
    wiki_url: str
    num: int
    rarity: str
    homepage_show: bool


class Act(TypedDict):
    id: int
    name: str
    type: str
    start_timestamp: int
    start_time: PoetryAbyssDateTime
    end_timestamp: int
    end_time: PoetryAbyssDateTime
    desc: str
    strategy: str
    countdown_seconds: int
    status: int
    reward_list: List[RewardItem]
    is_finished: bool
    x: int
    y: int


class RoleCombatDetail(TypedDict):
    is_unlock: bool
    max_round_id: int
    has_data: bool


class TowerDetail(TypedDict):
    is_unlock: bool
    max_star: int
    total_star: int
    has_data: bool


class ExploreDetail(TypedDict):
    explore_percent: int
    is_finished: bool


class FixedAct(TypedDict):
    id: int
    name: str
    type: str
    start_timestamp: int
    start_time: PoetryAbyssDateTime
    end_timestamp: int
    end_time: PoetryAbyssDateTime
    desc: str
    strategy: str
    countdown_seconds: int
    status: int
    reward_list: List[RewardItem]
    is_finished: bool
    role_combat_detail: Optional[RoleCombatDetail]
    tower_detail: Optional[TowerDetail]
    hard_challenge_detail: Optional[HardChallengeDetail]
    explore_detail: Optional[ExploreDetail]
    x: int
    y: int


class Pool(TypedDict):
    pool_id: int
    version_name: str
    pool_name: str
    pool_type: int
    avatars: List[AvatarPool]
    weapon: List[WeaponPool]
    start_timestamp: int
    start_time: PoetryAbyssDateTime
    end_timestamp: int
    end_time: PoetryAbyssDateTime
    jump_url: str
    pool_status: int
    countdown_seconds: int


class CalendarData(TypedDict):
    avatar_card_pool_list: List[Pool]
    weapon_card_pool_list: List[Pool]
    mixed_card_pool_list: List[Pool]
    selected_avatar_card_pool_list: List[Pool]
    selected_mixed_card_pool_list: List[Pool]
    act_list: List[Act]
    fixed_act_list: List[FixedAct]
    selected_act_list: List[Act]
