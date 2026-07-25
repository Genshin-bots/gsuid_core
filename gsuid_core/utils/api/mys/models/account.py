from __future__ import annotations

from typing import Dict, List, Literal, Optional, TypedDict

from ._typing import NotRequired


class CookieTokenInfo(TypedDict):
    uid: str
    cookie_token: str
    cookie_token_name: NotRequired[str]
    cookies: NotRequired[Dict[str, str]]


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


class QrCodeUrl(TypedDict):
    url: str


class QrPayload(TypedDict):
    proto: str
    raw: str
    ext: str


class QrCodeStatus(TypedDict):
    stat: Literal["Init", "Scanned", "Confirmed"]
    payload: QrPayload


class HypQrCodeStatus(TypedDict):
    status: Literal["Created", "Scanned", "Confirmed"]
    tokens: NotRequired[List[StokenInfo]]
    user_info: NotRequired[Optional[UserInfo]]


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
