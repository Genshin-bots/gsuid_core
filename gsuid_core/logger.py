import re
import sys
import json
import asyncio
import logging
import datetime
from pathlib import Path
from copy import deepcopy
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Dict, List, Optional, Protocol

import aiofiles
import structlog
from colorama import Fore, Style, init
from structlog.dev import ConsoleRenderer
from structlog.types import EventDict, Processor, WrappedLogger
from structlog.processors import CallsiteParameter, CallsiteParameterAdder

from gsuid_core.config import core_config
from gsuid_core.models import Event, Message
from gsuid_core.data_store import get_res_path

log_history: List[EventDict] = []
LOG_PATH = get_res_path() / 'logs'
IS_DEBUG_LOG: bool = False


class DailyNamedFileHandler(TimedRotatingFileHandler):
    """
    一个会自动使用 YYYY-MM-DD.log 作为文件名的日志处理器。
    """

    def __init__(self, log_dir, backupCount=0, encoding='utf-8'):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.base_filename_template = "{date}.log"
        filename = self._get_dated_filename()

        super().__init__(
            filename=filename,
            when='midnight',
            interval=1,
            backupCount=backupCount,
            encoding=encoding,
        )

    def _get_dated_filename(self):
        """根据当前日期生成完整的文件路径。"""
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')
        return str(
            self.log_dir / self.base_filename_template.format(date=date_str)
        )

    def doRollover(self):
        """在午夜执行轮转。"""
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore

        self.baseFilename = self._get_dated_filename()

        # self._cleanup_logs()

        if not self.delay:
            self.stream = self._open()


class TraceCapableLogger(Protocol):
    def trace(self, event: Any, *args: Any, **kwargs: Any) -> None: ...
    def debug(self, event: Any, *args: Any, **kwargs: Any) -> None: ...
    def info(self, event: Any, *args: Any, **kwargs: Any) -> None: ...
    def warning(self, event: Any, *args: Any, **kwargs: Any) -> None: ...
    def error(self, event: Any, *args: Any, **kwargs: Any) -> None: ...
    def critical(self, event: Any, *args: Any, **kwargs: Any) -> None: ...
    def exception(self, event: Any, *args: Any, **kwargs: Any) -> None: ...
    def success(self, event: Any, *args: Any, **kwargs: Any) -> None: ...
    def bind(self, **new_values: Any) -> 'TraceCapableLogger': ...
    def new(self, **new_values: Any) -> 'TraceCapableLogger': ...
    def unbind(self, *keys: str) -> 'TraceCapableLogger': ...


class TraceCapableBoundLogger(structlog.stdlib.BoundLogger):
    def trace(self, event: str, *args: Any, **kwargs: Any) -> None:
        self._proxy_to_logger("trace", event, *args, **kwargs)

    def success(self, event: str, *args: Any, **kwargs: Any) -> None:
        self._proxy_to_logger("success", event, *args, **kwargs)


def format_callsite_processor(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
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
    event_dict["event"] = (
        f"{Fore.YELLOW}{callsite}{Style.RESET_ALL} {original_event}"
    )

    return event_dict


def reduce_message(messages: List[Message]):
    mes = deepcopy(messages)
    for message in mes:
        dd = str(message.data)
        if message.data and len(dd) >= 500:
            try:
                message.data = dd[:100]
            except Exception:
                pass
    return mes


def format_event_for_console(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    event: Optional[Event] = deepcopy(event_dict.get("event_payload"))
    if isinstance(event, Event):
        # 使用 colorama 的颜色代码重新构建主事件消息
        event_dict['event'] = (
            f'{Style.BRIGHT}{Fore.CYAN}[Receive]{Style.RESET_ALL} '
            f'bot_id={event.bot_id}, '
            f'bot_self_id={event.bot_self_id}, '
            f'msg_id={event.msg_id}, '
            f'user_type={event.user_type}, '
            f'group_id={event.group_id}, '
            f'user_id={event.user_id}, '
            f'user_pm={event.user_pm}, '
            f'content={reduce_message(event.content)}, '
        )
        event_dict.pop("event_payload")

    messages: Optional[List[Message]] = event_dict.get("messages")
    if isinstance(messages, List):
        event_dict['messages'] = reduce_message(messages)

    return event_dict


def colorize_brackets_processor(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """
    一个后处理器，用于给 event 字符串中所有被 [] 包围的部分上色。
    """
    event = event_dict.get("event", "")

    # 如果事件内容是字符串类型
    if isinstance(event, str):
        # 定义我们想要的“橙色” (亮黄色在大多数终端中看起来像橙色)
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
                sanitized_dict[key] = (
                    "<Unstringable object of type "
                    f"{type(value).__name__}, error: {e}>"
                )

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

    s = ''
    for g in _event_dict:
        if g not in ['event', 'timestamp', 'level']:
            s += f'{g}={event_dict[g]}, '

    s = s.rstrip(', ')
    if s:
        s = f'\n{s}'
    _event_dict['gevent'] = str(event_dict['event']) + s
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
    logger.critical(
        "Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback)
    )


def setup_logging():
    """配置日志，使其同时向文件和控制台输出不同格式的日志。"""
    init(autoreset=True)

    TRACE_LEVEL: int = 5
    TRACE_LEVEL_NAME: str = "TRACE"
    logging.addLevelName(TRACE_LEVEL, TRACE_LEVEL_NAME)

    def trace(
        self: logging.Logger, message: str, *args: Any, **kws: Any
    ) -> None:
        if self.isEnabledFor(TRACE_LEVEL):
            self._log(TRACE_LEVEL, message, args, **kws)

    setattr(logging.Logger, TRACE_LEVEL_NAME.lower(), trace)

    SUCCESS_LEVEL: int = 25
    SUCCESS_LEVEL_NAME: str = "SUCCESS"
    logging.addLevelName(SUCCESS_LEVEL, SUCCESS_LEVEL_NAME)

    def success(
        self: logging.Logger, message: str, *args: Any, **kws: Any
    ) -> None:
        if self.isEnabledFor(SUCCESS_LEVEL):
            self._log(SUCCESS_LEVEL, message, args, **kws)

    setattr(logging.Logger, SUCCESS_LEVEL_NAME.lower(), success)

    # 从配置读取
    log_config = core_config.get_config('log')
    LEVEL: str = log_config.get('level', 'INFO').upper()
    logger_list: List[str] = log_config.get(
        'output', ['stdout', 'stderr', 'file']
    )

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
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
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
    file_processors: List[Processor] = shared_processors + [
        structlog.dev.set_exc_info,
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        log_to_history,
        structlog.processors.JSONRenderer(ensure_ascii=False),
    ]

    # --- 控制台处理链 ---
    console_processors: List[Processor] = shared_processors + [
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
    my_app_logger.setLevel(LEVEL)

    # a. 配置 stdout handler (低于 ERROR)
    if 'stdout' in logger_list:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(LEVEL)
        stdout_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(processors=console_processors)
        )
        root_logger.addHandler(stdout_handler)

    # c. 配置文件 handler (每日轮转)
    if 'file' in logger_list:
        # 关键：使用 TimedRotatingFileHandler 实现每日轮转
        file_handler = DailyNamedFileHandler(
            log_dir=LOG_PATH,
            backupCount=0,
            encoding='utf-8',
        )
        file_handler.setLevel(LEVEL)
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(processors=file_processors)
        )
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

    '''
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.setLevel(LEVEL)

    for handler in root_logger.handlers:
        uvicorn_logger.addHandler(handler)
    # 防止日志重复记录（如果父日志器已处理）
    uvicorn_logger.propagate = False
    '''


setup_logging()
logger: TraceCapableLogger = structlog.get_logger('GsCore')


async def read_log():
    index = 0
    while True:
        if index <= len(log_history) - 1:
            ev = log_history[index]
            if ev:
                log_data = {
                    "level": ev['level'].upper(),
                    "message": ev['gevent'],
                    "message_type": "html",
                    "timestamp": ev['timestamp'],
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


def handle_exceptions(async_function):
    @wraps(async_function)
    async def wrapper(*args, **kwargs):
        try:
            return await async_function(*args, **kwargs)
        except Exception as e:
            logger.exception('[错误发生] %s: %s', async_function.__name__, e)
            return None

    return wrapper


class HistoryLogData:
    def __init__(self):
        self.log_list: Dict[str, List[Dict]] = {}

    async def get_parse_logs(self, log_file_path: Path):
        if log_file_path.name in self.log_list:
            return self.log_list[log_file_path.name]

        log_entries: List[Dict] = []

        async with aiofiles.open(log_file_path, 'r', encoding='utf-8') as file:
            lines = await file.readlines()

        current_entry = None

        _id = 1
        for line in lines:
            ev: Dict[str, str] = json.loads(line.strip())

            if current_entry:
                log_entries.append(current_entry)
            current_entry = {
                'id': _id,
                '时间': ev['timestamp'],
                '日志等级': ev['level'].upper(),
                # '模块': ev['pathname'],
                '内容': ev['event'],
            }
            _id += 1

        if current_entry:
            log_entries.append(current_entry)

        self.log_list[log_file_path.name] = log_entries
        return log_entries


def get_all_log_path():
    return [
        file
        for file in LOG_PATH.iterdir()
        if file.is_file() and file.suffix == '.log'
    ]
