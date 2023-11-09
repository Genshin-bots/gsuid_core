from typing import Literal, Optional

from gsuid_core.utils.api.mys import MysApi
from gsuid_core.utils.database.models import GsUser
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

gsconfig = core_plugins_config


class _MysApi(MysApi):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def get_ck(
        self, uid: str, mode: Literal['OWNER', 'RANDOM'] = 'RANDOM'
    ) -> Optional[str]:
        if mode == 'RANDOM':
            return await GsUser.get_random_cookie(uid)
        else:
            return await GsUser.get_user_cookie_by_uid(uid)

    async def get_stoken(self, uid: str) -> Optional[str]:
        return await GsUser.get_user_stoken_by_uid(uid)

    async def get_user_fp(self, uid: str) -> Optional[str]:
        data = await GsUser.get_user_attr_by_uid(uid, 'fp')
        if data is None:
            seed_id, seed_time = self.get_seed()
            device_id = self.get_device_id()
            data = await self.generate_fake_fp(device_id, seed_id, seed_time)
            await GsUser.update_data_by_uid_without_bot_id(uid, fp=data)
        return data

    async def get_user_device_id(self, uid: str) -> Optional[str]:
        data = await GsUser.get_user_attr_by_uid(uid, 'device_id')
        if data is None:
            data = self.get_device_id()
            await GsUser.update_data_by_uid_without_bot_id(uid, device_id=data)
        return data


mys_api = _MysApi()
