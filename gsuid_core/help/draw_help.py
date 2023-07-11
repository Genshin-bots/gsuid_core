from pathlib import Path
from typing import Dict, List, Tuple, Union, Optional

from PIL import Image, ImageDraw

from gsuid_core.sv import SV

# from gsuid_core.utils.image.image_tools import get_color_bg
from gsuid_core.utils.fonts.fonts import core_font

TEXT_PATH = Path(__file__).parent / 'texture2d'
CORE_HELP_IMG = Path(__file__).parent / 'core_help.jpg'

plugin_title = 92
sv_title = 67

tag_color = {
    'prefix': (137, 228, 124),
    'suffix': (124, 180, 228),
    'file': (190, 228, 217),
    'keyword': (217, 228, 254),
    'fullmatch': (228, 124, 124),
    'regex': (225, 228, 124),
    'command': (228, 124, 124),
    'other': (228, 190, 191),
}

tag_text: Dict[str, str] = {
    'prefix': '前缀',
    'suffix': '后缀',
    'file': '文件',
    'keyword': '包含',
    'fullmatch': '完全',
    'regex': '正则',
    'command': '命令',
    'other': '其他',
}

tags: Dict[str, Optional[Image.Image]] = {
    'prefix': None,
    'suffix': None,
    'file': None,
    'keyword': None,
    'fullmatch': None,
    'regex': None,
    'command': None,
    'other': None,
}


def get_tag(tag_type: str) -> Image.Image:
    cache = tags[tag_type]
    if cache is not None:
        return cache
    text = tag_text[tag_type]
    tag = Image.new('RGBA', (60, 40))
    tag_draw = ImageDraw.Draw(tag)
    tag_draw.rounded_rectangle((7, 5, 53, 35), 10, tag_color[tag_type])
    tag_draw.text((30, 20), text, (62, 62, 62), core_font(22), 'mm')
    tags[tag_type] = tag
    return tag


def get_command_bg(command: str, tag_type: str):
    img = Image.new('RGBA', (220, 40))
    img_draw = ImageDraw.Draw(img)
    img_draw.rounded_rectangle((6, 5, 160, 35), 10, (230, 202, 167))
    img_draw.text((83, 20), command, (62, 62, 62), core_font(20), 'mm')
    tag = get_tag(tag_type)
    img.paste(tag, (160, 0), tag)
    return img


def _c(data: Union[int, str, bool]) -> Tuple[int, int, int]:
    gray_color = (184, 184, 184)

    if isinstance(data, bool):
        color = tag_color['prefix'] if data else gray_color
    elif isinstance(data, str):
        color = (
            tag_color['prefix']
            if data == 'ALL'
            else tag_color['command']
            if data == 'GROUP'
            else tag_color['file']
        )
    else:
        colors = list(tag_color.values())
        if data <= len(colors) and data >= 0:
            color = colors[data]
        else:
            color = tag_color['other']
    return color


def _t(data: Union[int, str, bool]) -> str:
    if isinstance(data, bool):
        text = '开启' if data else '关闭'
    elif isinstance(data, str):
        text = '不限' if data == 'ALL' else '群聊' if data == 'GROUP' else '私聊'
    else:
        texts = ['主人', '超管', '群主', '管理', '频管', '子管', '正常', '低', '黑']
        if data <= len(texts) and data >= 0:
            text = ['主人', '超管', '群主', '管理', '频管', '子管', '正常', '低', '黑'][data]
        else:
            text = '最低'
    return text


def get_plugin_bg(plugin_name: str, sv_list: List[SV]):
    img_list: List[Image.Image] = []

    for sv in sv_list:
        sv_img = Image.new(
            'RGBA',
            (
                900,
                sv_title + ((len(sv.TL) + 3) // 4) * 40,
            ),
        )
        sv_img_draw = ImageDraw.Draw(sv_img)
        for index, trigger_name in enumerate(sv.TL):
            tg_img = get_command_bg(trigger_name, sv.TL[trigger_name].type)
            sv_img.paste(
                tg_img, (6 + 220 * (index % 4), 67 + 40 * (index // 4)), tg_img
            )

        sv_img_draw.rounded_rectangle((15, 19, 25, 50), 10, (62, 62, 62))
        sv_img_draw.text((45, 31), sv.name, (62, 62, 62), core_font(36), 'lm')

        sv_img_draw.rounded_rectangle((710, 15, 760, 50), 10, _c(sv.enabled))
        sv_img_draw.rounded_rectangle((770, 15, 820, 50), 10, _c(sv.pm))
        sv_img_draw.rounded_rectangle((830, 15, 880, 50), 10, _c(sv.area))

        sv_img_draw.text(
            (735, 32), _t(sv.enabled), (62, 62, 62), core_font(22), 'mm'
        )
        sv_img_draw.text(
            (795, 32), _t(sv.pm), (62, 62, 62), core_font(22), 'mm'
        )
        sv_img_draw.text(
            (855, 32), _t(sv.area), (62, 62, 62), core_font(22), 'mm'
        )
        img_list.append(sv_img)

    img = Image.new(
        'RGBA',
        (
            900,
            plugin_title + sum([i.size[1] for i in img_list]),
        ),
    )
    img_draw = ImageDraw.Draw(img)
    img_draw.rounded_rectangle((10, 26, 890, 76), 10, (230, 202, 167))
    img_draw.text((450, 51), plugin_name, (62, 62, 62), core_font(42), 'mm')

    temp = 0
    for _img in img_list:
        img.paste(_img, (0, 92 + temp), _img)
        temp += _img.size[1]

    return img


async def get_help_img() -> Image.Image:
    from gsuid_core.sv import SL

    content = SL.detail_lst
    img_list: List[Image.Image] = []
    for plugin_name in content:
        plugin_img = get_plugin_bg(plugin_name, content[plugin_name])
        img_list.append(plugin_img)

    x = 900
    y = 200 + sum([i.size[1] for i in img_list])
    # img = await get_color_bg(x, y)
    img = Image.new('RGBA', (x, y), (255, 255, 255))
    title = Image.open(TEXT_PATH / 'title.png')

    # white = Image.new('RGBA', img.size, (255, 255, 255, 120))
    # img.paste(white, (0, 0), white)
    img.paste(title, (0, 50), title)

    temp = 0
    for _img in img_list:
        img.paste(_img, (0, 340 + temp), _img)
        temp += _img.size[1]

    img = img.convert('RGB')
    img.save(
        CORE_HELP_IMG,
        format='JPEG',
        quality=80,
        subsampling=0,
    )

    return img
