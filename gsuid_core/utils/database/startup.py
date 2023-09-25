from sqlalchemy.sql import text

from gsuid_core.server import on_core_start

from .base_models import async_maker

exec_list = []


@on_core_start
async def sr_adapter():
    async with async_maker() as session:
        for _t in exec_list:
            try:
                await session.execute(text(_t))
                await session.commit()
            except:  # noqa: E722
                pass
