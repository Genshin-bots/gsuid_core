from typing import List, Literal, Optional

from msgspec import Struct


class Button(Struct):
    text: str
    data: str  # 具体数据
    pressed_text: Optional[str] = None  # 按下之后显示的值
    style: Literal[0, 1] = 1  # 0灰色线框，1蓝色线框
    action: Literal[0, 1, 2] = 2  # 0跳转按钮，1回调按钮，2命令按钮
    permisson: Literal[0, 1, 2, 3] = 2  # 0指定用户，1管理者，2所有人可按，3指定身份组
    specify_role_ids: List[str] = []  # 仅限频道可用
    specify_user_ids: List[str] = []  # 指定用户
    unsupport_tips: str = '您的客户端暂不支持该功能, 请升级后适配...'
