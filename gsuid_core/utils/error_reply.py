from pathlib import Path
from typing import Dict, Union, Optional

from PIL import Image, ImageDraw

from gsuid_core.handler import command_start
from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.plugins_config.gs_config import send_security_config
from gsuid_core.utils.image.image_tools import (
    get_color_bg,
    draw_center_text_by_line,
)

if command_start and command_start[0]:
    _start = command_start[0]
else:
    _start = ''

UID_HINT = f'你还没有绑定过uid哦!\n请使用[{_start}绑定uid123456]命令绑定!'

MYS_HINT = f'你还没有绑定过mysid哦!\n请使用[{_start}绑定mys1234]命令绑定!'

CK_HINT = f"""你还没有绑定过Cookie哦!发送【{_start}ck帮助】获取帮助!
警告:绑定Cookie可能会带来未知的账号风险,请确保信任机器人管理员"""

_CHAR_HINT = f'请先使用【{_start}强制刷新】命令来缓存数据! \n或者使用【{_start}查询展柜角色】命令查看已缓存角色！'

CHAR_HINT = '你还没有{}的缓存噢！\n' + _CHAR_HINT

VERIFY_HINT = f'''出现验证码!
如已绑定CK: 请至米游社软件->我的->我的角色处解锁验证码
（可使用[{_start}gs关闭推送]命令关闭体力推送以减少出现验证码风险）
如未绑定CK: 可联系管理员使用[{_start}gs清除缓存]命令
'''

SK_HINT = (
    '你还没有绑定过Stoken或者Stoken已失效~\n'
    f'请加好友私聊Bot\n [{_start}扫码登陆] 或 [{_start}添加]后跟SK格式 以绑定SK'
)

UPDATE_HINT = f'''更新失败!更多错误信息请查看控制台...
>> 可以尝试使用
>> [{_start}gs强制更新](危险)
>> [{_start}gs强行强制更新](超级危险)!'''

error_dict = {
    -51: CK_HINT,
    -100: '您的cookie已经失效, 请重新获取!',
    -503: CK_HINT,
    10001: '您的cookie已经失效, 请重新获取!',
    10101: '当前查询CK已超过每日30次上限!',
    10102: '当前查询id已经设置了隐私, 无法查询!',
    1034: VERIFY_HINT,
    -10001: '请求体出错, 请检查具体实现代码...',
    -512009: '[留影叙佳期]已经获取过该内容~!',
    -201: '你的账号可能已被封禁, 请联系米游社客服...',
    -501101: '当前角色冒险等阶未达到10级, 暂时无法参加此活动...',
    400: '[MINIGG]暂未找到此内容...',
    -400: '请输入更详细的名称...',
    1008: '该API需要CK, 查询的用户/UID未绑定CK...',
    10104: 'CK与用户信息不符, 请检查代码实现...',
    -999: VERIFY_HINT,
    125: '该充值方式暂时不可用!',
    126: '该充值方式不正确!',
}

TEXT_PATH = Path(__file__).parent / 'image' / 'texture2d'
is_pic_error = send_security_config.get_config('ChangeErrorToPic').data


def get_error(
    retcode: Union[int, str], message: Union[str, Dict, None] = None
) -> str:
    if isinstance(message, str):
        _msg = message
    elif isinstance(message, Dict):
        if 'message' in message:
            _msg = message['message']
        elif 'msg' in message:
            _msg = message['msg']
        else:
            _msg = '无可用信息。'
    else:
        _msg = '无可用信息。'
    return error_dict.get(
        int(retcode), f'未知错误, 错误码为{retcode}!\n可能的错误消息: {_msg}'
    )


def get_error_type(retcode: Union[int, str]) -> str:
    retcode = int(retcode)
    if retcode in [-51, 10104]:
        return '绑定信息错误'
    elif retcode in [-400, 400]:
        return 'MGGApi错误'
    else:
        return 'Api错误'


async def get_error_img(
    retcode: Union[int, str],
    force_image: bool = False,
    message: Union[str, Dict, None] = None,
) -> Union[bytes, str]:
    error_message = get_error(retcode, message)
    if is_pic_error or force_image:
        error_type = get_error_type(retcode)
        return await draw_error_img(retcode, error_message, error_type)
    else:
        return error_message


async def draw_error_img(
    retcode: Union[int, str] = 51233,
    error_message: Optional[str] = None,
    error_type: Optional[str] = None,
) -> bytes:
    if error_type is None:
        error_type = 'API报错'
    if error_message is None:
        error_message = '未知错误, 请检查控制台输出...'

    error_img = Image.open(TEXT_PATH / 'error_img.png')
    img = await get_color_bg(
        *error_img.size, is_full=True, color=(228, 222, 210)
    )
    img.paste(error_img, (0, 0), error_img)
    img_draw = ImageDraw.Draw(img)
    img_draw.text((350, 646), error_type, 'white', core_font(26), 'mm')
    img_draw.text(
        (350, 695), f'错误码 {retcode}', 'white', core_font(36), 'mm'
    )
    draw_center_text_by_line(
        img_draw, (350, 750), error_message, core_font(30), 'black', 440
    )
    return await convert_img(img)
