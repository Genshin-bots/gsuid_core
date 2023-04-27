from typing import Dict

from sqlalchemy import event

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.database.dal import SQLA

is_wal = False

active_sqla: Dict[str, SQLA] = {}
active_sr_sqla: Dict[str, SQLA] = {}
db_url = str(get_res_path().parent / 'GsData.db')


class DBSqla:
    def __init__(self, is_sr: bool = False) -> None:
        self.is_sr = is_sr

    def get_sqla(self, bot_id) -> SQLA:
        return self._get_sqla(bot_id, self.is_sr)

    def _get_sqla(self, bot_id, is_sr: bool = False) -> SQLA:
        sqla_list = active_sr_sqla if is_sr else active_sqla
        if bot_id not in sqla_list:
            sqla = SQLA(db_url, bot_id, is_sr)
            sqla_list[bot_id] = sqla
            sqla.create_all()

            @event.listens_for(sqla.engine.sync_engine, 'connect')
            def engine_connect(conn, branch):
                if is_wal:
                    cursor = conn.cursor()
                    cursor.execute('PRAGMA journal_mode=WAL')
                    cursor.close()

        return sqla_list[bot_id]

    def get_gs_sqla(self, bot_id):
        return self._get_sqla(bot_id, False)

    def get_sr_sqla(self, bot_id):
        return self._get_sqla(bot_id, True)
