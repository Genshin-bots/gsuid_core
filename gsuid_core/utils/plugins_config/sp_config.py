from typing import Dict

from .models import GSC, GsIntConfig, GsStrConfig, GsListStrConfig

SP_CONIFG: Dict[str, GSC] = {
    "ButtonRow": GsIntConfig(
        "按钮默认一行几个",
        "除了插件作者特殊设定的按钮排序",
        2,
        5,
    ),
    "HelpMode": GsStrConfig(
        "帮助模式",
        "帮助模式",
        "dark",
        ["light", "dark"],
    ),
    "AtSenderPos": GsStrConfig(
        "@发送者位置",
        "消息@发送者的位置",
        "消息最前",
        ["消息最前", "消息最后"],
    ),
    "SameUserEventCD": GsIntConfig(
        "启用同个人触发命令CD(0为不启用)",
        "启用同个人触发命令CD(0为不启用)",
        0,
        3600,
        [0, 1, 2, 3, 5, 10, 15, 30],
    ),
    "BlackList": GsListStrConfig(
        "黑名单",
        "黑名单用户/群, 不会触发任何命令",
        [],
    ),
}
