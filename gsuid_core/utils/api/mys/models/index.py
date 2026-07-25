from __future__ import annotations

from typing import Dict, List, TypedDict

from .character import MihoyoRole, MihoyoAvatar
from .hard_challenge import HardChallenge


class ExtMap(TypedDict):
    link: str
    backup_link: str


class IndexRoleCombat(TypedDict):
    is_unlock: bool
    max_round_id: int
    has_data: bool
    has_detail_data: bool
    tarot_finished_cnt: int
    difficulty_id: int


class Stats(TypedDict):
    active_day_number: int
    achievement_number: int
    anemoculus_number: int
    geoculus_number: int
    avatar_number: int
    way_point_number: int
    domain_number: int
    spiral_abyss: str
    precious_chest_number: int
    luxurious_chest_number: int
    exquisite_chest_number: int
    common_chest_number: int
    electroculus_number: int
    magic_chest_number: int
    dendroculus_number: int
    hydroculus_number: int
    pyroculus_number: int
    cryoculus_number: int
    moonoculus_number: int
    field_ext_map: Dict[str, ExtMap]
    role_combat: IndexRoleCombat
    full_fetter_avatar_num: int
    hard_challenge: HardChallenge


class Offering(TypedDict):
    name: str
    level: int
    icon: str


class WorldExploration(TypedDict):
    level: int
    exploration_percentage: int
    icon: str
    name: str
    type: str
    offerings: List[Offering]
    id: int
    parent_id: int
    map_url: str
    strategy_url: str
    background_image: str
    inner_icon: str
    area_exploration_list: List[Area]
    boss_list: List[BossKill]
    cover: str


class Area(TypedDict):
    name: str
    exploration_percentage: int


class BossKill(TypedDict):
    name: str
    kill_num: int


class Home(TypedDict):
    level: int
    visit_num: int
    comfort_num: int
    item_num: int
    name: str
    icon: str
    comfort_level_name: str
    comfort_level_icon: str


class IndexData(TypedDict):
    role: MihoyoRole
    avatars: List[MihoyoAvatar]
    stats: Stats
    city_explorations: List
    world_explorations: List[WorldExploration]
    homes: List[Home]
