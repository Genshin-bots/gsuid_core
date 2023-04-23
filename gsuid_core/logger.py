import sys
import logging
import datetime
from typing import TYPE_CHECKING

import loguru

from gsuid_core.config import core_config
from gsuid_core.models import Event, Message
from gsuid_core.data_store import get_res_path

if TYPE_CHECKING:
    # avoid sphinx autodoc resolve annotation failed
    # because loguru module do not have `Logger` class actually
    from loguru import Logger

logger: 'Logger' = loguru.logger


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


def format_event(record):
    if 'trigger' in record['extra']:
        _tg = record['extra']['trigger']
        message = (
            f'<m><b>[Trigger]</b></m> 消息 「{_tg[0]}」 触发'
            f' 「{_tg[1]}」 类型触发器, 关键词:'
            f' 「{_tg[2]}」 '
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

        if 'event' in record['extra']:
            message = (
                f'<c><b>[Raw]</b></c> '
                f'raw_text={event.raw_text}, '
                f'image={event.image}, '
                f'at={event.at}, '
                f'image_list={event.image}, '
                f'at_list={event.at_list}, '
                f'is_tome={event.is_tome}, '
                f'reply={event.reply}, '
                f'file_name={event.file_name}, '
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
                f'command={event.command}, '
                f'text={event.text}'
            )
        message = message.replace('{', '{{').replace('}', '}}')
    else:
        message = '{message}'

    def_name: str = record['name']
    time = '<g>{time:MM-DD HH:mm:ss}</g>'
    level = '[<lvl>{level}</lvl>]'
    def_name = f'<c><u>{".".join(def_name.split(".")[-5:])}</u></c>'
    return f'{time} {level} {def_name} | {message} \n'


LEVEL: str = core_config.get_config('log').get('level', 'INFO')

logger.remove()
logger_id = logger.add(
    sys.stdout, level=LEVEL, diagnose=False, format=format_event
)

logger.add(
    sink=get_res_path() / 'logs/{time:YYYY-MM-DD}.log',
    format=format_event,
    rotation=datetime.time(),
    level=LEVEL,
    diagnose=False,
)
