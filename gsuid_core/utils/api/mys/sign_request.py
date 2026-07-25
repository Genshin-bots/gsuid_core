"""
米游社签到 API 请求模块。
"""

from copy import deepcopy
from typing import Dict, Union, cast

from .api import (
    SIGN,
    GS_BASE,
    SIGN_SR,
    SIGN_INFO,
    SIGN_LIST,
    SIGN_BASE_OS,
    SIGN_INFO_SR,
    SIGN_LIST_SR,
    MONTHLY_AWARD,
    SIGN_INFO_ZZZ,
    SIGN_SR_BASE_OS,
    ApiEndpoint,
)
from .tools import random_hex, generate_os_ds, get_web_ds_token
from .models import MysSign, SignInfo, SignList, MonthlyAward
from .bbs_request import BBSMysApi

_ACT_ID = {
    "gs": {
        "cn_gf01": "e202311201442471",
        "cn_qd01": "e202311201442471",
        "os_usa": "e202102251931481",
        "os_euro": "e202102251931481",
        "os_asia": "e202102251931481",
        "os_cht": "e202102251931481",
    },
    "sr": {
        "prod_gf_cn": "e202304121516551",
        "prod_qd_cn": "e202304121516551",
        "prod_official_usa": "e202303301540311",
        "prod_official_euro": "e202303301540311",
        "prod_official_asia": "e202303301540311",
        "prod_official_cht": "e202303301540311",
    },
    "zzz": {
        "prod_gf_cn": "e202406242138391",
        "prod_gf_us": "e202406031448091",
        "prod_gf_jp": "e202406031448091",
        "prod_gf_sg": "e202406031448091",
    },
}

_GAME_NAME = {
    "gs": "hk4e",
    "sr": "hkrpg",
    "zzz": "zzz",
}

_BASE_URL = {
    "gs": {"os": SIGN_BASE_OS, "cn": GS_BASE},
    "sr": {"os": SIGN_SR_BASE_OS, "cn": GS_BASE},
    "zzz": {"os": SIGN_BASE_OS, "cn": GS_BASE},
}

_SIGN_END_POINT: dict[str, ApiEndpoint] = {
    "gs": SIGN,
    "sr": SIGN_SR,
    "zzz": SIGN,
}

_SIGN_INFO_END_POINT: dict[str, ApiEndpoint] = {
    "gs": SIGN_INFO,
    "sr": SIGN_INFO_SR,
    "zzz": SIGN_INFO_ZZZ,
}

_SIGN_LIST_END_POINT: dict[str, ApiEndpoint] = {
    "gs": SIGN_LIST,
    "sr": SIGN_LIST_SR,
    "zzz": SIGN_LIST,
}


class SignMysApi(BBSMysApi):
    async def get_sign_list(
        self,
        uid: str,
        game_name: str = "gs",
        server_id: str = "cn_gf01",
    ) -> Union[SignList, int]:
        is_os = self.check_os(uid, game_name)
        base_url = _BASE_URL[game_name]["os" if is_os else "cn"]
        end_point = _SIGN_LIST_END_POINT[game_name].get(is_os)
        server_id = self.get_server_id(uid, game_name)
        act_id = _ACT_ID[game_name][server_id]
        ck = await self.get_ck(uid, "OWNER", game_name)
        if ck is None:
            return -51
        header = deepcopy(self._HEADER_OS) if is_os else {"Cookie": ck}
        header["Cookie"] = ck
        params = {"act_id": act_id, "lang": "zh-cn"}

        if is_os:
            header["DS"] = generate_os_ds()
            header["x-rpc-device_id"] = await self.get_user_device_id(uid, game_name)
            header["x-rpc-device_fp"] = await self.get_user_fp(uid, game_name)
        else:
            header["x-rpc-signgame"] = _GAME_NAME[game_name]

        data = await self._mys_request(
            end_point,
            "GET",
            header,
            params,
            base_url=base_url,
        )
        if isinstance(data, Dict):
            data = cast(SignList, data["data"])
        return data

    async def get_sign_info(
        self,
        uid: str,
        game_name: str = "gs",
    ) -> Union[SignInfo, int]:
        is_os = self.check_os(uid, game_name)
        server_id = self.get_server_id(uid, game_name)
        base_url = _BASE_URL[game_name]["os" if is_os else "cn"]
        end_point = _SIGN_INFO_END_POINT[game_name].get(is_os)
        ck = await self.get_ck(uid, "OWNER", game_name)
        if ck is None:
            return -51
        header = deepcopy(self._HEADER_OS) if is_os else {"Cookie": ck}
        header["Cookie"] = ck
        params = {
            "act_id": _ACT_ID[game_name][server_id],
            "lang": "zh-cn",
            "region": server_id,
            "uid": uid,
        }

        if is_os:
            header["DS"] = generate_os_ds()
            header["x-rpc-device_id"] = await self.get_user_device_id(uid, game_name)
            header["x-rpc-device_fp"] = await self.get_user_fp(uid, game_name)
        else:
            header["x-rpc-signgame"] = _GAME_NAME[game_name]

        data = await self._mys_request(end_point, "GET", header, params, base_url=base_url)

        if isinstance(data, Dict):
            data = cast(SignInfo, data["data"])
        return data

    async def mys_sign(
        self,
        uid: str,
        game_name: str = "gs",
        header: Dict = {},
    ) -> Union[MysSign, int]:
        is_os = self.check_os(uid, game_name)
        server_id = self.get_server_id(uid, game_name)
        base_url = _BASE_URL[game_name]["os" if is_os else "cn"]
        end_point = _SIGN_END_POINT[game_name].get(is_os)
        data = {
            "act_id": _ACT_ID[game_name][server_id],
            "lang": "zh-cn",
            "uid": uid,
            "region": server_id,
        }

        ck = await self.get_ck(uid, "OWNER", game_name)
        if ck is None:
            return -51

        if is_os:
            HEADER = deepcopy(self._HEADER_OS)
            HEADER["Cookie"] = ck
            HEADER["DS"] = generate_os_ds()
            HEADER.update(header)
        else:
            HEADER = deepcopy(self._HEADER)
            HEADER["Cookie"] = ck
            header["x-rpc-device_id"] = await self.get_user_device_id(uid)
            header["x-rpc-device_fp"] = await self.get_user_fp(uid)
            HEADER["x-rpc-client_type"] = "5"
            HEADER["X_Requested_With"] = "com.mihoyo.hyperion"
            HEADER["DS"] = get_web_ds_token(True)
            header["x-rpc-signgame"] = _GAME_NAME[game_name]
            HEADER.update(header)

        data = await self._mys_request(
            url=end_point,
            method="POST",
            header=HEADER,
            data=data,
            base_url=base_url,
        )

        if isinstance(data, Dict):
            data = cast(MysSign, data["data"])
        return data

    async def get_award(self, uid) -> Union[MonthlyAward, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])
        ck = await self.get_ck(uid, "OWNER")
        if ck is None:
            return -51
        if int(str(uid)[0]) < 6:
            HEADER = deepcopy(self._HEADER)
            HEADER["Cookie"] = ck
            HEADER["DS"] = get_web_ds_token(True)
            HEADER["x-rpc-device_id"] = random_hex(32)
            data = await self._mys_request(
                url=MONTHLY_AWARD.get(),
                method="GET",
                header=HEADER,
                params={
                    "act_id": "e202009291139501",
                    "bind_region": server_id,
                    "bind_uid": uid,
                    "month": "0",
                    "bbs_presentation_style": "fullscreen",
                    "bbs_auth_required": "true",
                    "utm_source": "bbs",
                    "utm_medium": "mys",
                    "utm_campaign": "icon",
                },
            )
        else:
            HEADER = deepcopy(self._HEADER_OS)
            HEADER["Cookie"] = ck
            HEADER["x-rpc-device_id"] = await self.get_user_device_id(uid, "gs")
            HEADER["x-rpc-device_fp"] = await self.get_user_fp(uid, "gs")
            HEADER["DS"] = generate_os_ds()
            data = await self._mys_request(
                url=MONTHLY_AWARD.get(True),
                method="GET",
                header=HEADER,
                params={
                    "act_id": "e202009291139501",
                    "region": server_id,
                    "uid": uid,
                    "month": "0",
                },
                use_proxy=True,
            )
        if isinstance(data, Dict):
            data = cast(MonthlyAward, data["data"])
        return data
