"""
米游社 API 请求模块。
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Dict, List, Union, Literal, Optional, cast
from datetime import datetime, timedelta

from gsuid_core.utils.cache import gs_cache

from .api import (
    ACHI,
    COMPUTE,
    BS_INDEX,
    GCG_DECK,
    GCG_INFO,
    DAILY_NOTE,
    CHAR_DETAIL,
    PLAYER_INFO,
    SEASON_POST,
    ACT_CALENDAR,
    POETRY_ABYSS,
    WIDGET_RESIN,
    GET_GACHA_LOG,
    CALCULATE_INFO,
    HARD_CHALLENGE,
    PLAYER_ABYSS_INFO,
    PLAYER_DETAIL_INFO,
    PLAYER_CHARACTER_LIST,
    MIHOYO_BBS_PLAYER_INFO,
)
from .tools import generate_os_ds
from .models import (
    BsIndex,
    GcgInfo,
    MysGame,
    GachaLog,
    AbyssData,
    Character,
    IndexData,
    ComputeData,
    GcgDeckInfo,
    WidgetResin,
    CalendarData,
    CalculateInfo,
    DailyNoteData,
    CharDetailData,
    SeasonPostData,
    AchievementData,
    PoetryAbyssDatas,
    HardChallengeData,
)
from .sign_request import SignMysApi


class MysApi(SignMysApi):
    # ------------------------------------------------------------------
    # 基础战绩
    # ------------------------------------------------------------------

    @gs_cache(360)
    async def get_info(self, uid, ck: Optional[str] = None) -> Union[IndexData, int]:
        """角色概览 index。"""
        data = await self.simple_mys_req(PLAYER_INFO, uid, cookie=ck, game_name="gs")
        if isinstance(data, Dict):
            data = cast(IndexData, data["data"])
        return data

    async def get_daily_data(self, uid: str) -> Union[DailyNoteData, int]:
        data = await self.simple_mys_req(DAILY_NOTE, uid, game_name="gs")
        if isinstance(data, Dict):
            data = cast(DailyNoteData, data["data"])
        return data

    async def get_gcg_info(self, uid: str) -> Union[GcgInfo, int]:
        data = await self.simple_mys_req(GCG_INFO, uid, game_name="gs")
        if isinstance(data, Dict):
            data = cast(GcgInfo, data["data"])
        return data

    async def get_gcg_deck(self, uid: str) -> Union[GcgDeckInfo, int]:
        data = await self.simple_mys_req(GCG_DECK, uid, game_name="gs")
        if isinstance(data, Dict):
            data = cast(GcgDeckInfo, data["data"])
        return data

    async def get_bs_index(self, uid: str) -> Union[int, BsIndex]:
        server_id = self.get_server_id(uid, "gs")
        ck = await self.get_ck(uid, "OWNER")
        if ck is None:
            return -51
        hk4e_token = await self.get_hk4e_token(uid)
        header = {"Cookie": f"{ck};{hk4e_token}"}
        data = await self._mys_request(
            BS_INDEX.get(),
            "GET",
            header,
            {
                "lang": "zh-cn",
                "badge_uid": uid,
                "badge_region": server_id,
                "game_biz": "hk4e_cn",
                "activity_id": 20220301153521,
            },
        )
        if isinstance(data, Dict):
            return cast(BsIndex, data["data"])
        return data

    @gs_cache(3600)
    async def get_achievement_info(self, uid: str) -> Union[List[AchievementData], int]:
        server_id = self.get_server_id(uid, "gs")
        data = await self.endpoint_request(
            ACHI,
            uid,
            method="POST",
            data={"role_id": uid, "server": server_id},
            ds_q="",
            ds_body={"role_id": uid, "server": server_id},
        )
        if isinstance(data, Dict):
            if "retcode" in data:
                if data["retcode"] == 0:
                    data = cast(List[AchievementData], data["data"]["list"])
                else:
                    data = cast(int, data["retcode"])
            else:
                data = -999
        return data

    @gs_cache(360)
    async def get_spiral_abyss_info(
        self, uid: str, schedule_type="1", ck: Optional[str] = None
    ) -> Union[AbyssData, int]:
        server_id = self.get_server_id(uid, "gs")
        data = await self.simple_mys_req(
            PLAYER_ABYSS_INFO,
            uid,
            {
                "role_id": uid,
                "schedule_type": schedule_type,
                "server": server_id,
            },
            cookie=ck,
            game_name="gs",
        )
        if isinstance(data, Dict):
            data = cast(AbyssData, data["data"])
        return data

    @gs_cache(360)
    async def get_poetry_abyss_data(
        self,
        uid: str,
        active: Optional[int] = None,
    ) -> Union[PoetryAbyssDatas, int]:
        server_id = self.get_server_id(uid, "gs")
        params: Dict = {
            "server": server_id,
            "role_id": uid,
            "need_detail": True,
        }
        if active:
            params["active"] = active
        data = await self.endpoint_request(POETRY_ABYSS, uid, params=params)
        if isinstance(data, Dict):
            data = cast(PoetryAbyssDatas, data["data"])
        return data

    # ------------------------------------------------------------------
    # 角色：三套语义
    #   get_character_list   — 国际服 character/list 原始载荷
    #   get_character_detail — character/detail 嵌套 Character 列表
    #   get_character        — 扁平列表（国服 list / 国际服 list 归一化，供面板绘图）
    # ------------------------------------------------------------------

    async def _request_character_list(
        self,
        uid: str,
        ck: str,
    ) -> Union[Dict, int]:
        """国际服 character/list（可能直接返回嵌套 detail 结构）。"""
        server_id = self.get_server_id(uid, "gs")
        data = await self.endpoint_request(
            PLAYER_CHARACTER_LIST,
            uid,
            method="POST",
            data={"role_id": uid, "server": server_id},
            cookie=ck,
            ds_mode="auto",
            ds_q="",
            ds_body={"role_id": uid, "server": server_id},
        )
        # endpoint_request 对 OS 才有 PLAYER_CHARACTER_LIST；国服 has_cn=False
        # 调用方应仅在 is_os 时使用。若误调国服会 ValueError。
        if isinstance(data, Dict):
            data = cast(Dict, data["data"])
        return data

    @gs_cache(360)
    async def get_character_list(
        self,
        uid: str,
        mode: Literal["OWNER", "RANDOM"] = "RANDOM",
    ) -> Union[Dict, int]:
        """获取国际服 character/list 原始 data（list 项可能是嵌套 detail）。"""
        ck = await self.get_ck(uid, mode)
        if ck is None:
            return -51
        return await self._request_character_list(uid, ck)

    @staticmethod
    def _normalize_character_list(data: Dict) -> CharDetailData:
        """将国际服嵌套角色详情转换为国服扁平 list 结构。"""
        characters = []
        for raw_character in data.get("list", []):
            base = raw_character.get("base")
            if not isinstance(base, Dict):
                characters.append(raw_character)
                continue

            character = dict(base)
            character["weapon"] = raw_character.get("weapon") or base.get("weapon", {})
            character["reliquaries"] = raw_character.get("relics", [])
            character["constellations"] = raw_character.get("constellations", [])
            character["costumes"] = raw_character.get("costumes", [])
            character["card_image"] = base.get("image", "")
            characters.append(character)
        return {"list": characters}

    @gs_cache(360)
    async def get_character(
        self, uid: str, character_ids: List[int], ck: Union[str, None] = None
    ) -> Union[CharDetailData, int]:
        """扁平角色详情列表（国服 character/list；国际服用 list 归一化）。

        供角色面板等需要国服风格扁平结构的场景。
        """
        server_id = self.get_server_id(uid, "gs")

        if ck is None:
            ck = await self.get_ck(uid)
            if ck is None:
                return -51

        if not self.check_os(uid, "gs"):
            body = {
                "character_ids": character_ids,
                "role_id": uid,
                "server": server_id,
            }
            data = await self.endpoint_request(
                PLAYER_DETAIL_INFO,
                uid,
                method="POST",
                data=body,
                cookie=ck,
                ds_q="",
                ds_body=body,
            )
            if isinstance(data, Dict):
                data = cast(CharDetailData, data["data"])
            return data

        # 国际服：list 归一化为扁平结构
        data = await self._request_character_list(uid, ck)
        if isinstance(data, Dict):
            return self._normalize_character_list(data)
        return data

    @gs_cache(360)
    async def get_character_detail(
        self,
        uid: str,
        char_id_list: List[int],
    ) -> Union[List[Character], int]:
        """character/detail 嵌套角色详情列表（含 base/weapon/relics/skills）。"""
        server_id = self.get_server_id(uid, "gs")
        body = {
            "role_id": uid,
            "server": server_id,
            "character_ids": char_id_list,
        }
        data = await self.endpoint_request(
            CHAR_DETAIL,
            uid,
            method="POST",
            data=body,
            ds_q="",
            ds_body=body,
        )
        if isinstance(data, Dict):
            data = cast(List[Character], data["data"]["list"])
        return data

    @gs_cache(360)
    async def get_calculate_info(self, uid, char_id: int) -> Union[CalculateInfo, int]:
        server_id = self.get_server_id(uid, "gs")
        data = await self.simple_mys_req(
            CALCULATE_INFO,
            uid,
            {"avatar_id": char_id, "uid": uid, "region": server_id},
            game_name="gs",
        )
        if isinstance(data, Dict):
            data = cast(CalculateInfo, data["data"])
        return data

    @gs_cache(3600)
    async def get_batch_compute_info(
        self, uid: str, items: Union[List[Dict], List[str], List[int]]
    ) -> Union[ComputeData, int]:
        if not items:
            return -200

        server_id = self.get_server_id(uid, "gs")
        body = {
            "items": items,
            "region": server_id,
            "uid": uid,
        }
        raw_data = await self.endpoint_request(
            COMPUTE,
            uid,
            method="POST",
            data=body,
            ds_q="",
            ds_body=body,
        )
        if isinstance(raw_data, Dict):
            raw_data = cast(ComputeData, raw_data["data"])
        return raw_data

    @gs_cache(360)
    async def get_mihoyo_bbs_info(
        self,
        mys_id: str,
        cookie: Optional[str] = None,
        is_os: bool = False,
    ) -> Union[List[MysGame], int]:
        if not cookie:
            cookie = await self.get_ck(mys_id, "OWNER")
        data = await self.simple_mys_req(
            MIHOYO_BBS_PLAYER_INFO,
            is_os,
            {"uid": mys_id},
            {"Cookie": cookie},
            game_name="account",
        )
        if isinstance(data, Dict):
            data = cast(List[MysGame], data["data"]["list"])
        return data

    async def get_gacha_log_by_authkey(
        self,
        uid: str,
        gacha_type: str = "301",
        page: int = 1,
        end_id: str = "0",
    ) -> Union[int, GachaLog]:
        is_os = self.check_os(uid, "gs")
        server_id = self.get_server_id(uid, "gs")
        authkey_rawdata = await self.get_authkey_by_cookie(uid)
        if isinstance(authkey_rawdata, int):
            return authkey_rawdata
        authkey = authkey_rawdata["authkey"]
        header = deepcopy(self._HEADER_OS if is_os else self._HEADER)
        if is_os:
            header["DS"] = generate_os_ds()
        data = await self._mys_request(
            url=GET_GACHA_LOG.get(is_os),
            method="GET",
            header=header,
            params={
                "authkey_ver": "1",
                "sign_type": "2",
                "auth_appid": "webview_gacha",
                "init_type": gacha_type,
                "gacha_id": "fecafa7b6560db5f3182222395d88aaa6aaac1bc",
                "timestamp": str(int(time.time())),
                "lang": "zh-cn",
                "device_type": "mobile",
                "plat_type": "ios",
                "region": server_id,
                "authkey": authkey,
                "game_biz": "hk4e_global" if is_os else "hk4e_cn",
                "gacha_type": gacha_type,
                "page": page,
                "size": "20",
                "end_id": end_id,
            },
            use_proxy=is_os,
            game_name="gs",
        )
        if isinstance(data, Dict):
            data = cast(GachaLog, data["data"])
        return data

    # ------------------------------------------------------------------
    # 扩展战绩（自 GenshinUID 上浮）
    # ------------------------------------------------------------------

    @gs_cache(3600)
    async def get_season_post_data(self, uid: str) -> Union[SeasonPostData, int]:
        server_id = self.get_server_id(uid, "gs")
        now = datetime.now()
        now_90 = now - timedelta(days=90)
        data = await self.endpoint_request(
            SEASON_POST,
            uid,
            params={
                "role_id": uid,
                "server": server_id,
                "year": str(now_90.year),
                "month": str(now_90.month),
                "day": str(now_90.day),
            },
        )
        if isinstance(data, Dict):
            data = cast(SeasonPostData, data["data"])
        return data

    @gs_cache(360)
    async def get_hard_challenge_data(self, uid: str) -> Union[HardChallengeData, int]:
        server_id = self.get_server_id(uid, "gs")
        body = {
            "role_id": uid,
            "server": server_id,
            "need_detail": True,
        }
        data = await self.endpoint_request(
            HARD_CHALLENGE,
            uid,
            method="GET",
            data=body,
            ds_q="",
            ds_body=body,
        )
        if isinstance(data, Dict):
            data = cast(HardChallengeData, data["data"])
        return data

    @gs_cache(300)
    async def get_calendar_data(self, uid: str) -> Union[CalendarData, int]:
        server_id = self.get_server_id(uid, "gs")
        body = {"role_id": uid, "server": server_id}
        data = await self.endpoint_request(
            ACT_CALENDAR,
            uid,
            method="POST",
            data=body,
            ds_q="",
            ds_body=body,
        )
        if isinstance(data, Dict):
            data = cast(CalendarData, data["data"])
        return data

    @gs_cache(300)
    async def get_widget_resin_data(self, uid: str) -> Union[WidgetResin, int]:
        data = await self.endpoint_request(
            WIDGET_RESIN,
            uid,
            params={"game_id": 2},
            cookie_type="stoken",
            ds_mode="web",
            header={"x-rpc-channel": "miyousheluodi"},
        )
        if isinstance(data, Dict):
            data = cast(WidgetResin, data["data"])
        return data
