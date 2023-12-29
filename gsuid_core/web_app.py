import asyncio
from io import BytesIO
from typing import Dict
from pathlib import Path
from contextlib import asynccontextmanager

from PIL import Image
from starlette.requests import Request
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import StreamingResponse

from gsuid_core.sv import SL
from gsuid_core.data_store import image_res
from gsuid_core.webconsole.mount_app import site
from gsuid_core.logger import logger, read_log, clear_log
from gsuid_core.global_val import bot_val, get_value_analysis
from gsuid_core.aps import start_scheduler, shutdown_scheduler
from gsuid_core.server import core_start_def, core_shutdown_def
from gsuid_core.utils.plugins_config.models import GsListStrConfig
from gsuid_core.utils.plugins_config.gs_config import all_config_list
from gsuid_core.utils.plugins_update._plugins import (
    check_status,
    check_plugins,
    install_plugin,
    update_plugins,
    check_can_update,
    get_plugins_list,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        _task = [_def() for _def in core_start_def]
        await asyncio.gather(*_task)
    except Exception as e:
        logger.exception(e)

    from gsuid_core.webconsole.__init__ import start_check

    await start_check()  # type:ignore
    await start_scheduler()
    yield
    await shutdown_scheduler()
    try:
        _task = [_def() for _def in core_shutdown_def]
        await asyncio.gather(*_task)
    except Exception as e:
        logger.exception(e)


app = FastAPI(lifespan=lifespan)


@app.post('/genshinuid/setSV/{name}')
@site.auth.requires('root')
def _set_SV(request: Request, data: Dict, name: str):
    if name in SL.lst:
        sv = SL.lst[name]
        data['pm'] = int(data['pm'])
        data['priority'] = int(data['priority'])

        data['black_list'] = data['black_list'].split(',')
        data['white_list'] = data['white_list'].split(',')

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
            data[name] = data[name].replace('：', ':').replace(':', ',')
            value = data[name].split(',')
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


@app.get('/genshinuid/api/getAnalysisData/{bot_id}/{bot_self_id}')
@site.auth.requires('root')
async def _get_data_analysis(
    request: Request,
    bot_id: str,
    bot_self_id: str,
):
    if bot_id not in bot_val:
        retcode, msg, data = -1, '不存在该bot_id!', {}
    elif bot_self_id not in bot_val[bot_id]:
        retcode, msg, data = -1, '不存在该bot_self_id!', {}
    else:
        retcode, msg = 0, 'ok'

        xaxis = []
        series = []

        send_data = []
        receive_data = []
        command_data = []
        image_gen_data = []

        seven_data = await get_value_analysis(bot_id, bot_self_id)
        for day in seven_data:
            xaxis.append(day)
            local_val = seven_data[day]
            send_data.append(local_val['send'])
            receive_data.append(local_val['receive'])
            command_data.append(local_val['command'])
            image_gen_data.append(local_val['image'])

        series.append({'name': '发送', 'type': 'line', 'data': send_data})
        series.append(
            {
                'name': '接收',
                'type': 'line',
                'data': receive_data,
            }
        )
        series.append(
            {
                'name': '命令调用',
                'type': 'line',
                'data': command_data,
            }
        )
        series.append(
            {
                'name': '图片生成',
                'type': 'line',
                'data': image_gen_data,
            }
        )

        data = {
            'title': {'text': ''},
            'tooltip': {'trigger': 'axis'},
            'legend': {'data': ['发送', '接收', '命令调用', '图片生成']},
            'xAxis': {'type': 'category', 'boundaryGap': False, 'data': xaxis},
            'yAxis': {'type': 'value'},
            'series': series,
        }

    return {'status': retcode, 'msg': msg, 'data': data}


@app.get('/genshinuid/api/getAnalysisUserGroup/{bot_id}/{bot_self_id}')
@site.auth.requires('root')
async def _get_usergroup_analysis(
    request: Request,
    bot_id: str,
    bot_self_id: str,
):
    if bot_id not in bot_val:
        retcode, msg, data = -1, '不存在该bot_id!', {}
    elif bot_self_id not in bot_val[bot_id]:
        retcode, msg, data = -1, '不存在该bot_self_id!', {}
    else:
        retcode, msg = 0, 'ok'

        xaxis = []
        series = []

        group_data = []
        user_data = []

        seven_data = await get_value_analysis(bot_id, bot_self_id)
        for day in seven_data:
            xaxis.append(day)
            local_val = seven_data[day]
            group_data.append(len(local_val['group']))
            user_data.append(len(local_val['user']))

        series.append({'name': '用户', 'type': 'bar', 'data': user_data})
        series.append(
            {
                'name': '群组',
                'type': 'bar',
                'data': group_data,
            }
        )

        data = {
            'title': {'text': ''},
            'tooltip': {'trigger': 'axis'},
            'legend': {'data': ['用户', '群组']},
            'xAxis': {'type': 'category', 'boundaryGap': False, 'data': xaxis},
            'yAxis': {'type': 'value'},
            'series': series,
        }

    return {'status': retcode, 'msg': msg, 'data': data}


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


@app.get("/corelogs")
async def core_log():
    asyncio.create_task(clear_log())
    return StreamingResponse(read_log(), media_type='text/plain')


site.mount_app(app)
