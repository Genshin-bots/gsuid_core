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
    enable_cache: bool = True,
):
    help_path = get_res_path('help') / f'{plugin_name}.jpg'

    if (
        help_path.exists()
        and plugin_name in cache
        and cache[plugin_name]
        and enable_cache
    ):
        return await convert_img(Image.open(help_path))

    if banner_bg is None:
        banner_bg = Image.open(TEXT_PATH / 'banner_bg.jpg')
    if help_bg is None:
        help_bg = Image.open(TEXT_PATH / 'help_bg.jpg')
    if cag_bg is None:
        cag_bg = Image.open(TEXT_PATH / 'cag_bg.png')
    if footer is None:
        footer = Image.open(TEXT_PATH / f'footer_{help_mode}.png')
    if item_bg is None:
        item_bg = Image.open(TEXT_PATH / f'item_{help_mode}.png')

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

    plugin_icon = plugin_icon.resize((128, 128))

    # 准备计算整体帮助图大小
    w, h = 1545, 300 + footer.height

    cag_num = len(plugin_help)
    h += cag_num * 100
    for cag in plugin_help:
        cag_data = plugin_help[cag]['data']
        sv_num = len(cag_data)
        h += (((sv_num - 1) // 3) + 1) * 175

    # 基准图
    img = crop_center_img(help_bg, w, h)

    # 绘制banner
    banner_bg.paste(plugin_icon, (89, 88), plugin_icon)
    banner_draw = ImageDraw.Draw(banner_bg)

    _banner_name = plugin_name + '帮助'
    banner_draw.text(
        (262, 128),
        _banner_name,
        main_color,
        font=core_font(50),
        anchor='lm',
    )
    banner_draw.text(
        (262, 183),
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
            (262 + plugin_name_len + 10, 128 - badge.height // 2),
            badge,
        )

        plugin_name_len += badge.width + 10

    img.paste(banner_bg, (0, 0), banner_bg)

    # 开始粘贴服务
    hs = 0
    for cag in plugin_help:
        sv = plugin_help[cag]
        cag_bar = deepcopy(cag_bg)
        cag_desc = sv['desc']
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
        img.paste(cag_bar, (0, 280 + hs), cag_bar)

        for i, command in enumerate(cag_data):
            command_name = command['name']
            # command_desc = command['desc']
            command_eg = command['eg']
            command_bg = deepcopy(item_bg)

            icon = find_icon(command_name, icon_path)
            command_bg.paste(icon, (6, 12), icon)

            command_draw = ImageDraw.Draw(command_bg)

            command_draw.text(
                (160, 67),
                plugin_prefix + command_name,
                main_color,
                font=core_font(40),
                anchor='lm',
            )

            command_draw.text(
                (160, 116),
                plugin_prefix + command_eg,
                sub_color,
                font=core_font(26),
                anchor='lm',
            )

            x, y = 45 + (i % 3) * 490, 370 + (i // 3) * 175 + hs
            img.paste(command_bg, (x, y), command_bg)

        hs += (((len(cag_data) - 1) // 3) + 1) * 175 + 100

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
