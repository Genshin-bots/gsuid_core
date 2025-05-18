from gsuid_core.server import GsServer
from gsuid_core.aps import remove_repeat_job

gss = GsServer()


async def load_gss():
    if not gss.is_load:
        gss.is_load = True
        await gss.load_plugins()
        remove_repeat_job()
