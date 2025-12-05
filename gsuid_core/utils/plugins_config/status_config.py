from typing import Dict

from gsuid_core.data_store import get_res_path

from .models import GSC, GsStrConfig, GsBoolConfig, GsImageConfig

STATUS_CONIFG: Dict[str, GSC] = {
    "CustomBg": GsBoolConfig(
        "是否开启自定义背景",
        "开启路径位于GsCore/BG",
        False,
    ),
    "CustomIcon": GsBoolConfig(
        "是否开启自定义头像",
        "开启路径位于GsCore/BG",
        False,
    ),
    "CustomName": GsStrConfig(
        "自定义名称",
        "可自定义名称",
        "机器人小柚子",
        ["机器人小柚子", "早柚Core", "柚子"],
    ),
    "CustomBgPath": GsImageConfig(
        "自定义背景",
        "自定义背景图片",
        str(get_res_path(["GsCore", "bg"]) / "MsgBg.jpg"),
        str(get_res_path(["GsCore", "bg"])),
        "MsgBg",
        "jpg",
    ),
    "CustomIconPath": GsImageConfig(
        "自定义头像",
        "自定义头像图片",
        str(get_res_path(["GsCore", "bg"]) / "MsgIcon.png"),
        str(get_res_path(["GsCore", "bg"])),
        "MsgIcon",
        "png",
    ),
    "CustomTheme": GsStrConfig(
        "自定义主题色",
        "可自定义主题色",
        "#5E4FA9",
        ["#5E4FA9", "#38A1F3", "#FFC107", "#4CAF50", "#F44336"],
    ),
    "CustomSubtitle": GsStrConfig(
        "自定义副标题",
        "可自定义副标题",
        "祝你拥有美好的一天！",
        ["祝你拥有美好的一天！", "很不高兴为你服务..."],
    ),
}
