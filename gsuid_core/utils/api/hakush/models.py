from typing import Dict, List, TypedDict


# 角色信息中的配音部分
class VAInfo(TypedDict):
    Chinese: str
    Japanese: str
    English: str
    Korean: str


# 角色信息中的特别食品
class SpecialFoodInfo(TypedDict):
    Id: int
    Recipe: int
    Name: str
    Icon: str
    Rank: int


# 角色信息中的名片
class NamecardInfo(TypedDict):
    Id: int
    Name: str
    Desc: str
    Icon: str


# 角色信息中的装扮
class CostumeInfo(TypedDict):
    Id: int
    Name: str
    Desc: str
    Icon: str
    Quality: str


# 角色信息中的天赋故事
class StoryInfo(TypedDict):
    Title: str
    Text: str
    Unlock: List[str]


# 角色信息中的名言
class QuoteInfo(TypedDict):
    Title: str
    Text: str
    Unlocked: List[str]


# 角色信息
class CharaInfo(TypedDict):
    ReleaseDate: str
    Birth: List[int]
    Vision: str
    Constellation: str
    Region: str
    Title: str
    Native: str
    Detail: str
    VA: VAInfo
    Stories: List[StoryInfo]
    Quotes: List[QuoteInfo]
    SpecialFood: SpecialFoodInfo
    Namecard: NamecardInfo
    Costume: List[CostumeInfo]


# 角色属性成长曲线
class PropGrowCurve(TypedDict):
    type: str
    growCurve: str


class Ascension(TypedDict):
    FIGHT_PROP_BASE_HP: float
    FIGHT_PROP_BASE_DEFENSE: float
    FIGHT_PROP_BASE_ATTACK: float
    FIGHT_PROP_CRITICAL: float


# 角色基础属性
class CharStatsModifier(TypedDict):
    HP: Dict[str, float]
    ATK: Dict[str, float]
    DEF: Dict[str, float]
    Ascension: List[Ascension]
    PropGrowCurves: List[PropGrowCurve]


# 技能等级提升信息
class SkillPromoteInfo(TypedDict):
    Level: int
    Icon: str
    Desc: List[str]
    Param: List[float]


# 技能信息
class SkillInfo(TypedDict):
    Name: str
    Desc: str
    Promote: Dict[str, SkillPromoteInfo]


# 被动天赋信息
class PassiveInfo(TypedDict):
    Name: str
    Desc: str
    Icon: str
    Unlock: int
    ParamList: List[float]


# 星座信息
class ConstellationInfo(TypedDict):
    Name: str
    Desc: str
    Icon: str
    ParamList: List[float]


# 材料需求信息
class MaterialReq(TypedDict):
    Name: str
    Id: int
    Count: int
    Rank: int


# 升级材料
class TalentUpgradeMaterials(TypedDict):
    Mats: List[MaterialReq]
    Cost: int


class CharMaterials(TypedDict):
    Ascensions: List[MaterialReq]
    Talents: List[List[TalentUpgradeMaterials]]


# 角色数据
class CharacterData(TypedDict):
    Name: str
    Desc: str
    CharaInfo: CharaInfo
    Weapon: str
    Rarity: str
    Icon: str
    StaminaRecovery: int
    BaseHP: float
    BaseATK: float
    BaseDEF: float
    CritRate: float
    CritDMG: float
    StatsModifier: CharStatsModifier
    Skills: List[SkillInfo]
    Passives: List[PassiveInfo]
    Constellations: List[ConstellationInfo]
    Materials: CharMaterials


# 定义武器属性的 TypedDict
class WeaponProp(TypedDict):
    propType: str
    initValue: float
    type: str


# 定义统计数据修改器的 TypedDict
class StatsModifierLevel(TypedDict):
    Base: float
    Levels: Dict[int, float]


class WeaponStatsModifier(TypedDict):
    ATK: StatsModifierLevel
    FIGHT_PROP_CRITICAL_HURT: StatsModifierLevel


# 定义精炼效果的 TypedDict
class RefinementParam(TypedDict):
    Name: str
    Desc: str
    ParamList: List[float]


# 定义材料需求的 TypedDict
class Material(TypedDict):
    Name: str
    Id: int
    Count: int
    Rank: int


class MaterialsLevel(TypedDict):
    Mats: List[Material]
    Cost: int


# 定义武器的 TypedDict
class WeaponData(TypedDict):
    Name: str
    Desc: str
    WeaponType: str
    WeaponProp: List[WeaponProp]
    Rarity: int
    Icon: str
    StatsModifier: WeaponStatsModifier
    XPRequirements: Dict[int, int]
    Ascension: Dict[int, Dict[str, float]]
    Refinement: Dict[str, RefinementParam]
    Materials: Dict[str, MaterialsLevel]
