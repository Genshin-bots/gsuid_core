"""
MiniGG API 响应模型。
"""

# TODO: - @KimigaiiWuyi 补文档
from __future__ import annotations

import sys
from typing import Dict, List, Literal, TypedDict

# https://peps.python.org/pep-0655/#usage-in-python-3-11
if sys.version_info >= (3, 11):
    from typing import NotRequired
else:
    from typing_extensions import NotRequired

# https://peps.python.org/pep-0613
if sys.version_info >= (3, 10):
    from typing import TypeAlias
else:
    from typing_extensions import TypeAlias

R: TypeAlias = List[str]


class FandomUrl(TypedDict):
    fandom: str


class WeaponImage(TypedDict):
    filename_icon: str
    filename_awakenIcon: str
    filename_gacha: str
    icon: str
    nameawakenicon: str
    awakenicon: str
    mihoyo_icon: str
    mihoyo_awakenIcon: str


class AscendItem(TypedDict):
    name: str
    count: int


class Costs(TypedDict):
    ascend1: List[AscendItem]
    ascend2: List[AscendItem]
    ascend3: List[AscendItem]
    ascend4: List[AscendItem]
    ascend5: List[AscendItem]
    ascend6: List[AscendItem]


class WeaponR(TypedDict):
    description: str
    values: List[str]


class Weapon(TypedDict):
    name: str
    description: str
    descriptionRaw: str
    weaponType: str
    weaponText: str
    rarity: int
    story: str
    baseAtkValue: int
    mainStatType: str
    mainStatText: str
    baseStatText: str
    effectName: str
    effectTemplateRaw: str
    r1: WeaponR
    r2: WeaponR
    r3: WeaponR
    r4: WeaponR
    r5: WeaponR
    images: WeaponImage
    version: str
    costs: Costs


class WeaponStats(TypedDict):
    level: int
    ascension: int
    attack: float
    specialized: float


class Character(TypedDict):
    id: int
    name: str
    fullname: str
    title: str
    description: str
    rarity: str
    elementType: str
    elementText: str
    weaponType: str
    weaponText: str
    substatType: str
    substatText: str
    gender: Literal["男", "女"]
    qualityType: str
    bodyType: str
    association: str
    region: Literal[
        "蒙德", "璃月", "稻妻", "须弥", "枫丹", "纳塔", "至冬", "穆纳塔"
    ]
    affiliation: str
    birthdaymmdd: str
    birthday: str
    constellation: str
    cv: CharacterCv
    costs: Costs
    image: CharacterImage
    url: FandomUrl
    version: str


class CharacterCv(TypedDict):
    english: str
    chinese: str
    japanese: str
    korean: str


class CharacterImage(TypedDict):
    card: str
    portrait: str
    icon: str
    sideicon: str
    cover1: str
    cover2: str
    hoyolab_avatar: str
    filename_icon: str
    filename_iconcard: str
    namegachasplash: str
    namegachaslice: str
    namesideicon: str


class CharacterStats(TypedDict):
    level: int
    ascension: int
    hp: float
    attack: float
    defense: float
    specialized: float


class CharacterConstellations(TypedDict):
    name: str
    c1: CharacterConstellation
    c2: CharacterConstellation
    c3: CharacterConstellation
    c4: CharacterConstellation
    c5: CharacterConstellation
    c6: CharacterConstellation
    images: ConstellationsImage
    version: str


class CharacterConstellation(TypedDict):
    name: str
    descriptionRaw: str
    description: str


class ConstellationsImage(TypedDict):
    filename_c1: str
    filename_c2: str
    filename_c3: str
    filename_c4: str
    filename_c5: str
    filename_c6: str


class MiniGGError(TypedDict):
    retcode: int
    error: str


class CharacterTalents(TypedDict):
    id: int
    name: str
    combat1: TalentCombat
    combat2: TalentCombat
    combat3: TalentCombat
    passive1: TalentPassive
    passive2: TalentPassive
    passive3: TalentPassive
    passive4: NotRequired[TalentPassive]
    costs: TalentsCosts
    images: TalentsImages


class TalentsCosts(TypedDict):
    lvl2: List[AscendItem]
    lvl3: List[AscendItem]
    lvl4: List[AscendItem]
    lvl5: List[AscendItem]
    lvl6: List[AscendItem]
    lvl7: List[AscendItem]
    lvl8: List[AscendItem]
    lvl9: List[AscendItem]
    lvl10: List[AscendItem]


class TalentsImages(TypedDict):
    combat1: str
    combat2: str
    combat3: str
    passive1: str
    passive2: str
    passive3: str
    passive4: NotRequired[str]


class TalentCombat(TypedDict):
    name: str
    descriptionRaw: str
    description: str
    attributes: TalentAttr


class TalentPassive(TypedDict):
    name: str
    description: str


class TalentAttr(TypedDict):
    labels: List[str]
    parameters: Dict[str, List[float]]


class Food(TypedDict):
    id: int
    name: str
    rarity: str
    foodtype: str
    filterType: str
    filterText: str
    effect: str
    description: str
    suspicious: FoodEffect
    normal: FoodEffect
    delicious: FoodEffect
    ingredients: List[AscendItem]
    images: FoodImage
    version: str


class FoodEffect(TypedDict):
    effect: str
    description: str


class Image(TypedDict):
    filename_icon: str


class FoodImage(Image):
    filename_buff: str


class Enemy(TypedDict):
    name: str
    specialName: str
    enemyType: str
    category: str
    description: str
    investigation: EnemyInvest
    rewardPreview: List[EnemyReward]
    images: Image
    version: str


class EnemyReward(TypedDict):
    name: str
    count: NotRequired[float]


class EnemyInvest(TypedDict):
    name: str
    category: str
    description: str


class Domain(TypedDict):
    name: str
    region: Literal[
        "蒙德", "璃月", "稻妻", "须弥", "枫丹", "纳塔", "至冬", "穆纳塔"
    ]
    domainentrance: str
    domaintype: str
    description: str
    recommendedlevel: int
    recommendedelements: List[
        Literal[
            "冰元素",
            "火元素",
            "雷元素",
            "水元素",
            "草元素",
            "岩元素",
            "风元素",
        ]
    ]
    daysofweek: List[
        Literal["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
    ]
    unlockrank: int
    rewardpreview: List[EnemyReward]
    disorder: List[str]
    monsterlist: List[str]
    images: Image
    version: str


class Piece(TypedDict):
    name: str
    relicType: str
    relicText: str
    description: str
    story: str


class PieceFlower(Piece):
    relicText: Literal["生之花"]


class PiecePlume(Piece):
    relicText: Literal["死之羽"]


class PieceSands(Piece):
    relicText: Literal["时之沙"]


class PieceGoblet(Piece):
    relicText: Literal["空之杯"]


class PieceCirclet(Piece):
    relicText: Literal["理之冠"]


class PieceImages(TypedDict):
    flower: str
    plume: str
    sands: str
    goblet: str
    circlet: str
    filename_flower: str
    filename_plume: str
    filename_sands: str
    filename_goblet: str
    filename_circlet: str
    mihoyo_flower: str
    mihoyo_plume: str
    mihoyo_sands: str
    mihoyo_goblet: str
    mihoyo_circlet: str


Artifact = TypedDict(
    "Artifact",
    {
        "id": int,
        "name": str,
        "rarityList": List[int],
        "effect1Pc": str,
        "effect2Pc": str,
        "effect4Pc": str,
        "flower": PieceFlower,
        "plume": PiecePlume,
        "sands": PieceSands,
        "goblet": PieceGoblet,
        "circlet": PieceCirclet,
        "images": PieceImages,
        "url": FandomUrl,
        "version": str,
    },
)


class MaterialImage(TypedDict):
    filename_icon: str
    redirect: str


class Material(TypedDict):
    id: int
    name: str
    description: str
    sortRank: int
    rarity: int
    category: str
    typeText: str
    dropDomainId: int
    dropDomainName: str
    source: List[str]
    images: MaterialImage
    version: str
    # 下面两个当且仅当materialtype是xx突破素材的情况才有
    daysOfWeek: List[str]
