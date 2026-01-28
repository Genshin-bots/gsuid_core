from pathlib import Path

from PIL import Image

ICON = Path(__file__).parent.parent.parent.parent / "ICON.png"


def get_ICON() -> Image.Image:
    return Image.open(ICON)
