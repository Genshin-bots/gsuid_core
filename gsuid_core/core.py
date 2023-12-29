import sys
import asyncio
from pathlib import Path
from asyncio import CancelledError

import uvicorn
from msgspec import json as msgjson
from fastapi import WebSocket, WebSocketDisconnect

sys.path.append(str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[1]))

from gsuid_core.gss import gss  # noqa: E402
from gsuid_core.web_app import app  # noqa: E402
from gsuid_core.logger import logger  # noqa: E402
from gsuid_core.config import core_config  # noqa: E402
from gsuid_core.handler import handle_event  # noqa: E402
from gsuid_core.models import MessageReceive  # noqa: E402
from gsuid_core.utils.database.startup import exec_list  # noqa: E402

HOST = core_config.get_config('HOST')
PORT = int(core_config.get_config('PORT'))

exec_list.extend(
    [
        'ALTER TABLE GsBind ADD COLUMN group_id TEXT',
        'ALTER TABLE GsBind ADD COLUMN sr_uid TEXT',
        'ALTER TABLE GsUser ADD COLUMN sr_uid TEXT',
        'ALTER TABLE GsUser ADD COLUMN sr_region TEXT',
        'ALTER TABLE GsUser ADD COLUMN fp TEXT',
        'ALTER TABLE GsUser ADD COLUMN device_id TEXT',
        'ALTER TABLE GsUser ADD COLUMN bb_uid TEXT',
        'ALTER TABLE GsUser ADD COLUMN bbb_uid TEXT',
        'ALTER TABLE GsUser ADD COLUMN zzz_uid TEXT',
        'ALTER TABLE GsUser ADD COLUMN wd_uid TEXT',
        'ALTER TABLE GsBind ADD COLUMN bb_uid TEXT',
        'ALTER TABLE GsBind ADD COLUMN bbb_uid TEXT',
        'ALTER TABLE GsBind ADD COLUMN zzz_uid TEXT',
        'ALTER TABLE GsBind ADD COLUMN wd_uid TEXT',
        'ALTER TABLE GsUser ADD COLUMN device_info TEXT',
        'ALTER TABLE GsUser ADD COLUMN sr_sign_switch TEXT DEFAULT "off"',
        'ALTER TABLE GsUser ADD COLUMN sr_push_switch TEXT DEFAULT "off"',
        'ALTER TABLE GsUser ADD COLUMN draw_switch TEXT DEFAULT "off"',
        'ALTER TABLE GsCache ADD COLUMN sr_uid TEXT',
    ]
)


def main():
    @app.websocket('/ws/{bot_id}')
    async def websocket_endpoint(websocket: WebSocket, bot_id: str):
        try:
            bot = await gss.connect(websocket, bot_id)

            async def start():
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        msg = msgjson.decode(data, type=MessageReceive)
                        await handle_event(bot, msg)
                except WebSocketDisconnect:
                    await gss.disconnect(bot_id)

            async def process():
                await bot._process()

            logger.info('启动GsCore...')
            await asyncio.gather(process(), start())
        except CancelledError:
            await gss.disconnect(bot_id)
        finally:
            await gss.disconnect(bot_id)

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_config={
            'version': 1,
            'disable_existing_loggers': False,
            'handlers': {
                'default': {
                    'class': 'gsuid_core.logger.LoguruHandler',
                },
            },
            'loggers': {
                'uvicorn.error': {'handlers': ['default'], 'level': 'INFO'},
                'uvicorn.access': {
                    'handlers': ['default'],
                    'level': 'INFO',
                },
            },
        },
    )


if __name__ == '__main__':
    main()
