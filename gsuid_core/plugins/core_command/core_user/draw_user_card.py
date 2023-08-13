from pathlib import Path
from typing import Tuple, Union, Optional

from PIL import Image, ImageDraw

from gsuid_core.utils.database.api import DBSqla
from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.database.models import GsPush
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.image.image_tools import (
    get_color_bg,
    get_qq_avatar,
    draw_pic_with_ring,
    easy_alpha_composite,
)

TEXT_PATH = Path(__file__).parent / 'texture2d'

status_off = Image.open(TEXT_PATH / 'status_off.png')
status_on = Image.open(TEXT_PATH / 'status_on.png')

EN_MAP = {'coin': '宝钱', 'resin': '体力', 'go': '派遣', 'transform': '质变仪'}


async def get_user_card(bot_id: str, user_id: str) -> Union[bytes, str]:
    get_sqla = DBSqla().get_sqla
    sqla = get_sqla(bot_id)
    uid_list = await sqla.get_bind_uid_list(user_id)
    sr_uid_list = await sqla.get_bind_sruid_list(user_id)
    user_list = await sqla.select_user_all_data_by_user_id(user_id)

    if user_list is None:
        return '你还没有绑定过UID和CK!\n(该功能须同时绑定CK和UID才能使用)'

    if uid_list is None:
        uid_list = []
    if sr_uid_list is None:
        sr_uid_list = []

    max_len = max(uid_list, sr_uid_list)
    w, h = 750, len(max_len) * 900 + 470

    # 获取背景图片各项参数
    _id = str(user_id)
    if _id.startswith('http'):
        char_pic = await get_qq_avatar(avatar_url=_id)
    else:
        char_pic = await get_qq_avatar(qid=_id)
    char_pic = await draw_pic_with_ring(char_pic, 290)

    img = await get_color_bg(w, h)
    img_mask = Image.new('RGBA', img.size, (255, 255, 255))
    title = Image.open(TEXT_PATH / 'user_title.png')
    title.paste(char_pic, (241, 40), char_pic)

    title_draw = ImageDraw.Draw(title)
    title_draw.text(
        (375, 444), f'{bot_id} - {user_id}', (29, 29, 29), core_font(30), 'mm'
    )
    img.paste(title, (0, 0), title)

    for index, user_data in enumerate(user_list):
        user_card = Image.open(TEXT_PATH / 'user_bg.png')
        user_draw = ImageDraw.Draw(user_card)

        if user_data.uid is not None and user_data.uid != '0':
            uid_text = f'原神UID {user_data.uid}'
            user_push_data = await sqla.select_push_data(user_data.uid)
            if user_push_data is None:
                await sqla.insert_push_data(user_data.uid)
                user_push_data = await sqla.select_push_data(user_data.uid)
        else:
            uid_text = '未发现原神UID'
            user_push_data = GsPush(bot_id='TEMP')

        user_draw.text(
            (375, 58),
            uid_text,
            (29, 29, 29),
            font=core_font(36),
            anchor='mm',
        )

        if user_data.sr_uid:
            sruid_text = f'星铁UID {user_data.sr_uid}'
        else:
            sruid_text = '未发现星铁UID'

        user_draw.text(
            (375, 119),
            sruid_text,
            (29, 29, 29),
            font=core_font(36),
            anchor='mm',
        )

        x, y = 331, 112
        b = 175
        paste_switch(user_card, user_data.cookie, (241, b))
        paste_switch(user_card, user_data.stoken, (241 + x, b))
        paste_switch(user_card, user_data.sign_switch, (241, b + y))
        paste_switch(user_card, user_data.bbs_switch, (241 + x, b + y))
        paste_switch(user_card, user_data.push_switch, (241, b + 2 * y))
        paste_switch(user_card, user_data.status, (241 + x, b + 2 * y), True)

        for _index, mode in enumerate(['coin', 'resin', 'go', 'transform']):
            paste_switch(
                user_card,
                getattr(user_push_data, f'{mode}_push'),
                (241 + _index % 2 * x, b + (_index // 2 + 3) * y),
            )
            if getattr(user_push_data, f'{mode}_push') != 'off':
                user_draw.text(
                    (268 + _index % 2 * x, 168 + 47 + (_index // 2 + 3) * y),
                    f'{getattr(user_push_data, f"{mode}_value")}',
                    (35, 35, 35),
                    font=core_font(15),
                    anchor='lm',
                )

        sr_sign = user_data.sr_sign_switch
        sr_push = user_data.sr_push_switch
        paste_switch(user_card, sr_sign, (241, b + 5 * y))
        paste_switch(user_card, sr_push, (241 + x, b + 5 * y))

        img.paste(user_card, (0, 500 + index * 870), user_card)

    img = easy_alpha_composite(img_mask, img, (0, 0))
    return await convert_img(img)


def paste_switch(
    card: Image.Image,
    status: Optional[str],
    pos: Tuple[int, int],
    is_status: bool = False,
):
    if is_status:
        pic = status_off if status else status_on
    else:
        pic = status_on if status != 'off' and status else status_off
    card.paste(pic, pos, pic)
