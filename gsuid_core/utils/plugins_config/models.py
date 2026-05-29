from typing import Dict, List, Tuple, Union, Optional

import msgspec


class GsConfig(msgspec.Struct, tag=True):
    title: str
    desc: str


class GsStrConfig(GsConfig, tag=True):
    data: str
    options: List[str] = []
    secret: bool = False


class GsBoolConfig(GsConfig, tag=True):
    data: bool
    secret: bool = False


class GsDictConfig(GsConfig, tag=True):
    data: Dict[str, List]
    secret: bool = False


class GsListStrConfig(GsConfig, tag=True):
    data: List[str]
    options: List[str] = []
    secret: bool = False


class GsListConfig(GsConfig, tag=True):
    data: List[int]
    secret: bool = False


class GsIntConfig(GsConfig, tag=True):
    data: int
    max_value: Optional[int] = None
    options: List[int] = []
    secret: bool = False


class GsFloatConfig(GsConfig, tag=True):
    data: float
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    secret: bool = False


class GsImageConfig(GsConfig, tag=True):
    data: str
    upload_to: str
    filename: str
    suffix: str = "jpg"
    secret: bool = False


class GsTimeRConfig(GsConfig, tag=True):
    data: Tuple[int, int]
    secret: bool = False


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
    GsTimeConfig,
]
