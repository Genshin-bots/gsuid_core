from typing import Dict

from .models import GSC, GsIntConfig, GsStrConfig

SP_CONIFG: Dict[str, GSC] = {
    'ButtonRow': GsIntConfig(
        '按钮默认一行几个',
        '除了插件作者特殊设定的按钮排序',
        2,
        5,
    ),
    'HelpMode': GsStrConfig(
        '帮助模式',
        '帮助模式',
        'dark',
        ['light', 'dark'],
    ),
    'AtSenderPos': GsStrConfig(
        '@发送者位置',
        '消息@发送者的位置',
        '消息最前',
        ['消息最前', '消息最后'],
    ),
}
