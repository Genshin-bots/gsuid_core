"""消息缓存配置（框架配置 → 消息缓存）。"""

from typing import Dict

from .models import GSC, GsIntConfig

# 上限一年，够大又不会让「误填 999999」把库撑爆
MAX_CACHE_HOURS = 8760

MESSAGE_CACHE_CONIFG: Dict[str, GSC] = {
    "CacheHours": GsIntConfig(
        "缓存消息时长(小时)",
        "把消息原始段写进数据库保留多久，重启后控制台还能翻；0 表示不缓存",
        0,
        MAX_CACHE_HOURS,
        [0, 1, 6, 12, 24, 72, 168, 720, 8760],
    ),
}
