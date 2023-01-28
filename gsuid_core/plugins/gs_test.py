import asyncio

from gsuid_core.bot import Bot
from gsuid_core.sv import SL, SV
from gsuid_core.model import MessageContent


@SV('开关').on_prefix(('关闭', '开启'))
async def get_switch_msg(bot: Bot, msg: MessageContent):
    name = msg.text
    if not name:
        return

    await bot.send('正在进行[关闭/开启开关]')

    if name in SL.lst:
        if msg.command == '关闭':
            SL.lst[name].disable()
            await bot.send('关闭成功！')
        else:
            SL.lst[name].enable()
            await bot.send('开启成功！')
    else:
        await bot.send('未找到该服务...')


@SV('测试').on_fullmatch('全匹配测试')
async def get_fullmatch_msg(bot: Bot, msg: MessageContent):
    await bot.send('正在进行[全匹配测试]')
    await asyncio.sleep(2)
    await bot.send('[全匹配测试]校验成功！')


@SV('测试').on_prefix('前缀测试')
async def get_prefix_msg(bot: Bot, msg: MessageContent):
    await bot.send('正在进行[前缀测试]')
    await asyncio.sleep(2)
    await bot.send('[前缀测试]校验成功！')


@SV('测试').on_suffix('后缀测试')
async def get_suffix_msg(bot: Bot, msg: MessageContent):
    await bot.send('正在进行[后缀测试]')
    await asyncio.sleep(2)
    await bot.send('[后缀测试]校验成功！')


@SV('测试').on_keyword('关键词测试')
async def get_keyword_msg(bot: Bot, msg: MessageContent):
    await bot.send('正在进行[关键词测试]')
    await asyncio.sleep(2)
    await bot.send('[关键词测试]校验成功！')
