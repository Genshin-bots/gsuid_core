'''
米游社 API 请求模块。
'''

from __future__ import annotations

import copy
import time
from typing import Dict, List, Union, Optional, cast

from .bbs_request import BBSMysApi
from .tools import (
    random_hex,
    mys_version,
    get_ds_token,
    generate_os_ds,
    get_web_ds_token,
)
from .models import (
    BsIndex,
    GcgInfo,
    MysGame,
    MysSign,
    GachaLog,
    SignInfo,
    SignList,
    AbyssData,
    IndexData,
    GcgDeckInfo,
    MonthlyAward,
    CalculateInfo,
    DailyNoteData,
    CharDetailData,
)


class MysApi(BBSMysApi):
    async def get_info(
        self, uid, ck: Optional[str] = None
    ) -> Union[IndexData, int]:
        data = await self.simple_mys_req('PLAYER_INFO_URL', uid, cookie=ck)
        if isinstance(data, Dict):
            data = cast(IndexData, data['data'])
        return data

    async def get_daily_data(self, uid: str) -> Union[DailyNoteData, int]:
        data = await self.simple_mys_req('DAILY_NOTE_URL', uid)
        if isinstance(data, Dict):
            data = cast(DailyNoteData, data['data'])
        return data

    async def get_gcg_info(self, uid: str) -> Union[GcgInfo, int]:
        data = await self.simple_mys_req('GCG_INFO', uid)
        if isinstance(data, Dict):
            data = cast(GcgInfo, data['data'])
        return data

    async def get_gcg_deck(self, uid: str) -> Union[GcgDeckInfo, int]:
        data = await self.simple_mys_req('GCG_DECK_URL', uid)
        if isinstance(data, Dict):
            data = cast(GcgDeckInfo, data['data'])
        return data

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
            HEADER = copy.deepcopy(self._HEADER)
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
            HEADER = copy.deepcopy(self._HEADER_OS)
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
            HEADER = copy.deepcopy(self._HEADER)
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
            HEADER = copy.deepcopy(self._HEADER_OS)
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

    async def get_bs_index(self, uid: str) -> Union[int, BsIndex]:
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        hk4e_token = await self.get_hk4e_token(uid)
        header = {}
        header['Cookie'] = f'{ck};{hk4e_token}'
        data = await self._mys_request(
            self.MAPI['BS_INDEX_URL'],
            'GET',
            header,
            {
                'lang': 'zh-cn',
                'badge_uid': uid,
                'badge_region': server_id,
                'game_biz': 'hk4e_cn',
                'activity_id': 20220301153521,
            },
        )
        if isinstance(data, Dict):
            return cast(BsIndex, data['data'])
        return data

    async def get_spiral_abyss_info(
        self, uid: str, schedule_type='1', ck: Optional[str] = None
    ) -> Union[AbyssData, int]:
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        data = await self.simple_mys_req(
            'PLAYER_ABYSS_INFO_URL',
            uid,
            {
                'role_id': uid,
                'schedule_type': schedule_type,
                'server': server_id,
            },
            cookie=ck,
        )
        if isinstance(data, Dict):
            data = cast(AbyssData, data['data'])
        return data

    async def get_character(
        self, uid: str, character_ids: List[int], ck: Union[str, None] = None
    ) -> Union[CharDetailData, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])

        if ck is None:
            ck = await self.get_ck(uid)
            if ck is None:
                return -51

        if int(str(uid)[0]) < 6:
            HEADER = copy.deepcopy(self._HEADER)
            HEADER['Cookie'] = ck
            HEADER['DS'] = get_ds_token(
                '',
                {
                    'character_ids': character_ids,
                    'role_id': uid,
                    'server': server_id,
                },
            )
            data = await self._mys_request(
                self.MAPI['PLAYER_DETAIL_INFO_URL'],
                'POST',
                HEADER,
                data={
                    'character_ids': character_ids,
                    'role_id': uid,
                    'server': server_id,
                },
            )
        else:
            HEADER = copy.deepcopy(self._HEADER_OS)
            HEADER['Cookie'] = ck
            HEADER['DS'] = generate_os_ds()
            data = await self._mys_request(
                self.MAPI['PLAYER_DETAIL_INFO_URL_OS'],
                'POST',
                HEADER,
                data={
                    'character_ids': character_ids,
                    'role_id': uid,
                    'server': server_id,
                },
                use_proxy=True,
            )
        if isinstance(data, Dict):
            data = cast(CharDetailData, data['data'])
        return data

    async def get_calculate_info(
        self, uid, char_id: int
    ) -> Union[CalculateInfo, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])
        data = await self.simple_mys_req(
            'CALCULATE_INFO_URL',
            uid,
            {'avatar_id': char_id, 'uid': uid, 'region': server_id},
        )
        if isinstance(data, Dict):
            data = cast(CalculateInfo, data['data'])
        return data

    async def get_mihoyo_bbs_info(
        self,
        mys_id: str,
        cookie: Optional[str] = None,
        is_os: bool = False,
    ) -> Union[List[MysGame], int]:
        if not cookie:
            cookie = await self.get_ck(mys_id, 'OWNER')
        data = await self.simple_mys_req(
            'MIHOYO_BBS_PLAYER_INFO_URL',
            is_os,
            {'uid': mys_id},
            {'Cookie': cookie},
        )
        if isinstance(data, Dict):
            data = cast(List[MysGame], data['data']['list'])
        return data

    async def get_gacha_log_by_authkey(
        self,
        uid: str,
        gacha_type: str = '301',
        page: int = 1,
        end_id: str = '0',
    ) -> Union[int, GachaLog]:
        server_id = 'cn_qd01' if uid[0] == '5' else 'cn_gf01'
        authkey_rawdata = await self.get_authkey_by_cookie(uid)
        if isinstance(authkey_rawdata, int):
            return authkey_rawdata
        authkey = authkey_rawdata['authkey']
        url = self.MAPI['GET_GACHA_LOG_URL']
        data = await self._mys_request(
            url=url,
            method='GET',
            header=self._HEADER,
            params={
                'authkey_ver': '1',
                'sign_type': '2',
                'auth_appid': 'webview_gacha',
                'init_type': gacha_type,
                'gacha_id': 'fecafa7b6560db5f3182222395d88aaa6aaac1bc',
                'timestamp': str(int(time.time())),
                'lang': 'zh-cn',
                'device_type': 'mobile',
                'plat_type': 'ios',
                'region': server_id,
                'authkey': authkey,
                'game_biz': 'hk4e_cn',
                'gacha_type': gacha_type,
                'page': page,
                'size': '20',
                'end_id': end_id,
            },
        )
        if isinstance(data, Dict):
            data = cast(GachaLog, data['data'])
        return data
