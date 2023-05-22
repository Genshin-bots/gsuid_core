from typing import Literal, Optional

from gsuid_core.utils.api.mys import MysApi
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

gsconfig = core_plugins_config


class _MysApi(MysApi):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def get_ck(
        self, uid: str, mode: Literal['OWNER', 'RANDOM'] = 'RANDOM'
    ) -> Optional[str]:
        if mode == 'RANDOM':
            return await self.dbsqla.get_sqla('TEMP').get_random_cookie(uid)
        else:
            return await self.dbsqla.get_sqla('TEMP').get_user_cookie(uid)

    async def get_stoken(self, uid: str) -> Optional[str]:
        return await self.dbsqla.get_sqla('TEMP').get_user_stoken(uid)

    async def get_user_fp(self, uid: str) -> Optional[str]:
        data = await self.dbsqla.get_sqla('TEMP').get_user_fp(uid)
        if data is None:
            data = await self.generate_fp_by_uid(uid)
            await self.dbsqla.get_sqla('TEMP').update_user_data(
                uid, {'fp': data}
            )
        return data

    async def get_user_device_id(self, uid: str) -> Optional[str]:
        data = await self.dbsqla.get_sqla('TEMP').get_user_device_id(uid)
        if data is None:
            data = self.get_device_id()
            await self.dbsqla.get_sqla('TEMP').update_user_data(
                uid, {'device_id': data}
            )
        return data


mys_api = _MysApi()
