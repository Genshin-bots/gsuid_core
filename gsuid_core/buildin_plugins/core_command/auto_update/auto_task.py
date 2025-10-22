from typing import List

from gsuid_core.utils.plugins_update._plugins import update_from_git_in_tread

from ..core_restart.restart import restart_genshinuid


async def update_core() -> List[str]:
    return await update_from_git_in_tread()


async def restart_core():
    await restart_genshinuid(None, False)
