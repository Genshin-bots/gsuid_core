"""`core更新记录` → 把 changelogs/ 目录的版本记录渲染成竖屏卡片。

不带参数看当前版本（提交不超过 6 个时再带上一版），带版本号看指定版，带「列表」看版本索引。
"""

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.utils.html_render import render_html_to_bytes

from .authors import attach_authors
from .template import (
    DEVICE_SCALE,
    LAYOUT_WIDTH,
    build_empty_html,
    build_index_html,
    build_recent_html,
    build_version_html,
)
from .changelog import (
    VersionRef,
    ChangelogVersion,
    is_current,
    pick_recent,
    list_versions,
    parse_version,
    resolve_query,
)

# priority=1 必须低于 Core管理(5)：`on_command("更新")` 是 startswith 匹配，
# `core更新记录` 会同时命中它，靠优先级先跑本命令再 block 掉。
sv_core_update_history = SV("Core更新记录", pm=0, priority=1)

# 索引触发词：`目录`/`index`/`idx` 这些是用户会顺手敲的说法，别只认「列表」。
_LIST_WORDS = ("列表", "目录", "全部", "索引", "list", "index", "idx", "menu")
_CMD = "core更新记录"
_MISSING_TITLE = "没有找到更新记录"
_MISSING_TEXT = (
    "当前部署里找不到 `changelogs/` 目录。\npip 安装的 gsuid-core 不带仓库文档，需要从源码运行 core 或挂载仓库目录。"
)


async def _render(html: str) -> bytes:
    """逻辑宽 480px 按 2 倍设备像素出图，正文行长控制在 30 字上下。"""
    return await render_html_to_bytes(
        html,
        max_width=float(LAYOUT_WIDTH * DEVICE_SCALE),
        dpi=96.0 * float(DEVICE_SCALE),
        default_font_size=0.0,
        image_format="png",
        root_max_width=float(LAYOUT_WIDTH),
    )


async def _send_empty(bot: Bot) -> None:
    await bot.send(await _render(build_empty_html(_MISSING_TITLE, _MISSING_TEXT)))


def _recent_versions(refs: tuple[VersionRef, ...]) -> tuple[ChangelogVersion, ...]:
    """默认那张图要画的版本：当前这段；提交不超过 6 个时再带上一版。"""
    return tuple(parse_version(ref) for ref in pick_recent(refs))


# 别名走元组直接注册：`on_command` 接受 `Union[str, Tuple[str, ...]]`。
# 「纪录」是常见误写，「更新目录」是口语说法，都收进来省得用户敲错。
_COMMANDS = ("更新记录", "更新纪录", "更新日志", "更新目录", "版本记录")


@sv_core_update_history.on_command(_COMMANDS, block=True)
async def send_update_history(bot: Bot, ev: Event):
    refs = list_versions()
    if not refs:
        await _send_empty(bot)
        return

    arg = ev.text.strip() if ev.text else ""
    if arg.lower() in _LIST_WORDS:
        await bot.send(await _render(build_index_html(refs, total=len(refs))))
        return

    if not arg:
        versions = await attach_authors(_recent_versions(refs))
        await bot.send(await _render(build_recent_html(versions)))
        return

    ref = resolve_query(arg, refs)
    if ref is None:
        await bot.send(
            await bot.t(
                "没有找到版本 [{raw}]，发送 {cmd}列表 可以看全部版本。",
                raw=arg,
                cmd=_CMD,
            )
        )
        return

    version = (await attach_authors((parse_version(ref),)))[0]
    await bot.send(await _render(build_version_html(version, is_current=is_current(ref))))
