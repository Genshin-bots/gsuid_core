"""
米游社账号相关操作 API 请求模块。
"""

import uuid
import random
from copy import deepcopy
from string import digits, ascii_letters
from typing import Any, Dict, Union, Optional, cast
from http.cookies import SimpleCookie

from aiohttp import ClientSession, ClientTimeout

from .tools import (
    random_hex,
    mys_version,
    random_text,
    generate_os_ds,
    get_web_ds_token,
    generate_passport_ds,
)
from .models import (
    AuthKeyInfo,
    QrCodeStatus,
    GameTokenInfo,
    CookieTokenInfo,
    HypQrCodeStatus,
    LoginTicketInfo,
)
from .pass_request import PassMysApi


class AccountMysApi(PassMysApi):
    """
    账号相关请求
    """

    HYP_VERSION = "1.3.3.182"
    # 米游社全球化/国内化 proxy 配置: 实例可覆盖, 类型强制为 str|None 防止拼错键。
    Gproxy: Optional[str] = None
    Nproxy: Optional[str] = None

    @staticmethod
    def _hyp_qrcode_header(device_id: str) -> Dict[str, str]:
        return {
            "x-rpc-device_id": device_id,
            "User-Agent": f"HYPContainer/{AccountMysApi.HYP_VERSION}",
            "x-rpc-app_id": "ddxf5dufpuyo",
            "x-rpc-client_type": "3",
        }

    async def get_cookie_token(self, token: str, uid: str) -> Union[CookieTokenInfo, int]:
        data = await self._mys_request(
            self.MAPI["GET_COOKIE_TOKEN_BY_GAME_TOKEN"],
            "GET",
            params={
                "game_token": token,
                "account_id": uid,
            },
        )
        if isinstance(data, Dict):
            data = cast(CookieTokenInfo, data["data"])
        return data

    async def create_qrcode_url(self) -> Union[Dict, int]:
        device_id: str = "".join(random.choices(ascii_letters + digits, k=64))
        app_id: str = "2"
        data = await self._mys_request(
            self.MAPI["CREATE_QRCODE"],
            "POST",
            header={},
            data={"app_id": app_id, "device": device_id},
        )
        if isinstance(data, Dict):
            url: str = data["data"]["url"]
            ticket = url.split("ticket=")[1]
            return {
                "app_id": app_id,
                "ticket": ticket,
                "device": device_id,
                "url": url,
            }
        return data

    async def create_hyp_qrcode_url(self) -> Union[Dict, int]:
        device_id = uuid.uuid4().hex + uuid.uuid4().hex
        data = await self._mys_request(
            self.MAPI["CREATE_QRCODE_HYP"],
            "POST",
            header=self._hyp_qrcode_header(device_id),
            data={},
        )
        if isinstance(data, Dict):
            return {
                "ticket": data["data"]["ticket"],
                "url": data["data"]["url"],
                "device_id": device_id,
            }
        return data

    async def check_qrcode(self, app_id: str, ticket: str, device: str) -> Union[QrCodeStatus, int]:
        data = await self._mys_request(
            self.MAPI["CHECK_QRCODE"],
            "POST",
            data={
                "app_id": app_id,
                "ticket": ticket,
                "device": device,
            },
        )
        if isinstance(data, Dict):
            data = cast(QrCodeStatus, data["data"])
        return data

    async def check_hyp_qrcode(
        self,
        ticket: str,
        device_id: str,
    ) -> Union[HypQrCodeStatus, int]:
        data = await self._mys_request(
            self.MAPI["CHECK_QRCODE_HYP"],
            "POST",
            header=self._hyp_qrcode_header(device_id),
            data={"ticket": ticket},
        )
        if isinstance(data, Dict):
            data = cast(HypQrCodeStatus, data["data"])
        return data

    async def get_cookie_token_by_game_token(self, token: str, uid: str) -> Union[CookieTokenInfo, int]:
        data = await self._mys_request(
            self.MAPI["GET_COOKIE_TOKEN_BY_GAME_TOKEN"],
            "GET",
            params={
                "game_token": token,
                "account_id": uid,
            },
        )
        if isinstance(data, Dict):
            data = cast(CookieTokenInfo, data["data"])
        return data

    async def get_cookie_token_by_stoken(
        self,
        stoken: str,
        mys_id: str,
        full_sk: Optional[str] = None,
        is_os: bool = False,
    ) -> Union[CookieTokenInfo, int]:
        if is_os:
            return await self.get_all_token_by_stoken_os(stoken, mys_id, full_sk)

        HEADER = deepcopy(self._HEADER)
        params = {
            "stoken": stoken,
            "uid": mys_id,
        }
        if full_sk:
            HEADER["Cookie"] = full_sk
            simp_dict = SimpleCookie(full_sk)
            if not stoken:
                if "stoken" in simp_dict:
                    stoken = simp_dict["stoken"].value
                elif "stoken_v2" in simp_dict:
                    stoken = simp_dict["stoken_v2"].value
                params["stoken"] = stoken
            if "mid" in simp_dict:
                params["mid"] = simp_dict["mid"].value
        else:
            HEADER["Cookie"] = f"stuid={mys_id};stoken={stoken}"
        data = await self._mys_request(
            url=self.MAPI["GET_COOKIE_TOKEN_URL"],
            method="GET",
            header=HEADER,
            params=params,
        )
        if isinstance(data, Dict):
            data = cast(CookieTokenInfo, data["data"])
        return data

    async def get_all_token_by_stoken_os(
        self,
        stoken: str,
        mys_id: str,
        full_sk: Optional[str] = None,
    ) -> Union[CookieTokenInfo, int]:
        """国际服通过Stoken一次换取Stoken、LToken和CookieToken。"""
        header = deepcopy(self._HEADER_OS)
        header["x-rpc-app_id"] = "c9oqaq3s3gu8"
        header["DS"] = generate_os_ds()
        header["x-rpc-device_id"] = self.get_overseas_device_id(mys_id)
        header["x-rpc-device_fp"] = self.get_overseas_device_fp(mys_id)
        if full_sk:
            header["Cookie"] = full_sk
            simp_dict = SimpleCookie(full_sk)
            if not stoken:
                for key in ("stoken", "stoken_v2"):
                    if key in simp_dict:
                        stoken = simp_dict[key].value
                        break
        else:
            header["Cookie"] = f"stuid={mys_id};stoken={stoken}"

        data = await self._mys_request(
            url=self.MAPI["GET_ALL_TOKEN_BY_STOKEN_OS"],
            method="POST",
            header=header,
            data={"dst_token_types": [1, 2, 4]},
            use_proxy=True,
            game_name="account",
        )
        if not isinstance(data, Dict):
            return data

        raw_data: Dict[str, Any] = data.get("data") or {}
        token_map = {
            token.get("token_type"): token.get("token", "")
            for token in raw_data.get("tokens", [])
            if token.get("token_type") in {1, 2, 4}
        }
        stoken = token_map.get(1) or stoken
        ltoken = token_map.get(2, "")
        cookie_token = token_map.get(4, "")
        if not cookie_token:
            return -999

        mid = str((raw_data.get("user_info") or {}).get("mid") or "")
        cookies: Dict[str, str] = {
            "stuid": str(mys_id),
            "stoken": stoken,
        }
        if mid:
            cookies["mid"] = mid

        if ltoken:
            if ltoken.startswith("v2_"):
                cookies["ltoken_v2"] = ltoken
                cookies["ltuid_v2"] = str(mys_id)
                if mid:
                    cookies["ltmid_v2"] = mid
            else:
                cookies["ltoken"] = ltoken
                cookies["ltuid"] = str(mys_id)

        cookie_token_name = "cookie_token_v2" if cookie_token.startswith("v2_") else "cookie_token"
        cookies[cookie_token_name] = cookie_token
        if cookie_token_name == "cookie_token_v2":
            cookies["account_id_v2"] = str(mys_id)
            if mid:
                cookies["account_mid_v2"] = mid
        else:
            cookies["account_id"] = str(mys_id)

        return {
            "uid": str(mys_id),
            "cookie_token": cookie_token,
            "cookie_token_name": cookie_token_name,
            "cookies": cookies,
        }

    async def get_stoken_by_login_ticket(
        self,
        lt: str,
        mys_id: str,
        is_os: Optional[bool] = None,
    ) -> Union[LoginTicketInfo, int]:
        if is_os is True:
            urls = (self.MAPI["GET_STOKEN_URL_OS"],)
        elif is_os is False:
            urls = (self.MAPI["GET_STOKEN_URL"],)
        else:
            urls = (
                self.MAPI["GET_STOKEN_URL"],
                self.MAPI["GET_STOKEN_URL_OS"],
            )

        data: Union[Dict, int] = -999
        for url in urls:
            is_overseas_url = url == self.MAPI["GET_STOKEN_URL_OS"]
            header = deepcopy(self._HEADER_OS if is_overseas_url else self._HEADER)
            if is_overseas_url:
                header["DS"] = generate_os_ds()
            data = await self._mys_request(
                url=url,
                method="GET",
                header=header,
                params={
                    "login_ticket": lt,
                    "token_types": "3",
                    "uid": mys_id,
                },
                use_proxy=is_overseas_url,
                game_name="account",
            )
            if isinstance(data, Dict):
                break
        if isinstance(data, Dict):
            data = cast(LoginTicketInfo, data["data"])
        return data

    async def get_stoken_by_game_token(self, account_id: int, game_token: str) -> Union[GameTokenInfo, int]:
        _data = {
            "account_id": account_id,
            "game_token": game_token,
        }
        data = await self._mys_request(
            self.MAPI["GET_STOKEN"],
            "POST",
            {
                "x-rpc-app_version": "2.41.0",
                "DS": generate_passport_ds(b=_data),
                "x-rpc-aigis": "",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-rpc-game_biz": "bbs_cn",
                "x-rpc-sys_version": "11",
                "x-rpc-device_id": uuid.uuid4().hex,
                "x-rpc-device_fp": "".join(random.choices(ascii_letters + digits, k=13)),
                "x-rpc-device_name": "GenshinUid_login_device_lulu",
                "x-rpc-device_model": "GenshinUid_login_device_lulu",
                "x-rpc-app_id": "bll8iq97cem8",
                "x-rpc-client_type": "2",
                "User-Agent": "okhttp/4.8.0",
            },
            data=_data,
        )
        if isinstance(data, Dict):
            data = cast(GameTokenInfo, data["data"])
        return data

    async def get_authkey_by_cookie(
        self, uid: str, game_biz: str = "hk4e_cn", server_id: str = ""
    ) -> Union[AuthKeyInfo, int]:
        if not server_id:
            server_id = self.RECOGNIZE_SERVER.get(str(uid)[0], "cn_gf01")
        is_os = self.check_os(uid, "gs")
        if is_os:
            game_biz = "hk4e_global"
            HEADER = deepcopy(self._HEADER_OS)
            HEADER["DS"] = generate_os_ds()
            device_id = await self.get_user_device_id(uid, "gs")
            device_fp = await self.get_user_fp(uid, "gs")
            if device_id:
                HEADER["x-rpc-device_id"] = device_id
            if device_fp:
                HEADER["x-rpc-device_fp"] = device_fp
        else:
            HEADER = deepcopy(self._HEADER)
            HEADER["DS"] = get_web_ds_token(True)

        stoken = await self.get_stoken(uid)
        if stoken is None:
            return -51
        HEADER["Cookie"] = stoken
        if not is_os:
            HEADER["User-Agent"] = "okhttp/4.8.0"
            HEADER["x-rpc-app_version"] = mys_version
            HEADER["x-rpc-sys_version"] = "12"
            HEADER["x-rpc-client_type"] = "5"
            HEADER["x-rpc-channel"] = "mihoyo"
            HEADER["x-rpc-device_id"] = random_hex(32)
            HEADER["x-rpc-device_name"] = random_text(random.randint(1, 10))
            HEADER["x-rpc-device_model"] = "Mi 10"
            HEADER["Referer"] = "https://app.mihoyo.com"
            HEADER["Host"] = "api-takumi.mihoyo.com"
        data = await self._mys_request(
            url=self.MAPI["GET_AUTHKEY_URL_OS"] if is_os else self.MAPI["GET_AUTHKEY_URL"],
            method="POST",
            header=HEADER,
            data={
                "auth_appid": "webview_gacha",
                "game_biz": game_biz,
                "game_uid": uid,
                "region": server_id,
            },
            use_proxy=is_os,
            game_name="gs",
        )
        if isinstance(data, Dict):
            data = cast(AuthKeyInfo, data["data"])
        return data

    async def get_hk4e_token(self, uid: str):
        # 获取e_hk4e_token
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        header = {
            "Cookie": await self.get_ck(uid, "OWNER", None),
            "Content-Type": "application/json;charset=UTF-8",
            "Referer": "https://webstatic.mihoyo.com/",
            "Origin": "https://webstatic.mihoyo.com",
        }
        use_proxy = False
        data = {
            "game_biz": "hk4e_cn",
            "lang": "zh-cn",
            "uid": f"{uid}",
            "region": f"{server_id}",
        }
        if int(str(uid)[0]) < 6:
            url = self.MAPI["HK4E_LOGIN_URL"]
        else:
            url = self.MAPI["HK4E_LOGIN_URL_OS"]
            data["game_biz"] = "hk4e_global"
            header.update(deepcopy(self._HEADER_OS))
            header["DS"] = generate_os_ds()
            device_id = await self.get_user_device_id(uid, "gs")
            device_fp = await self.get_user_fp(uid, "gs")
            if device_id:
                header["x-rpc-device_id"] = device_id
            if device_fp:
                header["x-rpc-device_fp"] = device_fp
            use_proxy = True

        if use_proxy and self.Gproxy:
            proxy = self.Gproxy
        elif self.Nproxy and not use_proxy:
            proxy = self.Nproxy
        else:
            proxy = None

        async with ClientSession() as client:
            async with client.request(
                method="POST",
                url=url,
                headers=header,
                json=data,
                proxy=proxy,
                timeout=ClientTimeout(total=300),
            ) as resp:
                raw_data = await resp.json()
                if "retcode" in raw_data and raw_data["retcode"] == 0:
                    _k = resp.cookies["e_hk4e_token"].key
                    _v = resp.cookies["e_hk4e_token"].value
                    ck = f"{_k}={_v}"
                    return ck
                else:
                    return None
