from pathlib import Path

from PIL import ImageFont

# MiSans 可变字体（wght 150–700）。CSS 字重由 pytakumi 驱动；PIL 无 CSS。
FONT_ORIGIN_PATH = Path(__file__).parent / "MiSansVF.ttf"
# 官方实例 Bold=630（轴顶 Heavy=700）。默认贴近旧静态 MiSans-Bold 观感。
_CORE_FONT_WGHT = 630.0
_CORE_FONT_WGHT_MIN = 150.0
_CORE_FONT_WGHT_MAX = 700.0


def core_font(size: int, weight: float = _CORE_FONT_WGHT) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_ORIGIN_PATH), size=size)
    set_axes = getattr(font, "set_variation_by_axes", None)
    if callable(set_axes):
        wght = min(_CORE_FONT_WGHT_MAX, max(_CORE_FONT_WGHT_MIN, float(weight)))
        try:
            set_axes([wght])
        except (OSError, ValueError, TypeError):
            pass
    return font
