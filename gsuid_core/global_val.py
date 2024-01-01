import json
import datetime
from copy import deepcopy
from typing import Dict, List, Optional, TypedDict

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


def get_all_bot_dict():
    data = {}
    for bot_id_path in global_val_path.iterdir():
        if bot_id_path.is_dir():
            data[bot_id_path.name] = []
            for self_id_path in bot_id_path.iterdir():
                if self_id_path.is_dir():
                    data[bot_id_path.name].append(self_id_path.name)
    return data


async def get_value_analysis(
    bot_id: str, bot_self_id: str, day: int = 7
) -> Dict[str, PlatformVal]:
    result = {}
    for day in range(day):
        today = datetime.date.today()
        endday = today - datetime.timedelta(days=day)
        endday_format = endday.strftime("%Y_%d_%b")

        local_val = await get_global_val(bot_id, bot_self_id, day)
        result[endday_format] = local_val
    return result


async def get_global_analysis(bot_id: str, bot_self_id: str):
    seven_data = await get_value_analysis(bot_id, bot_self_id)

    group_data = []
    user_data = []

    user_list: List[List[str]] = []
    user_all_list: List[str] = []

    for day in seven_data:
        local_val = seven_data[day]
        if local_val['receive'] == 0 and local_val['send'] == 0:
            continue

        _user_list = list(local_val['user'].keys())

        user_list.append(_user_list)
        user_all_list.extend(_user_list)

        group_data.append(len(local_val['group']))
        user_data.append(len(local_val['user']))

    user_before_list = [user for users in user_list[:-1] for user in users]
    user_after_list = [user for users in user_list[1:] for user in users]

    out_user = []
    new_user = []
    for i in user_list[0]:
        if i not in user_before_list:
            out_user.append(i)

    for i in user_list[-1]:
        if i not in user_after_list:
            new_user.append(i)

    _user_all_list = list(set(user_all_list))

    data = {
        'DAU': '{0:.2f}'.format(sum(user_data) / len(user_data)),
        'DAG': '{0:.2f}'.format(sum(group_data) / len(group_data)),
        'NU': str(len(new_user)),
        'OU': '{0:.2f}%'.format((len(out_user) / len(_user_all_list)) * 100)
         if len(_user_all_list) != 0
         else "0.00%",
    }
    return data


async def load_all_global_val():
    global bot_val
    today = datetime.date.today()
    date_format = today.strftime("%Y_%d_%b")

    for bot_id_path in global_val_path.iterdir():
        if bot_id_path.stem not in bot_val:
            bot_val[bot_id_path.stem] = {}
        for self_id_path in bot_id_path.iterdir():
            path = self_id_path / f'GlobalVal_{date_format}.json'
            if self_id_path.is_dir() and path.exists():
                async with aiofiles.open(path, 'rb') as fp:
                    data = json.loads(await fp.read())
                    bot_val[bot_id_path.stem][self_id_path.stem] = data


async def save_all_global_val():
    global bot_val
    for bot_id in bot_val:
        for bot_self_id in bot_val[bot_id]:
            await save_global_val(bot_id, bot_self_id)


async def get_global_val(
    bot_id: str, bot_self_id: str, day: Optional[int] = None
) -> PlatformVal:
    if day is None or day == 0:
        return get_platform_val(bot_id, bot_self_id)
    else:
        today = datetime.date.today()
        endday = today - datetime.timedelta(days=day)
        endday_format = endday.strftime("%Y_%d_%b")
        _path = global_val_path / bot_id / bot_self_id
        path = _path / f'GlobalVal_{endday_format}.json'
        if path.exists():
            async with aiofiles.open(path, 'rb') as fp:
                data = json.loads(await fp.read())
                return data
        else:
            return platform_val


async def save_global_val(bot_id: str, bot_self_id: str):
    if not bot_self_id:
        return

    local_val = get_platform_val(bot_id, bot_self_id)

    today = datetime.date.today()
    date_format = today.strftime("%Y_%d_%b")

    path = global_val_path / bot_id / bot_self_id
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)

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
