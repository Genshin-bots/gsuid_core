from typing import List, Union, Literal, TypeVar, Optional

from msgspec import Struct


class Button(Struct):
    text: str
    data: str  # 具体数据
    pressed_text: Optional[str] = None  # 按下之后显示的值
    style: Literal[0, 1] = 1  # 0灰色线框，1蓝色线框
    action: Literal[
        -1, 0, 1, 2
    ] = -1  # 0跳转按钮，1回调按钮，2命令按钮, 【-1自适应】
    permisson: Literal[0, 1, 2, 3] = (
        2  # 0指定用户，1管理者，2所有人可按，3指定身份组
    )
    specify_role_ids: List[str] = []  # 仅限频道可用
    specify_user_ids: List[str] = []  # 指定用户
    unsupport_tips: str = "您的客户端暂不支持该功能, 请升级后适配..."
    prefix: str = ""  # 命令前缀, 使用时请直接继承该类重写该值, 避免重复定义
    _edited: bool = False  # 是否编辑过


T = TypeVar("T", bound=Button)

ButtonType = Union[List[str], List[T], List[List[str]], List[List[T]]]
ButtonList = Union[List[T], List[List[T]]]
