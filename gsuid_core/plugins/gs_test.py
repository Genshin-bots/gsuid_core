import asyncio

from gsuid_core.bot import Bot
from gsuid_core.sv import SL, SV
from gsuid_core.model import MessageReceive


@SV('开关').on_prefix('关闭')
async def get_switch_msg(bot: Bot, msg: MessageReceive):
    await bot.send('正在进行[关闭开关]')
    name = msg.content[0].data
    if name is None:
        return
    name = name.replace('关闭', '')
    if name in SL.lst:
        SL.lst[name].disable()
        await bot.send('关闭成功！')
    else:
        await bot.send('未找到该服务...')


@SV('测试').on_fullmatch('全匹配测试')
async def get_fullmatch_msg(bot: Bot, msg: MessageReceive):
    await bot.send('正在进行[全匹配测试]')
    await asyncio.sleep(2)
    await bot.send('[全匹配测试]校验成功！')


@SV('测试').on_prefix('前缀测试')
async def get_prefix_msg(bot: Bot, msg: MessageReceive):
    await bot.send('正在进行[前缀测试]')
    await asyncio.sleep(2)
    await bot.send('[前缀测试]校验成功！')


@SV('测试').on_suffix('后缀测试')
async def get_suffix_msg(bot: Bot, msg: MessageReceive):
    await bot.send('正在进行[后缀测试]')
    await asyncio.sleep(2)
    await bot.send('[后缀测试]校验成功！')


@SV('测试').on_keyword('关键词测试')
async def get_keyword_msg(bot: Bot, msg: MessageReceive):
    print(msg)
    await bot.send('正在进行[关键词测试]')
    await asyncio.sleep(2)
    await bot.send('[关键词测试]校验成功！')
