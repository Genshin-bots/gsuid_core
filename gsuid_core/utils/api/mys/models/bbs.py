from __future__ import annotations

from typing import List, TypedDict


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
