from __future__ import annotations

from typing import Dict, Union, Literal, Optional, cast

from httpx import AsyncClient

from ..types import AnyDict
from ..utils import _HEADER
from .models import WeaponData, CharacterData
from .api import HAKUSH_CHAR_URL, HAKUSH_WEAPON_URL


async def get_hakush_char_data(
    id: Union[int, str],
) -> Optional[CharacterData]:
    data = await _hakush_request(url=HAKUSH_CHAR_URL.format(id))
    if isinstance(data, Dict):
        return cast(CharacterData, data)
    return None


async def get_hakush_weapon_data(
    id: Union[int, str],
) -> Optional[WeaponData]:
    data = await _hakush_request(url=HAKUSH_WEAPON_URL.format(id))
    if isinstance(data, Dict):
        return cast(WeaponData, data)
    return None


async def _hakush_request(
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
        return data
