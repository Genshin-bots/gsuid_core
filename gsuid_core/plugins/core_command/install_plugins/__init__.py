from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.utils.plugins_update._plugins import (
    refresh_list,
    update_plugins,
    get_plugins_url,
    install_plugins,
)

sv_core_install_plugins = SV('core管理插件', pm=1)


@sv_core_install_plugins.on_prefix(('core安装插件'))
async def send_plugins_install(bot: Bot, ev: Event):
    plugins = await get_plugins_url(ev.text.strip().lower())
    if not plugins:
        return await bot.send(
            '不存在该插件...可以使用[core刷新插件列表]获取最新列表!'
        )

    await bot.send('开始安装...请稍等一段时间...')
    im = install_plugins(plugins)
    await bot.send(im)


@sv_core_install_plugins.on_fullmatch(('core刷新插件列表'))
async def refresh_plugins_list(bot: Bot, ev: Event):
    _list = await refresh_list()
    if len(_list) <= 3:
        im = f'刷新成功! 刷新插件{",".join(_list)}!'
    else:
        im = f'刷新成功! 已刷新{len(_list)}个插件!'
    await bot.send(im)


@sv_core_install_plugins.on_prefix(('core更新插件'))
async def send_update_msg(bot: Bot, ev: Event):
    await bot.send('开始更新...请稍等一段时间...')
    _list = update_plugins(ev.text)
    await bot.send(_list)
