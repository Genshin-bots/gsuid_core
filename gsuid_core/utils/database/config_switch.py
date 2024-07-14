from typing import Dict, Type, Union, Optional

from gsuid_core.utils.database.base_models import Bind, Push, User

T_MODEL = Union[Type[User], Type[Bind], Type[Push]]


async def set_database_value(
    model: T_MODEL,
    game_name: str,
    command_start: str,
    command_value: str,
    uid: str,
    bot_id: str,
    value: str,
):
    fields = model.__fields__
    for keyname in fields:
        field = fields[keyname]
        field_info = field.field_info  # type: ignore
        key_name = field.name  # type: ignore
        title: Optional[str] = field_info.title
        extra: Dict[str, str] = field_info.extra
        desc: Optional[str] = extra['hint'] if 'hint' in extra else None
        title = title if title else key_name

        if desc:
            if desc == f'{command_start}{command_value}':
                await model.update_data_by_uid(
                    uid,
                    bot_id,
                    game_name,
                    **{
                        f'{keyname}': value,
                    },
                )

                return f'✅{title}\n📝已设置为{value}'
