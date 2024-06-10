'''
米游社签到 API 请求模块。
'''

from copy import deepcopy
from typing import Dict, Union, cast

from .bbs_request import BBSMysApi
from .models import MysSign, SignInfo, SignList, MonthlyAward
from .tools import random_hex, mys_version, generate_os_ds, get_web_ds_token


class SignMysApi(BBSMysApi):
    async def get_sign_list(self, uid) -> Union[SignList, int]:
        is_os = self.check_os(uid)
        if is_os:
            params = {
                'act_id': 'e202102251931481',
                'lang': 'zh-cn',
            }
            header = {}
        else:
            params = {'act_id': 'e202311201442471'}
            header = {
                'x-rpc-signgame': 'hk4e',
            }
        data = await self._mys_req_get('SIGN_LIST_URL', is_os, params, header)
        if isinstance(data, Dict):
            data = cast(SignList, data['data'])
        return data

    async def get_sign_info(self, uid) -> Union[SignInfo, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])
        is_os = self.check_os(uid)
        if is_os:
            params = {
                'act_id': 'e202102251931481',
                'lang': 'zh-cn',
                'region': server_id,
                'uid': uid,
            }
            header = {
                'DS': generate_os_ds(),
            }
        else:
            params = {
                'act_id': 'e202311201442471',
                'region': server_id,
                'uid': uid,
            }
            header = {
                'x-rpc-signgame': 'hk4e',
            }
        data = await self._mys_req_get('SIGN_INFO_URL', is_os, params, header)
        if isinstance(data, Dict):
            data = cast(SignInfo, data['data'])
        return data

    async def mys_sign(
        self, uid, header={}, server_id='cn_gf01'
    ) -> Union[MysSign, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        if int(str(uid)[0]) < 6:
            HEADER = deepcopy(self._HEADER)
            HEADER['Cookie'] = ck
            HEADER['x-rpc-app_version'] = mys_version
            header['x-rpc-device_id'] = await self.get_user_device_id(uid)
            header['x-rpc-device_fp'] = await self.get_user_fp(uid)
            HEADER['x-rpc-client_type'] = '5'
            HEADER['X_Requested_With'] = 'com.mihoyo.hyperion'
            HEADER['DS'] = get_web_ds_token(True)
            HEADER['Referer'] = (
                'https://webstatic.mihoyo.com/bbs/event/signin-ys/index.html'
                '?bbs_auth_required=true&act_id=e202009291139501'
                '&utm_source=bbs&utm_medium=mys&utm_campaign=icon'
            )
            header['x-rpc-signgame'] = 'hk4e'
            HEADER.update(header)
            data = await self._mys_request(
                url=self.MAPI['SIGN_URL'],
                method='POST',
                header=HEADER,
                data={
                    'act_id': 'e202311201442471',
                    'uid': uid,
                    'region': server_id,
                },
            )
        else:
            HEADER = deepcopy(self._HEADER_OS)
            HEADER['Cookie'] = ck
            HEADER['DS'] = generate_os_ds()
            HEADER.update(header)
            data = await self._mys_request(
                url=self.MAPI['SIGN_URL_OS'],
                method='POST',
                header=HEADER,
                data={
                    'act_id': 'e202102251931481',
                    'lang': 'zh-cn',
                    'uid': uid,
                    'region': server_id,
                },
                use_proxy=True,
            )
        if isinstance(data, Dict):
            data = cast(MysSign, data['data'])
        return data

    async def get_award(self, uid) -> Union[MonthlyAward, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        if int(str(uid)[0]) < 6:
            HEADER = deepcopy(self._HEADER)
            HEADER['Cookie'] = ck
            HEADER['DS'] = get_web_ds_token(True)
            HEADER['x-rpc-device_id'] = random_hex(32)
            data = await self._mys_request(
                url=self.MAPI['MONTHLY_AWARD_URL'],
                method='GET',
                header=HEADER,
                params={
                    'act_id': 'e202009291139501',
                    'bind_region': server_id,
                    'bind_uid': uid,
                    'month': '0',
                    'bbs_presentation_style': 'fullscreen',
                    'bbs_auth_required': 'true',
                    'utm_source': 'bbs',
                    'utm_medium': 'mys',
                    'utm_campaign': 'icon',
                },
            )
        else:
            HEADER = deepcopy(self._HEADER_OS)
            HEADER['Cookie'] = ck
            HEADER['x-rpc-device_id'] = random_hex(32)
            HEADER['DS'] = generate_os_ds()
            data = await self._mys_request(
                url=self.MAPI['MONTHLY_AWARD_URL_OS'],
                method='GET',
                header=HEADER,
                params={
                    'act_id': 'e202009291139501',
                    'region': server_id,
                    'uid': uid,
                    'month': '0',
                },
                use_proxy=True,
            )
        if isinstance(data, Dict):
            data = cast(MonthlyAward, data['data'])
        return data
