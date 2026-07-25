from __future__ import annotations

from typing import Any, List, TypedDict

from .poetry_abyss import RoundData, FightStatistic


class SeasonDateTime(TypedDict):
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int


class BackupAvatar(TypedDict):
    avatar_id: int
    avatar_type: int
    name: str
    element: str
    image: str
    level: int
    rarity: int


class DetailStat(TypedDict):
    difficulty_id: int
    max_round_id: int
    heraldry: int
    get_medal_round_list: List[int]
    medal_num: int
    coin_num: int
    avatar_bonus_num: int
    rent_cnt: int


class CombatStat(TypedDict):
    difficulty_id: int
    max_round_id: int
    heraldry: int
    get_medal_round_list: List[int]
    medal_num: int
    coin_num: int
    avatar_bonus_num: int
    rent_cnt: int


class ScheduleInfo(TypedDict):
    start_time: str
    end_time: str
    schedule_type: int
    schedule_id: int
    start_date_time: SeasonDateTime
    end_date_time: SeasonDateTime


class GCGChallenge(TypedDict):
    honor_avatar_list: List[Any]
    max_win_games: int
    begin: SeasonDateTime
    end: SeasonDateTime
    name: str


class GCGInfo(TypedDict):
    avatar_card_list: List[Any]
    cur_avatar_card_num: int
    new_avatar_card_num: int
    action_card_list: List[Any]
    cur_action_card_num: int
    new_action_card_num: int
    challenge_list: List[GCGChallenge]
    has_data: bool


class AbyssRank(TypedDict):
    avatar_id: int
    avatar_icon: str
    value: int
    rarity: int


class SpiralAbyssInfo(TypedDict):
    schedule_id: int
    start_time: str
    end_time: str
    total_battle_times: int
    max_floor: str
    reveal_rank: List[AbyssRank]
    damage_rank: List[AbyssRank]
    defeat_rank: List[AbyssRank]
    take_damage_rank: List[AbyssRank]
    total_star: int
    name: str
    normal_skill_rank: List[AbyssRank]
    energy_skill_rank: List[AbyssRank]
    is_just_skipped_floor: bool


class SeasonAchievement(TypedDict):
    name: str


class HistoryStatInfo(TypedDict):
    login_days: int
    task_num: int
    cur_achievement_num: int
    new_achievement_num: int
    achievement_list: List[SeasonAchievement]
    mostly_visit_dungeon_name: str
    mostly_visit_dungeon_times: int
    mostly_visit_weekly_boss_name: str
    mostly_visit_weekly_boss_times: int
    has_data: bool


class HCoinSource(TypedDict):
    type: int
    percent: int


class ResinConsume(TypedDict):
    type: str
    percent: int


class ResourceInfo(TypedDict):
    gain_scoin: int
    gain_hcoin: int
    hcoin_source_list: List[HCoinSource]
    consume_resin: int
    resin_consume_list: List[ResinConsume]
    has_data: bool


class SeasonWorldExploration(TypedDict):
    cur_number: int
    new_number: int
    icon: str
    name: str


class Crystal(TypedDict):
    cur_number: int
    new_number: int
    icon: str
    name: str


class Chest(TypedDict):
    cur_number: int
    new_number: int
    icon: str
    name: str


class TransPoint(TypedDict):
    cur_number: int
    new_number: int
    has_data: bool


class DungeonPoint(TypedDict):
    cur_number: int
    new_number: int
    has_data: bool


class ExplorationInfo(TypedDict):
    world_exploration_list: List[SeasonWorldExploration]
    crystal_list: List[Crystal]
    chest_list: List[Chest]
    trans_point: TransPoint
    dungeon_point: DungeonPoint
    has_data: bool


class Costume(TypedDict):
    id: int
    name: str
    avatar_name: str
    image: str
    is_new_costume: bool
    wide_image: str


class CostumeInfo(TypedDict):
    cur_costume_count: int
    new_costume_count: int
    costume_list: List[Costume]
    has_data: bool


class ChangedWeapon(TypedDict):
    id: int
    name: str
    image: str
    is_new_weapon: bool
    rarity: int


class WeaponInfo(TypedDict):
    cur_weapon_count: int
    new_weapon_count: int
    changed_weapon_list: List[ChangedWeapon]
    has_data: bool


class ChangedAvatar(TypedDict):
    id: int
    image: str
    is_new_avatar: bool
    start_level: int
    end_level: int
    start_fetter: int
    end_fetter: int
    start_actived_constellation_num: int
    end_actived_constellation_num: int
    rarity: int
    element: str
    name: str
    icon: str


class AvatarInfo(TypedDict):
    cur_avatar_count: int
    new_avatar_count: int
    changed_avatar_list: List[ChangedAvatar]
    has_data: bool


class ScheduleDetail(TypedDict):
    rounds_data: List[RoundData]
    detail_stat: DetailStat
    lineup_link: str
    backup_avatars: List[BackupAvatar]
    fight_statisic: FightStatistic


class Schedule(TypedDict):
    detail: ScheduleDetail
    stat: CombatStat
    schedule: ScheduleInfo
    has_data: bool
    has_detail_data: bool


class RoleCombat(TypedDict):
    schedule_list: List[Schedule]
    is_unlock: bool


class SeasonPostData(TypedDict):
    avatar_info: AvatarInfo
    weapon_info: WeaponInfo
    costume_info: CostumeInfo
    exploration_info: ExplorationInfo
    resource_info: ResourceInfo
    history_stat_info: HistoryStatInfo
    spiral_abyss_info: List[SpiralAbyssInfo]
    gcg_info: GCGInfo
    role_combat: RoleCombat
