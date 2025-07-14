import json
from pathlib import Path
from typing import Dict, Literal

import aiofiles
from PIL import Image

from gsuid_core.help.utils import ICON
from gsuid_core.version import __version__
from gsuid_core.help.model import PluginHelp
from gsuid_core.sv import get_plugin_available_prefix
from gsuid_core.utils.plugins_config.gs_config import sp_config
from gsuid_core.help.draw_new_plugin_help import TEXT_PATH, get_new_help

HELP_DATA = Path(__file__).parent / 'help.json'
help_mode: Literal['light', 'dark'] = sp_config.get_config('HelpMode').data


async def get_master_help_data() -> Dict[str, PluginHelp]:
    async with aiofiles.open(HELP_DATA, 'rb') as file:
        return json.loads(await file.read())


async def get_help_data() -> Dict[str, PluginHelp]:
    async with aiofiles.open(HELP_DATA, 'rb') as file:
        return json.loads(await file.read())


async def draw_core_help(pm: int):
    from .utils import plugins_help

    help_data = await get_help_data()
    help_data.update(plugins_help)  # type: ignore

    if help_mode == 'light':
        item_bg = Image.open(TEXT_PATH / f'item_bg_{help_mode}.png')
        need_cover = True
    else:
        item_bg = None
        need_cover = False

    img = await get_new_help(
        plugin_name='GsCore',
        plugin_info={f'v{__version__}': ''},
        plugin_icon=Image.open(ICON),
        plugin_help=help_data,
        plugin_prefix=get_plugin_available_prefix('core_command'),
        item_bg=item_bg,
        need_cover=need_cover,
        help_mode=help_mode,
        pm=pm,
    )

    return img
