import re
import asyncio
from typing import Dict, Type, Tuple, Union, Optional, overload

from sqlalchemy import event

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.utils.database.dal import SQLA
from gsuid_core.utils.database.base_models import Bind, engine

is_wal = False

active_sqla: Dict[str, SQLA] = {}
active_sr_sqla: Dict[str, SQLA] = {}


class DBSqla:
    def __init__(self, is_sr: bool = False) -> None:
        self.is_sr = is_sr

    def get_sqla(self, bot_id) -> SQLA:
        sqla = self._get_sqla(bot_id, self.is_sr)
        asyncio.create_task(sqla.sr_adapter())
        return sqla

    def _get_sqla(self, bot_id, is_sr: bool = False) -> SQLA:
        sqla_list = active_sr_sqla if is_sr else active_sqla
        if bot_id not in sqla_list:
            sqla = SQLA(bot_id, is_sr)
            sqla_list[bot_id] = sqla
            sqla.create_all()

            @event.listens_for(engine.sync_engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                if is_wal:
                    cursor = dbapi_connection.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.close()

        return sqla_list[bot_id]

    def get_gs_sqla(self, bot_id):
        return self._get_sqla(bot_id, False)

    def get_sr_sqla(self, bot_id):
        return self._get_sqla(bot_id, True)


@overload
async def get_uid(
    bot: Bot,
    ev: Event,
    bind_model: Type[Bind],
    game_name: Optional[str] = None,
) -> Optional[str]:
    ...


@overload
async def get_uid(
    bot: Bot,
    ev: Event,
    bind_model: Type[Bind],
    game_name: Optional[str] = None,
    get_user_id: bool = True,
) -> Tuple[Optional[str], str]:
    ...


async def get_uid(
    bot: Bot,
    ev: Event,
    bind_model: Type[Bind],
    game_name: Optional[str] = None,
    get_user_id: bool = False,
) -> Union[Optional[str], Tuple[Optional[str], str]]:
    uid_data = re.findall(r'\d+', ev.text)
    user_id = ev.at if ev.at else ev.user_id
    if uid_data:
        uid: Optional[str] = uid_data[0]
        if uid:
            ev.text = ev.text.replace(uid, '')
    else:
        uid = await bind_model.get_uid_by_game(user_id, ev.bot_id, game_name)
    if get_user_id:
        return uid, user_id
    return uid
