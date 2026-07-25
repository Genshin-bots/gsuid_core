from __future__ import annotations

from typing import List, TypedDict


class ComputeItem(TypedDict):
    id: int
    name: str
    icon: str
    num: int
    wiki_url: str
    level: int
    icon_url: str
    lack_num: int


class ConsumeItem(TypedDict):
    id: int
    name: str
    icon: str
    num: int
    wiki_url: str
    level: int
    icon_url: str
    lack_num: int


class ComputeAvatar(TypedDict):
    id: int
    icon: str
    avatar_level: int


class MaterialConsumeCategory(TypedDict):
    consume: List[ConsumeItem]
    avatars: List[ComputeAvatar]
    weapons: List[ConsumeItem]  # 假设 weapons 也是消耗材料的一部分，根据实际情况可能需要调整


class OverallMaterialConsume(TypedDict):
    avatar_consume: List[MaterialConsumeCategory]
    avatar_skill_consume: List[MaterialConsumeCategory]
    weapon_consume: List[MaterialConsumeCategory]


class SkillConsumeItem(TypedDict):
    id: int
    name: str
    icon: str
    num: int
    wiki_url: str
    level: int
    icon_url: str
    lack_num: int


class SkillInfo(TypedDict):
    id: str
    level_current: str
    level_target: str


class SkillsConsume(TypedDict):
    consume_list: List[SkillConsumeItem]
    skill_info: SkillInfo


class ComputeAvatarConsume(TypedDict):
    avatar_consume: List[ComputeItem]
    avatar_skill_consume: List[ComputeItem]
    weapon_consume: List[ComputeItem]
    reliquary_consume: List[ComputeItem]
    skills_consume: List[SkillsConsume]


class ComputeCalendar(TypedDict):
    dungeon_name: str
    drop_day: List[int]
    calendar_link: str
    has_data: bool


class OverallConsumeItem(TypedDict):
    id: int
    name: str
    icon: str
    num: int
    wiki_url: str
    level: int
    icon_url: str
    lack_num: int


class AvailableMaterial(TypedDict):
    id: int
    name: str
    icon: str
    num: int
    wiki_url: str
    level: int
    icon_url: str
    lack_num: int


class ComputeData(TypedDict):
    items: List[ComputeAvatarConsume]
    available_material: List[AvailableMaterial]
    overall_consume: List[OverallConsumeItem]
    overall_material_consume: OverallMaterialConsume
    has_user_info: bool
