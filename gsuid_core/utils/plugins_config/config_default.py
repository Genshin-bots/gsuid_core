from typing import Dict

from .models import GSC, GsStrConfig, GsBoolConfig

CONIFG_DEFAULT: Dict[str, GSC] = {
    'proxy': GsStrConfig('设置代理', '设置国际服的代理地址', ''),
    '_pass_API': GsStrConfig('神奇API', '设置某种神奇的API', ''),
    'restart_command': GsStrConfig(
        '重启命令',
        '自定义使用gs重启时触发的控制台命令(看不懂勿改)',
        'poetry run python',
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
}
