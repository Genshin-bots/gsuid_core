from pathlib import Path

from PIL import ImageFont

FONT_ORIGIN_PATH = Path(__file__).parent / 'SmileySans.ttf'


def smile_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_ORIGIN_PATH), size=size)
