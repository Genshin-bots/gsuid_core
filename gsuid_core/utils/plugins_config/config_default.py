from typing import Dict

from .models import GSC, GsStrConfig, GsBoolConfig, GsTimeRConfig

CONIFG_DEFAULT: Dict[str, GSC] = {
    "StartVENV": GsStrConfig(
        "设置启动环境工具",
        "可选pdm, poetry, python, auto, uv",
        "auto",
        ["pdm", "poetry", "python", "uv", "auto"],
    ),
    "is_use_custom_restart_command": GsBoolConfig(
        "使用自定义重启命令",
        "是否使用下面的自定义重启命令, 否则自动判断环境",
        False,
    ),
    "restart_command": GsStrConfig(
        "自定义重启命令",
        "自定义使用gs重启时触发的控制台命令(看不懂勿改)",
        "poetry run python",
    ),
    "AutoUpdateCore": GsBoolConfig(
        "自动更新Core",
        "每晚凌晨三点四十自动更新core本体, 但不会自动重启应用更新",
        True,
    ),
    "AutoUpdatePlugins": GsBoolConfig(
        "自动更新Core内所有插件",
        "每晚凌晨四点十分自动更新全部插件, 但不会自动重启应用更新",
        True,
    ),
    "AutoRestartCore": GsBoolConfig(
        "自动重启Core",
        "每晚凌晨四点四十自动重启core",
        False,
    ),
    "AutoUpdateCoreTime": GsTimeRConfig(
        "自动更新Core时间设置",
        "每晚自动更新Core时间设置(时, 分)",
        (3, 40),
    ),
    "AutoUpdatePluginsTime": GsTimeRConfig(
        "自动更新Core内所有插件时间设置",
        "每晚自动更新Core内所有插件时间设置(时, 分)",
        (4, 10),
    ),
    "AutoRestartCoreTime": GsTimeRConfig(
        "自动重启Core时间设置",
        "每晚自动重启Core时间设置(时, 分)",
        (4, 40),
    ),
    "AutoUpdateNotify": GsBoolConfig(
        "自动更新Core/插件时将内容通知主人",
        "自动更新Core/插件时将内容通知主人",
        True,
    ),
    "AutoInstallDep": GsBoolConfig(
        "自动安装依赖",
        "安装插件时将会自动安装依赖",
        True,
    ),
    "AutoUpdateDep": GsBoolConfig(
        "自动更新依赖",
        "启动Core时将会自动更新插件依赖",
        False,
    ),
    "AutoReloadPlugins": GsBoolConfig(
        "自动重载插件",
        "Core内插件更新/安装时自动载入/重载",
        True,
    ),
    "ProxyURL": GsStrConfig(
        "安装插件时使用git代理地址",
        "git代理地址",
        "",
    ),
}
