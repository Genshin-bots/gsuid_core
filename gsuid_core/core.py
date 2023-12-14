import sys
import asyncio
from io import BytesIO
from typing import Dict
from pathlib import Path
from asyncio import CancelledError
from contextlib import asynccontextmanager

import uvicorn
from PIL import Image
from msgspec import json as msgjson
from starlette.requests import Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from fastapi import FastAPI, WebSocket, BackgroundTasks, WebSocketDisconnect

sys.path.append(str(Path(__file__).resolve().parents[1]))
from gsuid_core.sv import SL  # noqa: E402
from gsuid_core.gss import gss  # noqa: E402
from gsuid_core.logger import logger  # noqa: E402
from gsuid_core.config import core_config  # noqa: E402
from gsuid_core.data_store import image_res  # noqa: E402
from gsuid_core.handler import handle_event  # noqa: E402
from gsuid_core.models import MessageReceive  # noqa: E402
from gsuid_core.webconsole.mount_app import site  # noqa: E402
from gsuid_core.utils.database.startup import exec_list  # noqa: E402
from gsuid_core.aps import start_scheduler, shutdown_scheduler  # noqa: E402
from gsuid_core.server import core_start_def, core_shutdown_def  # noqa: E402
from gsuid_core.utils.plugins_config.models import (  # noqa: E402
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import (  # noqa: E402
    all_config_list,
)
from gsuid_core.utils.plugins_update._plugins import (  # noqa: E402
    check_status,
    check_plugins,
    install_plugin,
    update_plugins,
    check_can_update,
    get_plugins_list,
)

app = FastAPI()
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        _task = [_def() for _def in core_start_def]
        await asyncio.gather(*_task)
    except Exception as e:
        logger.exception(e)

    from gsuid_core.webconsole.__init__ import start_check

    await start_check()
    await start_scheduler()
    yield
    await shutdown_scheduler()
    try:
        _task = [_def() for _def in core_shutdown_def]
        await asyncio.gather(*_task)
    except Exception as e:
        logger.exception(e)


def main():
    app = FastAPI(lifespan=lifespan)

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

            await asyncio.gather(process(), start())
        except CancelledError:
            await gss.disconnect(bot_id)
        finally:
            await gss.disconnect(bot_id)

    @app.post('/genshinuid/setSV/{name}')
    @site.auth.requires('root')
    def _set_SV(request: Request, data: Dict, name: str):
        if name in SL.lst:
            sv = SL.lst[name]
            data['pm'] = int(data['pm'])
            data['black_list'] = data['black_list'].replace('；', ';')
            data['white_list'] = data['white_list'].replace('；', ';')

            data['black_list'] = data['black_list'].split(';')
            data['white_list'] = data['white_list'].split(';')
            if data['black_list'] == ['']:
                data['black_list'] = []
            if data['white_list'] == ['']:
                data['white_list'] = []
            sv.set(**data)

    @app.post('/genshinuid/setGsConfig/{config_name}')
    @site.auth.requires('root')
    def _set_Config(request: Request, data: Dict, config_name: str):
        for name in data:
            if name == 'params':
                continue
            config = all_config_list[config_name][name]
            if isinstance(config, GsListStrConfig):
                data[name] = data[name].replace('：', ':')
                value = data[name].split(':')
            else:
                value = data[name]
            all_config_list[config_name].set_config(name, value)

    @app.get('/genshinuid/api/getPlugins')
    @site.auth.requires('root')
    async def _get_plugins(request: Request):
        tasks = []
        plugins_list = await get_plugins_list()
        for name in plugins_list:
            plugin = plugins_list[name]
            link = plugin['link']
            plugin_name = link.split('/')[-1]
            sample = {
                'label': plugin_name,
                'key': name,
                'status': await check_status(plugin_name),
                'remark': plugin['info'],
            }
            tasks.append(sample)

        return tasks

    @app.post('/genshinuid/api/updatePlugins')
    @site.auth.requires('root')
    async def _update_plugins(request: Request, data: Dict):
        repo = check_plugins(data['label'])
        if repo:
            if check_can_update(repo):
                try:
                    update_plugins(data['label'])
                    retcode = 0
                except:  # noqa:E722
                    retcode = -1
            else:
                retcode = 0
        else:
            try:
                retcode = await install_plugin(data['key'])
                retcode = 0
            except:  # noqa:E722
                retcode = -1
        return {'status': retcode, 'msg': '', 'data': {}}

    async def delete_image(image_path: Path):
        await asyncio.sleep(180)
        image_path.unlink()

    app.mount(
        "/webstatic",
        StaticFiles(directory=Path(__file__).parent / 'webstatic'),
        name="static",
    )

    @app.head('/genshinuid/image/{image_id}.jpg')
    @app.get('/genshinuid/image/{image_id}.jpg')
    async def get_image(image_id: str, background_tasks: BackgroundTasks):
        path = image_res / f'{image_id}.jpg'
        image = Image.open(path).convert('RGB')
        image_bytes = BytesIO()
        image.save(image_bytes, format='JPEG')
        image_bytes.seek(0)
        response = StreamingResponse(image_bytes, media_type='image/png')
        asyncio.create_task(delete_image(path))
        return response

    site.mount_app(app)

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
