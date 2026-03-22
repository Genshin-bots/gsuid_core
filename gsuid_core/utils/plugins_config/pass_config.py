from typing import Dict

from .models import GSC, GsStrConfig, GsBoolConfig

PASS_CONIFG_DEFAULT: Dict[str, GSC] = {
    "_pass_API": GsStrConfig(
        "神奇API",
        "设置某种神奇的API",
        "",
    ),
    "CaptchaPass": GsBoolConfig(
        "失效项",
        "该选项已经无效且可能有一定危险性...",
        False,
    ),
    "MysPass": GsBoolConfig(
        "无效项",
        "该选项已经无效且可能有一定危险性...",
        False,
    ),
}
