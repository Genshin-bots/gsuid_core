from typing import Dict

from .models import GSC, GsIntConfig

PIC_GEN_CONIFG: Dict[str, GSC] = {
    "PicQuality": GsIntConfig("图片生成质量", "设定生成图片的质量, 最高不可超过100", 85, 100),
}
