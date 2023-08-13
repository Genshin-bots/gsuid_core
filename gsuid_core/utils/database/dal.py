import re
import asyncio
from typing import Dict, List, Literal, Optional

from sqlmodel import SQLModel
from sqlalchemy.sql import text

from .utils import SERVER, SR_SERVER
from .base_models import engine, async_maker
from .models import GsBind, GsPush, GsUser, GsCache


class SQLA:
    def __init__(self, bot_id: str, is_sr: bool = False):
        self.bot_id = bot_id
        self.is_sr = is_sr

    def create_all(self):
        try:
            asyncio.create_task(self._create_all())
        except RuntimeError:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self._create_all())
            loop.close()

    async def _create_all(self):
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
            await self.sr_adapter()

    async def sr_adapter(self):
        exec_list = [
            'ALTER TABLE GsBind ADD COLUMN group_id TEXT',
            'ALTER TABLE GsBind ADD COLUMN sr_uid TEXT',
            'ALTER TABLE GsUser ADD COLUMN sr_uid TEXT',
            'ALTER TABLE GsUser ADD COLUMN sr_region TEXT',
            'ALTER TABLE GsUser ADD COLUMN fp TEXT',
            'ALTER TABLE GsUser ADD COLUMN device_id TEXT',
            'ALTER TABLE GsUser ADD COLUMN sr_sign_switch TEXT DEFAULT "off"',
            'ALTER TABLE GsUser ADD COLUMN sr_push_switch TEXT DEFAULT "off"',
            'ALTER TABLE GsUser ADD COLUMN draw_switch TEXT DEFAULT "off"',
            'ALTER TABLE GsCache ADD COLUMN sr_uid TEXT',
        ]
        async with async_maker() as session:
            for _t in exec_list:
                try:
                    await session.execute(text(_t))
                    await session.commit()
                except:  # noqa: E722
                    pass

    #####################
    # GsBind 部分 #
    #####################
    async def select_bind_data(self, user_id: str) -> Optional[GsBind]:
        return await GsBind.select_data(user_id, self.bot_id)

    async def insert_bind_data(self, user_id: str, **data) -> int:
        group_id = data['group_id'] if 'group_id' in data else None
        new_uid: str = data['uid'] if 'uid' in data else ''
        new_uid = new_uid.strip()
        new_sr_uid: str = data['sr_uid'] if 'sr_uid' in data else ''
        new_sr_uid = new_sr_uid.strip()
        if new_uid:
            retcode = await GsBind.insert_uid(
                user_id, self.bot_id, new_uid, group_id, 9, True
            )
            if retcode:
                return retcode
        if new_sr_uid:
            retcode = await GsBind.insert_uid(
                user_id,
                self.bot_id,
                new_sr_uid,
                group_id,
                9,
                True,
                'sr',
            )
            if retcode:
                return retcode
        return 0

    async def delete_bind_data(self, user_id: str, **data) -> int:
        _uid = data['uid'] if 'uid' in data else ''
        _sr_uid = data['sr_uid'] if 'sr_uid' in data else ''
        if _uid:
            return await GsBind.delete_uid(user_id, self.bot_id, _uid)
        elif _sr_uid:
            return await GsBind.delete_uid(user_id, self.bot_id, _uid, 'sr')
        else:
            return -1

    async def update_bind_data(self, user_id: str, data: Optional[Dict]):
        if data is not None:
            await GsBind.update_data(user_id, self.bot_id, **data)

    async def bind_exists(self, user_id: str) -> bool:
        return await GsBind.bind_exists(user_id, self.bot_id)

    async def get_all_uid_list(self) -> List[str]:
        return await GsBind.get_all_uid_list_by_game(
            self.bot_id, 'sr' if self.is_sr else None
        )

    async def get_bind_group_list(self, user_id: str) -> List[str]:
        return await GsBind.get_bind_group_list(user_id, self.bot_id)

    async def get_bind_group(self, user_id: str) -> Optional[str]:
        return await GsBind.get_bind_group(user_id, self.bot_id)

    async def get_group_all_uid(self, group_id: str):
        return await GsBind.get_group_all_uid(group_id)

    async def get_bind_uid_list(self, user_id: str) -> Optional[List[str]]:
        return await GsBind.get_uid_list_by_game(user_id, self.bot_id)

    async def get_bind_uid(self, user_id: str) -> Optional[str]:
        return await GsBind.get_uid_by_game(user_id, self.bot_id)

    async def get_bind_sruid_list(self, user_id: str) -> Optional[List[str]]:
        return await GsBind.get_uid_list_by_game(user_id, self.bot_id, 'sr')

    async def get_bind_sruid(self, user_id: str) -> Optional[str]:
        return await GsBind.get_uid_by_game(user_id, self.bot_id, 'sr')

    async def switch_uid(
        self, user_id: str, uid: Optional[str] = None
    ) -> Optional[List]:
        retcode = await GsBind.switch_uid_by_game(
            user_id,
            self.bot_id,
            uid,
            'sr' if self.is_sr else None,
        )
        if retcode == 0:
            return await GsBind.get_uid_list_by_game(
                user_id,
                self.bot_id,
                'sr' if self.is_sr else None,
            )

    #####################
    # GsUser、GsCache 部分 #
    #####################

    async def select_user_data(self, uid: str) -> Optional[GsUser]:
        return await GsUser.select_data_by_uid(
            uid, 'sr' if self.is_sr else None
        )

    async def select_user_all_data_by_user_id(
        self, user_id: str
    ) -> Optional[List[GsUser]]:
        return await GsUser.get_user_all_data_by_user_id(user_id)

    async def select_user_data_by_user_id(
        self, user_id: str
    ) -> Optional[GsUser]:
        return await GsUser.select_data(user_id)

    async def select_cache_cookie(self, uid: str) -> Optional[str]:
        return await GsCache.select_cache_cookie(
            uid, 'sr' if self.is_sr else None
        )

    async def delete_error_cache(self) -> bool:
        return await GsCache.delete_error_cache(GsUser)

    async def get_user_fp(self, uid: str) -> Optional[str]:
        data = await self.select_user_data(uid)
        return data.fp if data else None

    async def get_user_device_id(self, uid: str) -> Optional[str]:
        data = await self.select_user_data(uid)
        return data.device_id if data else None

    async def insert_cache_data(
        self,
        cookie: str,
        uid: Optional[str] = None,
        sr_uid: Optional[str] = None,
        mys_id: Optional[str] = None,
    ) -> bool:
        return await GsCache.insert_cache_data(
            cookie, uid=uid, sr_uid=sr_uid, mys_id=mys_id
        )

    async def insert_user_data(
        self,
        user_id: str,
        uid: Optional[str] = None,
        sr_uid: Optional[str] = None,
        cookie: Optional[str] = None,
        stoken: Optional[str] = None,
        fp: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> bool:
        if uid and await GsUser.user_exists(uid):
            retcode = await GsUser.update_data_by_uid(
                uid,
                self.bot_id,
                cookie=cookie,
                status=None,
                stoken=stoken,
                sr_uid=sr_uid,
                fp=fp,
            )
        elif sr_uid and await self.user_exists(sr_uid):
            retcode = await GsUser.update_data_by_uid(
                sr_uid,
                self.bot_id,
                'sr',
                cookie=cookie,
                status=None,
                stoken=stoken,
                sr_uid=sr_uid,
                fp=fp,
            )
        else:
            if cookie is None:
                return False

            account_id = re.search(r'account_id=(\d*)', cookie)
            assert account_id is not None
            account_id = str(account_id.group(1))

            retcode = await GsUser.insert_data(
                user_id=user_id,
                bot_id=self.bot_id,
                uid=uid,
                sr_uid=sr_uid,
                mys_id=account_id,
                cookie=cookie,
                stoken=stoken if stoken else None,
                sign_switch='off',
                push_switch='off',
                bbs_switch='off',
                draw_switch='off',
                region=SERVER.get(uid[0], 'cn_gf01') if uid else None,
                sr_region=SR_SERVER.get(sr_uid[0], None) if sr_uid else None,
                fp=fp,
                device_id=device_id,
                sr_push_switch='off',
                sr_sign_switch='off',
            )
        if retcode == 0:
            return True
        else:
            return False

    async def update_user_data(self, uid: str, data: Dict = {}):
        return await GsUser.update_data_by_uid(
            uid, self.bot_id, 'sr' if self.is_sr else None, **data
        )

    async def delete_user_data(self, uid: str):
        if await GsUser.user_exists(uid):
            return await GsUser.delete_user_data_by_uid(
                uid, 'sr' if self.is_sr else None
            )

    async def delete_cache(self):
        return await GsCache.delete_all_cache(GsUser)

    async def mark_invalid(self, cookie: str, mark: str):
        await GsUser.mark_invalid(cookie, mark)

    async def user_exists(self, uid: str) -> bool:
        data = await self.select_user_data(uid)
        return True if data else False

    async def update_user_stoken(
        self, uid: str, stoken: Optional[str]
    ) -> bool:
        retcode = -1
        if await GsUser.user_exists(uid):
            retcode = await GsUser.update_data_by_uid(
                uid, self.bot_id, 'sr' if self.is_sr else None, stoken=stoken
            )
        return bool(retcode)

    async def update_user_cookie(
        self, uid: str, cookie: Optional[str]
    ) -> bool:
        retcode = -1
        if await GsUser.user_exists(uid):
            retcode = await GsUser.update_data_by_uid(
                uid, self.bot_id, 'sr' if self.is_sr else None, cookie=cookie
            )
        return bool(retcode)

    async def update_switch_status(self, uid: str, data: Dict) -> bool:
        retcode = -1
        if await GsUser.user_exists(uid):
            retcode = await GsUser.update_data_by_uid(
                uid, self.bot_id, 'sr' if self.is_sr else None, **data
            )
        return bool(retcode)

    async def update_error_status(self, cookie: str, err: str) -> bool:
        return await GsUser.mark_invalid(cookie, err)

    async def get_user_cookie(self, uid: str) -> Optional[str]:
        return await GsUser.get_user_cookie_by_uid(
            uid, 'sr' if self.is_sr else None
        )

    async def get_user_cookie_by_user_id(self, user_id: str) -> Optional[str]:
        return await GsUser.get_user_cookie_by_user_id(user_id, self.bot_id)

    async def cookie_validate(self, uid: str) -> bool:
        return await GsUser.cookie_validate(uid, 'sr' if self.is_sr else None)

    async def get_user_stoken_by_user_id(self, user_id: str) -> Optional[str]:
        return await GsUser.get_user_stoken_by_user_id(user_id, self.bot_id)

    async def get_user_stoken(self, uid: str) -> Optional[str]:
        return await GsUser.get_user_stoken_by_uid(
            uid, 'sr' if self.is_sr else None
        )

    async def get_all_user(self) -> List[GsUser]:
        return await GsUser.get_all_user()

    async def get_all_cookie(self) -> List[str]:
        return await GsUser.get_all_cookie()

    async def get_all_stoken(self) -> List[str]:
        return await GsUser.get_all_stoken()

    async def get_all_error_cookie(self) -> List[str]:
        return await GsUser.get_all_error_cookie()

    async def get_all_push_user_list(self) -> List[GsUser]:
        return await GsUser.get_all_push_user_list()

    async def get_random_cookie(self, uid: str) -> Optional[str]:
        server = SERVER.get(uid[0], 'cn_gf01')
        return await GsUser.get_random_cookie(
            uid, GsCache, {'region': server}, 'sr' if self.is_sr else None
        )

    async def get_switch_status_list(
        self, switch: Literal['push', 'sign', 'bbs', 'sr_push', 'sr_sign']
    ) -> List[GsUser]:
        return await GsUser.get_switch_open_list(switch)

    #####################
    # GsPush 部分 #
    #####################
    async def insert_push_data(self, uid: str):
        await GsPush.full_insert_data(
            GsPush,
            bot_id=self.bot_id,
            uid=uid,
            coin_push='off',
            coin_value=2100,
            coin_is_push='off',
            resin_push='on',
            resin_value=140,
            resin_is_push='off',
            go_push='off',
            go_value=120,
            go_is_push='off',
            transform_push='off',
            transform_value=140,
            transform_is_push='off',
        )

    async def update_push_data(self, uid: str, data: dict) -> bool:
        retcode = -1
        if await GsPush.data_exist(uid=uid):
            retcode = await GsPush.update_data_by_uid(
                uid, self.bot_id, 'sr' if self.is_sr else None, **data
            )
        return not bool(retcode)

    async def change_push_status(
        self,
        mode: Literal['coin', 'resin', 'go', 'transform'],
        uid: str,
        status: str,
    ):
        await self.update_push_data(uid, {f'{mode}_is_push': status})

    async def select_push_data(self, uid: str) -> Optional[GsPush]:
        return await GsPush.base_select_data(uid=uid)

    async def push_exists(self, uid: str) -> bool:
        return await GsPush.data_exist(uid=uid)

    #####################
    # 杂项部分 #
    #####################

    async def refresh_cache(self, uid: str):
        await GsCache.refresh_cache(uid, 'sr' if self.is_sr else None)

    async def close(self):
        async with async_maker() as session:
            async with session.begin():
                await session.close()

    async def insert_new_bind(self, **kwargs):
        await GsBind.full_insert_data(GsBind, **kwargs)
