from typing import Dict

from .models import GSC, GsStrConfig

SEND_PIC_CONIFG: Dict[str, GSC] = {
    "onebot": GsStrConfig(
        "OneBot图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "red": GsStrConfig(
        "Red图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "onebot_v12": GsStrConfig(
        "OneBot V12图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "qqguild": GsStrConfig(
        "QQ Guild图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "qqgroup": GsStrConfig(
        "QQ Group图片发送方式",
        "可选link或base64",
        "link",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "telegram": GsStrConfig(
        "Telegram图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "discord": GsStrConfig(
        "Discord图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "kook": GsStrConfig(
        "KOOK图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "dodo": GsStrConfig(
        "DoDo图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "feishu": GsStrConfig(
        "飞书图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "ntchat": GsStrConfig(
        "NtChat图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "villa": GsStrConfig(
        "米游社大别野图片发送方式",
        "可选link或base64",
        "base64",
        ["link", "base64", "link_local", "link_remote"],
    ),
    "console": GsStrConfig(
        "本地client.py图片发送方式",
        "可选link或base64",
        "link_local",
        ["link", "base64", "link_local", "link_remote"],
    ),
}
