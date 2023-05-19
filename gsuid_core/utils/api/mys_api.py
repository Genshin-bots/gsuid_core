from typing import Dict, Literal, Optional

from gsuid_core.utils.api.mys import MysApi
from gsuid_core.utils.database.api import DBSqla
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

gsconfig = core_plugins_config


class _MysApi(MysApi):
    dbsqla: DBSqla = DBSqla()

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


mys_api = _MysApi()
