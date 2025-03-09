from typing import Dict, Union, Callable, Awaitable, TypedDict

from PIL import Image


class PluginStatus(TypedDict):
    icon: Image.Image
    status: Dict[str, Callable[..., Awaitable[Union[str, int, float]]]]
