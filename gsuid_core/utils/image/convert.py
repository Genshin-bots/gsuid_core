import math
from io import BytesIO
from pathlib import Path
from base64 import b64encode
from typing import Union, overload

import aiofiles
from PIL import Image, ImageDraw, ImageFont

from gsuid_core.logger import logger
from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.plugins_config.gs_config import pic_gen_config
from gsuid_core.utils.image.image_tools import draw_center_text_by_line

pic_quality: int = pic_gen_config.get_config('PicQuality').data


@overload
async def convert_img(
    img: Image.Image,
    is_base64: bool = False,
) -> bytes: ...


@overload
async def convert_img(
    img: Image.Image,
    is_base64: bool = True,
) -> str: ...


@overload
async def convert_img(
    img: bytes,
    is_base64: bool = False,
) -> str: ...


@overload
async def convert_img(
    img: Path,
    is_base64: bool = False,
) -> str: ...


async def convert_img(
    img: Union[Image.Image, str, Path, bytes],
    is_base64: bool = False,
):
    """
    :说明:
      将PIL.Image对象转换为bytes或者base64格式。
    :参数:
      * img (Image): 图片。
      * is_base64 (bool): 是否转换为base64格式, 不填默认转为bytes。
    :返回:
      * res: bytes对象或base64编码图片。
    """
    logger.info('🚀 [GsCore] 处理图片中....')

    if isinstance(img, Image.Image):
        if img.format == 'GIF':
            result_buffer = BytesIO()
            img.save(result_buffer, format='GIF')
        else:
            img = img.convert('RGB')
            result_buffer = BytesIO()
            img.save(
                result_buffer,
                format='JPEG',
                quality=pic_quality,
            )

        res = result_buffer.getvalue()
        if is_base64:
            res = 'base64://' + b64encode(res).decode()
        return res
    elif isinstance(img, bytes):
        pass
    else:
        async with aiofiles.open(Path(img), 'rb') as fp:
            img = await fp.read()

    logger.success('[GsCore] 图片处理完成！')

    return f'base64://{b64encode(img).decode()}'


def convert_img_sync(img_path: Path):
    with open(img_path, 'rb') as fp:
        img = fp.read()

    return f'base64://{b64encode(img).decode()}'


async def str_lenth(r: str, size: int, limit: int = 540) -> str:
    result = ''
    temp = 0
    for i in r:
        if i == '\n':
            temp = 0
            result += i
            continue

        if temp >= limit:
            result += '\n' + i
            temp = 0
        else:
            result += i

        if i.isdigit():
            temp += round(size / 10 * 6)
        elif i == '/':
            temp += round(size / 10 * 2.2)
        elif i == '.':
            temp += round(size / 10 * 3)
        elif i == '%':
            temp += round(size / 10 * 9.4)
        else:
            temp += size
    return result


def get_str_size(
    r: str, font: ImageFont.FreeTypeFont, limit: int = 540
) -> str:
    result = ''
    line = ''
    for i in r:
        if i == '\n':
            result += f'{line}\n'
            line = ''
            continue

        line += i

        if hasattr(font, 'getsize'):
            size, _ = font.getsize(line)  # type: ignore
        else:
            bbox = font.getbbox(line)
            size, _ = bbox[2] - bbox[0], bbox[3] - bbox[1]

        if size >= limit:
            result += f'{line}\n'
            line = ''
    else:
        result += line
    return result


def get_height(content: str, size: int) -> int:
    line_count = content.count('\n')
    return (line_count + 1) * size


async def text2pic(text: str, max_size: int = 800, font_size: int = 24):
    if text.endswith('\n'):
        text = text[:-1]

    img = Image.new(
        'RGB', (max_size, len(text) * font_size // 3), (255, 255, 255)
    )
    img_draw = ImageDraw.ImageDraw(img)
    y = draw_center_text_by_line(
        img_draw,
        (50, 50),
        text,
        core_font(font_size),
        'black',
        max_size - 80,
        True,
    )
    img = img.crop((0, 0, max_size, int(y + 80)))
    return await convert_img(img)


def number_to_chinese(num):
    units = [
        {'threshold': 10**8, 'suffix': '亿'},  # 1e8 (100,000,000)
        {'threshold': 10**7, 'suffix': '千万'},  # 1e7 (10,000,000)
        {'threshold': 10**4, 'suffix': '万'},  # 1e4 (10,000)
        {'threshold': 10**3, 'suffix': '千'},  # 1e3 (1,000)
    ]
    for unit in units:
        if num >= unit['threshold']:
            value = num / unit['threshold']
            truncated = math.floor(value * 10) / 10  # 截断一位小数，不四舍五入
            return f"{truncated:.1f}{unit['suffix']}"
    # 处理小于1e3的情况
    if isinstance(num, float) and num.is_integer():
        return str(int(num))
    else:
        return str(num)
