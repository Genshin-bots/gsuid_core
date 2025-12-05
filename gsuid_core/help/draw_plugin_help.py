from copy import deepcopy
from typing import Dict, List, Tuple, Callable, Optional
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.image.image_tools import crop_center_img
from gsuid_core.utils.plugins_config.gs_config import pic_gen_config

from .model import PluginHelp

cache: Dict[str, int] = {}
MICON_PATH = Path(__file__).parent / "icon"
DEFAULT_ICON = MICON_PATH / "拼图.png"
pic_quality: int = pic_gen_config.get_config("PicQuality").data


def cx(w: int, x: int) -> int:
    return int((w - x) / 2)


def _get_icon(name: str, ICON_PATH: Path) -> Optional[Image.Image]:
    path = ICON_PATH / f"{name}.png"
    icon = None
    if path.exists():
        icon = Image.open(path)
    else:
        for i in ICON_PATH.glob("*.png"):
            if i.stem in name:
                icon = Image.open(i)
                break

    return icon


def get_icon(name: str, ICON_PATH: Optional[Path]) -> Image.Image:
    if ICON_PATH is not None:
        icon = _get_icon(name, ICON_PATH)
        if icon is None:
            icon = _get_icon(name, MICON_PATH)
    else:
        icon = _get_icon(name, MICON_PATH)

    if icon is None:
        icon = Image.open(DEFAULT_ICON)

    return icon.resize((36, 36))


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
    sub_c: Optional[Tuple[int, int, int]] = None,
    op_color: Optional[Tuple[int, int, int]] = None,
    title_color: Tuple[int, int, int] = (250, 250, 250),
    sub_title_color: Tuple[int, int, int] = (235, 235, 235),
    sv_color: Tuple[int, int, int] = (250, 250, 250),
    sv_desc_color: Tuple[int, int, int] = (235, 235, 235),
    column: int = 5,
    is_gaussian: bool = False,
    gaussian_blur: int = 20,
    is_icon: bool = True,
    ICON_PATH: Optional[Path] = None,
    extra_message: Optional[List[str]] = None,
    enable_cache: bool = True,
) -> bytes:
    help_path = get_res_path("help") / f"{name}.jpg"

    if help_path.exists() and name in cache and cache[name] and enable_cache:
        return await convert_img(Image.open(help_path))

    if sub_c is None and is_dark:
        sub_c = tuple(x - 50 if x > 50 else x for x in text_color)  # type: ignore
    elif sub_c is None and not is_dark:
        sub_c = tuple(x + 50 if x < 205 else x for x in text_color)  # type: ignore

    if op_color is None and is_dark:
        op_color = tuple(x - 90 if x > 90 else x for x in text_color)  # type: ignore
    elif op_color is None and not is_dark:
        op_color = tuple(x + 90 if x < 160 else x for x in text_color)  # type: ignore

    _h = 600

    if extra_message:
        _h += 100

    w, h = 50 + 260 * column, _h + 30
    button_x = 260
    button_y = 103  # 80

    title = Image.new("RGBA", (w, _h))
    icon = icon.resize((300, 300))

    title.paste(icon, (cx(w, 300), 89), icon)
    title.paste(badge, (cx(w, 900), 390), badge)
    badge_s = badge.resize((720, 80))
    title.paste(badge_s, (cx(w, 720), 480), badge_s)

    title_draw = ImageDraw.Draw(title)

    if extra_message:
        all_lenth = 300 * (len(extra_message) - 1) + 720
        first_x = (w - all_lenth) / 2
        for _i, message in enumerate(extra_message):
            _x = int(first_x + _i * 300)
            title.paste(badge_s, (_x, 556), badge_s)
            title_draw.text((_x + 360, 596), message, sub_c, font(26), "mm")

    title_draw.text(
        (cx(w, 0), 440), f"{name} 帮助", title_color, font(36), "mm"
    )
    title_draw.text((cx(w, 0), 520), sub_text, sub_title_color, font(26), "mm")

    if is_dark:
        icon_mask = Image.new("RGBA", (36, 36), (255, 255, 255))
    else:
        icon_mask = Image.new("RGBA", (36, 36), (10, 10, 10))

    sv_img_list: List[Image.Image] = []
    for sv_name in help_data:
        tr_size = len(help_data[sv_name]["data"])
        y = 100 + ((tr_size + column - 1) // column) * button_y
        h += y

        # 生成单个服务的背景， 依据默认column
        sv_img = Image.new("RGBA", (w, y))
        sv_data = help_data[sv_name]["data"]
        sv_desc = help_data[sv_name]["desc"]

        bc = deepcopy(banner)
        bc_draw = ImageDraw.Draw(bc)
        bc_draw.text((30, 25), sv_name, sv_color, font(35), "lm")

        if hasattr(font, "getsize"):
            size, _ = font(35).getsize(sv_name)  # type: ignore
        else:
            bbox = font(35).getbbox(sv_name)
            size, _ = bbox[2] - bbox[0], bbox[3] - bbox[1]

        bc_draw.text((42 + size, 30), sv_desc, sv_desc_color, font(20), "lm")
        sv_img.paste(bc, (0, 10), bc)
        # sv_img = easy_alpha_composite(sv_img, bc, (0, 10))

        # 开始绘制各个按钮
        for index, tr in enumerate(sv_data):
            bt = deepcopy(button)
            bt_draw = ImageDraw.Draw(bt)

            # 限制长度
            if is_icon and len(tr["name"]) > 7:
                tr_name = tr["name"][:5] + ".."
            elif len(tr["name"]) > 10:
                tr_name = tr["name"][:8] + ".."
            else:
                tr_name = tr["name"]

            if is_icon:
                f = 38
                icon = get_icon(tr["name"], ICON_PATH)
                bt.paste(icon_mask, (14, 20), icon)
            else:
                f = 0

            # 标题
            bt_draw.text((20 + f, 28), tr_name, text_color, font(26), "lm")
            # 使用范例
            bt_draw.text((20 + f, 50), tr["eg"], sub_c, font(17), "lm")
            # 简单介绍
            bt_draw.text((20, 78), tr["desc"], op_color, font(16), "lm")

            offset_x = button_x * (index % column)
            offset_y = button_y * (index // column)
            sv_img.paste(bt, (25 + offset_x, 83 + offset_y), bt)

        sv_img_list.append(sv_img)

    img = crop_center_img(bg, w, h)
    if is_gaussian:
        img = img.filter(ImageFilter.GaussianBlur(gaussian_blur))

    img.paste(title, (0, 0), title)
    temp = 0
    for _sm in sv_img_list:
        img.paste(_sm, (0, _h + temp), _sm)
        temp += _sm.size[1]

    img = img.convert("RGBA")
    all_white = Image.new("RGBA", img.size, (255, 255, 255))
    img = Image.alpha_composite(all_white, img)

    img = img.convert("RGB")
    help_path = get_res_path("help") / f"{name}.jpg"
    if enable_cache:
        img.save(
            help_path,
            "JPEG",
            quality=pic_quality,
            subsampling=0,
        )
        cache[name] = 1

    return await convert_img(img)
