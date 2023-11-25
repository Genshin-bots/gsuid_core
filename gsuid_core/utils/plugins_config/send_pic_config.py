from typing import Dict

from .models import GSC, GsStrConfig

SEND_PIC_CONIFG: Dict[str, GSC] = {
    'onebot': GsStrConfig('OneBot图片发送方式', '可选link或base64', 'base64'),
    'onebot_v12': GsStrConfig('OneBot V12图片发送方式', '可选link或base64', 'base64'),
    'qqguild': GsStrConfig('QQ Guild图片发送方式', '可选link或base64', 'base64'),
    'qqgroup': GsStrConfig('QQ Group图片发送方式', '可选link或base64', 'link'),
    'telegram': GsStrConfig('Telegram图片发送方式', '可选link或base64', 'base64'),
    'discord': GsStrConfig('Discord图片发送方式', '可选link或base64', 'base64'),
    'kook': GsStrConfig('KOOK图片发送方式', '可选link或base64', 'base64'),
    'dodo': GsStrConfig('DoDo图片发送方式', '可选link或base64', 'base64'),
    'feishu': GsStrConfig('飞书图片发送方式', '可选link或base64', 'base64'),
    'ntchat': GsStrConfig('NtChat图片发送方式', '可选link或base64', 'base64'),
    'villa': GsStrConfig('米游社大别野图片发送方式', '可选link或base64', 'base64'),
    'console': GsStrConfig('本地client.py图片发送方式', '可选link或base64', 'link_local'),
}
