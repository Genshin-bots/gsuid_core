import math
import random
from io import BytesIO
from typing import Tuple, Union, Optional
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from httpx import get

from gsuid_core.models import Event
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.image.utils import sget

TEXT_PATH = Path(__file__).parent / "texture2d"
BG_PATH = Path(__file__).parents[1] / "default_bg"


def tint_image(input_image: Image.Image, color: Tuple[int, int, int]):
    """
    给PNG图像中的非完全透明部分上色，并保留原始的Alpha透明度。

    :param input_image_path: 输入PNG图片的路径
    :param output_image_path: 输出图片的保存路径
    :param color: 目标颜色，一个RGB元组，例如红色为 (255, 0, 0)
    """
    _, _, _, alpha = input_image.convert("RGBA").split()
    color_layer = Image.new("RGBA", input_image.size, color)

    red_channel, green_channel, blue_channel, _ = color_layer.split()

    tinted_img = Image.merge(
        "RGBA",
        (
            red_channel,
            green_channel,
            blue_channel,
            alpha,
        ),
    )
    return tinted_img


def get_font_x(font: ImageFont.FreeTypeFont, text: str):
    bbox = font.getbbox(text)
    return int(bbox[2] - bbox[0])


def get_div():
    return Image.open(TEXT_PATH / "div.png")


def draw_color_badge(
    text: str,
    color: Union[Tuple[int, int, int], str] = (252, 69, 69),
    font: ImageFont.FreeTypeFont = core_font(30),
    font_color: Tuple[int, int, int] = (255, 255, 255),
):
    x1, y1, x2, y2 = font.getbbox(text)
    offset_x = int((x2 - x1) * 0.3)
    offset_y = int((y2 - y1) * 0.5)
    x3, y3, x4, y4 = x1 - offset_x, y1 - offset_y, x2 + offset_x, y2 + offset_y
    w, h = int(x4 - x3), int(y4 - y3)
    center_x, center_y = int(w // 2), int(h // 2)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, w, h), fill=color, radius=20)
    draw.text(
        (center_x, center_y),
        text,
        font=font,
        fill=font_color,
        anchor="mm",
    )
    return img


def get_status_icon(status: Union[int, bool]) -> Image.Image:
    if status:
        img = Image.open(TEXT_PATH / "yes.png")
    else:
        img = Image.open(TEXT_PATH / "no.png")
    return img


def get_v4_footer():
    return Image.open(TEXT_PATH / "footer.png")


def add_footer(
    img: Image.Image,
    w: int = 0,
    footer: Optional[Image.Image] = None,
) -> Image.Image:
    if footer is None:
        footer = get_v4_footer()

    w = img.size[0] if not w else w
    if w != footer.size[0]:
        footer = footer.resize(
            (w, int(footer.size[1] * w / footer.size[0])),
        )
    x, y = (
        int((img.size[0] - footer.size[0]) / 2),
        img.size[1] - footer.size[1] - 10,
    )
    img.paste(footer, (x, y), footer)
    return img


def get_v4_bg(w: int, h: int, is_dark: bool = False, is_blur: bool = False):
    CI_img = CustomizeImage(BG_PATH)
    img = CI_img.get_image(None, w, h)
    if is_blur:
        img = img.filter(ImageFilter.GaussianBlur(radius=20))
    if is_dark:
        black_img = Image.new("RGBA", (w, h), (0, 0, 0, 180))
        img.paste(black_img, (0, 0), black_img)
    img = img.convert("RGBA")
    return img


async def get_event_avatar(ev: Event, avatar_path: Optional[Path] = None) -> Image.Image:
    img = None
    if ev.bot_id == "onebot" and ev.at:
        img = await get_qq_avatar(ev.at)

    if img is None and "avatar" in ev.sender and ev.sender["avatar"]:
        avatar_url: str = ev.sender["avatar"]
        if avatar_url.startswith(("http", "https")):
            try:
                content = (await sget(avatar_url)).content
                img = Image.open(BytesIO(content)).convert("RGBA")
            except Exception:
                img = None

    if img is None and ev.bot_id == "onebot" and not ev.sender:
        img = await get_qq_avatar(ev.user_id)
    elif img is None and ev.bot_id == "qqgroup":
        img = await get_qqgroup_avatar(ev.bot_self_id, ev.user_id)

    if img is None and avatar_path:
        pic_path_list = list(avatar_path.iterdir())
        if pic_path_list:
            path = random.choice(pic_path_list)
            img = Image.open(path).convert("RGBA")

    if img is None:
        img = Image.open(TEXT_PATH / "icon.jpg").convert("RGBA")

    return img


async def get_avatar_with_ring(
    ev: Event,
    size: int = 300,
    avatar_path: Optional[Path] = None,
    bg_color: Union[Tuple[int, int, int], None] = None,
    is_ring: bool = True,
) -> Image.Image:
    avatar = await get_event_avatar(ev, avatar_path)
    return await draw_pic_with_ring(avatar, size, bg_color, is_ring)


async def shift_image_hue(img: Image.Image, angle: float = 30) -> Image.Image:
    alpha = img.getchannel("A")
    img = img.convert("HSV")

    pixels = img.load()
    assert pixels is not None
    hue_shift = angle

    for y in range(img.height):
        for x in range(img.width):
            h, s, v = pixels[x, y]  # type: ignore
            h = (h + hue_shift) % 360
            pixels[x, y] = (h, s, v)  # type: ignore

    img = img.convert("RGBA")
    img.putalpha(alpha)
    return img


async def get_pic(url, size: Optional[Tuple[int, int]] = None) -> Image.Image:
    """
    从网络获取图片, 格式化为RGBA格式的指定尺寸
    """
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.get(url=url)
        if resp.status_code != 200:
            if size is None:
                size = (960, 600)
            return Image.new("RGBA", size)
        pic = Image.open(BytesIO(resp.read()))
        pic = pic.convert("RGBA")
        if size is not None:
            pic = pic.resize(size)
        return pic


def draw_center_text_by_line(
    img: ImageDraw.ImageDraw,
    pos: Tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Union[Tuple[int, int, int, int], str],
    max_length: float,
    not_center: bool = False,
) -> float:
    pun = "。！？；!?…"
    gun = "。！？；!?」』"
    x, y = pos

    if hasattr(font, "getsize"):
        _, h = font.getsize("X")  # type: ignore
    else:
        bbox = font.getbbox("X")
        _, h = 0, bbox[3] - bbox[1]

    line = ""
    lenth = 0
    anchor = "la" if not_center else "mm"
    for index, char in enumerate(text):
        if hasattr(font, "getsize"):
            # 获取当前字符的宽度
            size, _ = font.getsize(char)  # type: ignore
        else:
            bbox = font.getbbox(char)
            size, _ = bbox[2] - bbox[0], bbox[3] - bbox[1]
        lenth += size
        line += char
        if lenth < max_length and char not in pun and char != "\n":
            pass
        else:
            if index + 1 < len(text) and text[index + 1] in gun:
                pass
            else:
                line = line.replace("\n", "")
                img.text((x, y), line, fill, font, anchor)
                line, lenth = "", 0
                y += h * 2.5
    else:
        img.text((x, y), line, fill, font, anchor)
    return y


def draw_text_by_line(
    img: Image.Image,
    pos: Tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Union[Tuple[int, int, int, int], str],
    max_length: float,
    center=False,
    line_space: Optional[float] = None,
) -> float:
    """
    在图片上写长段文字, 自动换行
    max_length单行最大长度, 单位像素
    line_space  行间距, 单位像素, 默认是字体高度的0.3倍
    """
    x, y = pos

    if hasattr(font, "getsize"):
        _, h = font.getsize("X")  # type: ignore
    else:
        bbox = font.getbbox("X")
        _, h = 0, bbox[3] - bbox[1]

    if line_space is None:
        y_add = math.ceil(1.3 * h)
    else:
        y_add = math.ceil(h + line_space)
    draw = ImageDraw.Draw(img)
    row = ""  # 存储本行文字
    length = 0  # 记录本行长度
    for character in text:
        # 获取当前字符的宽度
        if hasattr(font, "getsize"):
            w, h = font.getsize(character)  # type: ignore
        else:
            bbox = font.getbbox("X")
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]

        if length + w * 2 <= max_length:
            row += character
            length += w
        else:
            row += character
            if center:
                if hasattr(font, "getsize"):
                    font_size = font.getsize(row)  # type: ignore
                else:
                    bbox = font.getbbox(character)
                    font_size = bbox[2] - bbox[0], bbox[3] - bbox[1]
                x = math.ceil((img.size[0] - font_size[0]) / 2)
            draw.text((x, y), row, font=font, fill=fill)
            row = ""
            length = 0
            y += y_add
    if row != "":
        if center:
            if hasattr(font, "getsize"):
                font_size = font.getsize(row)  # type: ignore
            else:
                bbox = font.getbbox(row)
                font_size = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = math.ceil((img.size[0] - font_size[0]) / 2)
        draw.text((x, y), row, font=font, fill=fill)
    return y


def easy_paste(im: Image.Image, im_paste: Image.Image, pos=(0, 0), direction="lt"):
    """
    inplace method
    快速粘贴, 自动获取被粘贴图像的坐标。
    pos应当是粘贴点坐标，direction指定粘贴点方位，例如lt为左上
    """
    x, y = pos
    size_x, size_y = im_paste.size
    if "d" in direction:
        y = y - size_y
    if "r" in direction:
        x = x - size_x
    if "c" in direction:
        x = x - int(0.5 * size_x)
        y = y - int(0.5 * size_y)
    im.paste(im_paste, (x, y, x + size_x, y + size_y), im_paste)


def easy_alpha_composite(im: Image.Image, im_paste: Image.Image, pos=(0, 0), direction="lt") -> Image.Image:
    """
    透明图像快速粘贴
    """
    base = Image.new("RGBA", im.size)
    easy_paste(base, im_paste, pos, direction)
    base = Image.alpha_composite(im, base)
    return base


async def get_qq_avatar(qid: Optional[Union[int, str]] = None, avatar_url: Optional[str] = None) -> Image.Image:
    if qid:
        avatar_url = f"http://q1.qlogo.cn/g?b=qq&nk={qid}&s=640"
    elif avatar_url is None:
        avatar_url = "https://q1.qlogo.cn/g?b=qq&nk=3399214199&s=640"
    char_pic = Image.open(BytesIO((await sget(avatar_url)).content)).convert("RGBA")
    return char_pic


async def get_qqgroup_avatar(
    bot_id: Optional[Union[int, str]] = None,
    qid: Optional[Union[int, str]] = None,
    avatar_url: Optional[str] = None,
) -> Optional[Image.Image]:
    if not qid or not bot_id:
        return None
    avatar_url = f"https://q.qlogo.cn/qqapp/{bot_id}/{qid}/100"
    char_pic = Image.open(BytesIO((await sget(avatar_url)).content)).convert("RGBA")
    return char_pic


async def draw_pic_with_ring(
    pic: Image.Image,
    size: int,
    bg_color: Optional[Tuple[int, int, int]] = None,
    is_ring: bool = True,
):
    """
    :说明:
      绘制一张带白色圆环的1:1比例图片。

    :参数:
      * pic: `Image.Image`: 要修改的图片。
      * size: `int`: 最后传出图片的大小(1:1)。
      * bg_color: `Optional[Tuple[int, int, int]]`: 是否指定圆环内背景颜色。

    :返回:
      * img: `Image.Image`: 图片对象
    """
    ring_pic = Image.open(TEXT_PATH / "ring.png")
    mask_pic = Image.open(TEXT_PATH / "mask.png")
    img = Image.new("RGBA", (size, size))
    resize_pic = crop_center_img(pic, size, size)
    resize_pic = resize_pic.convert("RGBA")
    mask = mask_pic.resize((size, size))
    if bg_color:
        img_color = Image.new("RGBA", (size, size), bg_color)
        img_color.paste(resize_pic, (0, 0), resize_pic)
        img.paste(img_color, (0, 0), mask)
    else:
        img.paste(resize_pic, (0, 0), mask)

    if is_ring:
        ring = ring_pic.resize((size, size))
        img.paste(ring, (0, 0), ring)

    return img


def crop_center_img(img: Image.Image, based_w: int, based_h: int) -> Image.Image:
    # 确定图片的长宽
    based_scale = "%.3f" % (based_w / based_h)
    w, h = img.size
    scale_f = "%.3f" % (w / h)
    new_w = math.ceil(based_h * float(scale_f))
    new_h = math.ceil(based_w / float(scale_f))
    if scale_f > based_scale:
        resize_img = img.resize((new_w, based_h), Image.Resampling.LANCZOS)
        x1 = int(new_w / 2 - based_w / 2)
        y1 = 0
        x2 = int(new_w / 2 + based_w / 2)
        y2 = based_h
    else:
        resize_img = img.resize((based_w, new_h), Image.Resampling.LANCZOS)
        x1 = 0
        y1 = int(new_h / 2 - based_h / 2)
        x2 = based_w
        y2 = int(new_h / 2 + based_h / 2)
    crop_img = resize_img.crop((x1, y1, x2, y2))
    return crop_img


async def get_color_bg(
    based_w: int,
    based_h: int,
    bg_path: Optional[Path] = None,
    without_mask: bool = False,
    is_full: bool = False,
    color: Optional[Tuple[int, int, int]] = None,
    full_opacity: int = 200,
) -> Image.Image:
    if bg_path is None:
        bg_path = get_res_path(["GsCore", "bg"])
    CI_img = CustomizeImage(bg_path)
    img = CI_img.get_image(None, based_w, based_h)
    if color is None:
        color = CI_img.get_bg_color(img)
    if is_full:
        color_img = Image.new("RGBA", (based_w, based_h), color)
        mask = Image.new("RGBA", (based_w, based_h), (255, 255, 255, full_opacity))
        img.paste(color_img, (0, 0), mask)
    elif not without_mask:
        color_mask = Image.new("RGBA", (based_w, based_h), color)
        enka_mask = Image.open(TEXT_PATH / "bg_mask.png").resize((based_w, based_h))
        img.paste(color_mask, (0, 0), enka_mask)
    return img


class CustomizeImage:
    def __init__(self, bg_path: Path) -> None:
        self.bg_path = bg_path

    def get_image(self, image: Union[str, Image.Image, None], based_w: int, based_h: int) -> Image.Image:
        # 获取背景图片
        if isinstance(image, Image.Image):
            edit_bg = image
        elif image:
            edit_bg = Image.open(BytesIO(get(image).content)).convert("RGBA")
        else:
            _lst = list(self.bg_path.iterdir())
            if _lst:
                path = random.choice(list(self.bg_path.iterdir()))
            else:
                path = random.choice(list(BG_PATH.iterdir()))
            edit_bg = Image.open(path).convert("RGBA")

        # 确定图片的长宽
        bg_img = crop_center_img(edit_bg, based_w, based_h)
        return bg_img

    @staticmethod
    def get_dominant_color(pil_img: Image.Image) -> Tuple[int, int, int]:
        img = pil_img.copy()
        img = img.convert("RGBA")
        img = img.resize((1, 1), resample=0)
        dominant_color = img.getpixel((0, 0))
        if isinstance(dominant_color, float):
            _dominant_color = tuple([int(dominant_color * 255) for _ in range(3)])  # type: ignore
        elif dominant_color is None or isinstance(dominant_color, int):
            _dominant_color: Tuple[int, int, int] = (255, 255, 255)
        else:
            _dominant_color = dominant_color  # type: ignore

        return _dominant_color

    @staticmethod
    def get_bg_color(edit_bg: Image.Image, is_light: Optional[bool] = False) -> Tuple[int, int, int]:
        # 获取背景主色
        color = 8
        q = edit_bg.quantize(colors=color, method=Image.Quantize.FASTOCTREE)
        bg_color = (0, 0, 0)
        if is_light:
            based_light = 195
        else:
            based_light = 120
        temp = 9999
        for i in range(color):
            bg = tuple(
                q.getpalette()[  # type:ignore
                    i * 3 : (i * 3)
                    + 3  # noqa:E203
                ]
            )
            light_value = bg[0] * 0.3 + bg[1] * 0.6 + bg[2] * 0.1
            if abs(light_value - based_light) < temp:  # noqa:E203
                bg_color = bg
                temp = abs(light_value - based_light)
        return bg_color  # type:ignore

    @staticmethod
    def get_text_color(bg_color: Tuple[int, int, int]) -> Tuple[int, int, int]:
        # 通过背景主色（bg_color）确定文字主色
        r = 125
        if max(*bg_color) > 255 - r:
            r *= -1
        text_color = (
            math.floor(bg_color[0] + r if bg_color[0] + r <= 255 else 255),
            math.floor(bg_color[1] + r if bg_color[1] + r <= 255 else 255),
            math.floor(bg_color[2] + r if bg_color[2] + r <= 255 else 255),
        )
        return text_color

    @staticmethod
    def get_char_color(bg_color: Tuple[int, int, int]) -> Tuple[int, int, int]:
        r = 140
        if max(*bg_color) > 255 - r:
            r *= -1
        char_color = (
            math.floor(bg_color[0] + 5 if bg_color[0] + r <= 255 else 255),
            math.floor(bg_color[1] + 5 if bg_color[1] + r <= 255 else 255),
            math.floor(bg_color[2] + 5 if bg_color[2] + r <= 255 else 255),
        )
        return char_color

    @staticmethod
    def get_char_high_color(
        bg_color: Tuple[int, int, int],
    ) -> Tuple[int, int, int]:
        r = 140
        d = 20
        if max(*bg_color) > 255 - r:
            r *= -1
        char_color = (
            math.floor(bg_color[0] + d if bg_color[0] + r <= 255 else 255),
            math.floor(bg_color[1] + d if bg_color[1] + r <= 255 else 255),
            math.floor(bg_color[2] + d if bg_color[2] + r <= 255 else 255),
        )
        return char_color

    @staticmethod
    def get_bg_detail_color(
        bg_color: Tuple[int, int, int],
    ) -> Tuple[int, int, int]:
        r = 140
        if max(*bg_color) > 255 - r:
            r *= -1
        bg_detail_color = (
            math.floor(bg_color[0] - 20 if bg_color[0] + r <= 255 else 255),
            math.floor(bg_color[1] - 20 if bg_color[1] + r <= 255 else 255),
            math.floor(bg_color[2] - 20 if bg_color[2] + r <= 255 else 255),
        )
        return bg_detail_color

    @staticmethod
    def get_highlight_color(
        color: Tuple[int, int, int],
    ) -> Tuple[int, int, int]:
        red_color = color[0]
        green_color = color[1]
        blue_color = color[2]

        highlight_color = {
            "red": red_color - 127 if red_color > 127 else 127,
            "green": green_color - 127 if green_color > 127 else 127,
            "blue": blue_color - 127 if blue_color > 127 else 127,
        }

        max_color = max(highlight_color.values())

        name = ""
        for _highlight_color in highlight_color:
            if highlight_color[_highlight_color] == max_color:
                name = str(_highlight_color)

        if name == "red":
            return red_color, highlight_color["green"], highlight_color["blue"]
        elif name == "green":
            return highlight_color["red"], green_color, highlight_color["blue"]
        elif name == "blue":
            return highlight_color["red"], highlight_color["green"], blue_color
        else:
            return 0, 0, 0  # Error
