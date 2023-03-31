import sys
import logging
import datetime
from typing import TYPE_CHECKING

import loguru

from gsuid_core.config import core_config

if TYPE_CHECKING:
    # avoid sphinx autodoc resolve annotation failed
    # because loguru module do not have `Logger` class actually
    from loguru import Logger

logger: "Logger" = loguru.logger


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


FORMAT = (
    "<g>{time:MM-DD HH:mm:ss}</g> "
    "[<lvl>{level}</lvl>] "
    "<c><u>{name}</u></c> | "
    # "<c>{function}:{line}</c>| "
    "{message}"
)

LEVEL: str = core_config.get_config("log").get("level", "INFO")

logger.remove()
logger_id = logger.add(sys.stdout, level=LEVEL, diagnose=False, format=FORMAT)

logger.add(
    "logs/{time:YYYY-MM-DD}.log",
    rotation=datetime.time(),
    level=LEVEL,
    diagnose=False,
    format=FORMAT,
)
