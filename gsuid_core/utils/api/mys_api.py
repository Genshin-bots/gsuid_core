from typing import Optional

from gsuid_core.utils.api.mys import MysApi
from gsuid_core.utils.database.models import GsUser
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

gsconfig = core_plugins_config


class _MysApi(MysApi):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def get_stoken(
        self, uid: str, game_name: Optional[str] = None
    ) -> Optional[str]:
        return await GsUser.get_user_stoken_by_uid(uid, game_name)

    async def get_user_fp(
        self, uid: str, game_name: Optional[str] = None
    ) -> Optional[str]:
        data = await GsUser.get_user_attr_by_uid(
            uid,
            'fp',
            game_name,
        )
        if data is None:
            seed_id, seed_time = self.get_seed()
            device_id = self.get_device_id()
            data = await self.generate_fake_fp(device_id, seed_id, seed_time)
            await GsUser.update_data_by_uid_without_bot_id(
                uid,
                game_name,
                fp=data,
            )
        return data

    async def get_user_device_id(
        self, uid: str, game_name: Optional[str] = None
    ) -> Optional[str]:
        data = await GsUser.get_user_attr_by_uid(
            uid,
            'device_id',
            game_name,
        )
        if data is None:
            data = self.get_device_id()
            await GsUser.update_data_by_uid_without_bot_id(
                uid,
                game_name,
                device_id=data,
            )
        return data


mys_api = _MysApi()
