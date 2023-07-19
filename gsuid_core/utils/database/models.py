from typing import Optional

from sqlmodel import Field, SQLModel


class GsBind(SQLModel, table=True):
    __table_args__ = {'keep_existing': True}
    id: Optional[int] = Field(default=None, primary_key=True, title='序号')
    bot_id: str = Field(title='平台')
    user_id: str = Field(title='账号')
    group_id: Optional[str] = Field(title='群号')
    uid: Optional[str] = Field(default=None, title='原神UID')
    sr_uid: Optional[str] = Field(default=None, title='星铁UID')
    mys_id: Optional[str] = Field(default=None, title='米游社通行证')


class GsUser(SQLModel, table=True):
    __table_args__ = {'keep_existing': True}
    id: Optional[int] = Field(default=None, primary_key=True, title='序号')
    bot_id: str = Field(title='平台')
    uid: Optional[str] = Field(default=None, title='原神UID')
    sr_uid: Optional[str] = Field(default=None, title='星铁UID')
    mys_id: Optional[str] = Field(default=None, title='米游社通行证')
    region: Optional[str] = Field(default=None, title='原神地区')
    sr_region: Optional[str] = Field(default=None, title='星铁地区')
    cookie: Optional[str] = Field(default=None, title='Cookie')
    stoken: Optional[str] = Field(default=None, title='Stoken')
    user_id: str = Field(title='账号')
    push_switch: str = Field(default='off', title='全局推送开关')
    sign_switch: str = Field(default='off', title='自动签到')
    bbs_switch: str = Field(default='off', title='自动米游币')
    draw_switch: str = Field(default='off', title='自动留影叙佳期')
    sr_push_switch: str = Field(default='off', title='星铁全局推送开关')
    sr_sign_switch: str = Field(default='off', title='星铁自动签到')
    status: Optional[str] = Field(default=None, title='状态')
    fp: Optional[str] = Field(default=None, title='Fingerprint')
    device_id: Optional[str] = Field(default=None, title='设备ID')


class GsCache(SQLModel, table=True):
    __table_args__ = {'keep_existing': True}
    id: Optional[int] = Field(default=None, primary_key=True, title='序号')
    cookie: str = Field(default=None, title='Cookie')
    uid: Optional[str] = Field(default=None, title='原神UID')
    sr_uid: Optional[str] = Field(default=None, title='星铁UID')
    mys_id: Optional[str] = Field(default=None, title='米游社通行证')


class GsPush(SQLModel, table=True):
    __table_args__ = {'keep_existing': True}
    id: Optional[int] = Field(default=None, primary_key=True, title='序号')
    bot_id: str = Field(title='平台')
    uid: str = Field(default=None, title='原神UID')
    coin_push: Optional[str] = Field(title='洞天宝钱推送', default='off')
    coin_value: Optional[int] = Field(title='洞天宝钱阈值', default=2100)
    coin_is_push: Optional[str] = Field(title='洞天宝钱是否已推送', default='off')
    resin_push: Optional[str] = Field(title='体力推送', default='off')
    resin_value: Optional[int] = Field(title='体力阈值', default=140)
    resin_is_push: Optional[str] = Field(title='体力是否已推送', default='off')
    go_push: Optional[str] = Field(title='派遣推送', default='off')
    go_value: Optional[int] = Field(title='派遣阈值', default=300)
    go_is_push: Optional[str] = Field(title='派遣是否已推送', default='off')
    transform_push: Optional[str] = Field(title='质变仪推送', default='off')
    transform_value: Optional[int] = Field(title='质变仪阈值', default=1000)
    transform_is_push: Optional[str] = Field(title='质变仪是否已推送', default='off')
