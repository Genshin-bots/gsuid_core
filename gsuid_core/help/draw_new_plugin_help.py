import re
import random
from pathlib import Path
from copy import deepcopy
from typing import Dict, Literal, Optional

from PIL import Image, ImageDraw

from gsuid_core.help.model import PluginHelp
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.plugins_config.gs_config import pic_gen_config
from gsuid_core.utils.image.image_tools import (
    CustomizeImage,
    tint_image,
    crop_center_img,
    draw_color_badge,
)

cache: Dict[str, int] = {}
ICON_PATH = Path(__file__).parent / 'new_icon'
TEXT_PATH = Path(__file__).parent / 'texture2d'
pic_quality: int = pic_gen_config.get_config('PicQuality').data


def find_icon(name: str, icon_path: Path = ICON_PATH):
    for icon in icon_path.glob('*.png'):
        if icon.stem == name:
            _r = icon
            break
    else:
        for icon in icon_path.glob('*.png'):
            if icon.stem in name:
                _r = icon
                break
        else:
            if (icon_path / '通用.png').exists():
                _r = icon_path / '通用.png'
            else:
                _r = random.choice(list(icon_path.iterdir()))
    return Image.open(_r)


def calculate_string_length(s: str):
    total_length = 0
    result = ''
    for char in s:
        result += char
        if '\u4e00' <= char <= '\u9fff':
            total_length += 1
        elif re.match(r'[A-Za-z0-9]', char):
            total_length += 0.5
        elif re.match(r'[^\w\s]', char):
            total_length += 0.3

        if total_length >= 8:
            return result
    else:
        return result


async def get_new_help(
    plugin_name: str,
    plugin_info: Dict[str, str],
    plugin_icon: Image.Image,
    plugin_help: Dict[str, PluginHelp],
    plugin_prefix: str = '',
    help_mode: Literal['dark', 'light'] = 'dark',
    banner_bg: Optional[Image.Image] = None,
    banner_sub_text: str = '💖且听风吟。',
    help_bg: Optional[Image.Image] = None,
    cag_bg: Optional[Image.Image] = None,
    item_bg: Optional[Image.Image] = None,
    icon_path: Path = ICON_PATH,
    footer: Optional[Image.Image] = None,
    column: int = 3,
    need_cover: bool = False,
    enable_cache: bool = True,
    pm: int = 6,
    highlight_bg: Optional[Image.Image] = None,
):
    help_path = get_res_path('help') / f'{plugin_name}_{pm}.jpg'

    if (
        help_path.exists()
        and plugin_name in cache
        and cache[plugin_name]
        and enable_cache
    ):
        return await convert_img(Image.open(help_path))

    if banner_bg is None:
        banner_bg = Image.open(TEXT_PATH / f'banner_bg_{help_mode}.jpg')
    if help_bg is None:
        help_bg = Image.open(TEXT_PATH / f'bg_{help_mode}.jpg')
    if cag_bg is None:
        cag_bg = Image.open(TEXT_PATH / f'cag_bg_{help_mode}.png')
    if footer is None:
        footer = Image.open(TEXT_PATH / f'footer_{help_mode}.png')
    if item_bg is None:
        item_bg = Image.open(TEXT_PATH / f'item_{help_mode}.png')
    if highlight_bg is None:
        highlight_bg = Image.open(TEXT_PATH / 'highlight.png')

    if help_mode == 'dark':
        main_color = (255, 255, 255)
        sub_color = (206, 206, 206)
    else:
        main_color = (0, 0, 0)
        sub_color = (102, 102, 102)

    banner_bg = banner_bg.convert('RGBA')
    help_bg = help_bg.convert('RGBA')
    cag_bg = cag_bg.convert('RGBA')
    item_bg = item_bg.convert('RGBA')
    footer = footer.convert('RGBA')
    highlight_bg = highlight_bg.convert('RGBA')

    plugin_icon = plugin_icon.resize((128, 128))

    # 准备计算整体帮助图大小
    w, h = 120 + 475 * column, footer.height

    cag_num = 0
    for cag in plugin_help:
        cag_data = plugin_help[cag]['data']
        sv = plugin_help[cag]
        if 'pm' in sv and isinstance(sv['pm'], int) and pm > sv['pm']:
            continue

        cag_num += 1
        sv_num = len(cag_data)
        h += (((sv_num - 1) // column) + 1) * 175

    banner_h = banner_bg.size[1]
    # 绘制banner
    banner_bg.paste(plugin_icon, (89, banner_h - 212), plugin_icon)
    banner_draw: ImageDraw.ImageDraw = ImageDraw.Draw(banner_bg)

    _banner_name = plugin_name + '帮助'
    banner_draw.text(
        (262, banner_h - 172),
        _banner_name,
        main_color,
        font=core_font(50),
        anchor='lm',
    )
    banner_draw.text(
        (262, banner_h - 117),
        banner_sub_text,
        sub_color,
        font=core_font(30),
        anchor='lm',
    )
    x1, y1, x2, y2 = core_font(50).getbbox(_banner_name)
    plugin_name_len = int(x2 - x1)

    for key, value in plugin_info.items():
        if value == 'any' or not value:
            value = (252, 69, 69)
        badge = draw_color_badge(
            key,
            value,
            core_font(30),
            (255, 255, 255),
        )
        banner_bg.paste(
            badge,
            (
                262 + plugin_name_len + 10,
                banner_h - 172 - badge.height // 2,
            ),
            badge,
        )

        plugin_name_len += badge.width + 10

    bscale = w / banner_bg.size[0]
    new_banner_h = int(banner_h * bscale)
    new_cag_h = int(cag_bg.size[1] * bscale)
    banner_bg = banner_bg.resize((w, new_banner_h))

    h += new_banner_h
    soft = 10
    h += cag_num * (new_cag_h + soft)

    # 基准图
    img = crop_center_img(help_bg, w, h)
    if need_cover:
        color = CustomizeImage.get_bg_color(
            img, False if help_mode == 'dark' else True
        )
        if help_mode == 'light':
            add_color = 40
            max_color = 255
            c0 = color[0] + add_color
            c0 = c0 if c0 < max_color else max_color
            c1 = color[1] + add_color
            c2 = color[2] + add_color
            c1 = c1 if c1 < max_color else max_color
            c2 = c2 if c2 < max_color else max_color
        else:
            add_color = -40
            max_color = 0
            c0 = color[0] + add_color
            c0 = c0 if c0 > max_color else max_color
            c1 = color[1] + add_color
            c2 = color[2] + add_color
            c1 = c1 if c1 > max_color else max_color
            c2 = c2 if c2 > max_color else max_color
        _color = (c0, c1, c2, 190)
        _color_img = Image.new(
            'RGBA',
            (w, h),
            _color,
        )
        img.paste(_color_img, (0, 0), _color_img)

    img.paste(banner_bg, (0, 0), banner_bg)

    # 开始粘贴服务
    hs = 0
    for cag in plugin_help:
        sv = plugin_help[cag]
        cag_bar = deepcopy(cag_bg)
        cag_desc = sv['desc']
        if 'pm' in sv and isinstance(sv['pm'], int) and pm > sv['pm']:
            continue

        cag_data = sv['data']
        cag_draw = ImageDraw.Draw(cag_bar)

        cag_draw.text(
            (136, 50),
            cag,
            main_color,
            font=core_font(45),
            anchor='lm',
        )
        bbox = core_font(45).getbbox(cag)
        cag_name_len = int(bbox[2] - bbox[0])

        cag_draw.text(
            (136 + cag_name_len + 15, 55),
            cag_desc,
            sub_color,
            font=core_font(30),
            anchor='lm',
        )

        cag_bar = cag_bar.resize((w, new_cag_h))

        img.paste(
            cag_bar,
            (0, int(new_banner_h + hs - 14 * bscale)),
            cag_bar,
        )

        for i, command in enumerate(cag_data):
            command_name = command['name']
            # command_desc = command['desc']
            command_eg = command['eg']
            command_bg = deepcopy(item_bg)
            if 'highlight' in command:
                highlight = command['highlight']
            else:
                highlight = 6

            if highlight == 0:
                hbg = tint_image(highlight_bg, (211, 67, 59))
            elif highlight == 1:
                hbg = tint_image(highlight_bg, (230, 126, 34))
            elif highlight == 2:
                hbg = tint_image(highlight_bg, (46, 204, 113))
            elif highlight == 3:
                hbg = tint_image(highlight_bg, (52, 152, 219))
            elif highlight == 4:
                hbg = tint_image(highlight_bg, (213, 129, 219))
            elif highlight == 5:
                hbg = tint_image(highlight_bg, (219, 215, 219))
            else:
                hbg = None

            if hbg:
                command_bg.paste(hbg, (0, 0), hbg)

            if 'icon' in command:
                if isinstance(command['icon'], Image.Image):
                    icon: Image.Image = command['icon']
                else:
                    icon = Image.open(command['icon'])
            else:
                icon = find_icon(command_name, icon_path)

            if icon.width > 200:
                icon = icon.resize((128, 128))
                _icon = Image.new('RGBA', (150, 150))
                _icon.paste(icon, (11, 11), icon)
                icon = _icon
            else:
                icon = icon.resize((150, 150))
            command_bg.paste(icon, (6, 12), icon)

            command_draw = ImageDraw.Draw(command_bg)

            _command_name = calculate_string_length(command_name)

            command_draw.text(
                (156, 67),
                _command_name,
                main_color,
                font=core_font(38),
                anchor='lm',
            )

            if cag == '插件帮助一览':
                eg = command_eg
            else:
                eg = plugin_prefix + command_eg

            command_draw.text(
                (156, 116),
                eg,
                sub_color,
                font=core_font(26),
                anchor='lm',
            )

            x, y = (
                45 + (i % column) * 490,
                int(
                    new_banner_h
                    + 70 * bscale
                    + (i // column) * 175
                    + hs
                    + soft
                ),
            )
            img.paste(command_bg, (x, y), command_bg)

        hs += (((len(cag_data) - 1) // column) + 1) * 175 + new_cag_h + soft

    img.paste(
        footer,
        ((w - footer.width) // 2, h - footer.height - 20),
        footer,
    )

    img = img.convert('RGB')
    img.save(
        help_path,
        'JPEG',
        quality=pic_quality,
        subsampling=0,
    )
    cache[plugin_name] = 1

    return await convert_img(img)
