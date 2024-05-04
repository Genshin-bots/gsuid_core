import uuid
from copy import deepcopy
from typing import List, Union, Literal, cast

from .tools import gen_payment_sign
from .account_request import AccountMysApi
from .models import MysGoods, MysOrder, MysOrderCheck


class TopupMysApi(AccountMysApi):
    '''充值相关'''

    async def get_fetchgoods(self) -> Union[int, List[MysGoods]]:
        data = {
            'released_flag': True,
            'game': 'hk4e_cn',
            'region': 'cn_gf01',
            'uid': '1',
            'account': '1',
        }
        resp = await self._mys_request(
            url=self.MAPI['fetchGoodsurl'],
            method='POST',
            data=data,
        )
        if isinstance(resp, int):
            return resp
        return cast(List[MysGoods], resp['data']['goods_list'])

    async def topup(
        self,
        uid: str,
        goods: MysGoods,
        method: Literal['weixin', 'alipay'] = 'alipay',
    ) -> Union[int, MysOrder]:
        device_id = str(uuid.uuid4())
        HEADER = deepcopy(self._HEADER)
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        HEADER['Cookie'] = ck
        account = HEADER['Cookie'].split('account_id=')[1].split(';')[0]
        order = {
            'account': str(account),
            'region': 'cn_gf01',
            'uid': uid,
            'delivery_url': '',
            'device': device_id,
            'channel_id': 1,
            'client_ip': '',
            'client_type': 4,
            'game': 'hk4e_cn',
            'amount': goods['price'],
            # 'amount': 600,
            'goods_num': 1,
            'goods_id': goods['goods_id'],
            'goods_title': (
                f'{goods["goods_name"]}×{str(goods["goods_unit"])}'
                if int(goods['goods_unit']) > 0
                else goods['goods_name']
            ),
            'price_tier': goods['tier_id'],
            # 'price_tier': 'Tier_1',
            'currency': 'CNY',
            'pay_plat': method,
        }
        data = {
            'order': order,
            'special_info': 'topup_center',
            'sign': gen_payment_sign(order),
        }
        HEADER['x-rpc-device_id'] = device_id
        HEADER['x-rpc-client_type'] = '4'
        resp = await self._mys_request(
            url=self.MAPI['CreateOrderurl'],
            method='POST',
            header=HEADER,
            data=data,
        )
        if isinstance(resp, int):
            return resp
        return cast(MysOrder, resp['data'])

    async def check_order(
        self, order: MysOrder, uid: str
    ) -> Union[int, MysOrderCheck]:
        HEADER = deepcopy(self._HEADER)
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        HEADER['Cookie'] = ck
        data = {
            'order_no': order['order_no'],
            'game': 'hk4e_cn',
            'region': 'cn_gf01',
            'uid': uid,
        }
        resp = await self._mys_request(
            url=self.MAPI['CheckOrderurl'],
            method='GET',
            header=HEADER,
            params=data,
        )
        if isinstance(resp, int):
            return resp
        return cast(MysOrderCheck, resp['data'])
