from pathlib import Path
from copy import deepcopy
from typing import Any, Dict, Type, Union

from PIL import Image, ImageDraw

from gsuid_core.models import Event
from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.database.base_models import Bind, Push, User
from gsuid_core.utils.image.image_tools import (
    get_v4_bg,
    get_v4_footer,
    get_event_avatar,
    draw_pic_with_ring,
)

TEXT_PATH = Path(__file__).parent / 'texture2d'

status_off = Image.open(TEXT_PATH / 'status_off.png')
status_on = Image.open(TEXT_PATH / 'status_on.png')
BLACK = (10, 10, 10)
GREY = (57, 57, 57)

EN_MAP = {'coin': '宝钱', 'resin': '体力', 'go': '派遣', 'transform': '质变仪'}

T_MODEL = Union[Type[User], Type[Bind], Type[Push]]


def get_class_name(cls: T_MODEL):
    module = cls.__module__
    if 'plugins' in module:
        module_name = module.split('.')[1]
    else:
        module_name = 'SayuCore'
    return module_name


def get_keys(model: T_MODEL):
    keys = {}
    fields = model.__fields__
    # 获取模型全部的键，并判断哪些需要显示
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

        # 拿到键标题
        title = field_info.title  # type: ignore
        desc = extra['hint'] if extra and 'hint' in extra else '未提供'
        # 拿到键类型
        # type_ = user_field.type_
        title = title if title else keyname
        if (
            not keyname.endswith(('uid', '_value', '_is_push', 'region'))
            and keyname != 'id'
            and keyname != 'user_id'
            and keyname != 'bot_id'
            and keyname != 'status'
            and keyname != 'fp'
            and keyname != 'mys_id'
            and keyname != 'device_id'
        ):
            keys[keyname] = {'title': title, 'desc': desc}
    return keys


def get_status_bool(data: Any):
    if isinstance(data, bool):
        return data
    elif data is None:
        return False
    elif isinstance(data, str):
        if data == 'off':
            return False
        else:
            return True
    else:
        return bool(data)


async def get_user_card(bot_id: str, ev: Event) -> Union[bytes, str]:
    module_h = 80
    _line = 117
    id_line = 90
    ez = 20
    w, h = 1200, 600 - 90

    user_id = ev.user_id
    all_bind_model = {get_class_name(i): i for i in Bind.__subclasses__()}
    all_user_model = {get_class_name(i): i for i in User.__subclasses__()}
    all_push_model = {get_class_name(i): i for i in Push.__subclasses__()}

    all_plugin_data: Dict[
        str, Dict[str, Dict[str, Dict[str, Dict[str, Any]]]]
    ] = {}

    for name in all_bind_model:
        user_keys: Dict[str, Dict[str, str]] = {}
        push_keys: Dict[str, Dict[str, str]] = {}

        bind_model = all_bind_model[name]

        # 先判断该绑定模型是否存在相应用户模型
        if name in all_user_model:
            user_model = all_user_model[name]
            user_keys = get_keys(user_model)
        # 先判断该绑定模型是否存在相应推送模型
        if name in all_push_model:
            push_model = all_push_model[name]
            push_keys = get_keys(push_model)

        UD = {}

        bind_fields = bind_model.__fields__
        for keyname in bind_fields:
            model_field = bind_fields[keyname]
            if hasattr(model_field, 'field_info'):
                field_info = model_field.field_info  # type: ignore
            else:
                field_info = model_field

            UID_NAME = field_info.title  # type: ignore
            if keyname.endswith('uid'):
                if keyname == 'uid':
                    game_name = None
                else:
                    game_name = keyname.replace('_uid', '')

                uid_list = await bind_model.get_uid_list_by_game(
                    user_id,
                    bot_id,
                    game_name,
                )

                if uid_list:
                    for uid in uid_list:
                        all_data = {}
                        union_id = -1

                        if user_model:
                            user_data = {}
                            user = await user_model.select_data_by_uid(
                                uid,
                                game_name,
                            )
                            if user_keys:
                                if not user:
                                    user = user_model(
                                        bot_id=bot_id,
                                        user_id=user_id,
                                    )
                                union_id = user.id
                                for _keyname in user_keys:
                                    data = getattr(user, _keyname)
                                    user_keyvalue = user_keys[_keyname]
                                    user_data[user_keyvalue['title']] = {
                                        'status': get_status_bool(data),
                                        'data': data,
                                        'hint': user_keyvalue['desc'],
                                    }
                            all_data.update(user_data)

                        if push_model:
                            push_data = {}
                            try:
                                push = await push_model.select_data_by_uid(
                                    uid,
                                    game_name,
                                )
                            except AttributeError:
                                push = push_model(
                                    bot_id=bot_id,
                                )
                            if push_keys:
                                if not push:
                                    push = push_model(
                                        bot_id=bot_id,
                                    )
                                for _keyname in push_keys:
                                    data = getattr(push, _keyname)
                                    push_keyvalue = push_keys[_keyname]
                                    push_data[push_keyvalue['title']] = {
                                        'status': get_status_bool(data),
                                        'data': data,
                                        'hint': push_keyvalue['desc'],
                                    }
                            all_data.update(push_data)

                        if union_id not in UD:
                            h += (
                                (((len(all_data) - 1) // 3) + 1) * (_line + ez)
                                + id_line
                                + ez
                            )
                            UD[union_id] = {f'{UID_NAME} {uid}': all_data}
                        else:
                            h += id_line
                            UD[union_id][f'{UID_NAME} {uid}'] = all_data

        all_plugin_data[name] = deepcopy(UD)

    all_plugin: Dict[str, Dict[str, Dict[str, Dict[str, Dict[str, Any]]]]] = {}

    # 遍历字典中的所有项
    for key, value in all_plugin_data.items():
        if value is not None and value != {}:
            all_plugin[key] = value

    h += len(all_plugin_data) * 80

    # 开始绘图
    img = get_v4_bg(w, h, is_blur=True)

    char_pic = await draw_pic_with_ring(await get_event_avatar(ev), 377)
    title = Image.open(TEXT_PATH / 'user_title.png')
    title.paste(char_pic, (411, 46), char_pic)

    title_draw = ImageDraw.Draw(title)
    title_draw.text(
        (600, 486),
        '绑定信息',
        BLACK,
        core_font(34),
        'mm',
    )

    title_draw.text(
        (600, 565),
        f'{bot_id} - {user_id}',
        BLACK,
        core_font(34),
        'mm',
    )
    img.paste(title, (0, 0), title)

    _h = 600
    for pulgin_name in all_plugin:
        plugin_data = all_plugin[pulgin_name]
        bar = Image.open(TEXT_PATH / 'bar.png')
        bar_draw = ImageDraw.Draw(bar)
        bar_draw.text(
            (121, 40),
            pulgin_name,
            (240, 240, 240),
            core_font(50),
            'lm',
        )
        img.paste(bar, (0, _h), bar)
        _h += module_h + ez

        for _id in plugin_data:
            _uid_list = plugin_data[_id]
            _uid_len = len(_uid_list)

            _id_data = list(_uid_list.values())[0]
            _data_h = (
                (((len(_id_data) - 1) // 3) + 1) * _line
                + id_line * _uid_len
                + 20
            )

            temp_img = Image.new('RGBA', img.size)
            temp_draw = ImageDraw.Draw(temp_img)
            temp_draw.rounded_rectangle(
                (60, _h, 1140, _h + _data_h),
                25,
                (255, 255, 255, 60),
            )

            for uid_index, _uid in enumerate(_uid_list):
                offset = uid_index * id_line - 60
                f_o = 50
                temp_draw.text(
                    (134, _h + 68 + f_o + offset),
                    _uid,
                    BLACK,
                    core_font(40),
                    'lm',
                )
                temp_draw.rounded_rectangle(
                    (102, _h + 48 + f_o + offset, 112, _h + 88 + f_o + offset),
                    10,
                    (106, 208, 71),
                )

            img.alpha_composite(temp_img)

            for index, title in enumerate(_id_data):
                data_dict = _id_data[title]

                data_status = data_dict['status']
                # data_data = data_dict['data']
                data_hint = data_dict['hint']

                status_pic = Image.open(TEXT_PATH / 'status.png')
                status_draw = ImageDraw.Draw(status_pic)

                status_draw.text((45, 71), title, BLACK, core_font(30), 'lm')
                status_draw.text(
                    (45, 103), data_hint, GREY, core_font(22), 'lm'
                )

                pic = status_on if data_status else status_off
                status_pic.paste(pic, (212, 45), pic)
                img.paste(
                    status_pic,
                    (
                        78 + (index % 3) * 343,
                        _h + _uid_len * id_line - 30 + _line * (index // 3),
                    ),
                    status_pic,
                )

            _h += _data_h + ez

        _h += ez

    footer = get_v4_footer()
    img.paste(footer, (0, h - 50), footer)

    return await convert_img(img)
