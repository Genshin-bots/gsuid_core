'''
安柏计划 API 请求模块。
'''

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Dict, Union, Literal, Optional, cast

import aiofiles
from PIL import Image
from httpx import AsyncClient

from ..types import AnyDict
from ..utils import _HEADER
from .models import (
    AmbrBook,
    AmbrDaily,
    AmbrEvent,
    AmbrWeapon,
    AmbrGCGList,
    AmbrMonster,
    AmbrCharacter,
    AmbrGCGDetail,
    AmbrBookDetail,
    AmbrMonsterList,
    AmbrUpgradeItem,
)
from .api import (
    AMBR_BOOK_URL,
    AMBR_CHAR_URL,
    AMBR_ICON_URL,
    AMBR_DAILY_URL,
    AMBR_EVENT_URL,
    AMBR_GCG_DETAIL,
    AMBR_WEAPON_URL,
    AMBR_MONSTER_URL,
    AMBR_UPGRADE_URL,
    AMBR_GCG_LIST_URL,
    AMBR_MONSTER_LIST,
    AMBR_BOOK_DATA_URL,
    AMBR_BOOK_DETAILS_URL,
)


async def get_ambr_event_info() -> Optional[Dict[str, AmbrEvent]]:
    data = await _ambr_request(url=AMBR_EVENT_URL)
    if isinstance(data, Dict):
        return cast(Dict[str, AmbrEvent], data)
    return None


async def get_ambr_char_data(id: Union[int, str]) -> Optional[AmbrCharacter]:
    data = await _ambr_request(url=AMBR_CHAR_URL.format(id))
    if isinstance(data, Dict) and data['response'] == 200:
        data = data['data']
        return cast(AmbrCharacter, data)
    return None


async def get_ambr_monster_data(id: Union[int, str]) -> Optional[AmbrMonster]:
    data = await _ambr_request(url=AMBR_MONSTER_URL.format(id))
    if isinstance(data, Dict) and data['response'] == 200:
        data = data['data']
        return cast(AmbrMonster, data)
    return None


async def get_ambr_gcg_detail(id: Union[int, str]) -> Optional[AmbrGCGDetail]:
    data = await _ambr_request(url=AMBR_GCG_DETAIL.format(id))
    if isinstance(data, Dict) and data['response'] == 200:
        data = data['data']
        return cast(AmbrGCGDetail, data)
    return None


async def get_ambr_gcg_list() -> Optional[AmbrGCGList]:
    data = await _ambr_request(url=AMBR_GCG_LIST_URL)
    if isinstance(data, Dict) and data['response'] == 200:
        data = data['data']
        return cast(AmbrGCGList, data)
    return None


async def get_ambr_monster_list() -> Optional[AmbrMonsterList]:
    data = await _ambr_request(url=AMBR_MONSTER_LIST)
    if isinstance(data, Dict) and data['response'] == 200:
        data = data['data']
        return cast(AmbrMonsterList, data)
    return None


async def get_ambr_weapon_data(id: Union[int, str]) -> Optional[AmbrWeapon]:
    data = await _ambr_request(url=AMBR_WEAPON_URL.format(id))
    if isinstance(data, Dict) and data['response'] == 200:
        data = data['data']
        return cast(AmbrWeapon, data)
    return None


async def get_ambr_daily_data() -> Optional[AmbrDaily]:
    data = await _ambr_request(url=AMBR_DAILY_URL)
    if isinstance(data, Dict) and data['response'] == 200:
        data = data['data']
        insert = {}
        for day in data:
            insert[day] = [value for value in data[day].values()]
        return cast(AmbrDaily, insert)
    return None


async def get_all_upgrade() -> Optional[AmbrUpgradeItem]:
    data = await _ambr_request(url=AMBR_UPGRADE_URL)
    if isinstance(data, Dict) and data['response'] == 200:
        data = data['data']
        return cast(AmbrUpgradeItem, data)
    return None


async def get_all_book_id() -> Optional[Dict[str, AmbrBook]]:
    data = await _ambr_request(url=AMBR_BOOK_URL)
    if isinstance(data, Dict) and data['response'] == 200:
        data = data['data']['items']
        return cast(Dict[str, AmbrBook], data)
    return None


async def get_book_volume(id: Union[int, str]) -> Optional[AmbrBookDetail]:
    data = await _ambr_request(url=AMBR_BOOK_DETAILS_URL.format(id))
    if isinstance(data, Dict) and data['response'] == 200:
        data = data['data']
        return cast(AmbrBookDetail, data)
    return None


async def get_story_data(story_id: Union[int, str]) -> Optional[str]:
    data = await _ambr_request(url=AMBR_BOOK_DATA_URL.format(story_id))
    if isinstance(data, Dict) and data['response'] == 200:
        return data['data']
    return None


async def get_ambr_icon(
    type: str,
    icon_name: str,
    path: Path,
    ui_name: Optional[str] = None,
    save_name: Optional[str] = None,
) -> Image.Image:
    '''
    获取ItemIcon:
        await get_ambr_icon('UI', '114004', path, 'ItemIcon')
        https://api.ambr.top/assets/UI/UI_ItemIcon_114004.png
    获取其他:
        await get_ambr_icon('UI', 'Chongyun', path, 'AvatarIcon')
        https://api.ambr.top/assets/UI/UI_AvatarIcon_Chongyun.png
    '''
    if ui_name:
        item_icon = f'UI_{ui_name}_{icon_name}.png'
        url = f'{AMBR_ICON_URL}/{item_icon}'
    else:
        item_icon = f'{icon_name}.png'
        url = f'{AMBR_ICON_URL}/{type}/{item_icon}'

    if save_name:
        item_icon = f'{save_name}.png'

    file_path = path / item_icon

    if file_path.exists():
        async with aiofiles.open(file_path, 'rb') as f:
            return Image.open(BytesIO(await f.read()))

    async with AsyncClient(timeout=None) as client:
        req = await client.get(
            url,
            headers=_HEADER,
        )
        if req.status_code == 200:
            content = req.read()
            async with aiofiles.open(file_path, 'wb') as f:
                await f.write(content)
            return Image.open(BytesIO(content))
        else:
            return Image.new('RGBA', (256, 256), (0, 0, 0))


async def _ambr_request(
    url: str,
    method: Literal['GET', 'POST'] = 'GET',
    header: AnyDict = _HEADER,
    params: Optional[AnyDict] = None,
    data: Optional[AnyDict] = None,
) -> Optional[AnyDict]:
    async with AsyncClient(timeout=None) as client:
        req = await client.request(
            method, url=url, headers=header, params=params, json=data
        )
        data = req.json()
        if data and 'code' in data:
            data['response'] = data['code']
        return data
