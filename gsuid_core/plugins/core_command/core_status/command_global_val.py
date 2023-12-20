from gsuid_core.server import on_core_start, on_core_shutdown
from gsuid_core.global_val import load_all_global_val, save_all_global_val


@on_core_start
async def load_global_val():
    await load_all_global_val()


@on_core_shutdown
async def save_global_val():
    await save_all_global_val()
