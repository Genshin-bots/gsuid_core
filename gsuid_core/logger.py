import re
import sys
import asyncio
import logging
import datetime
from pathlib import Path
from functools import wraps
from typing import TYPE_CHECKING, Dict, List, Optional

import loguru
import aiofiles
from uvicorn.config import LOGGING_CONFIG

from gsuid_core.config import core_config
from gsuid_core.models import Event, Message
from gsuid_core.data_store import get_res_path

log_history = []
LOG_PATH = get_res_path() / 'logs'


if TYPE_CHECKING:
    # avoid sphinx autodoc resolve annotation failed
    # because loguru module do not have `Logger` class actually
    from loguru import Logger

logger: 'Logger' = loguru.logger
logging.getLogger().handlers = []
LOGGING_CONFIG['disable_existing_loggers'] = False


# https://loguru.readthedocs.io/en/stable/overview.html#entirely-compatible-with-standard-logging
class LoguruHandler(logging.Handler):  # pragma: no cover
    def emit(self, record: logging.LogRecord):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def replace_tag(text: Optional[str]):
    if text is None:
        return ''
    return text.replace('<', '\<')  # type: ignore # noqa: W605


def format_event(record):
    if 'trigger' in record['extra']:
        _tg = record['extra']['trigger']
        _tg0 = replace_tag(_tg[0])
        _tg1 = replace_tag(_tg[1])
        _tg2 = replace_tag(_tg[2])
        message = (
            f'<m><b>[Trigger]</b></m> 消息 「{_tg0}」 触发'
            f' 「{_tg1}」 类型触发器, 关键词:'
            f' 「{_tg2}」 '
        )
        message = message.replace('{', '{{').replace('}', '}}')
    elif record['extra']:
        event: Event = (
            record['extra']['event']
            if 'event' in record['extra']
            else record['extra']['command']
        )
        if event.file and event.file_type != 'url':
            file = f'{event.file[:20]}...(base64)'
            content = [Message('file', f'{event.file_name}|{file}')]
        else:
            file = event.file
            content = event.content

        raw_text = replace_tag(event.raw_text)
        file_name = replace_tag(event.file_name)
        command = replace_tag(event.command)
        text = replace_tag(event.text)
        content = replace_tag(f'{content}')
        regex_dict = replace_tag(f'{event.regex_dict}')

        if 'event' in record['extra']:
            message = (
                f'<c><b>[Raw]</b></c> '
                f'raw_text={raw_text}, '
                f'image={event.image}, '
                f'at={event.at}, '
                f'image_list={event.image}, '
                f'at_list={event.at_list}, '
                f'is_tome={event.is_tome}, '
                f'reply={event.reply}, '
                f'file_name={file_name}, '
                f'file_type={event.file_type}, '
                f'file={file}'
                f' | <m><b>[Receive]</b></m> '
                f'bot_id={event.bot_id}, '
                f'bot_self_id={event.bot_self_id}, '
                f'msg_id={event.msg_id}, '
                f'user_type={event.user_type}, '
                f'group_id={event.group_id}, '
                f'user_id={event.user_id}, '
                f'user_pm={event.user_pm}, '
                f'content={content}, '
            )
        else:
            message = (
                f'<m><b>[Command]</b></m> '
                f'command={command}, '
                f'text={text}, '
                f'regex_dict={regex_dict}'
            )
        message = message.replace('{', '{{').replace('}', '}}')
    else:
        message = '{message}'

    def_name: str = record['name']
    time = '<g>{time:MM-DD HH:mm:ss}</g>'
    level = '[<lvl>{level}</lvl>]'
    def_name = f'<c><u>{".".join(def_name.split(".")[-5:])}</u></c>'
    _log = f'{time} {level} {def_name} | {message} \n {{exception}}'
    return _log


def std_format_event(record):
    try:
        data = format_event(record)
        _data = (
            data.replace('<g>', '\033[37m')
            .replace('</g>', '\033[0m')
            .replace('<c><u>', '\033[34m')
            .replace('</u></c>', '\033[0m')
            .replace('<m><b>', '\033[35m')
            .replace('</b></m>', '\033[0m')
            .replace('<c><b>', '\033[32m')
            .replace('</b></c>', '\033[0m')
            .replace('<lvl>', '')
            .replace('</lvl>', '')
        )
        log = _data.format_map(record)
        log_history.append(log[:-5])
        return data
    except:  # noqa: E722
        return 'UnknowLog'


LEVEL: str = core_config.get_config('log').get('level', 'INFO')
logger_list: List[str] = core_config.get_config('log').get(
    'output',
    ['stdout', 'stderr', 'file'],
)

logger.remove()

if 'stdout' in logger_list:
    logger_id = logger.add(
        sys.stdout,
        level=LEVEL,
        diagnose=True,
        backtrace=True,
        filter=lambda record: record['level'].no < 40,
        format=std_format_event,
    )

if 'stderr' in logger_list:
    logger.add(sys.stderr, level='ERROR')

if 'file' in logger_list:
    logger.add(
        sink=LOG_PATH / '{time:YYYY-MM-DD}.log',
        format=format_event,
        rotation=datetime.time(),
        level=LEVEL,
        diagnose=True,
        backtrace=True,
    )


async def read_log():
    index = 0
    while True:
        if index <= len(log_history) - 1:
            if log_history[index]:
                yield log_history[index]
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

        log_entry_pattern = re.compile(
            r'^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)] ([^\|]+) \| (.*)'
        )

        async with aiofiles.open(log_file_path, 'r', encoding='utf-8') as file:
            lines = await file.readlines()

        current_entry = None

        _id = 1
        for line in lines:
            line = line.strip()
            match = log_entry_pattern.match(line)

            if match:
                if current_entry:
                    log_entries.append(current_entry)
                current_entry = {
                    'id': _id,
                    '时间': match.group(1),
                    '日志等级': match.group(2),
                    '模块': match.group(3).strip(),
                    '内容': match.group(4).strip(),
                }
                _id += 1
            elif current_entry:
                current_entry['内容'] += '\n' + line

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
