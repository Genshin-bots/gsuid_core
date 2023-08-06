from copy import deepcopy
from typing import Dict, List, Tuple, Callable, Optional

from PIL import Image, ImageDraw, ImageFont

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.image.image_tools import (
    crop_center_img,
    easy_alpha_composite,
)

from .model import PluginHelp

cache: Dict[str, int] = {}


async def get_help(
    name: str,
    sub_text: str,
    help_data: Dict[str, PluginHelp],
    bg: Image.Image,
    icon: Image.Image,
    badge: Image.Image,
    banner: Image.Image,
    button: Image.Image,
    font: Callable[[int], ImageFont.FreeTypeFont],
    is_dark: bool = True,
    text_color: Tuple[int, int, int] = (250, 250, 250),
    sub_color: Optional[Tuple[int, int, int]] = None,
) -> bytes:
    help_path = get_res_path('help') / f'{name}.jpg'

    if help_path.exists() and name in cache and cache[name]:
        return await convert_img(Image.open(help_path))

    if sub_color is None and is_dark:
        sub_color = tuple(x - 50 for x in text_color if x > 50)
    elif sub_color is None and not is_dark:
        sub_color = tuple(x + 50 for x in text_color if x < 205)

    title = Image.new('RGBA', (900, 600))
    icon = icon.resize((300, 300))
    title.paste(icon, (300, 89), icon)
    title.paste(badge, (0, 390), badge)
    badge_s = badge.resize((720, 80))
    title.paste(badge_s, (90, 480), badge_s)
    title_draw = ImageDraw.Draw(title)

    title_draw.text((450, 440), f'{name} 帮助', text_color, font(36), 'mm')
    title_draw.text((450, 520), sub_text, sub_color, font(26), 'mm')

    w, h = 900, 630

    sv_img_list: List[Image.Image] = []
    for sv_name in help_data:
        tr_size = len(help_data[sv_name]['data'])
        y = 100 + ((tr_size + 3) // 4) * 80
        h += y
        sv_img = Image.new('RGBA', (900, y))
        sv_data = help_data[sv_name]['data']
        sv_desc = help_data[sv_name]['desc']

        bc = deepcopy(banner)
        bc_draw = ImageDraw.Draw(bc)
        bc_draw.text((30, 25), sv_name, text_color, font(35), 'lm')
        if hasattr(font, 'getsize'):
            size, _ = font(35).getsize(sv_name)
        else:
            bbox = font(35).getbbox(sv_name)
            size, _ = bbox[2] - bbox[0], bbox[3] - bbox[1]
        bc_draw.text((42 + size, 30), sv_desc, sub_color, font(20), 'lm')
        sv_img = easy_alpha_composite(sv_img, bc, (0, 10))
        # sv_img.paste(bc, (0, 10), bc)

        for index, tr in enumerate(sv_data):
            bt = deepcopy(button)
            bt_draw = ImageDraw.Draw(bt)
            if len(tr['name']) > 8:
                tr_name = tr['name'][:5] + '..'
            else:
                tr_name = tr['name']

            bt_draw.text((105, 28), tr_name, text_color, font(26), 'mm')
            bt_draw.text((105, 51), tr['eg'], sub_color, font(17), 'mm')
            offset_x = 210 * (index % 4)
            offset_y = 80 * (index // 4)
            sv_img = easy_alpha_composite(
                sv_img, bt, (26 + offset_x, 83 + offset_y)
            )
            # sv_img.paste(bt, (26 + offset_x, 83 + offset_y), bt)

        sv_img_list.append(sv_img)

    img = crop_center_img(bg, w, h)
    img.paste(title, (0, 0), title)
    temp = 0
    for _sm in sv_img_list:
        img.paste(_sm, (0, 600 + temp), _sm)
        temp += _sm.size[1]

    img = img.convert('RGB')
    help_path = get_res_path('help') / f'{name}.jpg'
    img.save(
        help_path,
        'JPEG',
        quality=85,
        subsampling=0,
    )
    cache[name] = 1
    return await convert_img(img)
