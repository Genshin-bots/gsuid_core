from __future__ import annotations

from typing import List, Literal, TypedDict


class MihoyoRole(TypedDict):
    AvatarUrl: str
    nickname: str
    region: str
    level: int


class MihoyoWeapon(TypedDict):
    id: int
    name: str
    icon: str
    type: int
    rarity: int
    level: int
    promote_level: int
    type_name: Literal["单手剑", "双手剑", "长柄武器", "弓", "法器"]
    desc: str
    affix_level: int


class ReliquaryAffix(TypedDict):
    activation_number: int
    effect: str


class ReliquarySet(TypedDict):
    id: int
    name: str
    affixes: List[ReliquaryAffix]


class MihoyoReliquary(TypedDict):
    id: int
    name: str
    icon: str
    pos: int
    rarity: int
    level: int
    set: ReliquarySet
    pos_name: str


class MihoyoConstellation(TypedDict):
    id: int
    name: str
    icon: str
    effect: str
    is_actived: bool
    pos: int


class MihoyoCostume(TypedDict):
    id: int
    name: str
    icon: str


class MihoyoAvatar(TypedDict):
    id: int
    image: str
    icon: str
    """在api/character接口有"""
    name: str
    element: Literal["Geo", "Anemo", "Dendro", "Electro", "Pyro", "Cryo", "Hydro"]
    fetter: int
    level: int
    rarity: int
    weapon: MihoyoWeapon
    """在api/character接口有"""
    reliquaries: List[MihoyoReliquary]
    """在api/character接口有"""
    constellations: List[MihoyoConstellation]
    """在api/character接口有"""
    actived_constellation_num: int
    costumes: List[MihoyoCostume]
    """在api/character接口有"""
    card_image: str
    """在api/index接口有"""
    is_chosen: bool
    """在api/index接口有"""


class CharDetailData(TypedDict):
    list: List[MihoyoAvatar]


class MainProperty(TypedDict):
    property_type: int
    base: str
    add: str
    final: str


class SubProperty(TypedDict):
    property_type: int
    base: str
    add: str
    final: str


class RelicMainProperty(TypedDict):
    property_type: int
    value: str
    times: int


class RelicSubProperty(TypedDict):
    property_type: int
    value: str
    times: int


class RelicSet(TypedDict):
    id: int
    name: str
    affixes: List[dict]


class Relic(TypedDict):
    id: int
    name: str
    icon: str
    pos: int
    rarity: int
    level: int
    set: RelicSet
    pos_name: str
    main_property: RelicMainProperty
    sub_property_list: List[RelicSubProperty]


class Constellation(TypedDict):
    id: int
    name: str
    icon: str
    effect: str
    is_actived: bool
    pos: int


class Property(TypedDict):
    property_type: int
    base: str
    add: str
    final: str


class DetailWeapon(TypedDict):
    id: int
    name: str
    icon: str
    type: int
    rarity: int
    level: int
    promote_level: int
    type_name: str
    desc: str
    affix_level: int
    main_property: MainProperty
    sub_property: SubProperty


class CharacterBase(TypedDict):
    id: int
    icon: str
    name: str
    element: str
    fetter: int
    level: int
    rarity: int
    actived_constellation_num: int
    image: str
    is_chosen: bool
    side_icon: str
    weapon_type: int
    weapon: DetailWeapon


class SkillAffix(TypedDict):
    name: str
    value: str


class Skill(TypedDict):
    skill_id: int
    skill_type: int
    level: int
    desc: str
    skill_affix_list: List[SkillAffix]
    icon: str
    is_unlock: bool
    name: str


class Character(TypedDict):
    base: CharacterBase
    weapon: DetailWeapon
    relics: List[Relic]
    constellations: List[Constellation]
    costumes: List[dict]
    selected_properties: List[Property]
    base_properties: List[Property]
    extra_properties: List[Property]
    element_properties: List[Property]
    skills: List[Skill]


Weapon = DetailWeapon
