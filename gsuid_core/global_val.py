import json
import datetime
from copy import deepcopy
from typing import Dict, List, Optional, TypedDict

import aiofiles

from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.database.global_val_models import (
    CoreDataSummary,
    CoreDataAnalysis,
)

global_val_path = get_res_path(['GsCore', 'global'])
global_backup_path = get_res_path(['GsCore', 'global_backup'])


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


def get_platform_val(bot_id: Optional[str], bot_self_id: Optional[str]):
    if bot_id is None or bot_self_id is None:
        return platform_val

    if bot_id not in bot_val:
        bot_val[bot_id] = {}
    if bot_self_id not in bot_val[bot_id]:
        bot_val[bot_id][bot_self_id] = deepcopy(platform_val)
    return bot_val[bot_id][bot_self_id]


async def get_all_bot_dict():
    datas = await CoreDataSummary.get_distinct_list(
        CoreDataSummary.bot_id,  # type: ignore
    )
    bot_ids = [j for i in datas for j in i]
    print(datas)

    result = {}
    for data in bot_ids:
        result[data] = []
        self_ids = await CoreDataSummary.select_rows(bot_id=data)
        if self_ids:
            self_ids = [i.bot_self_id for i in self_ids]
            result[data] = list(set(self_ids))

    return result


async def get_value_analysis(
    bot_id: Optional[str], bot_self_id: Optional[str], day: int = 7
) -> Dict[str, PlatformVal]:
    result = {}
    result_temp = {}
    today = datetime.date.today()
    endday = today - datetime.timedelta(days=day)
    summary_datas: List[
        CoreDataSummary
    ] = await CoreDataSummary.get_recently_data(
        endday,
    )  # type: ignore
    detail_datas: List[
        CoreDataAnalysis
    ] = await CoreDataAnalysis.get_recently_data(
        endday,
    )  # type: ignore
    for row in summary_datas:
        if bot_id and row.bot_id != bot_id:
            continue
        if bot_self_id and row.bot_self_id != bot_self_id:
            continue

        day_key = row.date.strftime("%Y_%d_%b")
        result_temp[day_key] = [row]

    for row in detail_datas:
        if bot_id and row.bot_id != bot_id:
            continue
        if bot_self_id and row.bot_self_id != bot_self_id:
            continue

        day_key = row.date.strftime("%Y_%d_%b")
        result_temp[day_key].append(row)

    for i in result_temp:
        result[i] = await trans_database_to_val(result[i][0], result[i][1:])
    return result


async def get_global_analysis(
    bot_id: str,
    bot_self_id: Optional[str],
):
    seven_data = await get_value_analysis(bot_id, bot_self_id, 30)

    group_data = []
    user_data = []

    user_list: List[List[str]] = []
    group_list: List[List[str]] = []
    group_all_list: List[str] = []
    user_all_list: List[str] = []

    for day in seven_data:
        local_val = seven_data[day]
        if local_val['receive'] == 0 and local_val['send'] == 0:
            continue

        _user_list = list(local_val['user'].keys())
        _group_list = list(local_val['group'].keys())

        user_list.append(_user_list)
        user_all_list.extend(_user_list)
        group_list.append(_group_list)
        group_all_list.extend(_group_list)

        group_data.append(len(local_val['group']))
        user_data.append(len(local_val['user']))

    # 七天内的用户
    user_7_list = [user for users in user_list[:7] for user in users]
    user_1_7_list = [user for users in user_list[1:8] for user in users]
    # 七天内的群组
    group_7_list = [group for groups in group_list[:7] for group in groups]
    group_1_7_list = [group for groups in group_list[1:8] for group in groups]

    # 昨日到三十日之前的用户
    user_after_list = [user for users in user_list[1:] for user in users]
    # 昨日到三十日之前的群组
    group_after_list = [group for groups in group_list[1:] for group in groups]

    # 三十天内的用户没有在这七天出现过
    out_user = []
    # 三十天内的群组没有在这七天出现过
    out_group = []
    # 今天的用户从来没在这个月内出现过
    new_user = []
    # 今天的群组从来没在这个月内出现过
    new_group = []

    for i in group_all_list:
        if i not in group_7_list:
            out_group.append(i)

    if group_list:
        for i in group_list[0]:
            if i not in group_after_list:
                new_group.append(i)

    for i in user_all_list:
        if i not in user_7_list:
            out_user.append(i)

    if user_list:
        for i in user_list[0]:
            if i not in user_after_list:
                new_user.append(i)

    _user_all_list = list(set(user_all_list))
    _group_sll_list = list(set(group_all_list))
    out_user = list(set(out_user))
    out_group = list(set(out_group))

    # user_num = len(user_data)
    # group_num = len(group_data)

    day7_user_num = len(user_1_7_list)
    day7_group_num = len(group_1_7_list)

    data = {
        'DAU': '{0:.2f}'.format(day7_user_num / 7) if day7_user_num else 0,
        'DAG': ('{0:.2f}'.format(day7_group_num / 7) if day7_group_num else 0),
        'NU': str(len(new_user)),
        'OU': (
            '{0:.2f}%'.format((len(out_user) / len(_user_all_list)) * 100)
            if len(_user_all_list) != 0
            else "0.00%"
        ),
        'NG': str(len(new_group)),
        'OG': (
            '{0:.2f}%'.format((len(out_group) / len(_group_sll_list)) * 100)
            if len(_group_sll_list) != 0
            else "0.00%"
        ),
    }
    return data


async def load_all_global_val():
    today = datetime.date.today()
    summarys: Optional[List[CoreDataSummary]] = (
        await CoreDataSummary.select_rows(date=today)
    )
    if summarys:
        for summary in summarys:
            bot_val[summary.bot_id] = {}
            datas: Optional[List[CoreDataAnalysis]] = (
                await CoreDataAnalysis.select_rows(
                    date=today,
                    bot_id=summary.bot_id,
                    bot_self_id=summary.bot_self_id,
                )
            )
            if datas:
                platform_val = await trans_database_to_val(summary, datas)
                bot_val[summary.bot_id][summary.bot_self_id] = platform_val


async def save_all_global_val(day: int = 0):
    for bot_id in bot_val:
        for bot_self_id in bot_val[bot_id]:
            await save_global_val(bot_id, bot_self_id, day)


def merge_dict(dict1: PlatformVal, dict2: PlatformVal) -> PlatformVal:
    result = dict1.copy()

    for key, value in dict2.items():
        if key in result:
            if isinstance(value, (int, float)) and isinstance(
                result[key], (int, float)
            ):
                result[key] += value
            elif isinstance(value, dict) and isinstance(result[key], dict):
                result[key] = merge_dict(result[key], value)  # type: ignore
            else:
                result[key] = value
        else:
            result[key] = value

    return result


async def get_global_val(
    bot_id: Optional[str],
    bot_self_id: Optional[str],
    day: Optional[int] = None,
) -> PlatformVal:
    if bot_self_id is None or bot_id is None:
        pv = deepcopy(platform_val)
        today = datetime.date.today()
        summarys: Optional[List[CoreDataSummary]] = []

        if day:
            date = today - datetime.timedelta(days=day)
            _s = await CoreDataSummary.select_rows(date=date)
            if _s:
                summarys.extend(_s)
        else:
            summarys = await CoreDataSummary.select_rows(date=today)

        if summarys:
            for summary in summarys:
                datas: Optional[List[CoreDataAnalysis]] = (
                    await CoreDataAnalysis.select_rows(
                        date=summary.date,
                        bot_id=summary.bot_id,
                        bot_self_id=summary.bot_self_id,
                    )
                )
                if datas:
                    vl = await trans_database_to_val(summary, datas)
                    pv = merge_dict(
                        vl,
                        pv,
                    )

        return pv

    if day is None or day == 0:
        return get_platform_val(bot_id, bot_self_id)
    else:
        today = datetime.date.today()
        endday = today - datetime.timedelta(days=day)
        return await get_sp_val(
            bot_id,
            bot_self_id,
            endday,
        )


async def trans_database_to_val(
    summary: CoreDataSummary, datas: List[CoreDataAnalysis]
):
    platform_val: PlatformVal = {
        'user': {},
        'group': {},
        'command': 0,
        'image': 0,
        'receive': 0,
        'send': 0,
    }

    platform_val['command'] = summary.command
    platform_val['image'] = summary.image
    platform_val['receive'] = summary.receive
    platform_val['send'] = summary.send
    for data in datas:
        if data.data_type == 'user':
            platform_val['user'][data.target_id] = {
                data.command_name: data.command_count
            }
        if data.data_type == 'group':
            platform_val['group'][data.target_id] = {
                data.command_name: data.command_count
            }
    return platform_val


async def get_sp_val(
    bot_id: Optional[str],
    bot_self_id: Optional[str],
    date: datetime.date,
) -> PlatformVal:
    platform_val: PlatformVal = {
        'user': {},
        'group': {},
        'command': 0,
        'image': 0,
        'receive': 0,
        'send': 0,
    }
    if bot_id is None and bot_self_id is None:
        return platform_val

    summary = await CoreDataSummary.base_select_data(
        bot_id=bot_id,
        bot_self_id=bot_self_id,
        date=date,
    )
    if summary:
        datas = await CoreDataAnalysis.select_rows(
            bot_id=bot_id,
            bot_self_id=bot_self_id,
            date=date,
        )

        if datas:
            platform_val = await trans_database_to_val(summary, datas)

    return platform_val


async def save_global_val(bot_id: str, bot_self_id: str, day: int = 0):
    if not bot_self_id:
        return

    local_val = get_platform_val(bot_id, bot_self_id)

    today = datetime.date.today() - datetime.timedelta(days=day)
    await _save_global_val_to_database(local_val, bot_id, bot_self_id, today)


async def _save_global_val_to_database(
    local_val: PlatformVal,
    bot_id: str,
    bot_self_id: str,
    today_datetime: datetime.date,
):
    insert_datas = []
    for _g in local_val['group']:
        group_data = local_val['group'][_g]
        for command_name in group_data:
            command_count = group_data[command_name]
            insert_datas.append(
                CoreDataAnalysis(
                    data_type='group',
                    target_id=_g,
                    command_name=command_name,
                    command_count=command_count,
                    date=today_datetime,
                    bot_id=bot_id,
                    bot_self_id=bot_self_id,
                )
            )
    for _u in local_val['user']:
        user_data = local_val['user'][_u]
        for command_name in user_data:
            command_count = user_data[command_name]
            insert_datas.append(
                CoreDataAnalysis(
                    data_type='user',
                    target_id=_u,
                    command_name=command_name,
                    command_count=command_count,
                    date=today_datetime,
                    bot_id=bot_id,
                    bot_self_id=bot_self_id,
                )
            )

    await CoreDataAnalysis.batch_insert_data_with_update(
        insert_datas,
        ['command_count'],
        [
            'data_type',
            'target_id',
            'date',
            'command_name',
            'bot_id',
            'bot_self_id',
        ],
    )

    insert_summary = []
    insert_summary.append(
        CoreDataSummary(
            receive=local_val['receive'],
            send=local_val['send'],
            command=local_val['command'],
            image=local_val['image'],
            date=today_datetime,
            bot_id=bot_id,
            bot_self_id=bot_self_id,
        )
    )
    await CoreDataSummary.batch_insert_data_with_update(
        insert_summary,
        ['receive', 'send', 'command', 'image'],
        ['date', 'bot_id', 'bot_self_id'],
    )


async def trans_global_val():
    if global_val_path.exists() and any(global_val_path.iterdir()):
        logger.info('[数据迁移] 开始迁移全局数据！该LOG应该只会出现一次！')
    else:
        return

    for bot_id_path in global_val_path.iterdir():
        if not bot_id_path.is_dir():
            continue
        for bot_self_id_path in bot_id_path.iterdir():
            if not bot_id_path.is_dir():
                continue
            for json_data in bot_self_id_path.iterdir():
                if json_data.suffix == '.json':
                    date_string = json_data.stem[10:]
                    format_code = "%Y_%d_%b"

                    datetime_object = datetime.datetime.strptime(
                        date_string, format_code
                    )

                    # 2. 从 datetime 对象中提取 date 部分
                    date_object = datetime_object.date()

                    async with aiofiles.open(
                        json_data, 'r', encoding='utf-8'
                    ) as f:
                        json_str = await f.read()
                    local_val = json.loads(json_str)
                    await _save_global_val_to_database(
                        local_val,
                        bot_id_path.name,
                        bot_self_id_path.name,
                        date_object,
                    )

    # 转移路径
    if global_backup_path.exists():
        if global_backup_path.is_dir():
            # 只有在目录为空时才删除
            if not any(global_backup_path.iterdir()):
                import shutil

                shutil.rmtree(global_backup_path)
            else:
                logger.success('[数据迁移] 全局数据迁移完成！')
                return
        else:
            global_backup_path.unlink()
    global_val_path.rename(global_backup_path)
    logger.success('[数据迁移] 全局数据迁移完成！')
