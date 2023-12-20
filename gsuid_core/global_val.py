import json
import datetime
from copy import deepcopy
from typing import Dict, Optional, TypedDict

import aiofiles

from gsuid_core.data_store import get_res_path

global_val_path = get_res_path(['GsCore', 'global'])


class PlatformVal(TypedDict):
    receive: int
    send: int
    command: int
    image: int
    group: Dict[str, Dict[str, int]]
    user: Dict[str, Dict[str, int]]


GlobalVal = Dict[str, PlatformVal]
BotVal = Dict[str, GlobalVal]

platform_val: PlatformVal = {
    'receive': 0,
    'send': 0,
    'command': 0,
    'image': 0,
    'group': {},
    'user': {},
}

bot_val: BotVal = {}


def get_platform_val(bot_id: str, bot_self_id: str):
    if bot_id not in bot_val:
        bot_val[bot_id] = {}
    if bot_self_id not in bot_val[bot_id]:
        bot_val[bot_id][bot_self_id] = deepcopy(platform_val)

    return bot_val[bot_id][bot_self_id]


async def load_all_global_val():
    global bot_val
    today = datetime.date.today()
    date_format = today.strftime("%Y_%d_%b")

    for bot_id_path in global_val_path.iterdir():
        if bot_id_path.stem not in bot_val:
            bot_val[bot_id_path.stem] = {}
        for self_id_path in bot_id_path.iterdir():
            path = self_id_path / f'GlobalVal_{ date_format}.json'
            if path.exists():
                async with aiofiles.open(path, 'r') as fp:
                    data = json.loads(await fp.read())
                    bot_val[bot_id_path.stem][self_id_path.stem] = data
            else:
                bot_val[bot_id_path.stem][self_id_path.stem] = deepcopy(
                    platform_val
                )


async def save_all_global_val():
    global bot_val
    for bot_id in bot_val:
        for bot_self_id in bot_val[bot_id]:
            await save_global_val(bot_id, bot_self_id)


async def get_global_val(
    bot_id: str, bot_self_id: str, day: Optional[int] = None
) -> PlatformVal:
    if day is None:
        return get_platform_val(bot_id, bot_self_id)
    else:
        today = datetime.date.today()
        endday = today - datetime.timedelta(days=day)
        endday_format = endday.strftime("%Y_%d_%b")
        _path = global_val_path / bot_id / bot_self_id
        path = _path / f'GlobalVal_{endday_format}.json'
        if path.exists():
            async with aiofiles.open(path, 'r') as fp:
                data = json.loads(await fp.read())
                return data
        else:
            return platform_val


async def save_global_val(bot_id: str, bot_self_id: str):
    local_val = get_platform_val(bot_id, bot_self_id)

    today = datetime.date.today()
    date_format = today.strftime("%Y_%d_%b")

    path = global_val_path / bot_id / bot_self_id
    if not path.exists():
        path.mkdir()

    async with aiofiles.open(
        path / f'GlobalVal_{date_format}.json', 'w', encoding='utf8'
    ) as fp:
        await fp.write(
            json.dumps(
                local_val,
                indent=4,
                ensure_ascii=False,
            )
        )
