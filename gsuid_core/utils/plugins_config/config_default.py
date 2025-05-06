from typing import Dict

from .models import GSC, GsStrConfig, GsBoolConfig, GsListStrConfig

CONIFG_DEFAULT: Dict[str, GSC] = {
    'StartVENV': GsStrConfig(
        '设置启动环境工具',
        '可选pdm, poetry, python, auto, uv',
        'auto',
        ['pdm', 'poetry', 'python', 'uv', 'auto'],
    ),
    'Gproxy': GsStrConfig('设置米游社国际代理', '设置国际服的代理地址', ''),
    'Nproxy': GsStrConfig('设置米游社常规代理', '设置常规的代理地址', ''),
    '_pass_API': GsStrConfig('神奇API', '设置某种神奇的API', ''),
    'WebConsoleCDN': GsStrConfig(
        '网页控制台CDN',
        '网页控制台CDN地址',
        'https://unpkg.com/',
        options=[
            'https://unpkg.com/',
            'https://cdn.jsdelivr.net/npm/',
            'https://cdn.jsdmirror.com',
        ],
    ),
    'is_use_custom_restart_command': GsBoolConfig(
        '使用自定义重启命令',
        '是否使用下面的自定义重启命令, 否则自动判断环境',
        False,
    ),
    'restart_command': GsStrConfig(
        '自定义重启命令',
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
        '自动更新Core内所有插件时间设置',
        '每晚自动更新Core内所有插件时间设置(时, 分)',
        ['4', '10'],
    ),
    'AutoUpdateNotify': GsBoolConfig(
        '自动更新Core/插件时将内容通知主人',
        '自动更新Core/插件时将内容通知主人',
        True,
    ),
    'AutoRestartCoreTime': GsListStrConfig(
        '自动重启Core时间设置', '每晚自动重启Core时间设置(时, 分)', ['4', '40']
    ),
    'AutoInstallDep': GsBoolConfig(
        '自动安装依赖',
        '安装插件时将会自动安装依赖',
        True,
    ),
    'AutoUpdateDep': GsBoolConfig(
        '自动更新依赖',
        '启动Core时将会自动更新插件依赖',
        False,
    ),
    'AutoReloadPlugins': GsBoolConfig(
        '自动重载插件',
        'Core内插件更新/安装时自动载入/重载',
        True,
    ),
    'EnablePicSrv': GsBoolConfig(
        '启用将图片转链接发送(需公网)',
        '发送图片转链接',
        False,
    ),
    'PicSrv': GsStrConfig(
        '图片转链接为(需公网)',
        '发送图片转链接',
        '',
    ),
    'EnableCleanPicSrv': GsBoolConfig(
        '是否定期清理本地图床',
        '定期清理图床开关',
        True,
    ),
    'ScheduledCleanPicSrv': GsStrConfig(
        '本地图床定期清理(秒)',
        '定期删除图片',
        '180',
    ),
    'ProxyURL': GsStrConfig(
        '安装插件时使用git代理地址',
        'git代理地址',
        '',
    ),
    'SendMDPlatform': GsListStrConfig(
        '默认发送MD的平台列表',
        '发送MD的平台列表',
        [],
        [
            "villa",
            "kaiheila",
            "dodo",
            "discord",
            "telegram",
            "qqgroup",
            "qqguild",
        ],
    ),
    'SendButtonsPlatform': GsListStrConfig(
        '默认发送按钮的平台列表',
        '发送按钮的平台列表',
        ["villa", "kaiheila", "dodo", "discord", "telegram"],
        [
            "villa",
            "kaiheila",
            "dodo",
            "discord",
            "telegram",
            "qqgroup",
            "qqguild",
        ],
    ),
    'SendTemplatePlatform': GsListStrConfig(
        '默认发送模板按钮/MD的平台列表',
        '发送按钮的平台列表',
        ["qqgroup", "qqguild"],
        ["qqgroup", "qqguild"],
    ),
    'TryTemplateForQQ': GsBoolConfig(
        '启用后尝试读取模板文件并发送',
        '发送MD和按钮模板',
        True,
    ),
    'ForceSendMD': GsBoolConfig(
        '强制使用MD发送图文',
        '强制使用MD发送图文',
        False,
    ),
    'UseCRLFReplaceLFForMD': GsBoolConfig(
        '发送MD时使用CR替换LF',
        '发送MD时使用CR替换LF',
        True,
    ),
    'SplitMDAndButtons': GsBoolConfig(
        '发送MD消息时将按钮分开发送',
        '发送MD消息时将按钮分开发送',
        False,
    ),
    'ShieldQQBot': GsListStrConfig(
        '含@该ID时消息禁止响应',
        '当消息中包含@QQ机器人时禁止Core响应其他平台',
        ['38890', '28541', '28542'],
        ['38890', '28541', '28542'],
    ),
    'ScheduledCleanLogDay': GsStrConfig(
        '定时清理几天外的日志',
        '定时清理几天外的日志',
        '8',
    ),
    'EnableForwardMessage': GsStrConfig(
        '是否允许发送合并转发',
        '可选循环发送、合并消息、合并转发、禁止',
        '允许',
        [
            "允许",
            "禁止(不发送任何消息)",
            "合并为一条消息",
            "1",
            "2",
            "3",
            "4",
            "5",
            "全部拆成单独消息",
        ],
    ),
}
