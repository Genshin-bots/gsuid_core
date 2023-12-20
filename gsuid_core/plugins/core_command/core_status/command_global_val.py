import json
import datetime

import aiofiles

from gsuid_core.global_val import global_val, global_val_path
from gsuid_core.server import on_core_start, on_core_shutdown


@on_core_start
async def load_global_val():
    global global_val
    today = datetime.date.today()
    date_format = today.strftime("%Y_%d_%b")
    path = global_val_path / f'global_val_{date_format}.json'
    if path.exists():
        async with aiofiles.open(path, 'r') as fp:
            global_val = json.loads(await fp.read())


@on_core_shutdown
async def save_global_val():
    today = datetime.date.today()
    date_format = today.strftime("%Y_%d_%b")
    async with aiofiles.open(
        global_val_path / f'global_val_{date_format}.json', 'w'
    ) as fp:
        await fp.write(
            json.dumps(
                global_val,
                indent=4,
                ensure_ascii=False,
            )
        )
