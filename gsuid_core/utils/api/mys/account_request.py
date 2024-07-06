'''
米游社账号相关操作 API 请求模块。
'''

import uuid
import random
from copy import deepcopy
from string import digits, ascii_letters
from typing import Dict, Union, Optional, cast

from aiohttp import ClientSession

from .pass_request import PassMysApi
from .models import (
    AuthKeyInfo,
    QrCodeStatus,
    GameTokenInfo,
    CookieTokenInfo,
    LoginTicketInfo,
)
from .tools import (
    random_hex,
    mys_version,
    random_text,
    get_web_ds_token,
    generate_passport_ds,
)


class AccountMysApi(PassMysApi):
    """
    账号相关请求
    """

    async def get_cookie_token(
        self, token: str, uid: str
    ) -> Union[CookieTokenInfo, int]:
        data = await self._mys_request(
            self.MAPI['GET_COOKIE_TOKEN_BY_GAME_TOKEN'],
            'GET',
            params={
                'game_token': token,
                'account_id': uid,
            },
        )
        if isinstance(data, Dict):
            data = cast(CookieTokenInfo, data['data'])
        return data

    async def create_qrcode_url(self) -> Union[Dict, int]:
        device_id: str = ''.join(random.choices(ascii_letters + digits, k=64))
        app_id: str = '8'
        data = await self._mys_request(
            self.MAPI['CREATE_QRCODE'],
            'POST',
            header={},
            data={'app_id': app_id, 'device': device_id},
        )
        if isinstance(data, Dict):
            url: str = data['data']['url']
            ticket = url.split('ticket=')[1]
            return {
                'app_id': app_id,
                'ticket': ticket,
                'device': device_id,
                'url': url,
            }
        return data

    async def check_qrcode(
        self, app_id: str, ticket: str, device: str
    ) -> Union[QrCodeStatus, int]:
        data = await self._mys_request(
            self.MAPI['CHECK_QRCODE'],
            'POST',
            data={
                'app_id': app_id,
                'ticket': ticket,
                'device': device,
            },
        )
        if isinstance(data, Dict):
            data = cast(QrCodeStatus, data['data'])
        return data

    async def get_cookie_token_by_game_token(
        self, token: str, uid: str
    ) -> Union[CookieTokenInfo, int]:
        data = await self._mys_request(
            self.MAPI['GET_COOKIE_TOKEN_BY_GAME_TOKEN'],
            'GET',
            params={
                'game_token': token,
                'account_id': uid,
            },
        )
        if isinstance(data, Dict):
            data = cast(CookieTokenInfo, data['data'])
        return data

    async def get_cookie_token_by_stoken(
        self, stoken: str, mys_id: str, full_sk: Optional[str] = None
    ) -> Union[CookieTokenInfo, int]:
        HEADER = deepcopy(self._HEADER)
        if full_sk:
            HEADER['Cookie'] = full_sk
        else:
            HEADER['Cookie'] = f'stuid={mys_id};stoken={stoken}'
        data = await self._mys_request(
            url=self.MAPI['GET_COOKIE_TOKEN_URL'],
            method='GET',
            header=HEADER,
            params={
                'stoken': stoken,
                'uid': mys_id,
            },
        )
        if isinstance(data, Dict):
            data = cast(CookieTokenInfo, data['data'])
        return data

    async def get_stoken_by_login_ticket(
        self, lt: str, mys_id: str
    ) -> Union[LoginTicketInfo, int]:
        data = await self._mys_request(
            url=self.MAPI['GET_STOKEN_URL'],
            method='GET',
            header=self._HEADER,
            params={
                'login_ticket': lt,
                'token_types': '3',
                'uid': mys_id,
            },
        )
        if isinstance(data, int):
            data = await self._mys_request(
                url=self.MAPI['GET_STOKEN_URL_OS'],
                method='GET',
                header=self._HEADER,
                params={
                    'login_ticket': lt,
                    'token_types': '3',
                    'uid': mys_id,
                },
            )
        if isinstance(data, Dict):
            data = cast(LoginTicketInfo, data['data'])
        return data

    async def get_stoken_by_game_token(
        self, account_id: int, game_token: str
    ) -> Union[GameTokenInfo, int]:
        _data = {
            'account_id': account_id,
            'game_token': game_token,
        }
        data = await self._mys_request(
            self.MAPI['GET_STOKEN'],
            'POST',
            {
                'x-rpc-app_version': '2.41.0',
                'DS': generate_passport_ds(b=_data),
                'x-rpc-aigis': '',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'x-rpc-game_biz': 'bbs_cn',
                'x-rpc-sys_version': '11',
                'x-rpc-device_id': uuid.uuid4().hex,
                'x-rpc-device_fp': ''.join(
                    random.choices(ascii_letters + digits, k=13)
                ),
                'x-rpc-device_name': 'GenshinUid_login_device_lulu',
                'x-rpc-device_model': 'GenshinUid_login_device_lulu',
                'x-rpc-app_id': 'bll8iq97cem8',
                'x-rpc-client_type': '2',
                'User-Agent': 'okhttp/4.8.0',
            },
            data=_data,
        )
        if isinstance(data, Dict):
            data = cast(GameTokenInfo, data['data'])
        return data

    async def get_authkey_by_cookie(
        self, uid: str, game_biz: str = 'hk4e_cn', server_id: str = ''
    ) -> Union[AuthKeyInfo, int]:
        if not server_id:
            server_id = self.RECOGNIZE_SERVER.get(str(uid)[0], 'cn_gf01')
        HEADER = deepcopy(self._HEADER)
        stoken = await self.get_stoken(uid)
        if stoken is None:
            return -51
        HEADER['Cookie'] = stoken
        HEADER['DS'] = get_web_ds_token(True)
        HEADER['User-Agent'] = 'okhttp/4.8.0'
        HEADER['x-rpc-app_version'] = mys_version
        HEADER['x-rpc-sys_version'] = '12'
        HEADER['x-rpc-client_type'] = '5'
        HEADER['x-rpc-channel'] = 'mihoyo'
        HEADER['x-rpc-device_id'] = random_hex(32)
        HEADER['x-rpc-device_name'] = random_text(random.randint(1, 10))
        HEADER['x-rpc-device_model'] = 'Mi 10'
        HEADER['Referer'] = 'https://app.mihoyo.com'
        HEADER['Host'] = 'api-takumi.mihoyo.com'
        data = await self._mys_request(
            url=self.MAPI['GET_AUTHKEY_URL'],
            method='POST',
            header=HEADER,
            data={
                'auth_appid': 'webview_gacha',
                'game_biz': game_biz,
                'game_uid': uid,
                'region': server_id,
            },
        )
        if isinstance(data, Dict):
            data = cast(AuthKeyInfo, data['data'])
        return data

    async def get_hk4e_token(self, uid: str):
        # 获取e_hk4e_token
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        header = {
            'Cookie': await self.get_ck(uid, 'OWNER'),
            'Content-Type': 'application/json;charset=UTF-8',
            'Referer': 'https://webstatic.mihoyo.com/',
            'Origin': 'https://webstatic.mihoyo.com',
        }
        use_proxy = False
        data = {
            'game_biz': 'hk4e_cn',
            'lang': 'zh-cn',
            'uid': f'{uid}',
            'region': f'{server_id}',
        }
        if int(str(uid)[0]) < 6:
            url = self.MAPI['HK4E_LOGIN_URL']
        else:
            url = self.MAPI['HK4E_LOGIN_URL_OS']
            data['game_biz'] = 'hk4e_global'
            use_proxy = True

        if use_proxy and self.Gproxy:
            proxy = self.Gproxy
        elif self.Nproxy and not use_proxy:
            proxy = self.Nproxy
        else:
            proxy = None

        async with ClientSession() as client:
            async with client.request(
                method='POST',
                url=url,
                headers=header,
                json=data,
                proxy=proxy,
                timeout=300,
            ) as resp:
                raw_data = await resp.json()
                if 'retcode' in raw_data and raw_data['retcode'] == 0:
                    _k = resp.cookies['e_hk4e_token'].key
                    _v = resp.cookies['e_hk4e_token'].value
                    ck = f'{_k}={_v}'
                    return ck
                else:
                    return None
