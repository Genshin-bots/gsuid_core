"""
米游社 API 包装
"""

from .models import (  # noqa: F401
    AbyssData,
    IndexData,
    MihoyoRole,
    MihoyoAvatar,
    MihoyoWeapon,
    DailyNoteData,
    MihoyoReliquary,
    MihoyoConstellation,
)
from .request import MysApi  # noqa: F401

__all__ = ["models", "request"]
