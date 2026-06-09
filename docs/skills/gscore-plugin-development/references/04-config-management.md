# 四、配置管理

## 4.1 定义插件配置项

在插件目录（如 `my_plugin/`）下创建文件夹 `my_plugin_config`，在其中创建 `config_default.py`，使用 `Dict[str, GSC]` 定义默认配置。

```python
# my_plugin/config_default.py
from typing import Dict
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsStrConfig,
    GsBoolConfig,
    GsIntConfig,
    GsListStrConfig,
)
from gsuid_core.data_store import get_res_path

CONFIG_DEFAULT: Dict[str, GSC] = {
    "api_key": GsStrConfig(
        title="API Key",
        desc="第三方服务的 API Key",
        data="",
    ),
    "max_count": GsIntConfig(
        title="最大查询数量",
        desc="单次最多返回多少条结果",
        data=10,
    ),
    "enable_cache": GsBoolConfig(
        title="启用缓存",
        desc="是否缓存查询结果",
        data=True,
    ),
    "blocked_users": GsListStrConfig(
        title="屏蔽用户列表",
        desc="不响应的用户 ID",
        data=[],
    ),
    "card_bg": GsImageConfig(
        title="卡片背景图",
        desc="用于渲染卡片的背景图片",
        data="",
        upload_to=str(get_res_path("my_plugin") / "bg"),
        filename="card_bg",
        suffix="jpg",
    ),
}
```

> **注意**：
> - 所有配置类型的字段名是 `title`、`desc`、`data`，而非示例代码中的 `title`、`description`、`default`。
> - `GsImageConfig`、`GsFileUploadConfig`、`GsFilesUploadConfig` 的 `upload_to` / `data` 字段**必须使用通过 `get_res_path()` 获取的绝对路径**，且只能指向**本插件名下的子目录**，不可写入相对路径或跨插件路径。

## 4.2 创建配置实例并注册到 Web 控制台

在插件配置文件夹（如 `my_plugin/my_plugin_config/my_plugin_config.py`）中：

- 用 `get_res_path()` 获取插件资源目录并指定配置文件路径。
- 创建 `StringConfig` 单例。

```python
# my_plugin/__init__.py
from gsuid_core.sv import SV
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.gs_config import StringConfig
from .config_default import CONFIG_DEFAULT

sv = SV("my_plugin")

# 配置文件路径
CONFIG_PATH = get_res_path() / "my_plugin" / "config.json"

# 创建配置实例（全局单例，config_name 应唯一）
my_config = StringConfig("my_plugin", CONFIG_PATH, CONFIG_DEFAULT)


```

## 4.3 读取与修改配置

在其他模块中直接导入 `my_config` 使用。

```python
from my_plugin.my_plugin_config.my_plugin_config import my_config

# 读取配置值（注意 .data）
api_key: str = my_config.get_config("api_key").data          # GsStrConfig
max_count: int = my_config.get_config("max_count").data      # GsIntConfig
enable_cache: bool = my_config.get_config("enable_cache").data  # GsBoolConfig

# 运行时修改（自动持久化到文件）
my_config.set_config("api_key", "new_key")
my_config.set_config("max_count", 20)
```

> **提示**：`set_config` 会校验类型，类型不匹配将拒绝写入并打印警告。

## 4.4 完整示例目录结构

```
my_plugin/
├── __init__.py          # 包含 my_config 实例和 __plugin_config_class__
├── config_default.py    # 配置项定义
├── ...
```

## 4.5 所有可用配置类型

从 `gsuid_core.utils.plugins_config.models` 导入：

```python
from gsuid_core.utils.plugins_config.models import (
    GsStrConfig,          # 字符串 (支持 options 可选值、regex 前端正则校验)
    GsBoolConfig,         # 布尔开关
    GsIntConfig,          # 整数 (可限制 max_value，支持 options)
    GsFloatConfig,        # 浮点数 (可限制 min_value / max_value)
    GsListStrConfig,      # 字符串列表 (支持 options)
    GsListConfig,         # 整数列表
    GsDictConfig,         # 字典 (Dict[str, List])
    GsImageConfig,        # 图片配置 (上传相关，需指定 upload_to / filename / suffix)
    GsTimeRConfig,        # 时间点配置 (时:分，定时任务相关)
    GsDivider,            # 分割线 (data 为可选标题, 仅前端展示用)
    GsFileUploadConfig,   # 文件上传 (需指定 upload_to / filename / suffix)
    GsFilesUploadConfig,  # 批量文件上传 (data 即为上传目录，需指定 suffix)
    GsDateConfig,         # 日期配置 (YYYY-MM-DD)
    GsTimeRangeConfig,    # 时间范围配置 (如 08:00-20:00)
    GsColorConfig,        # 颜色配置 (HEX 如 #FFFFFF 或 RGBA)
)
# 联合类型 GSC = Union[上述所有类型]
# ⚠️ GsTimeConfig 已废弃，请使用 GsTimeRConfig 代替
```

> **配置类型一览**：

| 类型 | 说明 | `data` 类型 | 特有字段 |
|------|------|------------|---------|
| `GsStrConfig` | 字符串配置 | `str` | `options: List[str]`, `regex: Optional[str]`, `secret` |
| `GsBoolConfig` | 布尔配置 | `bool` | `secret` |
| `GsIntConfig` | 整数配置 | `int` | `max_value: Optional[int]`, `options: List[int]`, `secret` |
| `GsFloatConfig` | 浮点数配置 | `float` | `min_value: Optional[float]`, `max_value: Optional[float]`, `secret` |
| `GsListStrConfig` | 字符串列表 | `List[str]` | `options: List[str]`, `secret` |
| `GsListConfig` | 整数列表 | `List[int]` | `secret` |
| `GsDictConfig` | 字典配置 | `Dict[str, List]` | `secret` |
| `GsImageConfig` | 图片配置 | `str` | `upload_to`, `filename`, `suffix`, `secret` |
| `GsTimeRConfig` | 时间点 | `Tuple[int, int]` | `secret` |
| `GsDivider` | 分割线 | `Optional[str]` | `data` (分割线标题, None=无标题) |
| `GsFileUploadConfig` | 文件上传 | `str` | `upload_to`, `filename`, `suffix`, `secret` |
| `GsFilesUploadConfig` | 批量文件上传 | `str` | `suffix`, `secret` |

> **路径安全约定**：`GsImageConfig.upload_to`、`GsFileUploadConfig.upload_to` 和 `GsFilesUploadConfig.data` 均表示**绝对文件(夹)路径**。默认值必须通过 `str(get_res_path("插件目录名") / "子目录")` 生成，确保文件落在 `data/插件目录/` 下，禁止跨插件或向任意目录写入。
| `GsDateConfig` | 日期 | `datetime.date` | `secret` |
| `GsTimeRangeConfig` | 时间范围 | `Tuple[Tuple[int,int], Tuple[int,int]]` | `secret` |
| `GsColorConfig` | 颜色 | `str` | — |

> 所有配置类型继承自 `GsConfig(msgspec.Struct)`，必须包含 `title`、`desc` 字段。除 `GsDivider` 和 `GsColorConfig` 外，均支持 `secret: bool` 字段用于敏感信息脱敏。
