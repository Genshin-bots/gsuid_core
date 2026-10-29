import datetime
from typing import Dict, List, Tuple, Union, Optional, TypedDict
from typing_extensions import deprecated

import msgspec


class GsConfig(msgspec.Struct, tag=True):
    """基类, 禁止直接使用"""

    title: str
    desc: str


class GsStrConfig(GsConfig, tag=True):
    """字符串配置"""

    data: str
    options: List[str] = []
    regex: Optional[str] = None
    """仅用于前端正则校验的正则表达式"""
    details: Optional[dict] = None
    secret: bool = False


class GsBoolConfig(GsConfig, tag=True):
    """布尔开关配置"""

    data: bool
    secret: bool = False


class GsDictConfig(GsConfig, tag=True):
    """字典配置"""

    data: Dict[str, List]
    secret: bool = False


class GsListStrConfig(GsConfig, tag=True):
    """字符串列表配置"""

    data: List[str]
    options: List[str] = []
    secret: bool = False


class GsListConfig(GsConfig, tag=True):
    """整数列表配置"""

    data: List[int]
    secret: bool = False


class GsIntConfig(GsConfig, tag=True):
    """整数配置"""

    data: int
    max_value: Optional[int] = None
    options: List[int] = []
    secret: bool = False


class GsFloatConfig(GsConfig, tag=True):
    """浮点数配置"""

    data: float
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    secret: bool = False


class GsImageConfig(GsConfig, tag=True):
    """图片配置

    upload_to: 通过 get_res_path() 获取的绝对目录路径，不可写相对路径或跨插件路径
    """

    data: str
    upload_to: str
    filename: str
    suffix: str = "jpg"
    secret: bool = False


class GsTimeRConfig(GsConfig, tag=True):
    """时间点配置, data为时、分"""

    data: Tuple[int, int]
    secret: bool = False


class GsDivider(GsConfig, tag=True):
    """无实际作用, 在前端中用于分割配置, 便于用户UX"""

    data: Optional[str] = None
    """分割线标题, 为None时前端仅渲染分割线, 非None时渲染带标题的分割线"""


class GsFileUploadConfig(GsConfig, tag=True):
    """文件上传配置

    upload_to: 通过 get_res_path() 获取的绝对目录路径，不可写相对路径或跨插件路径
    """

    data: str
    upload_to: str
    filename: str
    suffix: str = "html"
    secret: bool = False


class GsFilesUploadConfig(GsConfig, tag=True):
    """批量文件上传配置

    data: 通过 get_res_path() 获取的绝对目录路径，同时也是上传目标目录，
          不可写相对路径或跨插件路径
    """

    data: str
    suffix: str = "html"
    secret: bool = False


class GsDateConfig(GsConfig, tag=True):
    """日期配置 (如 YYYY-MM-DD)"""

    data: datetime.date
    secret: bool = False


class GsTimeRangeConfig(GsConfig, tag=True):
    """时间范围配置 (如 允许访问的时间段: 08:00 - 20:00)"""

    data: Tuple[Tuple[int, int], Tuple[int, int]]
    secret: bool = False


class GsColorConfig(GsConfig, tag=True):
    """颜色配置 (HEX格式如 #FFFFFF 或 RGBA)"""

    data: str


class ButtonStatusField(msgspec.Struct):
    """按钮旁展示的一行状态。key 由回调写入，label 是界面文案。"""

    key: str
    label: str


class ButtonState(msgspec.Struct):
    """按钮最近一次执行结果。写进配置文件，下次打开页面直接显示。"""

    phase: str = "idle"
    progress: int = 0
    message: str = ""
    fields: Dict[str, str] = msgspec.field(default_factory=dict)


class ButtonStateView(TypedDict):
    phase: str
    progress: int
    message: str
    fields: Dict[str, str]


def button_state_view(state: ButtonState) -> ButtonStateView:
    return {
        "phase": state.phase,
        "progress": state.progress,
        "message": state.message,
        "fields": dict(state.fields),
    }


class GsButtonConfig(GsConfig, tag=True):
    """按钮回调。

    endpoint 是插件声明的 API 路径，必须以 /api/ 开头。
    框架把 POST/GET 挂到这条路径上，并且只允许管理员调用。
    data 是最近一次进度和结果，不是给用户在表单里编辑的值。
    """

    data: ButtonState
    endpoint: str
    label: str
    level: str = "primary"
    confirm: str = ""
    status_fields: List[ButtonStatusField] = msgspec.field(default_factory=list)
    show_progress: bool = True
    poll_ms: int = 800


class GsRepeatGroupConfig(GsConfig, tag=True):
    """可重复配置组：data 每项是一组子配置(Dict[str, GSC], 与整份插件配置同构),
    template 是新增一项的原型/默认。可再嵌套本类型。前端复用逐项渲染器递归渲染。
    """

    # "GSC" 为字符串前向引用: GSC 在本类之后定义, msgspec 加载时惰性解析。
    data: List[Dict[str, "GSC"]]
    template: Dict[str, "GSC"]
    secret: bool = False


@deprecated("GsTimeConfig 已废弃，请使用 GsTimeRConfig 代替")
class GsTimeConfig(GsConfig, tag=True):
    """deprecated/已废弃"""

    data: str
    secret: bool = False


GSC = Union[
    GsDictConfig,
    GsBoolConfig,
    GsListConfig,
    GsListStrConfig,
    GsStrConfig,
    GsIntConfig,
    GsFloatConfig,
    GsImageConfig,
    GsTimeRConfig,
    GsDivider,
    GsFileUploadConfig,
    GsFilesUploadConfig,
    GsDateConfig,
    GsTimeRangeConfig,
    GsColorConfig,
    GsButtonConfig,
    GsRepeatGroupConfig,
    GsTimeConfig,
]
