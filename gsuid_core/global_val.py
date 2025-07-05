import json
import datetime
from copy import deepcopy
from typing import Any, Set, Dict, List, Tuple, Optional, Sequence, TypedDict

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
    user_count: int
    group_count: int
    group: Dict[str, Dict[str, int]]
    user: Dict[str, Dict[str, int]]


GlobalVal = Dict[str, PlatformVal]
BotVal = Dict[str, GlobalVal]

platform_val: PlatformVal = {
    'receive': 0,
    'send': 0,
    'command': 0,
    'image': 0,
    'user_count': 0,
    'group_count': 0,
    'group': {},
    'user': {},
}

bot_val: BotVal = {}


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


def get_platform_val(bot_id: Optional[str], bot_self_id: Optional[str]):
    if bot_id is None or bot_self_id is None:
        md = deepcopy(platform_val)
        for bot_id in bot_val:
            for bot_self_id in bot_val[bot_id]:
                md = merge_dict(bot_val[bot_id][bot_self_id], md)
        return md

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

    result = {}
    for data in bot_ids:
        result[data] = []
        self_ids = await CoreDataSummary.select_rows(bot_id=data)
        if self_ids:
            self_ids = [i.bot_self_id for i in self_ids]
            result[data] = list(set(self_ids))

    return result


async def get_value_analysis(
    bot_id: Optional[str],
    bot_self_id: Optional[str],
    day: int = 7,
    need_all: bool = False,
) -> Tuple[Dict[str, PlatformVal], Dict[str, PlatformVal]]:
    result = {}
    result_all = {}
    result_temp = {}
    result_temp_all = {}

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
        if need_all:
            day_key = row.date.strftime("%Y_%d_%b")
            result_temp_all[day_key] = [row]

        if (bot_id and row.bot_id != bot_id) or (
            bot_self_id and row.bot_self_id != bot_self_id
        ):
            continue

        day_key = row.date.strftime("%Y_%d_%b")
        result_temp[day_key] = [row]

    for row in detail_datas:
        if need_all:
            day_key = row.date.strftime("%Y_%d_%b")
            result_temp_all[day_key].append(row)

        if (bot_id and row.bot_id != bot_id) or (
            bot_self_id and row.bot_self_id != bot_self_id
        ):
            continue

        day_key = row.date.strftime("%Y_%d_%b")
        result_temp[day_key].append(row)

    for i in result_temp:
        result[i] = await trans_database_to_val(
            result_temp[i][0], result_temp[i][1:]
        )

    for i in result_temp_all:
        result_all[i] = await trans_database_to_val(
            result_temp_all[i][0], result_temp_all[i][1:]
        )

    return result, result_all


async def get_global_analysis(
    data: Dict[str, PlatformVal],
) -> Dict[str, Any]:
    try:
        sorted_days = sorted(data.keys(), reverse=True)
        if not sorted_days:
            return {
                'DAU': 0,
                'DAG': 0,
                'NU': '0',
                'OU': "0.00%",
                'NG': '0',
                'OG': "0.00%",
            }
    except (TypeError, ValueError):
        # 如果key不是可比较的类型，则返回错误或默认值
        # 这里选择返回默认值
        return {
            'DAU': 0,
            'DAG': 0,
            'NU': '0',
            'OU': "0.00%",
            'NG': '0',
            'OG': "0.00%",
        }

    # 2. 一次遍历，直接构建每日的用户和群组集合
    user_sets_by_day: List[Set[str]] = []
    group_sets_by_day: List[Set[str]] = []

    for day in sorted_days:
        local_val = data[day]
        if local_val.get('receive', 0) == 0 and local_val.get('send', 0) == 0:
            user_sets_by_day.append(set())
            group_sets_by_day.append(set())
            continue

        user_sets_by_day.append(set(local_val.get('user', {}).keys()))
        group_sets_by_day.append(set(local_val.get('group', {}).keys()))

    # 3. 使用集合运算高效计算各项指标

    # --- 指标计算所需集合 ---
    # 总用户/群组 (30天内所有不重复的用户/群组)
    all_users = set().union(*user_sets_by_day)
    all_groups = set().union(*group_sets_by_day)

    # 今天（day 0）的用户/群组
    todays_users = user_sets_by_day[0] if user_sets_by_day else set()
    todays_groups = group_sets_by_day[0] if group_sets_by_day else set()

    # 最近7天（day 0-6）的用户/群组
    recent_7_days_users = set().union(*user_sets_by_day[:7])
    recent_7_days_groups = set().union(*group_sets_by_day[:7])

    # 过去的用户/群组（day 1-29）
    past_users = set().union(*user_sets_by_day[1:])
    past_groups = set().union(*group_sets_by_day[1:])

    # 用于计算 DAU/DAG 的用户/群组 (day 1-7)
    # 对应原代码的 user_list[1:8]
    dau_users_list = [user for s in user_sets_by_day[1:8] for user in s]
    dag_groups_list = [group for s in group_sets_by_day[1:8] for group in s]

    # --- 开始计算 ---
    # 新用户/群组: 今天出现，但在过去29天未出现
    new_users = todays_users - past_users
    new_groups = todays_groups - past_groups

    # 流失用户/群组: 30天内出现过，但在最近7天未出现
    out_users = all_users - recent_7_days_users
    out_groups = all_groups - recent_7_days_groups

    # DAU/DAG
    day7_user_num = len(dau_users_list)
    day7_group_num = len(dag_groups_list)

    dau = day7_user_num / 7 if day7_user_num else 0
    dag = day7_group_num / 7 if day7_group_num else 0

    # 流失率
    out_user_rate = (len(out_users) / len(all_users)) * 100 if all_users else 0
    out_group_rate = (
        (len(out_groups) / len(all_groups)) * 100 if all_groups else 0
    )

    result_data = {
        'DAU': f'{dau:.2f}',
        'DAG': f'{dag:.2f}',
        'NU': str(len(new_users)),
        'OU': f'{out_user_rate:.2f}%',
        'NG': str(len(new_groups)),
        'OG': f'{out_group_rate:.2f}%',
    }
    return result_data


async def load_all_global_val():
    today = datetime.date.today()
    summarys: Optional[Sequence[CoreDataSummary]] = (
        await CoreDataSummary.select_rows(date=today)
    )
    if summarys:
        for summary in summarys:
            bot_val[summary.bot_id] = {}
            datas: Optional[Sequence[CoreDataAnalysis]] = (
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


async def trans_database_to_val(
    summary: CoreDataSummary, datas: Sequence[CoreDataAnalysis]
):
    pv: PlatformVal = deepcopy(platform_val)

    pv['command'] = summary.command
    pv['image'] = summary.image
    pv['receive'] = summary.receive
    pv['send'] = summary.send
    for data in datas:
        if data.data_type == 'user':
            pv['user'][data.target_id] = {
                data.command_name: data.command_count
            }
        if data.data_type == 'group':
            pv['group'][data.target_id] = {
                data.command_name: data.command_count
            }
    return pv


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
            user_count=len(local_val['user']),
            group_count=len(local_val['group']),
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


async def get_global_val(
    bot_id: Optional[str],
    bot_self_id: Optional[str],
    day: Optional[int] = None,
) -> PlatformVal:
    if bot_self_id is None or bot_id is None:
        pv = deepcopy(platform_val)
        today = datetime.date.today()
        summarys: Optional[Sequence[CoreDataSummary]] = []

        if day:
            date = today - datetime.timedelta(days=day)
            _s = await CoreDataSummary.select_rows(date=date)
            if _s:
                summarys.extend(_s)
        else:
            summarys = await CoreDataSummary.select_rows(date=today)

        if summarys:
            for summary in summarys:
                datas: Optional[Sequence[CoreDataAnalysis]] = (
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


async def get_sp_val(
    bot_id: Optional[str],
    bot_self_id: Optional[str],
    date: datetime.date,
) -> PlatformVal:
    pv = deepcopy(platform_val)
    if bot_id is None and bot_self_id is None:
        return pv

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
            pv = await trans_database_to_val(summary, datas)

    return pv
