from __future__ import annotations

from typing import List, Literal, TypedDict


class CardCovers(TypedDict):
    id: int
    image: str


class GcgInfo(TypedDict):
    level: int
    nickname: str
    avatar_card_num_gained: int
    avatar_card_num_total: int
    action_card_num_gained: int
    action_card_num_total: int
    covers: List[CardCovers]


class GcgDeckInfo(TypedDict):
    deck_list: List[GcgDeck]
    role_id: str  # uid
    level: int  # 世界等级
    nickname: str


class GcgDeck(TypedDict):
    id: int
    name: str
    is_valid: bool
    avatar_cards: List[GcgAvatar]
    action_cards: List[GcgAction]


class GcgAvatarSkill(TypedDict):
    id: int
    name: str
    desc: str
    tag: Literal["普通攻击", "元素战技", "元素爆发", "被动技能"]


class GcgAvatar(TypedDict):
    id: int
    name: str
    image: str
    desc: str
    card_type: Literal["CardTypeCharacter"]
    num: int
    tags: List[str]  # 元素和武器类型icon
    proficiency: int
    use_count: int
    hp: int
    card_skills: List[GcgAvatarSkill]
    action_cost: List[GcgCost]
    card_sources: List[str]
    rank_id: int
    deck_recommend: str
    card_wiki: str


class GcgCost(TypedDict):
    cost_type: Literal["CostTypeSame", "CostTypeVoid"]
    cost_value: int


class GcgAction(TypedDict):
    id: int
    name: str
    image: str
    desc: str
    card_type: str
    num: int
    tags: List[str]  # 元素和武器类型icon
    proficiency: int
    use_count: int
    hp: int
    card_skills: List[GcgAvatarSkill]
    action_cost: List[GcgCost]
    card_sources: List[str]
    rank_id: int
    deck_recommend: str
    card_wiki: str
