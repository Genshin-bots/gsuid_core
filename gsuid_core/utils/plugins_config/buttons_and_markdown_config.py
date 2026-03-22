from typing import Dict

from .models import GSC, GsIntConfig, GsBoolConfig, GsListStrConfig

BM_CONIFG_DEFAULT: Dict[str, GSC] = {
    "SendMDPlatform": GsListStrConfig(
        "默认发送MD的平台列表",
        "发送MD的平台列表",
        [],
        [
            "villa",
            "kaiheila",
            "dodo",
            "discord",
            "telegram",
            "qqgroup",
            "qqguild",
            "web",
        ],
    ),
    "ButtonRow": GsIntConfig(
        "按钮默认一行几个",
        "除了插件作者特殊设定的按钮排序",
        2,
        5,
    ),
    "SendButtonsPlatform": GsListStrConfig(
        "默认发送按钮的平台列表",
        "发送按钮的平台列表",
        [
            "villa",
            "kaiheila",
            "dodo",
            "discord",
            "telegram",
            "web",
        ],
        [
            "villa",
            "kaiheila",
            "dodo",
            "discord",
            "telegram",
            "qqgroup",
            "qqguild",
            "web",
        ],
    ),
    "SendTemplatePlatform": GsListStrConfig(
        "默认发送模板按钮/MD的平台列表",
        "发送按钮的平台列表",
        ["qqgroup", "qqguild"],
        [
            "qqgroup",
            "qqguild",
            "web",
        ],
    ),
    "TryTemplateForQQ": GsBoolConfig(
        "启用后尝试读取模板文件并发送",
        "发送MD和按钮模板",
        True,
    ),
    "ForceSendMD": GsBoolConfig(
        "强制使用MD发送图文",
        "强制使用MD发送图文",
        False,
    ),
    "UseCRLFReplaceLFForMD": GsBoolConfig(
        "发送MD时使用CR替换LF",
        "发送MD时使用CR替换LF",
        True,
    ),
    "SplitMDAndButtons": GsBoolConfig(
        "发送MD消息时将按钮分开发送",
        "发送MD消息时将按钮分开发送",
        False,
    ),
}
