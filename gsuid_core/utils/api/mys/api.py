# flake8: noqa
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ApiEndpoint:
    """CN / OS 成对 API 地址。用 ``.get(is_os)`` 解析，IDE 可对常量跳转。"""

    cn: str = ""
    os: str = ""
    name: str = ""

    def get(self, is_os: bool = False) -> str:
        url = self.os if is_os else self.cn
        if not url:
            region = "os" if is_os else "cn"
            label = self.name or "<unnamed>"
            raise ValueError(f"endpoint {label} has no {region} URL")
        return url

    @property
    def has_os(self) -> bool:
        return bool(self.os)

    @property
    def has_cn(self) -> bool:
        return bool(self.cn)

    def format(self, *args, **kwargs) -> str:
        return self.get(False).format(*args, **kwargs)

    def __str__(self) -> str:
        return self.cn or self.os


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------
GS_BASE = "https://api-takumi.mihoyo.com"
ZZZ_BASE = "https://act-nap-api.mihoyo.com"
RECORD_BASE = "https://api-takumi-record.mihoyo.com"
BBS_URL = "https://bbs-api.mihoyo.com"
HK4_URL = "https://hk4e-api.mihoyo.com"
NEW_BBS_URL = "https://bbs-api.miyoushe.com"

ACCOUNT_URL_OS = "https://api-account-sg.hoyoverse.com"
PASSPORT_MA_URL_OS = "https://passport-api-sg.hoyoverse.com/account/ma-passport"
PUBLIC_API_OS = "https://sg-public-api.hoyoverse.com"
GS_BASE_OS = "https://api-os-takumi.mihoyo.com"
RECORD_BASE_OS = "https://bbs-api-os.hoyolab.com"
BBS_URL_OS = "https://bbs-api-os.hoyolab.com"
HK4_URL_OS = "https://hk4e-api-os.hoyoverse.com"
SIGN_BASE_OS = "https://sg-hk4e-api.hoyolab.com"
SIGN_SR_BASE_OS = "https://sg-public-api.hoyolab.com"
ACT_URL_OS = "https://sg-hk4e-api.hoyoverse.com"

PASSPORT_URL = "https://passport-api.mihoyo.com"
HK4_SDK_URL = "https://hk4e-sdk.mihoyo.com"

# ---------------------------------------------------------------------------
# Geetest
# ---------------------------------------------------------------------------
GT_TEST = "https://api.geetest.com/ajax.php?"
GT_TEST_V6 = "https://apiv6.geetest.com/ajax.php?"
GT_QUERY = "gt={}&challenge={}&lang=zh-cn&pt=3&client_type=web_mobile"
GT_TEST_URL = GT_TEST + GT_QUERY
GT_TEST_URL_V6 = GT_TEST_V6 + GT_QUERY
GT_TPYE_URL = "https://api.geetest.com/gettype.php?gt={}"

# ---------------------------------------------------------------------------
# Account / login
# ---------------------------------------------------------------------------
HK4E_LOGIN = ApiEndpoint(
    cn=f"{GS_BASE}/common/badge/v1/login/account",
    os=f"{PUBLIC_API_OS}/common/badge/v1/login/account",
    name="HK4E_LOGIN",
)

VERIFICATION = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/card/wapi/createVerification?is_high=false",
    name="VERIFICATION",
)
BBS_VERIFICATION = ApiEndpoint(
    cn=f"{NEW_BBS_URL}/misc/api/createVerification?is_high=true",
    name="BBS_VERIFICATION",
)
VERIFY = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/card/wapi/verifyVerification",
    name="VERIFY",
)
BBS_VERIFY = ApiEndpoint(
    cn=f"{NEW_BBS_URL}/misc/api/verifyVerification",
    name="BBS_VERIFY",
)

GET_STOKEN_BY_LOGIN_TICKET = ApiEndpoint(
    cn=f"{GS_BASE}/auth/api/getMultiTokenByLoginTicket",
    os=f"{GS_BASE_OS}/auth/api/getMultiTokenByLoginTicket",
    name="GET_STOKEN_BY_LOGIN_TICKET",
)
GET_COOKIE_TOKEN = ApiEndpoint(
    cn=f"{PASSPORT_URL}/account/auth/api/getCookieAccountInfoBySToken",
    os=f"{ACCOUNT_URL_OS}/account/auth/api/getCookieAccountInfoBySToken",
    name="GET_COOKIE_TOKEN",
)
GET_ALL_TOKEN_BY_STOKEN = ApiEndpoint(
    os=f"{PUBLIC_API_OS}/account/ma-passport/token/getBySToken",
    name="GET_ALL_TOKEN_BY_STOKEN",
)
GET_AUTHKEY = ApiEndpoint(
    cn=f"{GS_BASE}/binding/api/genAuthKey",
    os=f"{PUBLIC_API_OS}/binding/api/genAuthKey",
    name="GET_AUTHKEY",
)
GET_GACHA_LOG = ApiEndpoint(
    cn="https://public-operation-hk4e.mihoyo.com/gacha_info/api/getGachaLog",
    os="https://public-operation-hk4e-sg.hoyoverse.com/gacha_info/api/getGachaLog",
    name="GET_GACHA_LOG",
)
GET_STOKEN_BY_GAME_TOKEN = ApiEndpoint(
    cn=f"{PASSPORT_URL}/account/ma-cn-session/app/getTokenByGameToken",
    name="GET_STOKEN_BY_GAME_TOKEN",
)
CREATE_QRCODE = ApiEndpoint(
    cn=f"{HK4_SDK_URL}/hk4e_cn/combo/panda/qrcode/fetch",
    name="CREATE_QRCODE",
)
CHECK_QRCODE = ApiEndpoint(
    cn=f"{HK4_SDK_URL}/hk4e_cn/combo/panda/qrcode/query",
    name="CHECK_QRCODE",
)
CREATE_QRCODE_HYP = ApiEndpoint(
    cn=f"{PASSPORT_URL}/account/ma-cn-passport/app/createQRLogin",
    name="CREATE_QRCODE_HYP",
)
CHECK_QRCODE_HYP = ApiEndpoint(
    cn=f"{PASSPORT_URL}/account/ma-cn-passport/app/queryQRLoginStatus",
    name="CHECK_QRCODE_HYP",
)
GET_COOKIE_TOKEN_BY_GAME_TOKEN = ApiEndpoint(
    cn=f"{GS_BASE}/auth/api/getCookieAccountInfoByGameToken",
    name="GET_COOKIE_TOKEN_BY_GAME_TOKEN",
)

# ---------------------------------------------------------------------------
# Sign paths (relative to sign base host)
# ---------------------------------------------------------------------------
SIGN_LIST = ApiEndpoint(cn="/event/luna/home", os="/event/sol/home", name="SIGN_LIST")
SIGN_INFO = ApiEndpoint(cn="/event/luna/info", os="/event/sol/info", name="SIGN_INFO")
SIGN = ApiEndpoint(cn="/event/luna/sign", os="/event/sol/sign", name="SIGN")
SIGN_LIST_SR = ApiEndpoint(cn="/event/luna/home", os="/event/luna/os/home", name="SIGN_LIST_SR")
SIGN_INFO_SR = ApiEndpoint(cn="/event/luna/info", os="/event/luna/os/info", name="SIGN_INFO_SR")
SIGN_SR = ApiEndpoint(cn="/event/luna/sign", os="/event/luna/os/sign", name="SIGN_SR")
SIGN_INFO_ZZZ = ApiEndpoint(
    cn="/event/luna/zzz/info",
    os="/event/sol/info",
    name="SIGN_INFO_ZZZ",
)

# ---------------------------------------------------------------------------
# Genshin
# ---------------------------------------------------------------------------
DAILY_NOTE = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/dailyNote",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/dailyNote",
    name="DAILY_NOTE",
)
MONTHLY_AWARD = ApiEndpoint(
    cn=f"{HK4_URL}/event/ys_ledger/monthInfo",
    os=f"{SIGN_BASE_OS}/event/ysledgeros/month_info",
    name="MONTHLY_AWARD",
)
PLAYER_INFO = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/index",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/index",
    name="PLAYER_INFO",
)
PLAYER_ABYSS_INFO = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/spiralAbyss",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/spiralAbyss",
    name="PLAYER_ABYSS_INFO",
)
PLAYER_DETAIL_INFO = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/character/list",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/character/detail",
    name="PLAYER_DETAIL_INFO",
)
PLAYER_CHARACTER_LIST = ApiEndpoint(
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/character/list",
    name="PLAYER_CHARACTER_LIST",
)
CALCULATE_INFO = ApiEndpoint(
    cn=f"{GS_BASE}/event/e20200928calculate/v1/sync/avatar/detail",
    os="https://sg-public-api.hoyolab.com/event/e20200928calculate/v1/sync/avatar/detail",
    name="CALCULATE_INFO",
)
COMPUTE = ApiEndpoint(
    cn=f"{GS_BASE}/event/e20200928calculate/v3/batch_compute",
    os="https://sg-public-api.hoyolab.com/event/e20200928calculate/v3/batch_compute",
    name="COMPUTE",
)
POETRY_ABYSS = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/role_combat",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/role_combat",
    name="POETRY_ABYSS",
)
WIDGET_RESIN = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/genshin/aapi/widget/v2",
    os=f"{RECORD_BASE_OS}/game_record/genshin/aapi/widget/v2",
    name="WIDGET_RESIN",
)
ACT_CALENDAR = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/act_calendar",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/act_calendar",
    name="ACT_CALENDAR",
)
HARD_CHALLENGE = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/hard_challenge",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/hard_challenge",
    name="HARD_CHALLENGE",
)
SEASON_POST = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/genshin/wapi/query_tool",
    os=f"{RECORD_BASE_OS}/game_record/genshin/wapi/query_tool",
    name="SEASON_POST",
)
CHAR_DETAIL = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/character/detail",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/character/detail",
    name="CHAR_DETAIL",
)
ACHI = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/achievement",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/achievement",
    name="ACHI",
)
MIHOYO_BBS_PLAYER_INFO = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/card/wapi/getGameRecordCard",
    os=f"{RECORD_BASE_OS}/game_record/card/wapi/getGameRecordCard",
    name="MIHOYO_BBS_PLAYER_INFO",
)
GCG_INFO = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/gcg/basicInfo",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/gcg/basicInfo",
    name="GCG_INFO",
)
GCG_DECK = ApiEndpoint(
    cn=f"{RECORD_BASE}/game_record/app/genshin/api/gcg/deckList",
    os=f"{RECORD_BASE_OS}/game_record/genshin/api/gcg/deckList",
    name="GCG_DECK",
)
REG_TIME = ApiEndpoint(
    cn=f"{HK4_URL}/event/e20220928anniversary/game_data?",
    os=f"{ACT_URL_OS}/event/e20220928anniversary/game_data?",
    name="REG_TIME",
)

# ---------------------------------------------------------------------------
# BBS
# ---------------------------------------------------------------------------
BBS_TASKS = ApiEndpoint(
    cn=f"{BBS_URL}/apihub/sapi/getUserMissionsState",
    name="BBS_TASKS",
)
BBS_SIGN = ApiEndpoint(
    cn=f"{BBS_URL}/apihub/app/api/signIn",
    name="BBS_SIGN",
)
BBS_LIST = ApiEndpoint(
    cn=BBS_URL + "/post/api/getForumPostList?forum_id={}&is_good=false&is_hot=false&page_size=20&sort_type=1",
    name="BBS_LIST",
)
BBS_COLLECTION = ApiEndpoint(
    cn=BBS_URL + "/post/wapi/getPostFullInCollection",
    name="BBS_COLLECTION",
)
BBS_DETAIL = ApiEndpoint(
    cn=BBS_URL + "/post/api/getPostFull?post_id={}",
    name="BBS_DETAIL",
)
BBS_SHARE = ApiEndpoint(
    cn=BBS_URL + "/apihub/api/getShareConf?entity_id={}&entity_type=1",
    name="BBS_SHARE",
)
BBS_LIKE = ApiEndpoint(
    cn=f"{BBS_URL}/apihub/sapi/upvotePost",
    name="BBS_LIKE",
)

# ---------------------------------------------------------------------------
# Top-up
# ---------------------------------------------------------------------------
FETCH_GOODS = ApiEndpoint(
    cn=f"{HK4_SDK_URL}/hk4e_cn/mdk/shopwindow/shopwindow/fetchGoods",
    name="FETCH_GOODS",
)
CREATE_ORDER = ApiEndpoint(
    cn=f"{HK4_SDK_URL}/hk4e_cn/mdk/atropos/api/createOrder",
    name="CREATE_ORDER",
)
CHECK_ORDER = ApiEndpoint(
    cn=f"{HK4_SDK_URL}/hk4e_cn/mdk/atropos/api/checkOrder",
    name="CHECK_ORDER",
)
PRICE_TIER = ApiEndpoint(
    cn=f"{HK4_SDK_URL}/hk4e_cn/mdk/shopwindow/shopwindow/listPriceTier",
    name="PRICE_TIER",
)

# ---------------------------------------------------------------------------
# Birthday star / device
# ---------------------------------------------------------------------------
DRAW_BASE = f"{HK4_URL}/event/birthdaystar/account"
CALENDAR = ApiEndpoint(cn=f"{DRAW_BASE}/calendar", name="CALENDAR")
RECEIVE = ApiEndpoint(cn=f"{DRAW_BASE}/post_my_draw", name="RECEIVE")
BS_INDEX = ApiEndpoint(cn=f"{DRAW_BASE}/index", name="BS_INDEX")

GET_FP = ApiEndpoint(
    cn="https://public-data-api.mihoyo.com/device-fp/api/getFp",
    name="GET_FP",
)
DEVICE_LOGIN = ApiEndpoint(
    cn=f"{NEW_BBS_URL}/apihub/api/deviceLogin",
    name="DEVICE_LOGIN",
)
SAVE_DEVICE = ApiEndpoint(
    cn=f"{NEW_BBS_URL}/apihub/api/saveDevice",
    name="SAVE_DEVICE",
)
