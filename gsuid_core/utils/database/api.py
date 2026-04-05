import re
from typing import Type, Tuple, Union, Literal, Optional, overload

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.utils.database.base_models import Bind

is_wal = False


@overload
async def get_uid(
    bot: Bot,
    ev: Event,
    bind_model: Type[Bind],
    game_name: Optional[str] = None,
    get_user_id: Literal[False] = False,
    partten: Optional[str] = r"\d+",
) -> Optional[str]: ...


@overload
async def get_uid(
    bot: Bot,
    ev: Event,
    bind_model: Type[Bind],
    game_name: Optional[str] = None,
    get_user_id: Literal[True] = True,
    partten: Optional[str] = r"\d+",
) -> Tuple[Optional[str], str]: ...


async def get_uid(
    bot: Bot,
    ev: Event,
    bind_model: Type[Bind],
    game_name: Optional[str] = None,
    get_user_id: bool = False,
    partten: Optional[str] = r"\d+",
) -> Union[Optional[str], Tuple[Optional[str], str]]:
    uid_data = []
    if partten:
        uid_data = re.findall(partten, ev.text)

    user_id = ev.at if ev.at else ev.user_id

    if uid_data:
        uid = uid_data[0]
    else:
        uid = await bind_model.get_uid_by_game(user_id, ev.bot_id, game_name)

    if get_user_id:
        return uid, user_id
    return uid
