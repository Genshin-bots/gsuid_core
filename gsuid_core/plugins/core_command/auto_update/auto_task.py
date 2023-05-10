from typing import List

from gsuid_core.utils.plugins_update.api import PLUGINS_PATH
from gsuid_core.utils.plugins_update._plugins import update_from_git
from gsuid_core.plugins.core_command.core_restart.restart import (
    restart_genshinuid,
)


async def update_core() -> List[str]:
    return update_from_git()


async def update_all_plugins() -> List[str]:
    log_list = []
    for plugin in PLUGINS_PATH.iterdir():
        log_list.extend(update_from_git(0, plugin))
    return log_list


async def restart_core():
    await restart_genshinuid('', '', '', False)
