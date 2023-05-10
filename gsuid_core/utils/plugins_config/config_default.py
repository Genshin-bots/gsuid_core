from typing import Dict

from .models import GSC, GsStrConfig, GsBoolConfig, GsListStrConfig

CONIFG_DEFAULT: Dict[str, GSC] = {
    'proxy': GsStrConfig('设置代理', '设置国际服的代理地址', ''),
    '_pass_API': GsStrConfig('神奇API', '设置某种神奇的API', ''),
    'restart_command': GsStrConfig(
        '重启命令',
        '自定义使用gs重启时触发的控制台命令(看不懂勿改)',
        'poetry run python',
    ),
    'MhySSLVerify': GsBoolConfig(
        'ssl校验',
        '开启或关闭米游社请求验证是否使用ssl校验',
        True,
    ),
    'CaptchaPass': GsBoolConfig(
        '失效项',
        '该选项已经无效且可能有一定危险性...',
        False,
    ),
    'MysPass': GsBoolConfig(
        '无效项',
        '该选项已经无效且可能有一定危险性...',
        False,
    ),
    'AutoUpdateCore': GsBoolConfig(
        '自动更新Core',
        '每晚凌晨三点四十自动更新core本体, 但不会自动重启应用更新',
        True,
    ),
    'AutoUpdatePlugins': GsBoolConfig(
        '自动更新Core内所有插件',
        '每晚凌晨四点十分自动更新全部插件, 但不会自动重启应用更新',
        True,
    ),
    'AutoRestartCore': GsBoolConfig(
        '自动重启Core',
        '每晚凌晨四点四十自动重启core',
        False,
    ),
    'AutoUpdateCoreTime': GsListStrConfig(
        '自动更新Core时间设置', '每晚自动更新Core时间设置(时, 分)', ['3', '40']
    ),
    'AutoUpdatePluginsTime': GsListStrConfig(
        '自动更新Core内所有插件时间设置', '每晚自动更新Core内所有插件时间设置(时, 分)', ['4', '10']
    ),
    'AutoRestartCoreTime': GsListStrConfig(
        '自动重启Core时间设置', '每晚自动重启Core时间设置(时, 分)', ['4', '40']
    ),
}
