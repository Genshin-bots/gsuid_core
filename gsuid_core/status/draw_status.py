import asyncio
import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Union, Optional

from PIL import Image, ImageOps, ImageDraw

import gsuid_core.global_val as gv
from gsuid_core.models import Event
from gsuid_core.version import __version__
from gsuid_core.help.draw_core_help import ICON
from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.database.models import CoreUser, CoreGroup
from gsuid_core.utils.plugins_config.gs_config import status_config
from gsuid_core.utils.image.convert import convert_img, number_to_chinese
from gsuid_core.utils.database.global_val_models import (
    CoreDataSummary,
    CoreDataAnalysis,
)
from gsuid_core.utils.image.image_tools import (
    add_footer,
    get_font_x,
    crop_center_img,
    draw_pic_with_ring,
)

from .utils import generate_y_ticks
from .plugin_status import plugins_status
from .get_hw import get_cpu_info, get_disk_info, get_swap_info, get_memory_info

TEXT_PATH = Path(__file__).parent / 'texture2d'

THEME_COLOR: str = status_config.get_config('CustomTheme').data
# THEME_COLOR = (94, 79, 171)
HINT_COLOR = (235, 54, 54)
BLACK = (24, 24, 24)
GREY = (101, 101, 101)
SE_COLOR = (71, 71, 71)


async def draw_title():
    title = Image.new('RGBA', (1400, 300))
    title_draw = ImageDraw.Draw(title)

    icon_path = ICON
    if status_config.get_config('CustomIcon').data:
        _icon_path = Path(status_config.get_config('CustomIconPath').data)
        if _icon_path.exists():
            icon_path = _icon_path

    icon = Image.open(icon_path)
    if icon.mode == 'RGB':
        icon = await draw_pic_with_ring(icon, 186, (189, 72, 37))
    else:
        icon = icon.resize((186, 186))

    title.paste(icon, (92, 77), icon)

    MAIN_TITLE: str = status_config.get_config('CustomName').data
    S_TITLE: str = status_config.get_config('CustomSubtitle').data

    all_group = await CoreGroup.get_all_group()
    all_user = await CoreUser.get_all_user_list()

    all_group_num = len(all_group) if all_group else 0
    all_user_num = len(all_user) if all_user else 0

    x = get_font_x(core_font(40), MAIN_TITLE)
    title_draw.text(
        (330, 147),
        MAIN_TITLE,
        BLACK,
        core_font(40),
        'lm',
    )
    tag_w, tag_h = 111, 47
    title_draw.rounded_rectangle(
        (340 + x, 121, 340 + x + tag_w, 121 + tag_h),
        12,
        HINT_COLOR,
    )
    title_draw.text(
        (340 + x + 55, 145),
        f'v{__version__}',
        'White',
        core_font(32),
        'mm',
    )
    title_draw.text(
        (330, 193),
        S_TITLE,
        GREY,
        core_font(30),
        'lm',
    )

    msg_w, msg_h = 156, 32
    title_draw.rounded_rectangle(
        (905, 180, 905 + msg_w, 180 + msg_h),
        10,
        THEME_COLOR,
    )
    title_draw.rounded_rectangle(
        (1126, 180, 1126 + msg_w, 180 + msg_h),
        10,
        THEME_COLOR,
    )
    title_draw.text(
        (983, 196),
        '已服务群聊',
        'White',
        core_font(24),
        'mm',
    )
    title_draw.text(
        (1204, 196),
        '已服务用户',
        'White',
        core_font(24),
        'mm',
    )

    title_draw.text(
        (983, 147),
        number_to_chinese(all_group_num),
        BLACK,
        core_font(60),
        'mm',
    )
    title_draw.text(
        (1204, 147),
        number_to_chinese(all_user_num),
        BLACK,
        core_font(60),
        'mm',
    )

    return title


async def draw_bar(
    text1: str,
    text2: str,
    sample: Optional[Dict[str, Union[Tuple[int, int, int], str]]] = None,
):

    bar = Image.new('RGBA', (1400, 100))
    bar_draw = ImageDraw.Draw(bar)

    bar_draw.rounded_rectangle(
        (116, 74, 1307, 79),
        12,
        THEME_COLOR,
    )

    bar_draw.text(
        (113, 48),
        text1,
        BLACK,
        core_font(40),
        'lm',
    )
    x = get_font_x(core_font(40), text1)
    bar_draw.text(
        (122 + x, 54),
        text2,
        GREY,
        core_font(32),
        'lm',
    )

    if sample:
        for index, key in enumerate(sample):
            color = sample[key]
            x1, y1 = 1150 - index * 175, 30
            x2, y2 = x1 + 60, y1 + 30
            bar_draw.rounded_rectangle(
                (x1, y1, x2, y2),
                12,
                color,
            )
            bar_draw.text(
                (x2 + 8, y1 + 15),
                key,
                BLACK,
                core_font(24),
                'lm',
            )

    return bar


async def draw_badge(
    title: str,
    value: Union[str, int, float],
    avg_value: Optional[int] = None,
    color: Union[Tuple[int, int, int], str] = THEME_COLOR,
):
    badge = Image.new('RGBA', (240, 150))
    badge_draw = ImageDraw.Draw(badge)

    badge_draw.rounded_rectangle(
        (42, 93, 198, 125),
        10,
        color,
    )

    badge_draw.text(
        (120, 109),
        title,
        'White',
        core_font(24),
        'mm',
    )

    if isinstance(value, int) or isinstance(value, float) or value.isdigit():
        value_str = number_to_chinese(float(value))
    else:
        value_str = value

    if avg_value is not None and (
        isinstance(value, int) or isinstance(value, float)
    ):
        if value >= avg_value * 1.2:
            arrow = Image.open(TEXT_PATH / 'up.png')
            x = get_font_x(core_font(46), value_str)
            point = (107, 51)
            badge.paste(
                arrow,
                (110 + x // 2, 26),
                arrow,
            )
        elif value <= avg_value * 0.8:
            arrow = Image.open(TEXT_PATH / 'down.png')
            x = get_font_x(core_font(46), value_str)
            point = (107, 51)
            badge.paste(
                arrow,
                (110 + x // 2, 26),
                arrow,
            )
        else:
            point = (120, 51)
    else:
        point = (120, 51)

    badge_draw.text(
        point,
        value_str,
        BLACK,
        core_font(46),
        'mm',
    )
    return badge


async def draw_data_analysis1(
    bot_id: Optional[str],
    bot_self_id: Optional[str],
):
    local_val = gv.get_platform_val(bot_id, bot_self_id)
    data_bar = Image.new('RGBA', (1400, 200))

    yesterday: Optional[CoreDataSummary] = (
        await CoreDataSummary.get_yesterday_data(
            bot_id=bot_id,
            bot_self_id=bot_self_id,
        )
    )
    if not yesterday:
        yesterday = CoreDataSummary(
            bot_id='1', bot_self_id='2', date=datetime.datetime.now()
        )

    badge1 = await draw_badge(
        '今日接收',
        local_val['receive'],
        yesterday.receive,
        SE_COLOR,
    )

    badge2 = await draw_badge(
        '今日发送',
        local_val['send'],
        yesterday.send,
        HINT_COLOR,
    )
    badge3 = await draw_badge(
        '绘制图片',
        local_val['image'],
        yesterday.image,
    )
    badge4 = await draw_badge(
        '触发命令',
        local_val['command'],
        yesterday.command,
    )
    badge5 = await draw_badge(
        '使用群聊',
        local_val['group_count'],
        yesterday.group_count,
        SE_COLOR,
    )
    badge6 = await draw_badge(
        '使用用户',
        local_val['user_count'],
        yesterday.user_count,
        SE_COLOR,
    )

    for index, i in enumerate(
        [badge1, badge2, badge3, badge4, badge5, badge6]
    ):
        data_bar.paste(i, (75 + index * 210, 25), i)

    return data_bar


async def draw_data_analysis2(
    data: Dict,
):
    badge1 = await draw_badge(
        'DAU',
        data['DAU'],
        0,
        HINT_COLOR,
    )
    badge2 = await draw_badge(
        'DAG',
        data['DAG'],
    )
    badge3 = await draw_badge(
        '用户新增',
        data['NU'],
    )
    badge4 = await draw_badge(
        '用户留存',
        data['OU'],
        0,
        HINT_COLOR,
    )
    badge5 = await draw_badge(
        '群聊新增',
        data['NG'],
    )
    badge6 = await draw_badge(
        '群聊留存',
        data['OG'],
        0,
    )

    data_bar = Image.new('RGBA', (1400, 200))
    for index, i in enumerate(
        [badge1, badge2, badge3, badge4, badge5, badge6]
    ):
        data_bar.paste(i, (75 + index * 210, 25), i)

    return data_bar


def draw_ring(value: float):
    img = Image.new('RGBA', (100, 100))
    resin_percent = value / 100
    ring_pic = Image.open(TEXT_PATH / 'ring.webp')
    percent = (
        round(resin_percent * 49) if round(resin_percent * 49) <= 49 else 49
    )
    ring_pic.seek(percent)
    img.paste(ring_pic, (0, 0), ring_pic)
    img_draw = ImageDraw.Draw(img)
    img_draw.text(
        (50, 50),
        f'{int(value)}',
        GREY,
        core_font(27),
        'mm',
    )
    return img


def draw_hw_status_bar(title: str, value: float, msg: str):
    img = Image.new('RGBA', (740, 100))
    img_draw = ImageDraw.Draw(img)
    ring = draw_ring(value)
    img.paste(ring, (77, 0), ring)

    img_draw.rounded_rectangle((175, 27, 266, 74), 12, THEME_COLOR)
    img_draw.text(
        (220, 50),
        title,
        'White',
        core_font(32),
        'mm',
    )
    img_draw.text(
        (280, 50),
        msg,
        GREY,
        core_font(32),
        'lm',
    )
    return img


async def draw_hw():
    img = Image.new('RGBA', (1400, 300))

    cpu_task = asyncio.create_task(get_cpu_info())
    memory_task = asyncio.create_task(get_memory_info())
    disk_task = asyncio.create_task(get_disk_info())
    swap_task = asyncio.create_task(get_swap_info())

    cpu, memory, disk, swap = await asyncio.gather(
        cpu_task, memory_task, disk_task, swap_task
    )

    cpu_img = draw_hw_status_bar('CPU', cpu['value'], cpu['name'])
    memory_img = draw_hw_status_bar('内存', memory['value'], memory['name'])
    disk_img = draw_hw_status_bar('磁盘', disk['value'], disk['name'])
    swap_img = draw_hw_status_bar('交换', swap['value'], swap['name'])

    for index, i in enumerate([cpu_img, memory_img, disk_img, swap_img]):
        img.paste(
            i,
            (20 + (index % 2) * 670, 50 + (index // 2) * 100),
            i,
        )

    return img


async def draw_plugins_status():
    plugins_num = len(plugins_status)
    plugins_h = 50 + plugins_num * 180

    img = Image.new('RGBA', (1400, plugins_h))
    img_draw = ImageDraw.Draw(img)

    if plugins_num == 0:
        img_draw.text(
            (700, 25),
            '当前没有插件有额外信息',
            GREY,
            core_font(32),
            'mm',
        )
    else:
        for index, i in enumerate(plugins_status):
            plugin_bar = Image.new('RGBA', (1400, 180))
            plugin_bar_draw = ImageDraw.Draw(plugin_bar)

            plugin_bar_draw.rounded_rectangle(
                (115, 75, 540, 133),
                30,
                THEME_COLOR,
            )

            plugin = plugins_status[i]
            icon = plugin['icon']
            icon = icon.resize((128, 128))
            status = plugin['status']

            plugin_bar.paste(icon, (109, 30), icon)
            plugin_bar_draw.text(
                (251, 104),
                i,
                'White',
                core_font(26),
                'lm',
            )

            for indexj, j in enumerate(status):
                badge = await draw_badge(j, await status[j]())
                plugin_bar.paste(
                    badge,
                    (605 + 210 * indexj, 11),
                    badge,
                )
                if indexj >= 2:
                    break

            img.paste(
                plugin_bar,
                (0, 25 + 180 * index),
                plugin_bar,
            )

    return img


async def draw_curve(
    datas: Dict[Union[Tuple[int, int, int], str], List[float]],
):
    img = Image.new('RGBA', (1400, 550))
    img_draw = ImageDraw.Draw(img)

    num = 30
    rad = 5
    a_y = 375
    a_x = 1200
    step_x = a_x / num
    start_x = 147

    is_text = False

    for color in datas:
        data = datas[color][:num]
        y_ticks = generate_y_ticks(data)
        y_ticks = [int(i) for i in y_ticks]

        if not is_text:
            for yindex, y in enumerate(y_ticks):
                img_draw.text(
                    (116, 460 - 75 * yindex),
                    str(number_to_chinese(y)),
                    BLACK,
                    core_font(30),
                    'rm',
                )
                is_text = True

        points = []
        for dataindex, data in enumerate(data):
            x1 = int(start_x + dataindex * step_x)
            if y_ticks[-1]:
                d_y = (data / y_ticks[-1]) * a_y
            else:
                d_y = 0
            y1 = 460 - d_y

            points.append((x1, y1))

        img_draw.line(points, color, 4)

        for p in points:
            img_draw.ellipse(
                (
                    p[0] - rad,
                    p[1] - rad,
                    p[0] + rad,
                    p[1] + rad,
                ),
                THEME_COLOR,
            )

    return img


async def draw_curve_img(trends: Dict[str, List[int]]):
    result: Dict[Union[Tuple[int, int, int], str], List[float]] = {
        THEME_COLOR: [],
        HINT_COLOR: [],
        (182, 122, 210): [],
        (27, 146, 210): [],
    }

    for day in range(30):
        result[THEME_COLOR].append(
            trends['all_bots_user_count'][day]
            if day < len(trends['all_bots_user_count'])
            else 0
        )
        result[HINT_COLOR].append(
            trends['all_bots_send'][day]
            if day < len(trends['all_bots_send'])
            else 0
        )

        result[(182, 122, 210)].append(
            trends['bot_user_count'][day]
            if day < len(trends['bot_user_count'])
            else 0
        )
        result[(27, 146, 210)].append(
            trends['bot_send'][day] if day < len(trends['bot_send']) else 0
        )

    curve_img = await draw_curve(result)
    return curve_img


async def draw_bg(w: int, h: int):
    path = TEXT_PATH / 'bg.jpg'
    if status_config.get_config('CustomBg').data:
        bg_path = Path(status_config.get_config('CustomBgPath').data)
        if bg_path.exists():
            path = bg_path

    bg = Image.open(path).convert('RGBA')
    bg = crop_center_img(bg, w, h)

    mask = Image.open(TEXT_PATH / 'mask.png')
    line = Image.open(TEXT_PATH / 'line.png')

    fg_temp = Image.new('RGBA', (w, h))
    fg_temp.paste(mask, (0, 222), mask)

    r, g, b, a = fg_temp.split()
    a_inv = ImageOps.invert(a)
    fg_temp = Image.merge("RGBA", (r, g, b, a_inv))
    _fg = Image.new('RGBA', (w, h))
    fg = crop_center_img(Image.open(TEXT_PATH / 'fg.png'), w, h)

    _fg.paste(fg, (0, 0), fg_temp)

    bg = Image.alpha_composite(bg, _fg)
    bg.paste(line, (0, 222), line)
    return bg


async def draw_status(ev: Event):
    title = await draw_title()
    bar1 = await draw_bar('服务器基础信息', 'Base Info')
    bar2_1 = await draw_bar('机器人数据统计(单)', 'Data Analysis')
    bar2_2 = await draw_bar('机器人数据统计(多)', 'Data Analysis')
    bar3 = await draw_bar(
        '日活曲线',
        'Daily Activity',
        {
            '全用户': THEME_COLOR,
            '全发送': HINT_COLOR,
            '使用用户': (182, 122, 210),
            '发送数量': (27, 146, 210),
        },
    )
    bar4 = await draw_bar('插件额外信息', 'Extra Data')

    mdata = await CoreDataAnalysis.calculate_dashboard_metrics()
    ndata = await CoreDataAnalysis.calculate_dashboard_metrics(
        ev.real_bot_id,
        ev.bot_self_id,
    )

    hw = await draw_hw()

    data_bar1_1 = await draw_data_analysis1(
        ev.real_bot_id,
        ev.bot_self_id,
    )
    data_bar1_2 = await draw_data_analysis1(
        None,
        None,
    )

    data_bar2_1 = await draw_data_analysis2(ndata)
    data_bar2_2 = await draw_data_analysis2(mdata)
    trends = await CoreDataSummary.get_day_trends(
        ev.real_bot_id,
        ev.bot_self_id,
    )

    plugin_status_img = await draw_plugins_status()

    curve_img = await draw_curve_img(trends)

    plugins_num = len(plugins_status)
    plugins_h = 100 + plugins_num * 180

    img = await draw_bg(1400, 2778 + 150 + plugins_h)

    img.paste(title, (0, 0), title)
    img.paste(bar1, (0, 855), bar1)
    img.paste(hw, (0, 920), hw)
    img.paste(bar2_1, (0, 1202), bar2_1)
    img.paste(data_bar1_1, (0, 1289), data_bar1_1)
    img.paste(data_bar2_1, (0, 1463), data_bar2_1)

    img.paste(bar2_2, (0, 1686), bar2_2)
    img.paste(data_bar1_2, (0, 1773), data_bar1_2)
    img.paste(data_bar2_2, (0, 1974), data_bar2_2)

    img.paste(bar3, (0, 2197), bar3)
    img.paste(curve_img, (0, 2266), curve_img)
    img.paste(bar4, (0, 2778), bar4)
    img.paste(plugin_status_img, (0, 2878), plugin_status_img)

    img = add_footer(img, footer=Image.open(TEXT_PATH / 'footer.png'))
    res = await convert_img(img)
    return res
