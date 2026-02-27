from gsuid_core.server import on_core_start

from .rag import sync_knowledge, init_collection


@on_core_start
async def start_rag():
    await init_collection()
    await sync_knowledge()
