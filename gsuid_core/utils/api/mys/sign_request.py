'''
米游社签到 API 请求模块。
'''

from copy import deepcopy
from typing import Dict, Union, cast

from .bbs_request import BBSMysApi
from .api import GS_BASE, ZZZ_BASE, SIGN_BASE_OS
from .models import MysSign, SignInfo, SignList, MonthlyAward
from .tools import random_hex, generate_os_ds, get_web_ds_token

_ACT_ID = {
    'gs': {
        'cn_gf01': 'e202311201442471',
        'cn_qd01': 'e202311201442471',
        'os_usa': 'e202102251931481',
        'os_euro': 'e202102251931481',
        'os_asia': 'e202102251931481',
        'os_cht': 'e202102251931481',
    },
    'sr': {
        'prod_gf_cn': 'e202304121516551',
        'prod_qd_cn': 'e202304121516551',
        'prod_official_usa': 'e202303301540311',
        'prod_official_euro': 'e202303301540311',
        'prod_official_asia': 'e202303301540311',
        'prod_official_cht': 'e202303301540311',
    },
    'zzz': {
        'prod_gf_cn': 'e202406242138391',
        'prod_gf_us': '',
        'prod_gf_jp': '',
        'prod_gf_sg': '',
    },
}

_GAME_NAME = {
    'gs': 'hk4e',
    'sr': 'hkrpg',
    'zzz': 'nap',
}

_BASE_URL = {
    'gs': {'os': SIGN_BASE_OS, 'cn': GS_BASE},
    'sr': {'os': SIGN_BASE_OS, 'cn': GS_BASE},
    'zzz': {'os': '', 'cn': ZZZ_BASE},
}


class SignMysApi(BBSMysApi):
    async def get_sign_list(
        self,
        uid: str,
        game_name: str = 'gs',
        server_id: str = 'cn_gf01',
    ) -> Union[SignList, int]:
        is_os = self.check_os(uid, game_name)
        base_url = _BASE_URL[game_name]['os' if is_os else 'cn']
        end_point = self.MAPI['SIGN_LIST_URL_OS' if is_os else 'SIGN_LIST_URL']
        server_id = self.get_server_id(uid, game_name)
        act_id = _ACT_ID[game_name][server_id]
        ck = await self.get_ck(uid, 'OWNER', game_name)
        if ck is None:
            return -51
        header = {'Cookie': ck}
        params = {'act_id': act_id, 'lang': 'zh-cn'}

        if is_os:
            header['DS'] = generate_os_ds()
        else:
            header['x-rpc-signgame'] = _GAME_NAME[game_name]

        data = await self._mys_request(
            end_point,
            'GET',
            header,
            params,
            base_url=base_url,
        )
        if isinstance(data, Dict):
            data = cast(SignList, data['data'])
        return data

    async def get_sign_info(
        self,
        uid: str,
        game_name: str = 'gs',
    ) -> Union[SignInfo, int]:
        is_os = self.check_os(uid, game_name)
        server_id = self.get_server_id(uid, game_name)
        base_url = _BASE_URL[game_name]['os' if is_os else 'cn']
        end_point = self.MAPI['SIGN_INFO_URL_OS' if is_os else 'SIGN_INFO_URL']
        ck = await self.get_ck(uid, 'OWNER', game_name)
        if ck is None:
            return -51
        header = {'Cookie': ck}
        params = {
            'act_id': _ACT_ID[game_name][server_id],
            'lang': 'zh-cn',
            'region': server_id,
            'uid': uid,
        }

        if is_os:
            header['DS'] = generate_os_ds()
        else:
            header['x-rpc-signgame'] = _GAME_NAME[game_name]

        data = await self._mys_request(
            end_point, 'GET', header, params, base_url=base_url
        )

        if isinstance(data, Dict):
            data = cast(SignInfo, data['data'])
        return data

    async def mys_sign(
        self,
        uid: str,
        game_name: str = 'gs',
        header: Dict = {},
    ) -> Union[MysSign, int]:
        is_os = self.check_os(uid, game_name)
        server_id = self.get_server_id(uid, game_name)
        base_url = _BASE_URL[game_name]['os' if is_os else 'cn']
        end_point = self.MAPI['SIGN_URL_OS' if is_os else 'SIGN_URL']
        data = {
            'act_id': _ACT_ID[game_name][server_id],
            'lang': 'zh-cn',
            'uid': uid,
            'region': server_id,
        }

        ck = await self.get_ck(uid, 'OWNER', game_name)
        if ck is None:
            return -51

        if is_os:
            HEADER = deepcopy(self._HEADER_OS)
            HEADER['Cookie'] = ck
            HEADER['DS'] = generate_os_ds()
            HEADER.update(header)
        else:
            HEADER = deepcopy(self._HEADER)
            HEADER['Cookie'] = ck
            header['x-rpc-device_id'] = await self.get_user_device_id(uid)
            header['x-rpc-device_fp'] = await self.get_user_fp(uid)
            HEADER['x-rpc-client_type'] = '5'
            HEADER['X_Requested_With'] = 'com.mihoyo.hyperion'
            HEADER['DS'] = get_web_ds_token(True)
            header['x-rpc-signgame'] = _GAME_NAME[game_name]
            HEADER.update(header)

        data = await self._mys_request(
            url=end_point,
            method='POST',
            header=HEADER,
            data=data,
            base_url=base_url,
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
