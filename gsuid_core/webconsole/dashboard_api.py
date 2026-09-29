"""
Dashboard APIs
提供 Dashboard 相关的 RESTful APIs
"""

import asyncio
from typing import Any, Dict, Literal, Sequence, TypedDict
from datetime import date as dt_date, datetime, timedelta

from fastapi import Depends, Request
from async_timeout import timeout as atimeout

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import TEMP_DICT, TEMP_DICT_MAX_ENTRIES, DailyCountCache, require_auth, set_temp_dict
from gsuid_core.utils.database.global_val_models import DataType, CoreDataSummary, CoreDataAnalysis

from ._api_tags import DASHBOARD

# 三个接口并发：commands 先作废上一轮，查完再发布。失败不能留在缓存里。
_DAILY_CACHE_MAX_WAIT_S = 30.0
# 轮次标记和 TEMP_DICT 一起淘汰，避免有图无标记或有标记无图。
_DAILY_STATE_MAX_ENTRIES = TEMP_DICT_MAX_ENTRIES
_daily_cond: asyncio.Condition | None = None
_daily_version: dict[str, int] = {}
_daily_settled: set[str] = set()
_daily_failed: set[str] = set()
_DailyOutcome = Literal["ok", "failed", "timeout"]


class _DailyChartResponse(TypedDict):
    status: int
    msg: str
    data: list[dict[str, str | int]]


def _daily_condition() -> asyncio.Condition:
    global _daily_cond
    if _daily_cond is None:
        _daily_cond = asyncio.Condition()
    return _daily_cond


def _daily_version_of(cache_key: str) -> int:
    if cache_key not in _daily_version:
        return 0
    return _daily_version[cache_key]


def _drop_round(key: str) -> None:
    _daily_version.pop(key, None)
    _daily_settled.discard(key)
    _daily_failed.discard(key)
    TEMP_DICT.pop(key, None)


def _touch_daily_key(cache_key: str) -> None:
    """移到登记序末尾。超限时连同载荷一起丢掉，避免标记和图表各淘汰各的。"""
    version = _daily_version.pop(cache_key, 0)
    _daily_version[cache_key] = version
    while len(_daily_version) > _DAILY_STATE_MAX_ENTRIES:
        _drop_round(next(iter(_daily_version)))


def _version_is_stale(cache_key: str, start: int, accept_same: bool) -> bool:
    version = _daily_version_of(cache_key)
    return version < start or (version == start and not accept_same)


def _daily_outcome(cache_key: str, start: int, accept_same: bool) -> Literal["ok", "failed"] | None:
    """有成功载荷就返回 ok。已结束的失败轮返回 failed。载荷被挤掉不算查询失败。"""
    if cache_key in TEMP_DICT and cache_key not in _daily_failed:
        if cache_key in _daily_version and _version_is_stale(cache_key, start, accept_same):
            return None
        if cache_key not in _daily_version or cache_key in _daily_settled:
            return "ok"
        return None
    if cache_key not in _daily_settled:
        return None
    if _version_is_stale(cache_key, start, accept_same):
        return None
    if cache_key in _daily_failed:
        return "failed"
    return None


async def begin_daily_round(cache_key: str) -> int:
    """作废上一轮。失败占位不能让下一次并发读立刻拿到空图。"""
    cond = _daily_condition()
    async with cond:
        _touch_daily_key(cache_key)
        version = _daily_version_of(cache_key) + 1
        _daily_version[cache_key] = version
        _daily_settled.discard(cache_key)
        _daily_failed.discard(cache_key)
        TEMP_DICT.pop(cache_key, None)
        cond.notify_all()
        return version


async def publish_daily_round(cache_key: str, version: int, payload: DailyCountCache | None) -> None:
    """只发布仍是最新的那一轮；更晚的 commands 已经作废这一轮时直接丢掉。"""
    cond = _daily_condition()
    async with cond:
        _touch_daily_key(cache_key)
        if _daily_version_of(cache_key) != version:
            return
        if payload is None:
            TEMP_DICT.pop(cache_key, None)
            _daily_failed.add(cache_key)
        else:
            for stale in set_temp_dict(cache_key, payload):
                if stale != cache_key:
                    _drop_round(stale)
            _daily_failed.discard(cache_key)
        _daily_settled.add(cache_key)
        cond.notify_all()


async def _wait_for_daily_cache(cache_key: str) -> tuple[_DailyOutcome, DailyCountCache | None]:
    """等这一轮 commands。上一轮的失败结果不算数，超时与失败跟空数据分开。

    成功时在锁内把缓存对象交出去。下一轮 ``begin`` 会删掉键，锁外再查会丢数据。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _DAILY_CACHE_MAX_WAIT_S
    cond = _daily_condition()
    async with cond:
        start = _daily_version_of(cache_key)
        settled_failure = cache_key in _daily_settled and cache_key in _daily_failed
        accept_same = not settled_failure
        while True:
            outcome = _daily_outcome(cache_key, start, accept_same)
            if outcome == "ok":
                return outcome, TEMP_DICT[cache_key]
            if outcome is not None:
                return outcome, None
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "timeout", None
            try:
                # 用 async-timeout 而非 asyncio.timeout：3.11+ 上它就是同语义，跨版本也不必改
                async with atimeout(remaining):
                    await cond.wait()
            except TimeoutError:
                return "timeout", None


def _daily_blocked_response(cache_key: str, outcome: str) -> _DailyChartResponse:
    if outcome == "timeout":
        logger.warning(t("log.webconsole.dashboard_daily_wait_timeout", cache_key=cache_key))
        return {"status": 1, "msg": t("msg.webconsole.daily_stats_timeout"), "data": []}
    return {"status": 1, "msg": t("msg.webconsole.daily_stats_failed"), "data": []}


def _daily_failed_response() -> _DailyChartResponse:
    """统计出错必须与「当天确实没有命令」区分开，不能返回 status 0 的空图。"""
    return {"status": 1, "msg": t("msg.webconsole.daily_stats_failed"), "data": []}


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


@app.get("/api/dashboard/metrics", summary="获取关键指标", tags=DASHBOARD)
async def get_dashboard_metrics(request: Request, bot_id: str = "all", _user: Dict[str, Any] = Depends(require_auth)):
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
        logger.warning(t("log.webconsole.dashboard_metrics_fail", error=e))
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


@app.get("/api/dashboard/commands", summary="获取命令统计", tags=DASHBOARD)
async def get_dashboard_commands(request: Request, bot_id: str = "all", _user: Dict[str, Any] = Depends(require_auth)):
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


@app.get("/api/dashboard/users-groups", summary="获取用户群组数据", tags=DASHBOARD)
async def get_dashboard_users_groups(
    request: Request, bot_id: str = "all", _user: Dict[str, Any] = Depends(require_auth)
):
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


@app.get("/api/dashboard/daily/command-counts", summary="近 N 天每日命令总数（日历）", tags=DASHBOARD)
async def get_daily_command_counts(
    request: Request,
    days: int = 60,
    bot_id: str = "all",
    _user: Dict[str, Any] = Depends(require_auth),
):
    """近 N 天每天的命令调用总数——供 Dashboard 日期选择器展示数字 / 禁用无数据日。

    Query:
    - ``days``: 回溯天数，默认 60，夹取到 [1, 366]
    - ``bot_id``: ``all`` 或 ``bot_self_id:bot_id``

    ``data`` 为按日期升序列表，每项 ``{date, count}``；``count == 0`` 表示当天无记录。
    口径与 ``/daily/commands`` 一致：仅汇总 USER 维度的 ``command_count``。
    """
    days = max(1, min(int(days or 60), 366))
    _bot_id = None
    _bot_self_id = None
    if bot_id and bot_id != "all" and ":" in bot_id:
        _bot_self_id, _bot_id = bot_id.split(":", 1)

    try:
        today = datetime.now().date()
        start = today - timedelta(days=days - 1)
        totals = await CoreDataAnalysis.get_daily_command_totals(
            start,
            today,
            _bot_id,
            _bot_self_id,
        )
        data = []
        for offset in range(days - 1, -1, -1):
            d = today - timedelta(days=offset)
            key = d.strftime("%Y-%m-%d")
            data.append({"date": key, "count": int(totals.get(key, 0))})
        return {"status": 0, "msg": "ok", "data": data}
    except Exception as e:
        logger.exception(t("log.webconsole.fetch_daily_command_counts", error=e))
        # 降级：仍返回连续日期，count=0，避免前端日历空白
        today = datetime.now().date()
        data = []
        for offset in range(days - 1, -1, -1):
            d = today - timedelta(days=offset)
            data.append({"date": d.strftime("%Y-%m-%d"), "count": 0})
        return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/dashboard/daily/commands", summary="每日命令使用统计", tags=DASHBOARD)
async def get_daily_commands(
    request: Request, date: str, bot_id: str = "all", _user: Dict[str, Any] = Depends(require_auth)
):
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

    # key 在查库前就定下来：失败也要结束这一轮，不能把上一轮结果留给并发读者。
    try:
        cache_key = f"{_bot_id}/{_bot_self_id}/{dt_date.fromisoformat(date).strftime('%Y-%m-%d')}"
    except ValueError:
        cache_key = f"{_bot_id}/{_bot_self_id}/{date}"

    version = await begin_daily_round(cache_key)
    try:
        date_obj = dt_date.fromisoformat(date)

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

        payload: DailyCountCache = {"c_data": c_data, "g_data": g_data, "u_data": u_data}
        sorted_items = sorted(c_data.items(), key=lambda x: x[1], reverse=True)
        result = [{"command": simplify_regex_command(k), "count": v} for k, v in sorted_items]
        await publish_daily_round(cache_key, version, payload)
        return {
            "status": 0,
            "msg": "ok",
            "data": result,
        }
    except Exception as e:
        logger.exception(t("log.webconsole.dashboard_daily_commands_fail", error=e))
        await publish_daily_round(cache_key, version, None)
        return _daily_failed_response()


@app.get("/api/dashboard/daily/group-triggers", summary="每日群触发统计", tags=DASHBOARD)
async def get_daily_group_triggers(
    request: Request, date: str, bot_id: str = "all", _user: Dict[str, Any] = Depends(require_auth)
) -> _DailyChartResponse:
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
        cache_key = f"{_bot_id}/{_bot_self_id}/{date_obj.strftime('%Y-%m-%d')}"

        outcome, payload = await _wait_for_daily_cache(cache_key)
        if payload is None:
            return _daily_blocked_response(cache_key, outcome)

        g_data = payload["g_data"]
        c_data = payload["c_data"]

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
        result: list[dict[str, str | int]] = []
        for group_id, cmds in g_data.items():
            group_data: dict[str, str | int] = {"group": group_id}
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
        logger.warning(t("log.webconsole.fetch_daily_group_triggers", error=e))
        return _daily_failed_response()


@app.get("/api/dashboard/daily/personal-triggers", summary="每日个人触发统计", tags=DASHBOARD)
async def get_daily_personal_triggers(
    request: Request, date: str, bot_id: str = "all", _user: Dict[str, Any] = Depends(require_auth)
) -> _DailyChartResponse:
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
        cache_key = f"{_bot_id}/{_bot_self_id}/{date_obj.strftime('%Y-%m-%d')}"

        outcome, payload = await _wait_for_daily_cache(cache_key)
        if payload is None:
            return _daily_blocked_response(cache_key, outcome)

        u_data = payload["u_data"]
        c_data = payload["c_data"]

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
        result: list[dict[str, str | int]] = []
        for user_id, cmds in u_data.items():
            user_data: dict[str, str | int] = {"user": user_id}
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
        logger.warning(t("log.webconsole.fetch_daily_personal_triggers", error=e))
        return _daily_failed_response()


@app.get("/api/dashboard/bots", summary="获取 Bot 列表", tags=DASHBOARD)
async def get_dashboard_bots(_user: Dict[str, Any] = Depends(require_auth)):
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
        logger.warning(t("log.webconsole.dashboard_bot_list_fail", error=e))
        return {
            "status": 0,
            "msg": "ok",
            "data": [{"id": "all", "name": "汇总"}],
        }
