from typing import Dict

from .models import GSC, GsStrConfig, GsBoolConfig, GsListStrConfig

SECURITY_CONFIG: Dict[str, GSC] = {
    'AutoAddRandomText': GsBoolConfig(
        '自动加入随机字符串', '自动加入随机字符串', False
    ),
    'RandomText': GsStrConfig(
        '随机字符串列表',
        '随机字符串列表',
        'abcdefg',
        ['abcdefghijklmnopqrstuvwxyz', 'abcdefg'],
    ),
    'ChangeErrorToPic': GsBoolConfig(
        '错误提示转换为图片', '将一部分报错提示转换为图片', True
    ),
    'AutoTextToPic': GsBoolConfig(
        '自动文字转图', '将所有发送的文字转图', False
    ),
    'TextToPicThreshold': GsStrConfig(
        '文转图阈值',
        '开启自动转图后超过该阈值的文字会转成图片',
        '150',
        ['80', '120', '150', '200'],
    ),
    'EnableSpecificMsgId': GsBoolConfig(
        '启用回复特殊ID', '如不知道请勿开启', False
    ),
    'SpecificMsgId': GsStrConfig('特殊返回消息ID', '如不知道请勿填写', ''),
    'EnableBanList': GsBoolConfig(
        '启用违禁词屏蔽', '自动检测发送违禁词并进行屏蔽', False
    ),
    'BanList': GsListStrConfig('违禁词屏蔽列表', '对列表中的词进行屏蔽', []),
}
