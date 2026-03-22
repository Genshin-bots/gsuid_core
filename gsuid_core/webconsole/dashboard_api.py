"""
Dashboard APIs
提供 Dashboard 相关的 RESTful APIs
"""

import asyncio
from typing import Dict, Sequence
from datetime import date as dt_date, datetime, timedelta

from fastapi import Depends, Request

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import TEMP_DICT, require_auth
from gsuid_core.utils.database.global_val_models import DataType, CoreDataSummary, CoreDataAnalysis


def simplify_regex_command(command: str) -> str:
    """
    简化正则表达式命令，提取关键信息便于显示
    """
    # 如果不是正则表达式，直接返回
    if not command.startswith("^") and "(?P<" not in command and "(?:" not in command:
        return command

    # 使用堆栈来匹配括号，找到第一个包含 | 的捕获组
    stack = []
    i = 0
    while i < len(command):
        if command[i : i + 4] == "(?P<":
            # 命名捕获组开始
            gt_pos = command.find(">", i)
            if gt_pos != -1:
                stack.append(("named", i, gt_pos))
                i = gt_pos + 1
                continue
        elif command[i : i + 3] == "(?:":
            # 非捕获组开始
            stack.append(("non_capture", i))
            i += 3
            continue
        elif command[i] == "(":
            stack.append(("capture", i))
            i += 1
            continue
        elif command[i] == ")":
            # 结束一个组
            if stack:
                group_type, start, *extra = stack.pop()
                if group_type == "named":
                    gt_pos = extra[0]
                    # 提取 > 后面的内容
                    inner = command[gt_pos + 1 : i]
                    if "|" in inner:
                        return inner.split("|")[0]
                elif group_type == "non_capture":
                    # 提取 (?: 后面的内容
                    inner = command[start + 3 : i]
                    if "|" in inner:
                        return inner.split("|")[0]
                elif group_type == "capture":
                    inner = command[start + 1 : i]
                    if "|" in inner:
                        return inner.split("|")[0]
            i += 1
            continue
        i += 1

    return command


@app.get("/api/dashboard/metrics")
async def get_dashboard_metrics(request: Request, bot_id: str = "all", _user: Dict = Depends(require_auth)):
    """
    获取 Dashboard 的关键指标数据

    包括日活用户(DAU)、日活群(DAG)、月活用户(MAU)、月活群(MAG)、
    留存率、新增用户、流失用户等核心数据。

    Args:
        request: FastAPI 请求对象
        bot_id: Bot ID 筛选，格式为 bot_self_id:bot_id 或 "all"
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 包含 dau、dag、mau、mag、retention、newUsers、churnedUsers 等字段
    """
    # 解析bot_id参数，支持格式：bot_self_id:bot_id 或者 "all"
    _bot_id = None
    _bot_self_id = None
    if bot_id and bot_id != "all" and ":" in bot_id:
        _bot_self_id, _bot_id = bot_id.split(":", 1)

    try:
        # 获取真实的看板指标数据
        data = await CoreDataAnalysis.calculate_dashboard_metrics(
            _bot_id,
            _bot_self_id,
        )

        # 转换为前端期望的格式
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "dau": float(data.get("DAU", 0)),
                "dag": float(data.get("DAG", 0)),
                "mau": int(data.get("MAU", 0)),
                "mag": int(data.get("MAG", 0)),
                "retention": data.get("DAU_MAU", "0%"),
                "newUsers": int(data.get("NewUser", 0)),
                "churnedUsers": float(data.get("OutUser", "0").rstrip("%")),
                "dauMauRatio": data.get("DAU_MAU", "0").rstrip("%") if "DAU_MAU" in data else "0",
                "dagMagRatio": data.get("DAG_MAG", "0").rstrip("%") if "DAG_MAG" in data else "0",
            },
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.warning(f"Failed to fetch dashboard metrics: {e}")
        # Fallback to mock data if no real data
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "dau": 0,
                "dag": 0,
                "mau": 0,
                "mag": 0,
                "retention": "0%",
                "newUsers": 0,
                "churnedUsers": 0,
                "dauMauRatio": "0",
                "dagMagRatio": "0",
            },
        }


@app.get("/api/dashboard/commands")
async def get_dashboard_commands(request: Request, bot_id: str = "all", _user: Dict = Depends(require_auth)):
    """
    获取最近 30 天的命令使用统计

    按日期返回每天的命令发送数、接收数和调用次数。

    Args:
        request: FastAPI 请求对象
        bot_id: Bot ID 筛选，格式为 bot_self_id:bot_id 或 "all"
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 包含 date、sentCommands、receivedCommands、commandCalls、imageGenerated 的列表
    """
    data = []
    now = datetime.now()

    # 解析bot_id参数，支持格式：bot_self_id:bot_id 或者 "all"
    actual_bot_id = None
    actual_bot_self_id = None
    if bot_id and bot_id != "all" and ":" in bot_id:
        actual_bot_self_id, actual_bot_id = bot_id.split(":", 1)

    # 获取数据
    datas = await CoreDataSummary.get_day_trends(actual_bot_id, actual_bot_self_id)

    # 确定使用的key
    if actual_bot_id is None or actual_bot_self_id is None:
        key = "all_bots"
    else:
        key = "bot"

    for i in range(29, -1, -1):
        date = now - timedelta(days=i)
        day_index = 45 - i  # 因为get_day_trends返回最近46天的数据，索引0是45天前，45是今天
        data.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "sentCommands": datas[f"{key}_send"][day_index],
                "receivedCommands": datas[f"{key}_receive"][day_index],
                "commandCalls": datas[f"{key}_command"][day_index],
                "imageGenerated": datas[f"{key}_image"][day_index],
            }
        )
    return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/dashboard/users-groups")
async def get_dashboard_users_groups(request: Request, bot_id: str = "all", _user: Dict = Depends(require_auth)):
    """
    获取最近 30 天的用户和群组数据

    按日期返回每天的用户数和群组数统计。

    Args:
        request: FastAPI 请求对象
        bot_id: Bot ID 筛选，格式为 bot_self_id:bot_id 或 "all"
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 包含 date、users、groups 的列表
    """
    data = []
    now = datetime.now()

    # 解析bot_id参数，支持格式：bot_self_id:bot_id 或者 "all"
    actual_bot_id = None
    actual_bot_self_id = None
    if bot_id and bot_id != "all" and ":" in bot_id:
        actual_bot_self_id, actual_bot_id = bot_id.split(":", 1)

    # 获取数据
    datas = await CoreDataSummary.get_day_trends(actual_bot_id, actual_bot_self_id)

    # 确定使用的key
    if actual_bot_id is None or actual_bot_self_id is None:
        group_key = "all_bots_group_count"
        user_key = "all_bots_user_count"
    else:
        group_key = "bot_group_count"
        user_key = "bot_user_count"

    for i in range(29, -1, -1):
        date = now - timedelta(days=i)
        day_index = 45 - i  # 因为get_day_trends返回最近46天的数据，索引0是45天前，45是今天
        data.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "users": datas[user_key][day_index],
                "groups": datas[group_key][day_index],
            }
        )
    return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/dashboard/daily/commands")
async def get_daily_commands(request: Request, date: str, bot_id: str = "all", _user: Dict = Depends(require_auth)):
    """
    获取指定日期的命令使用统计

    返回该日期各命令的调用次数排行。

    Args:
        request: FastAPI 请求对象
        date: 查询日期，格式为 YYYY-MM-DD
        bot_id: Bot ID 筛选，格式为 bot_self_id:bot_id 或 "all"
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 命令统计列表，每项包含 command 和 count
    """
    # 解析bot_id参数，支持格式：bot_self_id:bot_id 或者 "all"
    _bot_id = None
    _bot_self_id = None
    if bot_id and bot_id != "all" and ":" in bot_id:
        _bot_self_id, _bot_id = bot_id.split(":", 1)

    try:
        date_obj = dt_date.fromisoformat(date)
        date_format = date_obj.strftime("%Y-%m-%d")

        # 获取数据
        datas: Sequence[CoreDataAnalysis] = await CoreDataAnalysis.get_sp_data(
            date_obj,
            _bot_id,
            _bot_self_id,
        )

        c_data: Dict[str, int] = {}
        g_data: Dict[str, Dict[str, int]] = {}
        u_data: Dict[str, Dict[str, int]] = {}
        for d in datas:
            if d.data_type == DataType.USER:
                if d.command_name not in c_data:
                    c_data[d.command_name] = 0
                c_data[d.command_name] += d.command_count

                if d.target_id not in u_data:
                    u_data[d.target_id] = {}
                if d.command_name not in u_data[d.target_id]:
                    u_data[d.target_id][d.command_name] = 0
                u_data[d.target_id][d.command_name] += d.command_count

            if d.data_type == DataType.GROUP:
                if d.target_id not in g_data:
                    g_data[d.target_id] = {}
                if d.command_name not in g_data[d.target_id]:
                    g_data[d.target_id][d.command_name] = 0
                g_data[d.target_id][d.command_name] += d.command_count

        TEMP_DICT[f"{_bot_id}/{_bot_self_id}/{date_format}"] = {
            "c_data": c_data,
            "g_data": g_data,
            "u_data": u_data,
        }

        # 按值从大到小排序
        sorted_items = sorted(c_data.items(), key=lambda x: x[1], reverse=True)
        result = [{"command": simplify_regex_command(k), "count": v} for k, v in sorted_items]

        return {
            "status": 0,
            "msg": "ok",
            "data": result,
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.exception(f"Failed to fetch daily commands: {e}")
        return {
            "status": 0,
            "msg": "ok",
            "data": [],
        }


@app.get("/api/dashboard/daily/group-triggers")
async def get_daily_group_triggers(
    request: Request, date: str, bot_id: str = "all", _user: Dict = Depends(require_auth)
):
    """
    获取指定日期的群组命令触发统计

    返回该日期各群组的命令触发排行（取前20个群组）。

    Args:
        request: FastAPI 请求对象
        date: 查询日期，格式为 YYYY-MM-DD
        bot_id: Bot ID 筛选，格式为 bot_self_id:bot_id 或 "all"
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 群组触发统计列表
    """
    # 解析bot_id参数，支持格式：bot_self_id:bot_id 或者 "all"
    _bot_id = None
    _bot_self_id = None
    if bot_id and bot_id != "all" and ":" in bot_id:
        _bot_self_id, _bot_id = bot_id.split(":", 1)

    try:
        date_obj = dt_date.fromisoformat(date)
        date_format = date_obj.strftime("%Y-%m-%d")
        cache_key = f"{_bot_id}/{_bot_self_id}/{date_format}"

        # 等待数据准备好
        while cache_key not in TEMP_DICT:
            await asyncio.sleep(0.5)

        g_data = TEMP_DICT[cache_key]["g_data"]
        c_data = TEMP_DICT[cache_key]["c_data"]

        # 计算每个群组的命令总数，取前20个
        group_total = {gid: sum(cmds.values()) for gid, cmds in g_data.items()}
        top_groups = sorted(group_total.items(), key=lambda x: x[1], reverse=True)[:20]
        g_data = {gid: g_data[gid] for gid, _ in top_groups}

        # 获取前8个命令，其他合并为"其他命令"
        sorted_commands = sorted(c_data.items(), key=lambda x: x[1], reverse=True)
        # 创建原始命令到简化命令的映射
        cmd_mapping = {k: simplify_regex_command(k) for k, v in sorted_commands[:8]}
        # 获取简化的命令名称列表
        top_commands = list(cmd_mapping.values()) + ["其他命令"]

        # 构建结果
        result = []
        for group_id, cmds in g_data.items():
            group_data = {"group": group_id}
            others = 0
            for cmd, count in cmds.items():
                simplified_cmd = cmd_mapping.get(cmd)
                if simplified_cmd and simplified_cmd in top_commands:
                    group_data[simplified_cmd] = count
                else:
                    others += count
            # 补全所有top命令，没有的设为0
            for cmd in top_commands[:-1]:
                if cmd not in group_data:
                    group_data[cmd] = 0
            group_data["其他命令"] = others
            result.append(group_data)

        return {
            "status": 0,
            "msg": "ok",
            "data": result,
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.warning(f"Failed to fetch daily group triggers: {e}")
        return {
            "status": 0,
            "msg": "ok",
            "data": [],
        }


@app.get("/api/dashboard/daily/personal-triggers")
async def get_daily_personal_triggers(
    request: Request, date: str, bot_id: str = "all", _user: Dict = Depends(require_auth)
):
    """
    获取指定日期的个人命令触发统计

    返回该日期各用户的命令触发排行（取前20个用户）。

    Args:
        request: FastAPI 请求对象
        date: 查询日期，格式为 YYYY-MM-DD
        bot_id: Bot ID 筛选，格式为 bot_self_id:bot_id 或 "all"
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 个人触发统计列表
    """
    # 解析bot_id参数，支持格式：bot_self_id:bot_id 或者 "all"
    _bot_id = None
    _bot_self_id = None
    if bot_id and bot_id != "all" and ":" in bot_id:
        _bot_self_id, _bot_id = bot_id.split(":", 1)

    try:
        date_obj = dt_date.fromisoformat(date)
        date_format = date_obj.strftime("%Y-%m-%d")
        cache_key = f"{_bot_id}/{_bot_self_id}/{date_format}"

        # 等待数据准备好
        while cache_key not in TEMP_DICT:
            await asyncio.sleep(0.5)

        u_data = TEMP_DICT[cache_key]["u_data"]
        c_data = TEMP_DICT[cache_key]["c_data"]

        # 计算每个用户的命令总数，取前20个
        user_total = {uid: sum(cmds.values()) for uid, cmds in u_data.items()}
        top_users = sorted(user_total.items(), key=lambda x: x[1], reverse=True)[:20]
        u_data = {uid: u_data[uid] for uid, _ in top_users}

        # 获取前8个命令，其他合并为"其他命令"
        sorted_commands = sorted(c_data.items(), key=lambda x: x[1], reverse=True)
        # 创建原始命令到简化命令的映射
        cmd_mapping = {k: simplify_regex_command(k) for k, v in sorted_commands[:8]}
        # 获取简化的命令名称列表
        top_commands = list(cmd_mapping.values()) + ["其他命令"]

        # 构建结果
        result = []
        for user_id, cmds in u_data.items():
            user_data = {"user": user_id}
            others = 0
            for cmd, count in cmds.items():
                simplified_cmd = cmd_mapping.get(cmd)
                if simplified_cmd and simplified_cmd in top_commands:
                    user_data[simplified_cmd] = count
                else:
                    others += count
            # 补全所有top命令，没有的设为0
            for cmd in top_commands[:-1]:
                if cmd not in user_data:
                    user_data[cmd] = 0
            user_data["其他命令"] = others
            result.append(user_data)

        return {
            "status": 0,
            "msg": "ok",
            "data": result,
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.warning(f"Failed to fetch daily personal triggers: {e}")
        return {
            "status": 0,
            "msg": "ok",
            "data": [],
        }


@app.get("/api/dashboard/bots")
async def get_dashboard_bots(_user: Dict = Depends(require_auth)):
    """
    获取所有可用的 Bot 列表

    返回所有已注册的 bot_id - bot_self_id 对，用于 Dashboard 页面的 Bot 选择器。

    Args:
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: Bot 列表，每项包含 id 和 name
    """
    try:
        # 从CoreDataSummary获取所有bot
        bots = await CoreDataSummary.get_all_bots()

        # 转换为前端期望的格式
        # 格式: bot_self_id:bot_id (后端API使用这种格式来区分不同bot)
        bot_list = [
            {"id": "all", "name": "汇总"},
        ]

        for bot in bots:
            bot_id = bot.get("bot_id", "")
            bot_self_id = bot.get("bot_self_id", "")
            if bot_id and bot_self_id:
                # 使用 bot_self_id:bot_id 格式作为id
                bot_list.append(
                    {
                        "id": f"{bot_self_id}:{bot_id}",
                        "name": f"{bot_self_id} ({bot_id})",
                    }
                )

        return {
            "status": 0,
            "msg": "ok",
            "data": bot_list,
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.warning(f"Failed to fetch bot list: {e}")
        return {
            "status": 0,
            "msg": "ok",
            "data": [{"id": "all", "name": "汇总"}],
        }
