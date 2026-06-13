"""
Logs APIs
提供日志相关的 RESTful APIs
"""

import json
from typing import Dict, List, Optional
from datetime import datetime

from fastapi import Body, Query, Depends, Request
from pydantic import Field, BaseModel
from fastapi.responses import StreamingResponse

from gsuid_core.logger import (
    LOG_PATH,
    LogEntry,
    HistoryLogData,
    read_log,
    get_all_log_path,
)
from gsuid_core.data_store import LOGS_CONFIG_PATH
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

# 可供用户选择持久化的日志级别集合（与 GET /api/logs/levels 中的真实 value 对齐）
# 注意：不包含 "all" —— 它是前端 UI 标志，不是真实日志级别。
LOG_LEVEL_VALUES: List[str] = [
    "trace",
    "debug",
    "info",
    "success",
    "warning",
    "error",
    "critical",
]

# 默认日志配置（与前端文档保持一致）
DEFAULT_LOGS_CONFIG: Dict = {
    "visible_levels": ["debug", "info", "warning", "error"],
}


class LogsConfigRequest(BaseModel):
    """日志控制台配置请求模型"""

    visible_levels: List[str] = Field(default_factory=list)


def _sanitize_visible_levels(values: Optional[List[str]]) -> List[str]:
    """校验并清理 visible_levels：
    - 仅保留 LOG_LEVEL_VALUES 集合内的小写字符串
    - 保留用户提交顺序，去重
    - 允许空列表（表示用户主动全不选）
    """
    if not values:
        return []
    seen: List[str] = []
    seen_set: set = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        v = raw.strip().lower()
        if not v or v == "all":
            continue
        if v not in LOG_LEVEL_VALUES:
            continue
        if v in seen_set:
            continue
        seen.append(v)
        seen_set.add(v)
    return seen


def _merge_defaults(config: Optional[Dict]) -> Dict:
    """将存储中的旧配置与当前默认配置合并，确保响应体始终包含完整字段集"""
    if not isinstance(config, dict):
        return dict(DEFAULT_LOGS_CONFIG)
    merged = dict(DEFAULT_LOGS_CONFIG)
    raw_levels = config.get("visible_levels")
    merged["visible_levels"] = _sanitize_visible_levels(raw_levels)
    return merged


def load_logs_config() -> Optional[Dict]:
    """Load logs console config from file"""
    if LOGS_CONFIG_PATH.exists():
        try:
            with open(LOGS_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_logs_config(config: Dict) -> bool:
    """Save logs console config to file"""
    try:
        LOGS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOGS_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


@app.get("/api/logs")
async def get_logs(
    request: Request,
    date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    level: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    _user: Dict = Depends(require_auth),
):
    """
    获取日志列表

    支持按日期/日期范围、级别、来源、文本搜索过滤和分页。

    Args:
        request: FastAPI 请求对象
        date: 单个日期，格式 YYYY-MM-DD，默认今天
        start_date: 开始日期，格式 YYYY-MM-DD，与 end_date 配合使用
        end_date: 结束日期，格式 YYYY-MM-DD，与 start_date 配合使用
        level: 日志级别筛选 (info/warn/error/debug)
        source: 来源筛选
        search: 文本搜索，匹配日志内容
        page: 页码，默认1
        per_page: 每页数量，默认50
        _user: 认证用户信息

    Returns:
        status: 0成功，404日期不存在
        data: 包含 count、rows、page、per_page 的分页对象
    """
    if start_date and end_date:
        # Multi-date range search
        all_log_files: list[LogEntry] = []
        from datetime import timedelta

        current_date = datetime.strptime(start_date, "%Y-%m-%d")
        end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")

        while current_date <= end_date_obj:
            date_str = current_date.strftime("%Y-%m-%d")
            log_file_path = LOG_PATH / f"{date_str}.log"
            if log_file_path.exists():
                history_log_data = HistoryLogData()
                logs = await history_log_data.get_parse_logs(log_file_path)
                for log in logs:
                    log["_date"] = date_str
                all_log_files.extend(logs)
            current_date += timedelta(days=1)

        log_files = all_log_files
    else:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        if date.endswith(".log"):
            date = date.removesuffix(".log")

        history_log_data = HistoryLogData()
        log_file_path = LOG_PATH / f"{date}.log"
        if not log_file_path.exists():
            return {"status": 404, "msg": "该日志不存在", "data": None}
        log_files = await history_log_data.get_parse_logs(log_file_path)
        for log in log_files:
            log["_date"] = date

    # Filter by level
    if level and level != "all":
        level_mapping = {
            "info": "info",
            "warning": "warn",
            "warn": "warn",
            "error": "error",
            "debug": "debug",
            "critical": "error",
            "fatal": "error",
        }
        filtered_logs = []
        for log in log_files:
            raw_level = log["日志等级"].lower()
            mapped_level = level_mapping[raw_level] if raw_level in level_mapping else "info"
            if mapped_level == level:
                filtered_logs.append(log)
        log_files = filtered_logs

    # Filter by source
    if source and source != "all":
        log_files = [log for log in log_files if (log["来源"] if "来源" in log else "core") == source]

    # Filter by search text
    if search:
        search_lower = search.lower()
        filtered_logs = []
        for log in log_files:
            message = log["内容"]
            if not isinstance(message, str):
                message = json.dumps(message, ensure_ascii=False)
            if search_lower in message.lower():
                filtered_logs.append(log)
        log_files = filtered_logs

    total = len(log_files)
    start = (page - 1) * per_page
    end = start + per_page
    log_page = log_files[start:end]

    # Convert to frontend expected format
    formatted_logs = []
    level_mapping = {
        "info": "info",
        "warning": "warn",
        "warn": "warn",
        "error": "error",
        "debug": "debug",
        "critical": "error",
        "fatal": "error",
    }
    for i, log in enumerate(log_page):
        raw_level = log["日志等级"].lower()
        level = level_mapping[raw_level] if raw_level in level_mapping else "info"
        message = log["内容"]
        if not isinstance(message, str):
            message = json.dumps(message, ensure_ascii=False)
        formatted_logs.append(
            {
                "id": start + i + 1,
                "log_id": log["id"],
                "date": log["_date"] if "_date" in log else "",
                "timestamp": log["时间"],
                "level": level,
                "source": "core",
                "message": message,
                "details": None,
            }
        )

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "count": total,
            "rows": formatted_logs,
            "page": page,
            "per_page": per_page,
        },
    }


@app.get("/api/logs/available-dates")
async def get_available_log_dates(
    _user: Dict = Depends(require_auth),
):
    """
    获取所有存在日志文件的日期列表，用于前端日历选择器标记可选择的日期

    Returns:
        包含以下字段的响应对象:
        - status: 状态码，0表示成功
        - msg: 状态信息
        - data: 日期字符串列表，按倒序排列(最新日期在前)，格式为YYYY-MM-DD
    """
    log_files = get_all_log_path()
    available_dates = [file.stem for file in log_files]
    available_dates.sort(reverse=True)
    return {"status": 0, "msg": "ok", "data": available_dates}


@app.get("/api/logs/sources")
async def get_log_sources(request: Request, _user: Dict = Depends(require_auth)):
    """
    获取可用的日志来源列表

    Returns:
        status: 0成功
        data: 来源列表
    """
    return {
        "status": 0,
        "msg": "ok",
        "data": ["api", "auth", "database", "scheduler", "core"],
    }


@app.get("/api/logs/stats")
async def get_log_stats(
    request: Request,
    date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    level: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    per_page: int = 100,
    _user: Dict = Depends(require_auth),
):
    """
    获取日志统计信息

    返回日志总数和页数统计，不返回具体日志内容。

    Args:
        request: FastAPI 请求对象
        date: 单个日期，格式 YYYY-MM-DD，默认今天
        start_date: 开始日期，格式 YYYY-MM-DD，与 end_date 配合使用
        end_date: 结束日期，格式 YYYY-MM-DD，与 start_date 配合使用
        level: 日志级别筛选
        source: 来源筛选
        search: 文本搜索，匹配日志内容
        per_page: 每页数量
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 统计信息
    """
    if start_date and end_date:
        # Multi-date range search
        all_log_files: list[LogEntry] = []
        from datetime import timedelta

        current_date = datetime.strptime(start_date, "%Y-%m-%d")
        end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")

        while current_date <= end_date_obj:
            date_str = current_date.strftime("%Y-%m-%d")
            log_file_path = LOG_PATH / f"{date_str}.log"
            if log_file_path.exists():
                history_log_data = HistoryLogData()
                logs = await history_log_data.get_parse_logs(log_file_path)
                for log in logs:
                    log["_date"] = date_str
                all_log_files.extend(logs)
            current_date += timedelta(days=1)

        log_files = all_log_files
    else:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        if date.endswith(".log"):
            date = date.removesuffix(".log")

        try:
            history_log_data = HistoryLogData()
            log_files = await history_log_data.get_parse_logs(LOG_PATH / f"{date}.log")
        except Exception:
            log_files = []

    try:
        level_mapping = {
            "info": "info",
            "warning": "warn",
            "warn": "warn",
            "error": "error",
            "debug": "debug",
            "critical": "error",
            "fatal": "error",
        }

        # Calculate statistics by level for entire date
        info_count = 0
        warn_count = 0
        error_count = 0
        debug_count = 0

        for log in log_files:
            raw_level = log["日志等级"].lower()
            mapped_level = level_mapping[raw_level] if raw_level in level_mapping else "info"
            if mapped_level == "info":
                info_count += 1
            elif mapped_level == "warn":
                warn_count += 1
            elif mapped_level == "error":
                error_count += 1
            elif mapped_level == "debug":
                debug_count += 1

        # Filter by level
        if level and level != "all":
            level_mapping = {
                "info": "info",
                "warning": "warn",
                "warn": "warn",
                "error": "error",
                "debug": "debug",
                "critical": "error",
                "fatal": "error",
            }
            filtered_logs = []
            for log in log_files:
                raw_level = log["日志等级"].lower()
                mapped_level = level_mapping[raw_level] if raw_level in level_mapping else "info"
                if mapped_level == level:
                    filtered_logs.append(log)
            log_files = filtered_logs

        # Filter by source
        if source and source != "all":
            log_files = [log for log in log_files if (log["来源"] if "来源" in log else "core") == source]

        # Filter by search text
        if search:
            search_lower = search.lower()
            filtered_logs = []
            for log in log_files:
                message = log["内容"]
                if not isinstance(message, str):
                    message = json.dumps(message, ensure_ascii=False)
                if search_lower in message.lower():
                    filtered_logs.append(log)
            log_files = filtered_logs

        total = len(log_files)
        total_pages = (total + per_page - 1) // per_page if per_page > 0 else 0

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "total": total,
                "total_pages": total_pages,
                "per_page": per_page,
                "info_count": info_count,
                "warn_count": warn_count,
                "error_count": error_count,
                "debug_count": debug_count,
            },
        }
    except Exception:
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "total": 0,
                "total_pages": 0,
                "per_page": per_page,
            },
        }


@app.get("/api/logs/context")
async def get_log_context(
    request: Request,
    log_id: int,
    date: str,
    before: int = 10,
    after: int = 10,
    _user: Dict = Depends(require_auth),
):
    """
    获取指定日志前后的上下文日志

    当用户搜索到某条关键日志后，可通过此接口获取该日志前后的日志记录，
    以便快速定位和理解关键日志的上下文环境。

    Args:
        request: FastAPI 请求对象
        log_id: 目标日志的原始行号（来自 /api/logs 返回的 log_id 字段）
        date: 目标日志所在日期，格式 YYYY-MM-DD
        before: 获取目标日志之前的日志条数，默认10，最大100
        after: 获取目标日志之后的日志条数，默认10，最大100
        _user: 认证用户信息

    Returns:
        status: 0成功，404日志不存在
        data: 包含 target、before_logs、after_logs 的上下文对象
    """
    # 限制 before/after 最大值，防止一次请求过多数据
    before = min(before, 100)
    after = min(after, 100)

    if date.endswith(".log"):
        date = date.removesuffix(".log")

    log_file_path = LOG_PATH / f"{date}.log"
    if not log_file_path.exists():
        return {"status": 404, "msg": "该日期的日志不存在", "data": None}

    history_log_data = HistoryLogData()
    log_files = await history_log_data.get_parse_logs(log_file_path)

    # 通过原始行号 (id) 查找目标日志
    target_index = None
    for i, log in enumerate(log_files):
        if log["id"] == log_id:
            target_index = i
            break

    if target_index is None:
        return {"status": 404, "msg": "未找到指定的日志条目", "data": None}

    # 计算前后日志的切片范围
    before_start = max(0, target_index - before)
    after_end = min(len(log_files), target_index + after + 1)

    before_logs = log_files[before_start:target_index]
    after_logs = log_files[target_index + 1 : after_end]
    target_log = log_files[target_index]

    # 日志格式化辅助函数
    level_mapping = {
        "info": "info",
        "warning": "warn",
        "warn": "warn",
        "error": "error",
        "debug": "debug",
        "critical": "error",
        "fatal": "error",
    }

    def format_context_log(log: LogEntry) -> Dict:
        raw_level = log["日志等级"].lower()
        mapped_level = level_mapping[raw_level] if raw_level in level_mapping else "info"
        message = log["内容"]
        if not isinstance(message, str):
            message = json.dumps(message, ensure_ascii=False)
        return {
            "log_id": log["id"],
            "date": date,
            "timestamp": log["时间"],
            "level": mapped_level,
            "source": "core",
            "message": message,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "target": format_context_log(target_log),
            "before_logs": [format_context_log(log) for log in before_logs],
            "after_logs": [format_context_log(log) for log in after_logs],
            "before_count": len(before_logs),
            "after_count": len(after_logs),
            "total_in_date": len(log_files),
            "has_more_before": before_start > 0,
            "has_more_after": after_end < len(log_files),
        },
    }


@app.get("/api/logs/stream")
async def stream_logs(
    level: Optional[List[str]] = Query(default=["DEBUG", "INFO", "ERROR"]),
    _user: Dict = Depends(require_auth),
):
    """Stream real-time logs using Server-Sent Events

    Args:
        level: 允许推送的日志级别列表，如 ["DEBUG", "INFO", "ERROR"]。
               默认为 ["DEBUG", "INFO", "ERROR"]；传 ["all"] 时推送全部级别日志。
               支持重复参数，如 ?level=DEBUG&level=INFO&level=ERROR。
    """
    if level and "all" in [ld.lower() for ld in level]:
        level = None
    return StreamingResponse(read_log(levels=level), media_type="text/event-stream")


@app.get("/api/logs/levels")
async def get_log_levels(_user: Dict = Depends(require_auth)):
    """获取可用的日志级别列表（供前端实时日志级别切换使用）"""
    return {
        "status": 0,
        "msg": "ok",
        "data": [
            {"label": "全部", "value": "all"},
            {"label": "TRACE", "value": "trace"},
            {"label": "DEBUG", "value": "debug"},
            {"label": "INFO", "value": "info"},
            {"label": "SUCCESS", "value": "success"},
            {"label": "WARNING", "value": "warning"},
            {"label": "ERROR", "value": "error"},
            {"label": "CRITICAL", "value": "critical"},
        ],
    }


@app.get("/api/logs/config")
async def get_logs_config(
    request: Request,
    _user: Dict = Depends(require_auth),
):
    """获取用户保存的日志控制台配置（供前端持久化级别选择偏好使用）

    读取时若存储中缺少字段或包含非法值，会回退到默认值，
    确保响应体始终包含完整的 `visible_levels` 字段。
    """
    config = load_logs_config()
    return {
        "status": 0,
        "msg": "ok",
        "data": _merge_defaults(config),
    }


@app.put("/api/logs/config")
async def save_logs_config_endpoint(
    request: Request,
    body: LogsConfigRequest = Body(default=LogsConfigRequest()),
    _user: Dict = Depends(require_auth),
):
    """保存用户日志控制台配置

    - `visible_levels` 必须是字符串数组，元素应为 `GET /api/logs/levels` 返回的合法 `value`
      （除 `all` 外）。后端会做合法性校验，剔除不在白名单内的值。
    - 数组可为空，表示用户主动全不选。
    """
    sanitized = _sanitize_visible_levels(body.visible_levels)
    new_config = {"visible_levels": sanitized}
    if save_logs_config(new_config):
        return {"status": 0, "msg": "saved", "data": new_config}
    return {"status": 1, "msg": "保存失败", "data": None}
