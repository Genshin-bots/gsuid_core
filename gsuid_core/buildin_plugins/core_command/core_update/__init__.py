import asyncio
from concurrent.futures import ThreadPoolExecutor

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.logger import logger
from gsuid_core.utils.plugins_update.reload_plugin import reload_plugin
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config
from gsuid_core.utils.plugins_update._plugins import (
    run_install,
    check_retcode,
    update_plugins,
    update_all_plugins,
    set_proxy_all_plugins,
    update_from_git_in_tread,
)

sv_core_config = SV('Core管理', pm=0)


@sv_core_config.on_prefix(('手动重载插件'))
async def send_core_reload_msg(bot: Bot, ev: Event):
    plugin_name = ev.text.strip()
    logger.info(f'🔔 开始执行 [重载] {plugin_name}')
    await bot.send(f'🔔 正在尝试重载插件{plugin_name}...')
    retcode = reload_plugin(plugin_name)
    await bot.send(retcode)


@sv_core_config.on_command(('更新', '强制更新'), block=True)
async def send_core_update_msg(bot: Bot, ev: Event):
    logger.info('开始执行[更新] 早柚核心')
    if '强制' in ev.command:
        level = 1
    else:
        level = 0
    txt = ev.text.replace('插件', '').strip() if ev.text else ''
    if txt:
        await bot.send(f'🔔 正在尝试更新插件{txt}, 请稍等...')
        log_list = await update_plugins(txt, level)
    else:
        await bot.send('🔔 正在尝试更新早柚核心, 请稍等...')
        log_list = await update_from_git_in_tread(level)

    await bot.send(log_list)


@sv_core_config.on_command(('设置代理'), block=True)
async def send_core_set_proxy(bot: Bot, ev: Event):
    logger.info('开始执行[设置代理]')
    proxy_url = ev.text.strip() if ev.text else ''
    core_plugins_config.set_config('ProxyURL', proxy_url)
    await bot.send(
        f'设置成功!\n当前插件安装代理为{core_plugins_config.get_config("ProxyURL").data}'
        '\n之后【新安装】的插件均会应用此代理(当前你的插件【不会改变安装代理地址】！！)'
        '\n你也可以输入命令[应用设置代理]以应用代理到现有全部插件。'
        '\n注意: 代理地址必须以http或https开头。'
        '\n注意: 你也可以输入[设置代理空]来清除当前代理。'
    )


@sv_core_config.on_command(('应用设置代理'), block=True)
async def send_core_update_proxy(bot: Bot, ev: Event):
    logger.info('开始执行[应用设置代理]')
    proxy = ev.text if ev.text else None
    if '无' in ev.text:
        proxy = None

    if proxy and not proxy.startswith(('http', 'https')):
        return (
            '你可能输入了一个错误的git代理地址...'
            '\n注意: 代理地址必须以http或https开头。'
            '\n注意: 你也可以输入[应用设置代理空]来清除当前代理。'
        )

    log_list = await set_proxy_all_plugins(proxy)
    await bot.send(log_list)


@sv_core_config.on_fullmatch(('更新依赖'), block=True)
async def send_core_poetry_install(bot: Bot, ev: Event):
    logger.info('开始执行[更新] 早柚核心依赖')
    if not hasattr(asyncio, 'to_thread'):
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            retcode = await loop.run_in_executor(executor, run_install)
    else:
        retcode = await asyncio.to_thread(run_install)

    im = check_retcode(retcode)
    await bot.send(im)


@sv_core_config.on_fullmatch(
    (
        '全部更新',
        '全部强制更新',
        '强制全部更新',
        '强行强制全部更新',
        '全部强行强制更新',
    ),
    block=True,
)
async def send_core_all_update_msg(bot: Bot, ev: Event):
    logger.info('开始执行[更新] 全部更新')

    if '强制' in ev.command:
        level = 1
        if '强行' in ev.command:
            level = 2
    else:
        level = 0

    log_list = await update_from_git_in_tread(min(level, 1))
    log_list.extend(await update_all_plugins(level))
    await bot.send(log_list)
