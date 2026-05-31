import datetime
from typing import Dict, List, Tuple, Union, Optional
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
    GsTimeConfig,
]
