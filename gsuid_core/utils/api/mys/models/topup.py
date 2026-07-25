from __future__ import annotations

from typing import Literal, TypedDict


class MysGoods(TypedDict):
    goods_id: str
    goods_name: str
    goods_name_i18n_key: str
    goods_desc: str
    goods_desc_i18n_key: str
    goods_type: Literal["Normal", "Special"]
    goods_unit: str
    goods_icon: str
    currency: Literal["CNY"]
    price: str
    symbol: Literal["￥"]
    tier_id: Literal["Tier_1"]
    bonus_desc: MysGoodsBonus
    once_bonus_desc: MysGoodsBonus
    available: bool
    tips_desc: str
    tips_i18n_key: str
    battle_pass_limit: str


class MysGoodsBonus(TypedDict):
    bonus_desc: str
    bonus_desc_i18n_key: str
    bonus_unit: int
    bonus_goods_id: str
    bonus_icon: str


class MysOrderCheck(TypedDict):
    status: int  # 900为成功
    amount: str
    goods_title: str
    goods_num: str
    order_no: str
    pay_plat: Literal["alipay", "weixin"]


class MysOrder(TypedDict):
    goods_id: str
    order_no: str
    currency: Literal["CNY"]
    amount: str
    redirect_url: str
    foreign_serial: str
    encode_order: str
    account: str  # mysid
    create_time: str
    ext_info: str
    balance: str
    method: str
    action: str
    session_cookie: str
