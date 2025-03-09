from typing import Dict, Union, Callable, Awaitable

from PIL import Image

from .models import PluginStatus

plugins_status: Dict[str, PluginStatus] = {}


def register_status(
    ICON: Image.Image,
    plugin_name: str,
    plugin_status: Dict[str, Callable[..., Awaitable[Union[str, int, float]]]],
):
    global plugins_status
    plugins_status[plugin_name] = {'icon': ICON, 'status': plugin_status}
