from typing import Type, Union, Optional

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
        if hasattr(field, 'field_info'):
            field_info = field.field_info  # type: ignore
            extra = (
                field_info.extra['json_schema_extra']
                if field_info.extra and 'json_schema_extra' in field_info.extra
                else {}
            )
        else:
            field_info = field
            if hasattr(field_info, 'json_schema_extra'):
                extra = field_info.json_schema_extra  # type: ignore
            else:
                extra = {}

        desc = extra['hint'] if extra and 'hint' in extra else None
        title: Optional[str] = field_info.title  # type: ignore
        title = title if title else keyname
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
