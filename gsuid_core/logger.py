import re
import sys
import json
import time
import asyncio
import logging
import datetime
from copy import deepcopy
from typing import Any, Dict, List, Optional, Protocol, Sequence, TypedDict, NotRequired
from pathlib import Path
from functools import wraps
from dataclasses import dataclass
from logging.handlers import TimedRotatingFileHandler

import aiofiles
import structlog
from colorama import Fore, Style, init
from structlog.dev import ConsoleRenderer
from structlog.types import EventDict, Processor, WrappedLogger
from structlog.processors import CallsiteParameter, CallsiteParameterAdder

from gsuid_core.config import core_config
from gsuid_core.models import Event, Message, TraceContext
from gsuid_core.data_store import get_res_path, error_mark_path

log_history: List[EventDict] = []
LOG_PATH = get_res_path() / "logs"
IS_DEBUG_LOG: bool = False

# 日志级别数值映射（用于 SSE 实时日志过滤）
LEVEL_NUM_MAP: Dict[str, int] = {
    "trace": 5,
    "debug": 10,
    "info": 20,
    "success": 25,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "critical": 50,
    "fatal": 50,
}


class DailyNamedFileHandler(TimedRotatingFileHandler):
    """
    一个会自动使用 YYYY-MM-DD.log 作为文件名的日志处理器。
    """

    def __init__(self, log_dir, backupCount=0, encoding="utf-8"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.base_filename_template = "{date}.log"
        filename = self._get_dated_filename()

        super().__init__(
            filename=filename,
            when="midnight",
            interval=1,
            backupCount=backupCount,
            encoding=encoding,
        )

    def _get_dated_filename(self):
        """根据当前日期生成完整的文件路径。"""
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        return str(self.log_dir / self.base_filename_template.format(date=date_str))

    def doRollover(self):
        """在午夜执行轮转。"""
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore

        self.baseFilename = self._get_dated_filename()

        # self._cleanup_logs()

        if not self.delay:
            self.stream = self._open()


class CollectLogHandler(logging.Handler):
    """
    专门用于触发格式化处理器链以收集日志的 Handler。
    不输出到任何流，仅让 log_to_history 等处理器将日志写入内存缓冲区，
    供 SSE 实时日志流消费。
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.format(record)


# ── 追踪日志条目 ──
@dataclass
class TraceLogEntry:
    timestamp: str
    level: str
    event: str


_MAX_EVENT_LEN: int = 4096
_MAX_TRACE_LOGS: int = 5000


class TraceCollector:
    """以 trace_id 为维度收集执行中命令的日志；命令一结束即落盘归档并从内存移除，
    内存中只保留正在执行中的追踪，不做内存保留。"""

    def __init__(
        self,
        max_traces: int = 1000,
        stale_running_sec: float = 3600.0,
    ):
        # 仅保存「正在执行中」的追踪；命令一结束即落盘并从内存移除，不做内存保留
        self._traces: Dict[str, List[TraceLogEntry]] = {}
        self._trace_meta: Dict[str, TraceContext] = {}
        self._max_traces = max_traces
        # running 超过该时长仍未 finalize，视为泄漏（命令异常退出未走 finally 等），可被回收
        self._stale_running_sec = stale_running_sec
        # 容量告警节流时间戳（perf_counter），避免每条命令都刷 warning
        self._last_capacity_warn: float = 0.0

    def start_trace(self, ctx: TraceContext) -> None:
        """开始一个新追踪——仅在任务实际执行时调用"""
        # 先回收，保证登记新追踪后内存不超过容量硬上限
        if len(self._traces) >= self._max_traces:
            self._evict_to_capacity()

        self._traces[ctx.trace_id] = []
        self._trace_meta[ctx.trace_id] = ctx

        trace_start_event = f"📝 [TraceStart] trace_id={ctx.trace_id} command={ctx.command} user_id={ctx.user_id}"
        _slg = structlog.get_logger("GsCore")
        _slg.info(trace_start_event, trace_id=ctx.trace_id)

        # 写入 JSONL running 标记
        try:
            from gsuid_core.trace_archive import write_trace_meta

            write_trace_meta(ctx.trace_id, ctx, status="running", log_count=0)
        except Exception as e:
            _slg = structlog.get_logger("GsCore")
            _slg.error(f"❌ [TraceCollector] JSONL running 标记写入失败 trace_id={ctx.trace_id}: {e}")

    def _drop(self, trace_id: str) -> None:
        """从内存中彻底移除一个追踪（两张表一起清，避免残留）"""
        self._traces.pop(trace_id, None)
        self._trace_meta.pop(trace_id, None)

    def reclaim_stale(self) -> int:
        """回收僵死（疑似泄漏）的 running 追踪——命令异常退出未走 finally 等场景。

        正常情况下命令结束即 finalize 落盘并移除，内存里只剩在执行中的追踪；但若
        finalize 因故未被调用，对应追踪会滞留为 running。该方法由后台定时任务周期调用，
        把存活超过 stale_running_sec 的 running 追踪兜底清掉。返回回收条数。
        """
        now = time.perf_counter()
        to_drop: List[str] = []
        for tid in list(self._traces.keys()):
            meta = self._trace_meta.get(tid)
            age = now - meta.start_time if meta else float("inf")
            if age >= self._stale_running_sec:
                to_drop.append(tid)
        for tid in to_drop:
            self._drop(tid)
        return len(to_drop)

    def _evict_to_capacity(self) -> None:
        """回收追踪直到低于容量硬上限，给即将登记的新追踪留出空位。

        内存里只保存在执行中的追踪，正常远低于上限。一旦逼近上限，多半是大量命令
        异常退出未 finalize 导致的泄漏堆积。回收优先级：① running 但已僵死（疑似泄漏）
        → ② 真正在跑的追踪（绝对兜底，最旧优先）。容量是硬约束，保证内存不会无限增长。
        """
        target = self._max_traces - 1  # 留一个空位给新追踪
        if len(self._traces) <= target:
            return

        now = time.perf_counter()
        stale_running: List[str] = []  # running 但僵死（疑似泄漏）：可丢
        running_active: List[str] = []  # 真正在跑：绝对兜底才丢

        # dict 保持插入顺序，故下列各组内部均为「最旧在前」
        for tid in list(self._traces.keys()):
            meta = self._trace_meta.get(tid)
            age = now - meta.start_time if meta else float("inf")
            if age >= self._stale_running_sec:
                stale_running.append(tid)
            else:
                running_active.append(tid)

        sacrificed = 0  # 被迫牺牲的活跃追踪数（用于告警）
        for group, is_safe in ((stale_running, True), (running_active, False)):
            for tid in group:
                if len(self._traces) <= target:
                    break
                self._drop(tid)
                if not is_safe:
                    sacrificed += 1
            if len(self._traces) <= target:
                break

        if sacrificed:
            self._warn_capacity(sacrificed)

    def _warn_capacity(self, sacrificed: int) -> None:
        """容量告警（节流到最多每 60s 一条），避免高并发时刷屏"""
        now = time.perf_counter()
        if now - self._last_capacity_warn < 60.0:
            return
        self._last_capacity_warn = now
        _slg = structlog.get_logger("GsCore")
        _slg.warning(
            f"[TraceCollector] 执行中追踪数达上限 {self._max_traces}，"
            f"已强制回收 {sacrificed} 条活跃追踪以保证内存上界；"
            f"通常意味着大量命令异常退出未正常结束追踪，请排查；如属正常高并发可调大 max_traces"
        )

    def collect(self, event_dict: EventDict) -> None:
        """收集一条日志到当前追踪——只存储精简字段，超长 event 截断"""
        if "trace_id" not in event_dict:
            return
        trace_id = event_dict["trace_id"]
        # 取一次列表引用：即使该追踪被并发回收（后台定时清理在其它线程触发日志期间），
        # 也只是往一个已脱钩的列表追加，不会 KeyError，也不会复活已回收的追踪。
        bucket = self._traces.get(trace_id)
        if bucket is None:
            return
        raw_event = str(event_dict["event"]) if "event" in event_dict else ""
        if len(raw_event) > _MAX_EVENT_LEN:
            raw_event = raw_event[:_MAX_EVENT_LEN] + " [truncated]"

        entry = TraceLogEntry(
            timestamp=str(event_dict["timestamp"]) if "timestamp" in event_dict else "",
            level=str(event_dict["level"]) if "level" in event_dict else "",
            event=raw_event,
        )
        bucket.append(entry)

        if len(bucket) > _MAX_TRACE_LOGS:
            truncated = bucket[:100] + bucket[-100:]
            truncated.insert(
                100,
                TraceLogEntry(
                    timestamp=bucket[100].timestamp,
                    level="warning",
                    event=f"[TraceCollector] 日志过多，已截断，原始条数={len(bucket)}",
                ),
            )
            # 仅当该追踪仍在内存中时回写，避免复活已被回收的追踪
            if trace_id in self._traces:
                self._traces[trace_id] = truncated

    def get_short_id(self, trace_id: str) -> Optional[str]:
        """从 meta 查找短码（供控制台格式化使用）"""
        meta = self._trace_meta.get(trace_id)
        return meta.short_id if meta else None

    def finalize_trace(self, trace_id: str) -> Optional[List[TraceLogEntry]]:
        """完成追踪——输出 TraceEnd 标记、JSONL 归档，然后立即从内存移除。

        命令的每条日志在执行过程中已实时写入 daily log 文件，JSONL 也记录了元数据，
        因此追踪结束后不再保留内存副本：直接落盘并 _drop。网页控制台查询已完成追踪时
        从 JSONL + daily log 重建即可。无论归档成功与否都会移除内存副本，避免追踪滞留。
        """
        meta = self._trace_meta.get(trace_id)
        if meta is None:
            # 追踪从未登记，或已被容量/僵死回收，无需处理
            return None

        logs = self._traces.get(trace_id)
        log_count = len(logs) if logs else 0
        duration_ms = int((time.perf_counter() - meta.start_time) * 1000)

        _slg = structlog.get_logger("GsCore")
        try:
            from gsuid_core.trace_archive import write_trace_meta

            write_trace_meta(trace_id, meta, status="completed", log_count=log_count, duration_ms=duration_ms)
            trace_end_event = (
                f"🏁 [TraceEnd] trace_id={trace_id} command={meta.command} duration={duration_ms}ms logs={log_count}"
            )
            _slg.info(trace_end_event, trace_id=trace_id)
        except Exception as e:
            _slg.error(f"❌ [TraceCollector] JSONL 归档失败 trace_id={trace_id}: {e}")
        finally:
            # 无论归档成败都从内存移除，避免追踪滞留导致泄漏
            self._drop(trace_id)

        return logs

    def get_active_traces(self) -> Dict[str, Dict]:
        """获取当前正在执行中的追踪列表（内存只保留 running，已完成的已落盘并移除）"""
        return {
            tid: {
                "command": meta.command,
                "user_id": meta.user_id,
                # 对外暴露墙钟时间戳（Unix 秒），不暴露 perf_counter 单调时钟
                "start_time": meta.start_ts,
                "log_count": len(self._traces.get(tid, [])),
                "status": "running",
            }
            for tid, meta in self._trace_meta.items()
        }

    def get_trace_meta(self, trace_id: str) -> Optional[TraceContext]:
        """获取指定追踪的元数据"""
        return self._trace_meta.get(trace_id)

    def get_trace_logs(self, trace_id: str) -> Optional[List[TraceLogEntry]]:
        """获取仍活跃在内存中的追踪日志"""
        return self._traces.get(trace_id)


# ── 绑定 / 解绑 ──
_TRACE_CONTEXT_KEYS = ("trace_id",)


def bind_trace_context(ctx: TraceContext) -> None:
    """绑定追踪上下文到 structlog contextvars"""
    structlog.contextvars.bind_contextvars(trace_id=ctx.trace_id)


def clear_trace_context() -> None:
    """精确解绑 trace_id，避免误清其他模块的 contextvars"""
    structlog.contextvars.unbind_contextvars(*_TRACE_CONTEXT_KEYS)


def trace_collect_processor(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """将带有 trace_id 的日志同时收集到 TraceCollector"""
    if "trace_id" in event_dict:
        _collector = _get_trace_collector()
        if _collector is not None:
            _collector.collect(event_dict)
    return event_dict


def format_trace_id_processor(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """在控制台日志中显示追踪短码"""
    if "trace_id" in event_dict:
        _collector = _get_trace_collector()
        if _collector is not None:
            short_id = _collector.get_short_id(event_dict["trace_id"])
            if short_id:
                original_event = event_dict["event"]
                if isinstance(original_event, str):
                    event_dict["event"] = f"[{short_id}] {original_event}"
    return event_dict


# 延迟初始化追踪收集器单例
_trace_collector_instance: Optional[TraceCollector] = None


def _get_trace_collector() -> Optional[TraceCollector]:
    return _trace_collector_instance


def _init_trace_collector() -> TraceCollector:
    global _trace_collector_instance
    if _trace_collector_instance is None:
        _trace_collector_instance = TraceCollector()
    return _trace_collector_instance


class TraceCapableLogger(Protocol):
    def trace(self, event: Any, *args: Any, **kwargs: Any) -> None: ...

    def debug(self, event: Any, *args: Any, **kwargs: Any) -> None: ...

    def info(self, event: Any, *args: Any, **kwargs: Any) -> None: ...

    def warning(self, event: Any, *args: Any, **kwargs: Any) -> None: ...

    def error(self, event: Any, *args: Any, **kwargs: Any) -> None: ...

    def critical(self, event: Any, *args: Any, **kwargs: Any) -> None: ...

    def exception(self, event: Any, *args: Any, **kwargs: Any) -> None: ...

    def success(self, event: Any, *args: Any, **kwargs: Any) -> None: ...

    def bind(self, **new_values: Any) -> "TraceCapableLogger": ...

    def new(self, **new_values: Any) -> "TraceCapableLogger": ...

    def unbind(self, *keys: str) -> "TraceCapableLogger": ...


class TraceCapableBoundLogger(structlog.stdlib.BoundLogger):
    def trace(self, event: str, *args: Any, **kwargs: Any) -> None:
        self._proxy_to_logger("trace", event, *args, **kwargs)

    def success(self, event: str, *args: Any, **kwargs: Any) -> None:
        self._proxy_to_logger("success", event, *args, **kwargs)


def save_error_report_processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """
    自定义处理器：当日志级别为 error/critical/exception 时，
    保存 JSON 报告，但自动忽略 Ctrl+C 和 任务取消 等系统级信号。
    """
    if method_name.lower() not in ("error", "critical", "exception"):
        return event_dict

    IGNORED_EXCEPTIONS = (
        KeyboardInterrupt,
        SystemExit,
        asyncio.CancelledError,
    )

    exc_info = event_dict.get("exc_info")
    current_exc = None

    if isinstance(exc_info, tuple):
        current_exc = exc_info[1]
    elif exc_info is True:
        _, current_exc, _ = sys.exc_info()

    if current_exc and isinstance(current_exc, IGNORED_EXCEPTIONS):
        return event_dict

    exception_text = str(event_dict.get("exception", ""))
    if "KeyboardInterrupt" in exception_text or "CancelledError" in exception_text:
        return event_dict

    event_text = str(event_dict.get("event", ""))
    if "KeyboardInterrupt" in event_text or "CancelledError" in event_text:
        return event_dict

    try:
        report_content = dict(event_dict)

        for key in ["_record", "_logger", "_from_structlog"]:
            report_content.pop(key, None)

        report_content.pop("exc_info", None)

        now_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        if not error_mark_path.exists():
            error_mark_path.mkdir(parents=True, exist_ok=True)

        report_file = error_mark_path / f"error_report_{now_str}.json"

        report_content["_report_timestamp"] = now_str
        report_content["_log_level"] = method_name

        def json_default(obj):
            if isinstance(obj, (datetime.datetime, datetime.date)):
                return obj.isoformat()
            if isinstance(obj, Path):
                return str(obj)
            return str(obj)

        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(
                report_content,
                f,
                ensure_ascii=False,
                indent=4,
                default=json_default,
            )

    except Exception as e:
        print(f"Failed to save error report: {e}")

    return event_dict


def format_callsite_processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """
    一个自定义处理器，用于将调用点信息格式化并前置到事件消息中。
    """
    pathname = event_dict.pop("pathname", "?")
    lineno = event_dict.pop("lineno", "?")
    func_name = event_dict.pop("func_name", "?")

    callsite = f"[{pathname}:{lineno}:{func_name}]"

    # 将调用点信息和原始事件消息拼接起来
    # 我们在调用点字符串和原事件之间加了一个空格
    original_event = event_dict.get("event", "")
    event_dict["event"] = f"{Fore.YELLOW}{callsite}{Style.RESET_ALL} {original_event}"

    return event_dict


def reduce_message(messages: List[Message]):
    mes = deepcopy(messages)
    for message in mes:
        # 处理 message 可能是 dict 或 Message 对象的情况
        if isinstance(message, dict):
            data = message.get("data")
        else:
            data = getattr(message, "data", None)

        if data:
            dd = str(data)
            if len(dd) >= 500:
                try:
                    truncated = dd[:100]
                    if isinstance(message, dict):
                        message["data"] = truncated
                    else:
                        message.data = truncated
                except Exception:
                    pass
    return mes


def format_event_for_console(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    event: Optional[Event] = deepcopy(event_dict.get("event_payload"))
    if isinstance(event, Event):
        # 使用 colorama 的颜色代码重新构建主事件消息
        event_dict["event"] = (
            f"{Style.BRIGHT}{Fore.CYAN}[Receive]{Style.RESET_ALL} "
            f"bot_id={event.bot_id}, "
            f"bot_self_id={event.bot_self_id}, "
            f"msg_id={event.msg_id}, "
            f"user_type={event.user_type}, "
            f"group_id={event.group_id}, "
            f"user_id={event.user_id}, "
            f"user_pm={event.user_pm}, "
            f"content={reduce_message(event.content)}, "
        )
        event_dict.pop("event_payload")

    messages: Optional[List[Message]] = event_dict.get("messages")
    if isinstance(messages, List):
        event_dict["messages"] = reduce_message(messages)

    return event_dict


def colorize_brackets_processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """
    一个后处理器，用于给 event 字符串中所有被 [] 包围的部分上色。
    """
    event = event_dict.get("event", "")

    # 如果事件内容是字符串类型
    if isinstance(event, str):
        # 定义我们想要的"橙色" (亮黄色在大多数终端中看起来像橙色)
        orange_color = Style.BRIGHT + Fore.LIGHTMAGENTA_EX

        # 使用正则表达式查找所有 [anything] 模式，并用颜色代码包裹它们
        # re.sub() 可以接受一个函数作为替换参数，这里用 lambda 更简洁
        # (\[.*?\]) -> 匹配一个完整的 [...] 块，并捕获它
        colored_event = re.sub(
            r"(\[.*?\])",
            lambda match: f"{orange_color}{match.group(1)}{Style.RESET_ALL}",
            event,
        )

        # 将修改后的、带颜色的字符串放回 event_dict
        event_dict["event"] = colored_event

    return event_dict


def safe_deepcopy_eventdict(event_dict: EventDict) -> EventDict:
    sanitized_dict: EventDict = {}

    for key, value in event_dict.items():
        try:
            sanitized_dict[key] = deepcopy(value)
        except TypeError:
            try:
                sanitized_dict[key] = str(value)
            except Exception as e:
                sanitized_dict[key] = f"<Unstringable object of type {type(value).__name__}, error: {e}>"

    return sanitized_dict


def log_to_history(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    try:
        _event_dict = deepcopy(event_dict)
    except Exception:
        _event_dict = safe_deepcopy_eventdict(event_dict)

    s = ""
    for g in _event_dict:
        if g not in ["event", "timestamp", "level"]:
            s += f"{g}={event_dict[g]}, "

    s = s.rstrip(", ")
    if s:
        s = f"\n{s}"
    _event_dict["gevent"] = str(event_dict["event"]) + s
    log_history.append(_event_dict)
    return event_dict


def handle_exception(exc_type, exc_value, exc_traceback):
    """
    一个自定义函数，用于处理所有未捕獲的异常。
    """
    # 如果是用户手动中断 (Ctrl+C)，我们遵循默认行为，不记录为严重错误
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    # 使用 structlog 获取一个 logger
    # logger 名称可以自定义，以区分这是未捕獲的异常
    # log: TraceCapableLogger = structlog.get_logger("unhandled_exception")

    # 使用 .critical() 或 .exception() 记录异常
    # 将 exc_info 参数设置为异常信息元组，structlog 会自动处理它
    logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))


def setup_logging():
    """配置日志，使其同时向文件和控制台输出不同格式的日志。"""
    init(autoreset=True)

    TRACE_LEVEL: int = 5
    TRACE_LEVEL_NAME: str = "TRACE"
    logging.addLevelName(TRACE_LEVEL, TRACE_LEVEL_NAME)

    def trace(self: logging.Logger, message: str, *args: Any, **kws: Any) -> None:
        if self.isEnabledFor(TRACE_LEVEL):
            self._log(TRACE_LEVEL, message, args, **kws)

    setattr(logging.Logger, TRACE_LEVEL_NAME.lower(), trace)

    SUCCESS_LEVEL: int = 25
    SUCCESS_LEVEL_NAME: str = "SUCCESS"
    logging.addLevelName(SUCCESS_LEVEL, SUCCESS_LEVEL_NAME)

    def success(self: logging.Logger, message: str, *args: Any, **kws: Any) -> None:
        if self.isEnabledFor(SUCCESS_LEVEL):
            self._log(SUCCESS_LEVEL, message, args, **kws)

    setattr(logging.Logger, SUCCESS_LEVEL_NAME.lower(), success)

    # 从配置读取
    log_config = core_config.get_config("log")
    LEVEL: str = log_config.get("level", "INFO").upper()
    logger_list: List[str] = log_config.get("output", ["stdout", "stderr", "file"])

    final_level_styles = ConsoleRenderer.get_default_level_styles()
    level_styles = {
        # '级别名称的小写形式': colorama样式
        "trace": Fore.MAGENTA,  # 洋红色
        "debug": Fore.CYAN,  # 青色 (覆盖默认)
        "info": Fore.BLUE,  # 蓝色 (覆盖默认)
        "success": Style.BRIGHT + Fore.GREEN,  # 亮绿色
        "warning": Fore.YELLOW,  # 黄色 (保持默认)
        "error": Fore.RED,  # 红色 (保持默认)
        "critical": Style.BRIGHT + Fore.RED,  # 亮红色 (保持默认)
    }
    final_level_styles.update(level_styles)

    # 定义所有处理器链共享的基础部分
    shared_processors: List[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        # structlog.processors.format_exc_info,
        structlog.processors.TimeStamper(fmt="%m-%d %H:%M:%S", utc=False),
    ]

    if IS_DEBUG_LOG:
        shared_processors.append(
            CallsiteParameterAdder(
                {
                    CallsiteParameter.PATHNAME,  # 文件路径
                    CallsiteParameter.LINENO,  # 行号
                    CallsiteParameter.FUNC_NAME,  # 函数名
                }
            )
        )

    # --- 文件处理链 ---
    file_processors: Sequence[Processor] = shared_processors + [
        structlog.dev.set_exc_info,
        structlog.processors.format_exc_info,
        save_error_report_processor,
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.processors.JSONRenderer(ensure_ascii=False),
    ]

    # --- 控制台处理链 ---
    console_processors: Sequence[Processor] = shared_processors + [
        format_trace_id_processor,
        colorize_brackets_processor,
        format_event_for_console,
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.dev.ConsoleRenderer(
            colors=True,
            level_styles=final_level_styles,
            exception_formatter=structlog.dev.RichTracebackFormatter(
                show_locals=False,
            ),
        ),
    ]

    if IS_DEBUG_LOG:
        console_processors.insert(-1, format_callsite_processor)

    # --- 配置 logging ---
    root_logger = logging.getLogger()
    root_logger.handlers = []  # 等效于 loguru.logger.remove()
    root_logger.setLevel(logging.INFO)  # 设置根级别

    my_app_logger = logging.getLogger("GsCore")
    my_app_logger.setLevel(5)  # TRACE 级别，确保全级别日志都能被收集

    # --- 内存收集 handler（全级别，用于 SSE 实时日志）---
    collect_processors: Sequence[Processor] = shared_processors + [
        trace_collect_processor,
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        log_to_history,
        structlog.processors.JSONRenderer(ensure_ascii=False),
    ]
    collect_handler = CollectLogHandler(level=5)
    collect_handler.setFormatter(structlog.stdlib.ProcessorFormatter(processors=collect_processors))
    my_app_logger.addHandler(collect_handler)

    # a. 配置 stdout handler
    if "stdout" in logger_list:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(LEVEL)
        stdout_handler.setFormatter(structlog.stdlib.ProcessorFormatter(processors=console_processors))
        root_logger.addHandler(stdout_handler)

    # c. 配置文件 handler (每日轮转)
    if "file" in logger_list:
        file_handler = DailyNamedFileHandler(
            log_dir=LOG_PATH,
            backupCount=0,
            encoding="utf-8",
        )
        file_handler.setLevel(LEVEL)
        file_handler.setFormatter(structlog.stdlib.ProcessorFormatter(processors=file_processors))
        root_logger.addHandler(file_handler)

    # --- 最后配置 structlog ---
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        context_class=dict,
        wrapper_class=TraceCapableBoundLogger,
        cache_logger_on_first_use=True,
    )

    sys.excepthook = handle_exception

    logging.getLogger("websockets").setLevel(logging.INFO)
    logging.getLogger("PIL").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    logging.getLogger("nonebot").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("casbin").setLevel(logging.WARNING)

    for logger_name in [
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
    ]:
        uvicorn_sub_logger = logging.getLogger(logger_name)
        uvicorn_sub_logger.handlers.clear()
        uvicorn_sub_logger.propagate = True

    """
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.setLevel(LEVEL)

    for handler in root_logger.handlers:
        uvicorn_logger.addHandler(handler)
    # 防止日志重复记录（如果父日志器已处理）
    uvicorn_logger.propagate = False
    """


setup_logging()
logger: TraceCapableLogger = structlog.get_logger("GsCore")

# 初始化追踪收集器（在 setup_logging 和 logger 就绪后）
trace_collector = _init_trace_collector()


async def read_log(levels: Optional[List[str]] = None):
    """
    SSE 实时日志生成器。

    Args:
        levels: 允许的日志级别列表，如 ["DEBUG", "INFO", "ERROR"]。
               为空时不过滤，推送所有已缓冲的日志。
    """
    index = 0
    # 将允许的级别统一转为小写 set，便于快速匹配
    allowed_levels: Optional[set] = set(ld.lower() for ld in levels) if levels else None
    while True:
        if index <= len(log_history) - 1:
            ev = log_history[index]
            if ev:
                level_str = str(ev.get("level", "")).lower()
                if allowed_levels is None or level_str in allowed_levels:
                    log_data = {
                        "level": ev["level"].upper(),
                        "message": ev["gevent"],
                        "message_type": "html",
                        "timestamp": ev["timestamp"],
                    }
                    yield f"data: {json.dumps(log_data)}\n\n"
            index += 1
        else:
            await asyncio.sleep(1)


async def clean_log():
    global log_history
    while True:
        await asyncio.sleep(480)
        log_history = []


async def clean_trace_collector():
    """后台定时回收 TraceCollector 中僵死（疑似泄漏）的 running 追踪。

    正常情况下命令结束即落盘并移除，内存里只剩在执行中的追踪；该任务由 app 生命周期
    启动，按 stale_running_sec 兜底清理异常退出导致未 finalize 而滞留的 running 追踪，
    避免内存随时间缓慢增长。
    """
    while True:
        await asyncio.sleep(300)
        try:
            collector = _get_trace_collector()
            if collector is not None:
                dropped = collector.reclaim_stale()
                if dropped:
                    logger.debug(f"🧹 [TraceCollector] 定时回收僵死追踪 {dropped} 条")
        except Exception as e:
            logger.warning(f"[TraceCollector] 定时回收异常: {e}")


def handle_exceptions(async_function):
    @wraps(async_function)
    async def wrapper(*args, **kwargs):
        try:
            return await async_function(*args, **kwargs)
        except Exception as e:
            logger.exception("[错误发生] %s: %s", async_function.__name__, e)
            return None

    return wrapper


class LogEntry(TypedDict):
    """单条已解析的历史日志条目。

    id / 时间 / 日志等级 / 内容 四个键由 get_parse_logs 解析时必然写入；
    来源、_date 为可选键，由调用方（如日志 API）按需补充。
    """

    id: int
    时间: str
    日志等级: str
    内容: object
    来源: NotRequired[str]
    _date: NotRequired[str]


class HistoryLogData:
    def __init__(self):
        self.log_list: Dict[str, List[LogEntry]] = {}

    async def get_parse_logs(self, log_file_path: Path) -> List[LogEntry]:
        if log_file_path.name in self.log_list:
            return self.log_list[log_file_path.name]

        log_entries: List[LogEntry] = []

        async with aiofiles.open(log_file_path, "r", encoding="utf-8") as file:
            lines = await file.readlines()

        current_entry: Optional[LogEntry] = None

        _id = 1
        for line in lines:
            ev: Dict[str, str] = json.loads(line.strip())

            if current_entry:
                log_entries.append(current_entry)
            current_entry = {
                "id": _id,
                "时间": ev["timestamp"],
                "日志等级": ev["level"].upper(),
                # '模块': ev['pathname'],
                "内容": ev["event"],
            }
            _id += 1

        if current_entry:
            log_entries.append(current_entry)

        self.log_list[log_file_path.name] = log_entries
        return log_entries


def get_all_log_path():
    return [file for file in LOG_PATH.iterdir() if file.is_file() and file.suffix == ".log"]
