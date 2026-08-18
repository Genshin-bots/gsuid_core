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
    GsRepeatGroupConfig,  # 可重复配置组 (data 为记录列表, 需指定 template 原型)
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
| `GsRepeatGroupConfig` | 可重复配置组 | `List[Dict[str, GSC]]` | `template: Dict[str, GSC]`, `secret` |

> 所有配置类型继承自 `GsConfig(msgspec.Struct)`，必须包含 `title`、`desc` 字段。除 `GsDivider` 和 `GsColorConfig` 外，均支持 `secret: bool` 字段用于敏感信息脱敏。

## 4.6 可重复配置组 `GsRepeatGroupConfig`

用于「同一组字段可重复出现任意多次」的场景，如多个 Webhook 通知目标、多条定时推送规则、多个账号等。前端渲染为一个可**增删条目**的列表，每个条目内部再递归渲染 `template` 定义的子配置。

- `template: Dict[str, GSC]`：一条记录的字段原型 / 默认值，**由代码所有**；用户新增一条时以它为模板。
- `data: List[Dict[str, GSC]]`：用户实际填写的每条记录，每条都与 `template` 同构。
- 可嵌套：`template` 里的某个字段本身也能是 `GsRepeatGroupConfig`，实现「组里再套组」。

### 定义

```python
from typing import Dict
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsStrConfig,
    GsBoolConfig,
    GsRepeatGroupConfig,
)

CONFIG_DEFAULT: Dict[str, GSC] = {
    "webhooks": GsRepeatGroupConfig(
        title="Webhook 通知目标",
        desc="可添加多个通知目标，逐条推送",
        data=[],  # 初始无记录，让用户在前端自行新增
        template={
            "name": GsStrConfig(title="名称", desc="备注名", data=""),
            "url": GsStrConfig(title="地址", desc="Webhook URL", data=""),
            "enable": GsBoolConfig(title="启用", desc="是否推送到此目标", data=True),
        },
    ),
}
```

> `template` 里每个字段就是普通 GSC，可用任意配置类型（含 `GsDivider` 分割线）。`data` 通常初始化为 `[]`。

### 读取

`data` 是**记录列表**，每条记录是「字段名 → GSC 对象」的字典，读取值仍需 `.data`：

```python
group = my_config.get_config("webhooks")   # GsRepeatGroupConfig
for item in group.data:                     # item: Dict[str, GSC]
    name: str = item["name"].data
    url: str = item["url"].data
    if item["enable"].data:                 # bool
        await push(url, ...)
```

### 修改

`set_config` 接收**原始值列表**——每条是「字段名 → 原始值」的字典（**不是 GSC 对象**）。核心会用 `template` 自动重建成完整结构并持久化：

```python
my_config.set_config("webhooks", [
    {"name": "群通知", "url": "https://a.com/hook", "enable": True},
    {"name": "备用",   "url": "https://b.com/hook", "enable": False},
])
```

> - 传入 dict 里的字段值会直接写入对应子配置的 `.data`，因此值类型要与该字段的 `data` 类型一致（布尔字段传 `bool` 等）。缺失字段用 `template` 默认值补齐，多余的键忽略，非 dict 项被丢弃。
> - 与顶层 `set_config` 不同，组内叶子字段**不做类型转换**（不会像 `GsDateConfig` 那样把字符串自动转 `date`）。若组内用到 `GsTimeRConfig` / `GsDateConfig` 等，请传入其 `.data` 对应的原生形式。

### 嵌套组

`template` 里的字段可再是 `GsRepeatGroupConfig`，读取 / 修改按相同规则递归——嵌套字段的值同样是**记录列表**：

```python
"rules": GsRepeatGroupConfig(
    title="推送规则", desc="每个规则可绑定多个时间点",
    data=[],
    template={
        "keyword": GsStrConfig(title="关键词", desc="", data=""),
        "times": GsRepeatGroupConfig(
            title="时间点", desc="", data=[],
            template={"at": GsStrConfig(title="时刻", desc="HH:MM", data="")},
        ),
    },
),

# 修改：嵌套字段的值是列表
my_config.set_config("rules", [
    {"keyword": "签到", "times": [{"at": "08:00"}, {"at": "20:00"}]},
])
```

> **模板由代码所有**：每次加载配置时，核心会用代码里的 `template` 覆盖存量模板，并对用户已有的每条记录做一次「向模板对齐」——自动补齐新增字段、重置类型不符字段、保留已填数据。因此后续给 `template` 增删字段是安全的，老数据不会丢。
