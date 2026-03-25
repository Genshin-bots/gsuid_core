"""
查看配置                          → 列出所有可配置插件
查看配置 <插件名>                  → 展示该插件所有配置项
查看配置 <插件名> <键>             → 查看某一项的详情
设置配置 <插件名> <键> <值>        → 设置配置项
查看插件                          → 列出所有已加载插件
查看插件 <插件名>                  → 查看插件详情
设置插件 <插件名> <参数> <值>      → 设置插件属性
查看命令                          → 列出所有命令(SV)
查看命令 <插件名>                  → 列出该插件下所有命令
查看命令 <命令名>                  → 查看命令详情
设置命令 <命令名> <参数> <值>      → 设置命令属性
"""

from gsuid_core.sv import SL, SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ._render import (
    fmt_val,
    is_skip_type,
    render_sv_list,
    render_sv_detail,
    render_config_list,
    render_plugin_list,
    render_config_items,
    render_config_detail,
    render_plugin_detail,
)
from ._setter import (
    SV_AREA,
    SV_PARAMS,
    PLUGIN_AREA,
    PLUGIN_EXTRA,
    PLUGIN_PARAMS,
    apply_set,
    set_config_val,
)
from ._resolve import (
    hint,
    prefix,
    resolve_sv,
    plugin_configs,
    resolve_plugin,
    all_config_names,
    find_config_item,
    match_config_name,
)

sv_core_config_cmd = SV("Core配置命令", pm=0)


async def _resolve_config_name(raw: str):
    """先精确匹配配置名, 失败则用别名解析第一个词"""
    matched = match_config_name(raw)
    if matched:
        return matched
    first, *remaining = raw.split(None, 1)
    resolved = await resolve_plugin(first)
    if resolved and plugin_configs(resolved):
        return resolved, (remaining[0] if remaining else "")
    return None


# ── 配置 ─────────────────────────────────────────────────


@sv_core_config_cmd.on_command("查看配置", block=True)
async def show_config(bot: Bot, ev: Event):
    raw = ev.text.strip()

    p = prefix()
    if not raw:
        names = sorted(all_config_names())
        if not names:
            await bot.send("暂无可配置插件")
            return
        msg = f"提示: {p}查看配置 <插件名>\n\n"
        await bot.send(msg + render_config_list(names))
        return

    matched = await _resolve_config_name(raw)
    if matched is None:
        await bot.send(f"未找到 [{raw}] 的配置, {hint('查看配置')}")
        return

    plugin_name, rest = matched

    if not rest:
        msg = f"提示: {p}查看配置 {plugin_name} <键>\n\n"
        await bot.send(msg + render_config_items(plugin_name, plugin_configs(plugin_name)))
        return

    result = find_config_item(plugin_name, rest)
    if result is None:
        await bot.send(f"未找到配置项 [{rest}], {hint(f'查看配置 {plugin_name}')}")
        return

    cfg, item = result
    if is_skip_type(item):
        await bot.send(f"[{plugin_name}] {rest} 为 Dict/Image 类型, 请使用 WebConsole 修改")
        return
    msg = f"提示: {p}设置配置 {plugin_name} {rest} <值>\n\n"
    await bot.send(msg + render_config_detail(plugin_name, rest, cfg, item))


@sv_core_config_cmd.on_command("设置配置", block=True)
async def set_config(bot: Bot, ev: Event):
    raw = ev.text.strip()

    if not raw:
        await bot.send(f"格式: {prefix()}设置配置 <插件名> <配置键> <值>")
        return

    matched = await _resolve_config_name(raw)
    if matched is None:
        await bot.send(f"未找到配置, {hint('查看配置')}")
        return

    plugin_name, rest = matched
    parts = rest.split(None, 1)
    if len(parts) < 2:
        await bot.send(f"格式: {prefix()}设置配置 <插件名> <配置键> <值>")
        return

    key, raw_value = parts
    found = find_config_item(plugin_name, key)
    if found is None:
        await bot.send(f"未找到配置项 [{key}]")
        return

    cfg, _ = found
    await bot.send(set_config_val(cfg, plugin_name, key, raw_value, is_skip_type, fmt_val))


# ── 插件 ─────────────────────────────────────────────────


@sv_core_config_cmd.on_command("查看插件", block=True)
async def show_plugin(bot: Bot, ev: Event):
    raw = ev.text.strip()

    p = prefix()
    if not raw:
        msg = f"提示: {p}查看插件 <插件名>\n\n"
        await bot.send(msg + render_plugin_list())
        return

    plugin_name = await resolve_plugin(raw)
    if plugin_name is None:
        await bot.send(f"未找到插件 [{raw}], {hint('查看插件')}")
        return

    detail = render_plugin_detail(plugin_name, SL.plugins[plugin_name])
    tip = f"\n\n提示: {p}设置插件 {plugin_name} <参数> <值>\n可用参数: {PLUGIN_PARAMS}"
    await bot.send(detail + tip)


@sv_core_config_cmd.on_command("设置插件", block=True)
async def set_plugin(bot: Bot, ev: Event):
    args = ev.text.strip().split(None, 2)
    if len(args) < 3:
        await bot.send(f"格式: {prefix()}设置插件 <插件名> <参数> <值>\n可用参数: {PLUGIN_PARAMS}")
        return

    raw_plugin, param, raw_value = args
    plugin_name = await resolve_plugin(raw_plugin)
    if plugin_name is None:
        await bot.send(f"未找到插件 [{raw_plugin}]")
        return

    await bot.send(
        apply_set(
            SL.plugins[plugin_name],
            f"插件 [{plugin_name}]",
            param,
            raw_value,
            PLUGIN_AREA,
            PLUGIN_PARAMS,
            PLUGIN_EXTRA,
        )
    )


# ── 命令 ─────────────────────────────────────────────────


@sv_core_config_cmd.on_command("查看命令", block=True)
async def show_sv(bot: Bot, ev: Event):
    raw = ev.text.strip()

    p = prefix()
    if not raw:
        msg = f"提示: {p}查看命令 <命令名>\n\n"
        await bot.send(msg + render_sv_list())
        return

    sv_name, sv = resolve_sv(raw)
    if sv is not None:
        detail = render_sv_detail(sv)
        tip = f"\n\n提示: {p}设置命令 {sv_name} <参数> <值>\n可用参数: {SV_PARAMS}"
        await bot.send(detail + tip)
        return

    # 尝试按插件名过滤
    plugin_name = await resolve_plugin(raw)
    if plugin_name and any(sv.self_plugin_name == plugin_name for sv in SL.lst.values()):
        msg = f"提示: {p}查看命令 <命令名> 查看详情\n\n"
        await bot.send(msg + render_sv_list(plugin_name))
        return

    await bot.send(f"未找到命令或插件 [{raw}], {hint('查看命令')}")


@sv_core_config_cmd.on_command("设置命令", block=True)
async def set_sv(bot: Bot, ev: Event):
    args = ev.text.strip().split(None, 2)
    if len(args) < 3:
        await bot.send(f"格式: {prefix()}设置命令 <命令名> <参数> <值>\n可用参数: {SV_PARAMS}")
        return

    sv_name, param, raw_value = args
    sv_name, sv = resolve_sv(sv_name)
    if sv is None:
        await bot.send(f"未找到命令 [{sv_name}]")
        return

    await bot.send(
        apply_set(
            sv,
            f"命令 [{sv_name}]",
            param,
            raw_value,
            SV_AREA,
            SV_PARAMS,
        )
    )
