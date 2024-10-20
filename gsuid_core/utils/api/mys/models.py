from __future__ import annotations

import sys
from typing import Dict, List, Literal, Optional, TypedDict

# https://peps.python.org/pep-0655/#usage-in-python-3-11
if sys.version_info >= (3, 11):
    from typing import NotRequired
else:
    from typing_extensions import NotRequired


# Response about
# https://api-takumi-record.mihoyo.com/game_record/app/genshin/api/index
# https://api-takumi-record.mihoyo.com/game_record/app/genshin/api/character
# 玩家、武器、圣遗物、角色模型


class PostDrawRole(TypedDict):
    role_id: int
    name: str
    jump_type: str
    jump_target: str
    jump_start_time: str
    jump_end_time: str
    role_gender: int
    take_picture: str
    gal_xml: str
    gal_resource: str
    is_partake: bool
    bgm: str


class PostDrawTask(TypedDict):
    task_id: int
    status: str


class _PostDraw(TypedDict):
    nick_name: str
    uid: int
    region: str
    role: List[PostDrawRole]
    draw_notice: bool
    CurrentTime: str
    gender: int
    is_show_remind: bool
    is_compensate_num: str
    current_compensate_num: int
    guide_task: bool
    guide_compensate: bool
    guide_draw: bool
    task_infos: List[PostDrawTask]
    is_year_subscribe: bool
    has_compensate_role: bool


class PostDraw(TypedDict):
    retcode: int
    message: str
    data: _PostDraw


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
    type_name: Literal['单手剑', '双手剑', '长柄武器', '弓', '法器']
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
    '''在api/character接口有'''
    name: str
    element: Literal[
        'Geo', 'Anemo', 'Dendro', 'Electro', 'Pyro', 'Cryo', 'Hydro'
    ]
    fetter: int
    level: int
    rarity: int
    weapon: MihoyoWeapon
    '''在api/character接口有'''
    reliquaries: List[MihoyoReliquary]
    '''在api/character接口有'''
    constellations: List[MihoyoConstellation]
    '''在api/character接口有'''
    actived_constellation_num: int
    costumes: List[MihoyoCostume]
    '''在api/character接口有'''
    card_image: str
    '''在api/index接口有'''
    is_chosen: bool
    '''在api/index接口有'''


# Response
# https://api-takumi-record.mihoyo.com/game_record/app/genshin/api/spiralAbyss


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


# Response
# https://api-takumi-record.mihoyo.com/game_record/app/genshin/api/dailyNote


class Expedition(TypedDict):
    avatar_side_icon: str
    status: Literal['Ongoing', 'Finished']
    remained_time: int


class RecoveryTime(TypedDict):
    Day: int
    Hour: int
    Minute: int
    Second: int
    reached: bool


class Transformer(TypedDict):
    obtained: bool
    recovery_time: RecoveryTime
    wiki: str
    noticed: bool
    latest_job_id: str


class TaskStatus(TypedDict):
    status: str


class DayilyTask(TypedDict):
    total_num: int
    finished_num: int
    is_extra_task_reward_received: bool
    task_rewards: List[TaskStatus]
    attendance_rewards: List[TaskStatus]


class ArchonStatus(TypedDict):
    status: str
    chapter_num: str
    chapter_title: str
    id: int


class ArchonProgress(TypedDict):
    list: List[ArchonStatus]
    is_open_archon_quest: bool
    is_finish_all_mainline: bool
    is_finish_all_interchapter: bool
    wiki_url: str


class DailyNoteData(TypedDict):
    current_resin: int
    max_resin: int
    resin_recovery_time: int
    finished_task_num: int
    total_task_num: int
    is_extra_task_reward_received: bool
    remain_resin_discount_num: int
    resin_discount_num_limit: int
    current_expedition_num: int
    max_expedition_num: int
    expeditions: List[Expedition]
    current_home_coin: int
    max_home_coin: int
    home_coin_recovery_time: int
    calendar_url: str
    transformer: Transformer
    daily_task: DayilyTask
    archon_quest_progress: ArchonProgress


# Response from https://api-takumi.mihoyo.com/game_record/app/genshin/api/index


class ExtMap(TypedDict):
    link: str
    backup_link: str


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
    field_ext_map: Dict[str, ExtMap]


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


class CharDetailData(TypedDict):
    list: List[MihoyoAvatar]


################
# Token Models #
################


class CookieTokenInfo(TypedDict):
    uid: str
    cookie_token: str


class StokenInfo(TypedDict):
    token_type: NotRequired[int]
    name: NotRequired[str]
    token: str


class GameTokenInfo(TypedDict):
    token: StokenInfo
    user_info: UserInfo


class LoginTicketInfo(TypedDict):
    list: List[StokenInfo]


class AuthKeyInfo(TypedDict):
    sign_type: int
    authkey_ver: int
    authkey: str


class Hk4eLoginInfo(TypedDict):
    game: str
    region: str
    game_uid: str
    game_biz: str
    level: int
    nickname: str
    region_name: str


################
# 扫码登录相关 #
################


class QrCodeUrl(TypedDict):
    url: str


class QrPayload(TypedDict):
    proto: str
    raw: str
    ext: str


class QrCodeStatus(TypedDict):
    stat: Literal['Init', 'Scanned', 'Confirmed']
    payload: QrPayload


################
# UserInfo相关 #
################


class UserLinks(TypedDict):
    thirdparty: str
    union_id: str
    nickname: str


class UserInfo(TypedDict):
    aid: str
    mid: str
    account_name: str
    email: str
    is_email_verify: int
    area_code: str
    mobile: str
    safe_area_code: str
    safe_mobile: str
    realname: str
    identity_code: str
    rebind_area_code: str
    rebind_mobile: str
    rebind_mobile_time: str
    links: List[UserLinks]


################
# 抽卡记录相关 #
################


class SingleGachaLog(TypedDict):
    uid: str
    gacha_type: str
    item_id: str
    count: str
    time: str
    name: str
    lang: str
    item_type: str
    rank_type: str
    id: str


class GachaLog(TypedDict):
    page: str
    size: str
    total: str
    list: List[SingleGachaLog]
    region: str


################
# 注册时间相关 #
################


class CardOpts(TypedDict):
    adjs: List[int]
    titles: List[int]
    items: List[int]
    data_version: str


Props = TypedDict(
    'Props',
    {
        '66a': str,
        '50a': str,
        '53b': str,
        'pre_69b': str,
        '49a': str,
        '52b': str,
        'pre_71b': str,
        '37': str,
        '48a': str,
        '57': str,
    },
)


class RegTime(TypedDict):
    data: str
    card_opts: CardOpts
    props: Props
    data_version: int
    prop_version: int


################
# 七圣召唤相关 #
################


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


################
# 每月札记相关 #
################


class DayData(TypedDict):
    current_primogems: int
    current_mora: int
    last_primogems: int
    last_mora: int


class GroupBy(TypedDict):
    action_id: int
    action: str
    num: int
    percent: int


class MonthData(TypedDict):
    current_primogems: int
    current_mora: int
    last_primogems: int
    last_mora: int
    current_primogems_level: int
    primogems_rate: int
    mora_rate: int
    group_by: List[GroupBy]


class MonthlyAward(TypedDict):
    uid: int
    region: str
    account_id: str
    nickname: str
    date: str
    month: str
    optional_month: List[int]
    data_month: int
    data_last_month: int
    day_data: DayData
    month_data: MonthData
    lantern: bool


################
# 签到相关 #
################


class MysSign(TypedDict):
    code: str
    risk_code: int
    gt: str
    challenge: str
    success: int
    message: str


class SignInfo(TypedDict):
    total_sign_day: int
    today: str
    is_sign: bool
    first_bind: bool
    is_sub: bool
    month_first: bool
    sign_cnt_missed: int
    month_last_day: bool


class SignAward(TypedDict):
    icon: str
    name: str
    cnt: int


class SignList(TypedDict):
    month: int
    awards: List[SignAward]
    resign: bool


################
# 养成计算器部分 #
################


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


################
#  RecordCard  #
################


class MysGame(TypedDict):
    has_role: bool
    game_id: int  # 2是原神
    game_role_id: str  # UID
    nickname: str
    region: str
    level: int
    background_image: str
    is_public: bool
    data: List[MysGameData]
    region_name: str
    url: str
    data_switches: List[MysGameSwitch]
    h5_data_switches: Optional[List]
    background_color: str  # 十六进制颜色代码


class MysGameData(TypedDict):
    name: str
    type: int
    value: str


class MysGameSwitch(TypedDict):
    switch_id: int
    is_public: bool
    switch_name: str


'''支付相关'''


class MysGoods(TypedDict):
    goods_id: str
    goods_name: str
    goods_name_i18n_key: str
    goods_desc: str
    goods_desc_i18n_key: str
    goods_type: Literal['Normal', 'Special']
    goods_unit: str
    goods_icon: str
    currency: Literal['CNY']
    price: str
    symbol: Literal['￥']
    tier_id: Literal['Tier_1']
    bonus_desc: MysGoodsBonus
    once_bonus_desc: MysGoodsBonus
    available: bool
    tips_desc: str
    tips_i18n_key: str
    battle_pass_limit: str


class MysGoodsBonus(TypedDict):
    bonus_desc: str
    bonus_desc_i18n_key: str
    bonus_unit: int
    bonus_goods_id: str
    bonus_icon: str


class MysOrderCheck(TypedDict):
    status: int  # 900为成功
    amount: str
    goods_title: str
    goods_num: str
    order_no: str
    pay_plat: Literal['alipay', 'weixin']


class MysOrder(TypedDict):
    goods_id: str
    order_no: str
    currency: Literal['CNY']
    amount: str
    redirect_url: str
    foreign_serial: str
    encode_order: str
    account: str  # mysid
    create_time: str
    ext_info: str
    balance: str
    method: str
    action: str
    session_cookie: str


'''七圣召唤牌组'''


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
    tag: Literal['普通攻击', '元素战技', '元素爆发', '被动技能']


class GcgAvatar(TypedDict):
    id: int
    name: str
    image: str
    desc: str
    card_type: Literal['CardTypeCharacter']
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
    cost_type: Literal['CostTypeSame', 'CostTypeVoid']
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


# 留影叙佳期
class GsRoleBirthDay(TypedDict):
    role_id: int
    name: str
    jump_tpye: str
    jump_target: str
    jump_start_time: str
    jump_end_time: str
    role_gender: int
    take_picture: str
    gal_xml: str
    gal_resource: str
    is_partake: bool
    bgm: str


class BsIndex(TypedDict):
    nick_name: str
    uid: int
    region: str
    role: List[GsRoleBirthDay]
    draw_notice: bool
    CurrentTime: str
    gender: int
    is_show_remind: bool


class RolesCalendar(TypedDict):
    calendar_role_infos: MonthlyRoleCalendar
    is_pre: bool
    is_next: bool
    is_year_subscribe: bool


class RoleCalendar(TypedDict):
    role_id: int
    name: str
    role_birthday: str
    head_icon: str
    is_subscribe: bool


class RoleCalendarList(TypedDict):
    calendar_role: List[RoleCalendar]


MonthlyRoleCalendar = TypedDict(
    'MonthlyRoleCalendar',
    {
        '1': RoleCalendarList,
        '2': RoleCalendarList,
        '3': RoleCalendarList,
        '4': RoleCalendarList,
        '5': RoleCalendarList,
        '6': RoleCalendarList,
        '7': RoleCalendarList,
        '8': RoleCalendarList,
        '9': RoleCalendarList,
        '10': RoleCalendarList,
        '11': RoleCalendarList,
        '12': RoleCalendarList,
    },
)


class ImageInfo(TypedDict):
    url: str
    height: int
    width: int
    format: str
    size: str
    crop: dict
    is_user_set_cover: bool
    image_id: str
    entity_type: str
    entity_id: str
    is_deleted: bool


class PostStatus(TypedDict):
    is_top: bool
    is_good: bool
    is_official: bool
    post_status: int


class UserCertification(TypedDict):
    type: int
    label: str


class User(TypedDict):
    uid: str
    nickname: str
    introduce: str
    avatar: str
    gender: int
    certification: UserCertification
    level_exp: dict
    is_following: bool
    is_followed: bool
    avatar_url: str
    pendant: str
    certifications: List[UserCertification]
    is_creator: bool
    avatar_ext: dict


class Topic(TypedDict):
    id: int
    name: str
    cover: str
    is_top: bool
    is_good: bool
    is_interactive: bool
    game_id: int
    content_type: int


class Forum(TypedDict):
    id: int
    name: str
    icon: str
    game_id: int
    forum_cate: dict


class Post(TypedDict):
    game_id: int
    post_id: str
    f_forum_id: int
    uid: str
    subject: str
    content: str
    cover: str
    view_type: int
    created_at: int
    images: List[str]
    post_status: PostStatus
    topic_ids: List[int]
    view_status: int
    max_floor: int
    is_original: int
    republish_authorization: int
    reply_time: str
    is_deleted: int
    is_interactive: bool
    structured_content: List[dict]
    structured_content_rows: List[dict]
    review_id: int
    is_profit: bool
    is_in_profit: bool
    updated_at: int
    deleted_at: int
    pre_pub_status: int
    cate_id: int
    profit_post_status: int
    audit_status: int
    meta_content: str
    is_missing: bool
    block_reply_img: int
    is_showing_missing: bool
    block_latest_reply_time: int
    selected_comment: int
    is_mentor: bool


class PostDetail(TypedDict):
    post: Post
    forum: Forum
    topics: List[Topic]
    user: User
    self_operation: dict
    stat: dict
    help_sys: dict
    cover: ImageInfo
    image_list: List[ImageInfo]
    is_official_master: bool
    is_user_master: bool
    hot_reply_exist: bool
    vote_count: int
    last_modify_time: int
    recommend_type: str
    collection: dict
    vod_list: List[str]
    is_block_on: bool
    forum_rank_info: dict
    link_card_list: List[str]
    news_meta: dict
    recommend_reason: str
    villa_card: dict
    is_mentor: bool
    villa_room_card: dict
    reply_avatar_action_info: dict
    challenge: dict
    hot_reply_list: List[str]
    villa_msg_image_list: List[str]


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
    weapons: List[
        ConsumeItem
    ]  # 假设 weapons 也是消耗材料的一部分，根据实际情况可能需要调整


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


class PoetryAbyssLinks(TypedDict):
    lineup_link: str
    lineup_link_pc: str
    strategy_link: str
    lineup_publish_link: str
    lineup_publish_link_pc: str


class PoetryAbyssAvatar(TypedDict):
    avatar_id: int
    avatar_type: int
    name: str
    element: str
    image: str
    level: int
    rarity: int


class PoetryAbyssChoiceCard(TypedDict):
    icon: str
    name: str
    desc: str
    is_enhanced: bool
    id: int


class PoetryAbyssBuff(TypedDict):
    icon: str
    name: str
    desc: str
    is_enhanced: bool
    id: int


class PoetryAbyssDateTime(TypedDict):
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int


class PoetryAbyssSchedule(TypedDict):
    start_time: int
    end_time: int
    schedule_type: int
    schedule_id: int
    start_date_time: PoetryAbyssDateTime
    end_date_time: PoetryAbyssDateTime


class PoetryAbyssDetailStat(TypedDict):
    difficulty_id: int
    max_round_id: int
    heraldry: int
    get_medal_round_list: List[int]
    medal_num: int
    coin_num: int
    avatar_bonus_num: int
    rent_cnt: int


class RoundData(TypedDict):
    avatars: List[PoetryAbyssAvatar]
    choice_cards: List[PoetryAbyssChoiceCard]
    buffs: List[PoetryAbyssBuff]
    is_get_medal: bool
    round_id: int
    finish_time: int
    finish_date_time: PoetryAbyssDateTime
    detail_stat: PoetryAbyssDetailStat


class PoetryAbyssDetail(TypedDict):
    rounds_data: List[RoundData]
    detail_stat: PoetryAbyssDetailStat
    backup_avatars: List[PoetryAbyssAvatar]


class PoetryAbyssData(TypedDict):
    detail: PoetryAbyssDetail
    stat: PoetryAbyssDetailStat
    schedule: PoetryAbyssSchedule
    has_data: bool
    has_detail_data: bool


class PoetryAbyssDatas(TypedDict):
    data: List[PoetryAbyssData]
    is_unlock: bool
    links: PoetryAbyssLinks


class AchievementData(TypedDict):
    name: str
    id: str
    percentage: int
    finish_num: int
    show_percent: bool
    icon: str
