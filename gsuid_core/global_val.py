import json
import datetime
from typing import Dict, Optional, TypedDict

import aiofiles

from gsuid_core.data_store import get_res_path

global_val_path = get_res_path(['GsCore', 'global'])


class GlobalVal(TypedDict):
    receive: int
    send: int
    command: int
    group: Dict[str, Dict[str, int]]


global_val: GlobalVal = {
    'receive': 0,
    'send': 0,
    'command': 0,
    'group': {},
}


async def get_blobal_val(day: Optional[int] = None) -> Optional[GlobalVal]:
    if day is None:
        return global_val
    else:
        today = datetime.date.today()
        endday = today - datetime.timedelta(days=day)
        endday_format = endday.strftime("%Y_%d_%b")
        path = global_val_path / f'global_val_{endday_format}.json'
        if path.exists():
            async with aiofiles.open(path, 'r') as fp:
                return json.loads(await fp.read())
