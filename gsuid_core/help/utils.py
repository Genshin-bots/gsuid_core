from pathlib import Path
from typing import Optional

from PIL import Image

ICON = Path(__file__).parent.parent.parent / 'ICON.png'
plugins_help = {
    "插件帮助一览": {"desc": "这里可以看到注册过的插件帮助。", "data": []},
}


def register_help(
    name: str,
    help: str,
    icon: Optional[Image.Image] = None,
):
    if icon is None:
        icon = Image.open(ICON)
    plugins_help['插件帮助一览']['data'].append(
        {
            "name": name,
            "desc": f"{name}插件帮助功能",
            "eg": f'发送 {help} 获得帮助',
            "icon": icon,
            "need_ck": False,
            "need_sk": False,
            "need_admin": False,
        }
    )
