from typing import Dict

from .models import GSC, GsStrConfig, GsBoolConfig

LOG_CONFIG: Dict[str, GSC] = {
    "ScheduledCleanLogDay": GsStrConfig(
        "定时清理几天外的日志",
        "定时清理几天外的日志",
        "8",
    ),
    "ShowReceive": GsBoolConfig(
        "显示用户普通消息",
        "关闭该选项将导致log只记录命令触发！",
        True,
    ),
}
