from typing import List

from gsuid_core.sv import SL, SV
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.utils.plugins_config.gs_config import sp_config

sv_pm_core_config = SV("Core权限管理", pm=0)


@sv_pm_core_config.on_command(
    (
        "拉黑",
        "加入黑名单",
        "封禁",
        "取消拉黑",
        "解封",
        "取消封禁",
        "移出黑名单",
    ),
    block=True,
)
async def add_blacklist_msg(bot: Bot, ev: Event):
    logger.info(f"[Core权限管理] {ev.command} {ev.text}")
    is_ban = ev.command in ["封禁", "加入黑名单", "拉黑"]
    ban_k = "移除" if is_ban else "重新给予"

    params = ev.text.split()

    alias_list = {
        alias.lower(): SL.plugins[plugin]
        for plugin in SL.plugins
        for alias in SL.plugins[plugin].alias
    }

    alias_list.update(
        {plugin.lower(): SL.plugins[plugin] for plugin in SL.plugins}
    )

    if ev.at_list:
        params.extend(ev.at_list)

    if not params:
        params = [ev.group_id or ev.user_id]

    if params[0].lower() in alias_list:
        plugin = alias_list[params[0].lower()]
        block_list = params[1:]
    else:
        plugin = None
        block_list = params

    if not block_list:
        block_list.append(ev.group_id or ev.user_id)

    logger.info(
        f"[Core权限管理] {ev.command} {plugin.name if plugin else '全局'} "
        f"{ban_k} {' '.join(block_list)}"
    )

    if plugin is None:
        resp = await bot.receive_resp(
            f"⛔ [Core权限管理] 你好像没有指定要{ev.command}的插件！\n"
            f"❓ 是需要全局{ev.command}用户/群组 {'/'.join(block_list)} 吗？(请输入是或否) \n"
            f"➡ 如需指定插件, 请输入: {ev.command} 插件名 {' '.join(block_list)}"
        )
        if resp:
            if resp.text.startswith("是"):
                all_balck_list: List[str] = sp_config.get_config(
                    "BlackList"
                ).data
                if is_ban:
                    all_balck_list.extend(block_list)
                    sp_config.set_config("BlackList", all_balck_list)
                    return await bot.send(
                        f"⛔ [Core权限管理] 已全局封禁{' '.join(block_list)}"
                    )
                else:
                    im_list = ["✅ [Core权限管理] 操作已完成!"]
                    for i in block_list:
                        if i in all_balck_list:
                            all_balck_list.remove(i)
                            im_list.append(f"✅ 已成功取消封禁{i}")
                        else:
                            im_list.append(f"❗ 该用户{i}未被封禁, 无需取消!")
                    sp_config.set_config("BlackList", all_balck_list)
                    return await bot.send("\n".join(im_list))
            else:
                return await bot.send("✅ [Core权限管理] 已取消操作！")
        else:
            return await bot.send("✅ [Core权限管理] 已取消操作！")

    resp = await bot.receive_resp(
        f"⛔ [Core权限管理] 你确定要对用户/群组 {'/'.join(block_list)} \n"
        f"{ban_k}对插件 {plugin.name} 的访问权限吗？(请输入是或否)"
    )
    if resp:
        if resp.text.startswith("是"):
            if is_ban:
                plugin.black_list.extend(block_list)
                plugin.set(black_list=plugin.black_list)
                await bot.send(
                    f"⛔ [Core权限管理] 已对用户/群组 {'/'.join(block_list)}"
                    f"封禁{plugin.name}"
                )
            else:
                im_list = ["✅ [Core权限管理] 操作已完成!"]
                for i in block_list:
                    if i in plugin.black_list:
                        plugin.black_list.remove(i)
                        im_list.append(
                            f"✅ 已成功给予 {i} 对插件 {plugin.name} 的访问权限!"
                        )
                    else:
                        im_list.append(f"❌ 该用户{i}未被封禁, 无需取消!")
                plugin.set(black_list=plugin.black_list)
                await bot.send("\n".join(im_list))
        else:
            return await bot.send("✅ [Core权限管理] 已取消操作！")
