import sys
import asyncio
from typing import Dict
from pathlib import Path

import uvicorn
from msgspec import json as msgjson
from starlette.requests import Request
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

sys.path.append(str(Path(__file__).resolve().parents[1]))
from gsuid_core.sv import SL  # noqa: E402
from gsuid_core.gss import gss  # noqa: E402
from gsuid_core.logger import logger  # noqa: E402
from gsuid_core.config import core_config  # noqa: E402
from gsuid_core.handler import handle_event  # noqa: E402
from gsuid_core.models import MessageReceive  # noqa: E402
from gsuid_core.webconsole.mount_app import site  # noqa: E402
from gsuid_core.aps import start_scheduler, shutdown_scheduler  # noqa: E402
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


@app.websocket('/ws/{bot_id}')
async def websocket_endpoint(websocket: WebSocket, bot_id: str):
    bot = await gss.connect(websocket, bot_id)

    async def start():
        try:
            while True:
                data = await websocket.receive_bytes()
                msg = msgjson.decode(data, type=MessageReceive)
                await handle_event(bot, msg)
        except WebSocketDisconnect:
            gss.disconnect(bot_id)

    async def process():
        await bot._process()

    await asyncio.gather(process(), start())


@app.on_event('startup')
async def startup_event():
    try:
        from gsuid_core.webconsole.__init__ import start_check

        await start_check()
    except ImportError:
        logger.warning('未加载GenshinUID...网页控制台启动失败...')
    await start_scheduler()


@app.on_event('shutdown')
async def shutdown_event():
    await shutdown_scheduler()


def main():
    @app.post('/genshinuid/setSV/{name}')
    @site.auth.requires('admin')
    async def _set_SV(request: Request, data: Dict, name: str):
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
    @site.auth.requires('admin')
    async def _set_Config(request: Request, data: Dict, config_name: str):
        for name in data:
            if name == 'params':
                continue
            config = all_config_list[config_name][name]
            if isinstance(config, GsListStrConfig):
                value = data[name].split(':')
            else:
                value = data[name]
            all_config_list[config_name].set_config(name, value)

    @app.get('/genshinuid/api/getPlugins')
    @site.auth.requires('admin')
    async def _get_plugins(request: Request):
        tasks = []
        plugins_list = await get_plugins_list()
        for name in plugins_list:
            plugin = plugins_list[name]
            link = plugin['link']
            plugin_name = link.split('/')[-1]
            # git_path = f'{proxy_url}{link}.git'
            sample = {
                'label': plugin_name,
                'key': name,
                'status': check_status(plugin_name),
                'remark': plugin['info'],
            }
            tasks.append(sample)

        return tasks

    @app.post('/genshinuid/api/updatePlugins')
    @site.auth.requires('admin')
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
