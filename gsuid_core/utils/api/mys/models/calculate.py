from __future__ import annotations

from typing import List, TypedDict


class CalculateInfo(TypedDict):
    skill_list: List[CalculateSkill]
    weapon: CalculateWeapon
    reliquary_list: List[CalculateReliquary]


class CalculateBaseData(TypedDict):
    id: int
    name: str
    icon: str
    max_level: int
    level_current: int


class CalculateWeapon(CalculateBaseData):
    weapon_cat_id: int
    weapon_level: int


class CalculateReliquary(CalculateBaseData):
    reliquary_cat_id: int
    reliquary_level: int


class CalculateSkill(CalculateBaseData):
    group_id: int
