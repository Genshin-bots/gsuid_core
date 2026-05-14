import asyncio
from concurrent.futures import ThreadPoolExecutor

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.utils.plugins_update._plugins import (
    is_reload,
    run_install,
    check_retcode,
    update_plugins,
    update_all_plugins,
    resolve_plugin_name,
    set_proxy_all_plugins,
    update_from_git_async,
)
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config
from gsuid_core.utils.plugins_update.reload_plugin import reload_plugin

sv_core_config = SV("Core管理", pm=0)


@sv_core_config.on_prefix(("手动重载插件", "重载插件"))
async def send_core_reload_msg(bot: Bot, ev: Event):
    plugin_name = ev.text.strip()
    if not plugin_name:
        await bot.send("请后跟有效的插件名称！\n例如：core重载插件genshinuid")
        return
    # 和 core更新 共用别名逻辑：支持别名 / 大小写不敏感
    plugin_name = await resolve_plugin_name(plugin_name)
    logger.info(f"🔔 开始执行 [重载] {plugin_name}")
    await bot.send(f"🔔 正在尝试重载插件{plugin_name}...")
    retcode = reload_plugin(plugin_name)
    await bot.send(retcode)


@sv_core_config.on_command(("更新", "强制更新"), block=True)
async def send_core_update_msg(bot: Bot, ev: Event):
    logger.info("开始执行[更新] 早柚核心")
    if "强制" in ev.command:
        level = 1
    else:
        level = 0
    txt = ev.text.replace("插件", "").strip() if ev.text else ""
    if txt:
        await bot.send(f"🔔 正在尝试更新插件{txt}, 请稍等...")
        log_list = await update_plugins(txt, level)
    else:
        await bot.send("🔔 正在尝试更新早柚核心, 请稍等...")
        log_list = await update_from_git_async(level)

    await bot.send(log_list)
    # 仅在未开启自动重载时提示手动重载；开启了的话 update_plugins 已自动重载（有更新才重载）
    if txt and not is_reload:
        await bot.send(f"可使用 core重载插件{txt} 重新加载插件")


@sv_core_config.on_command(("设置镜像"), block=True)
async def send_core_set_mirror(bot: Bot, ev: Event):
    logger.info("开始执行[设置镜像]")
    mirror_input = ev.text.strip() if ev.text else ""

    # 支持快捷名称
    mirror_map = {
        "gitcode": "https://gitcode.com/gscore-mirror/",
        "cnb": "https://cnb.cool/gscore-mirror/",
        "ghproxy": "https://ghproxy.mihomo.me/",
        "ssh": "ssh://",
        "github": "",
        "无": "",
        "空": "",
    }

    mirror_prefix = mirror_map.get(mirror_input.lower(), mirror_input)

    if mirror_prefix and not mirror_prefix.startswith(("http", "https", "ssh://")):
        await bot.send(
            "你可能输入了一个错误的镜像地址..."
            "\n支持的快捷名称: gitcode, cnb, ghproxy, ssh, github"
            "\n或直接输入完整的镜像前缀URL"
        )
        return

    core_plugins_config.set_config("GitMirror", mirror_prefix)
    display = mirror_prefix if mirror_prefix else "GitHub (默认)"
    await bot.send(
        f"设置成功!\n当前镜像源为: {display}"
        "\n之后【新安装】的插件均会使用此镜像源。"
        "\n你也可以输入命令[应用镜像]以应用镜像到现有全部插件。"
    )


@sv_core_config.on_command(("应用镜像"), block=True)
async def send_core_apply_mirror(bot: Bot, ev: Event):
    logger.info("开始执行[应用镜像]")
    mirror_input = ev.text.strip() if ev.text else ""

    # 支持快捷名称
    mirror_map = {
        "gitcode": "https://gitcode.com/gscore-mirror/",
        "cnb": "https://cnb.cool/gscore-mirror/",
        "ghproxy": "https://ghproxy.mihomo.me/",
        "ssh": "ssh://",
        "github": "",
        "无": "",
        "空": "",
    }

    mirror_prefix = mirror_map.get(mirror_input.lower(), mirror_input)

    if mirror_prefix and not mirror_prefix.startswith(("http", "https", "ssh://")):
        await bot.send(
            "你可能输入了一个错误的镜像地址..."
            "\n支持的快捷名称: gitcode, cnb, ghproxy, ssh, github"
            "\n或直接输入完整的镜像前缀URL"
        )
        return

    log_list = await set_proxy_all_plugins(mirror_prefix)
    await bot.send(log_list)


@sv_core_config.on_fullmatch(("更新依赖"), block=True)
async def send_core_poetry_install(bot: Bot, ev: Event):
    logger.info("开始执行[更新] 早柚核心依赖")
    if not hasattr(asyncio, "to_thread"):
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            retcode = await loop.run_in_executor(executor, run_install)
    else:
        retcode = await asyncio.to_thread(run_install)

    im = check_retcode(retcode)
    await bot.send(im)


@sv_core_config.on_fullmatch(
    (
        "全部更新",
        "全部强制更新",
        "强制全部更新",
        "强行强制全部更新",
        "全部强行强制更新",
    ),
    block=True,
)
async def send_core_all_update_msg(bot: Bot, ev: Event):
    logger.info("开始执行[更新] 全部更新")

    if "强制" in ev.command:
        level = 1
        if "强行" in ev.command:
            level = 2
    else:
        level = 0

    await bot.send("🔔 正在尝试更新本体 + 全部插件, 请稍等片刻。")
    log_list = await update_from_git_async(min(level, 1))
    log_list.extend(await update_all_plugins(level))
    await bot.send(log_list)
