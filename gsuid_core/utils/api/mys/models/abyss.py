from __future__ import annotations

from typing import List, TypedDict


class AbyssAvatar(TypedDict):
    avatar_id: int
    avatar_icon: str
    value: int
    rarity: int


class AbyssBattleAvatar(TypedDict):
    id: int
    icon: str
    level: int
    rarity: int


class AbyssBattle(TypedDict):
    index: int
    timestamp: str
    avatars: List[AbyssBattleAvatar]


class AbyssLevel(TypedDict):
    index: int
    star: int
    max_star: int
    battles: List[AbyssBattle]


class AbyssFloor(TypedDict):
    index: int
    icon: str
    is_unlock: bool
    settle_time: str
    star: int
    max_star: int
    levels: List[AbyssLevel]


class AbyssData(TypedDict):
    schedule_id: int
    start_time: str
    end_time: str
    total_battle_times: int
    total_win_times: int
    max_floor: str
    reveal_rank: List[AbyssAvatar]
    defeat_rank: List[AbyssAvatar]
    damage_rank: List[AbyssAvatar]
    take_damage_rank: List[AbyssAvatar]
    normal_skill_rank: List[AbyssAvatar]
    energy_skill_rank: List[AbyssAvatar]
    floors: List[AbyssFloor]
    total_star: int
    is_unlock: bool
