from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.utils.plugins_update._plugins import (
    refresh_list,
    update_plugins,
    get_plugins_url,
    install_plugins,
    uninstall_plugin,
    check_plugin_exist,
)

sv_core_install_plugins = SV('core管理插件', pm=0)


@sv_core_install_plugins.on_prefix(('core卸载插件'))
async def send_plugins_uninstall(bot: Bot, ev: Event):
    if not ev.text:
        return await bot.send(
            '请在命令之后加上要卸载插件名称!\n例如: core卸载插件GenshinUID'
        )
    plugin_name = ev.text.strip()
    path = await check_plugin_exist(plugin_name)
    if path is None:
        return await bot.send('不存在该插件...请检查是否输入正确！')
    elif isinstance(path, str):
        return await bot.send(path)

    resp = await bot.receive_resp(
        '再次确认是否要删除插件文件夹？\n输入Y确认删除！',
    )
    if resp is not None:
        if resp.text.lower() == 'y':
            await bot.send('开始删除...请稍等一段时间...')
            im = await uninstall_plugin(path)
            await bot.send(im)
        else:
            await bot.send('已取消删除！')
    else:
        await bot.send('已取消删除！')


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


@sv_core_install_plugins.on_prefix(
    ('core更新插件', 'core强制更新插件', 'core强行强制更新插件')
)
async def send_update_msg(bot: Bot, ev: Event):
    await bot.send('开始更新...请稍等一段时间...')
    if '强制' in ev.command:
        if '强行' in ev.command:
            level = 2
        else:
            level = 1
    else:
        level = 0
    _list = await update_plugins(ev.text, level)
    await bot.send(_list)
