"""插件命令触发热度。

命令次数已经在 CoreDataAnalysis 里按「用户 × 命令 × 日」累加，
这里只做一次 GROUP BY，再映射到插件。结果在进程内缓存，
插件页反复打开不会重算。消息流水不参与这次查询。
"""

from __future__ import annotations

import time
import asyncio
import datetime
from typing import Mapping, Optional, TypedDict

from gsuid_core.sv import SL
from gsuid_core.trigger import Trigger
from gsuid_core.global_val import bot_val
from gsuid_core.utils.database.global_val_models import (
    CoreDataAnalysis,
    is_uuid4_command_name,
)

# 近 7 天足够区分常用插件，又把扫描范围压在大约一周的命令汇总上。
USAGE_WINDOW_DAYS = 7
# 排名不需要实时。10 分钟内重复打开插件页直接读缓存。
USAGE_CACHE_TTL_SECONDS = 600


class PluginUsageItem(TypedDict):
    name: str
    triggers: int


class PluginUsagePayload(TypedDict):
    window_days: int
    plugins: list[PluginUsageItem]


_cache_lock = asyncio.Lock()
_cache_mono: float = 0.0
_cache_payload: Optional[PluginUsagePayload] = None


def fold_keywords_to_plugins(
    keyword_counts: Mapping[str, int],
    keyword_to_plugin: Mapping[str, str],
) -> dict[str, int]:
    """把命令关键字次数归到插件。同一个关键字只记给先注册的插件，避免重复累加。"""
    totals: dict[str, int] = {}
    for keyword, count in keyword_counts.items():
        if not keyword or count <= 0 or is_uuid4_command_name(keyword):
            continue
        plugin = keyword_to_plugin[keyword] if keyword in keyword_to_plugin else None
        if plugin is None and keyword[:100] in keyword_to_plugin:
            plugin = keyword_to_plugin[keyword[:100]]
        if plugin is None:
            continue
        totals[plugin] = (totals[plugin] if plugin in totals else 0) + int(count)
    return totals


def merge_plugin_counts(*parts: Mapping[str, int]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for part in parts:
        for name, count in part.items():
            if count <= 0:
                continue
            totals[name] = (totals[name] if name in totals else 0) + int(count)
    return totals


def rank_plugins(counts: Mapping[str, int]) -> list[PluginUsageItem]:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    return [{"name": name, "triggers": int(count)} for name, count in ranked if count > 0]


def keyword_plugin_map() -> dict[str, str]:
    """当前进程里已注册的命令关键字 → 插件名。on_message 不计入。"""
    mapping: dict[str, str] = {}
    for sv in SL.lst.values():
        plugin = sv.self_plugin_name
        if not plugin:
            continue
        for bucket in sv.TL.values():
            for trigger in bucket.values():
                if not isinstance(trigger, Trigger):
                    continue
                if trigger.type == "message":
                    continue
                keyword = trigger.keyword
                if not keyword or is_uuid4_command_name(keyword):
                    continue
                mapping.setdefault(keyword[:100], plugin)
    return mapping


def live_today_keyword_counts() -> dict[str, int]:
    """今天的次数以内存为准。启动时已从库灌进 bot_val，之后只在内存里加。"""
    totals: dict[str, int] = {}
    for platforms in bot_val.values():
        for platform in platforms.values():
            users = platform["user"]
            for commands in users.values():
                for keyword, count in commands.items():
                    if not keyword or is_uuid4_command_name(str(keyword)):
                        continue
                    key = str(keyword)
                    totals[key] = (totals[key] if key in totals else 0) + int(count or 0)
    return totals


async def compute_plugin_usage(today: Optional[datetime.date] = None) -> PluginUsagePayload:
    day = today or datetime.date.today()
    start = day - datetime.timedelta(days=USAGE_WINDOW_DAYS - 1)
    history = await CoreDataAnalysis.sum_user_commands_between(start, day)
    keyword_map = keyword_plugin_map()
    counts = merge_plugin_counts(
        fold_keywords_to_plugins(history, keyword_map),
        fold_keywords_to_plugins(live_today_keyword_counts(), keyword_map),
    )
    return {
        "window_days": USAGE_WINDOW_DAYS,
        "plugins": rank_plugins(counts),
    }


async def get_plugin_usage() -> PluginUsagePayload:
    global _cache_mono, _cache_payload
    now = time.monotonic()
    if _cache_payload is not None and now - _cache_mono < USAGE_CACHE_TTL_SECONDS:
        return _cache_payload
    async with _cache_lock:
        now = time.monotonic()
        if _cache_payload is not None and now - _cache_mono < USAGE_CACHE_TTL_SECONDS:
            return _cache_payload
        payload = await compute_plugin_usage()
        _cache_payload = payload
        _cache_mono = time.monotonic()
        return payload


def reset_plugin_usage_cache() -> None:
    global _cache_mono, _cache_payload
    _cache_mono = 0.0
    _cache_payload = None
