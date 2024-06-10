'''
米游社BBS相关操作 API 请求模块。
'''

from copy import deepcopy
from typing import Dict, List, Union, cast

from .tools import get_web_ds_token
from .topup_request import TopupMysApi
from .models import RegTime, PostDraw, PostDetail, RolesCalendar


class BBSMysApi(TopupMysApi):
    async def get_bbs_post_detail(self, post_id: str):
        url: str = self.MAPI['BBS_DETAIL_URL'].format(post_id)
        header = deepcopy(self._HEADER)
        header['DS'] = get_web_ds_token(web=True)
        data = await self._mys_request(url, 'GET', header)
        if isinstance(data, Dict):
            return data['data']
        else:
            return data

    async def get_bbs_collection(self, collection_id: str, gids: str = '2'):
        url: str = self.MAPI['BBS_COLLECTION_URL']
        header = deepcopy(self._HEADER)
        header['DS'] = get_web_ds_token(web=True)
        data = await self._mys_request(
            url,
            'GET',
            header,
            params={
                'collection_id': collection_id,
                'gids': gids,
                'order_type': 1,
            },
        )
        if isinstance(data, Dict):
            if 'data' in data and 'posts' in data['data']:
                return cast(List[PostDetail], data['data']['posts'])
            else:
                return -500
        else:
            return data

    async def get_regtime_data(self, uid: str) -> Union[RegTime, int]:
        hk4e_token = await self.get_hk4e_token(uid)
        ck_token = await self.get_ck(uid, 'OWNER')
        params = {
            'game_biz': 'hk4e_cn',
            'lang': 'zh-cn',
            'badge_uid': uid,
            'badge_region': self.RECOGNIZE_SERVER.get(uid[0]),
        }
        data = await self.simple_mys_req(
            'REG_TIME',
            uid,
            params,
            {'Cookie': f'{hk4e_token};{ck_token}' if int(uid[0]) <= 5 else {}},
        )
        if isinstance(data, Dict):
            return cast(RegTime, data['data'])
        else:
            return data

    async def get_draw_calendar(self, uid: str) -> Union[int, RolesCalendar]:
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        hk4e_token = await self.get_hk4e_token(uid)
        header = {}
        header['Cookie'] = f'{ck};{hk4e_token}'
        params = {
            'lang': 'zh-cn',
            'badge_uid': uid,
            'badge_region': server_id,
            'game_biz': 'hk4e_cn',
            'activity_id': 20220301153521,
            'year': 2023,
        }
        data = await self._mys_request(
            self.MAPI['CALENDAR_URL'], 'GET', header, params
        )
        if isinstance(data, Dict):
            return cast(RolesCalendar, data['data'])
        return data

    async def post_draw(
        self, uid: str, role_id: int
    ) -> Union[int, PostDraw, Dict]:
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        hk4e_token = await self.get_hk4e_token(uid)
        header = {}
        header['Cookie'] = f'{ck};{hk4e_token}'
        data = await self._mys_request(
            self.MAPI['RECEIVE_URL'],
            'POST',
            header,
            {
                'lang': 'zh-cn',
                'badge_uid': uid,
                'badge_region': server_id,
                'game_biz': 'hk4e_cn',
                'activity_id': 20220301153521,
            },
            {'role_id': role_id},
        )
        if isinstance(data, Dict):
            return data
        elif data == -512009:
            return {
                'data': None,
                'message': '这张画片已经被收录啦~',
                'retcode': -512009,
            }
        else:
            return -999
