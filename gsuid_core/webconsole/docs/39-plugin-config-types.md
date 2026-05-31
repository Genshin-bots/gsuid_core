# 39. 插件配置类型参考 - /api/plugins/{plugin_name}/config

本文档详细说明插件配置 API 的请求体与返回体结构，以及所有可用的配置类型。

---

## 39.1 获取插件配置

```
GET /api/plugins/{plugin_name}/config
```

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `plugin_name` | string | ✅ | 插件名称 |

**响应**：

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "config_key_1": {
            "value": "...",
            "default": "...",
            "type": "gsstr",
            "title": "配置项标题",
            "desc": "配置项描述"
        },
        "config_key_2": {
            "value": true,
            "default": true,
            "type": "gsbool",
            "title": "开关配置",
            "desc": "描述"
        }
    }
}
```

> **注意**：返回的 `data` 是一个扁平的键值对结构，键为配置项名称，值为配置项详情对象。如果插件有多个配置组，可通过 `GET /api/plugins/{plugin_name}` 返回的 `config_groups` 获取分组结构。

---

## 39.2 保存插件配置

```
POST /api/plugins/{plugin_name}/config
```

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `plugin_name` | string | ✅ | 插件名称 |

**请求体（平铺格式）**：

```json
{
    "config_key_1": "新值",
    "config_key_2": true
}
```

**请求体（config_groups 格式）**：

```json
{
    "config_groups": [
        {
            "config_name": "MyPlugin配置组名",
            "config": {
                "config_key_1": "新值",
                "config_key_2": true
            }
        }
    ]
}
```

**响应**：

```json
{
    "status": 0,
    "msg": "配置已保存"
}
```

**失败响应**：

```json
{
    "status": 1,
    "msg": "未找到可更新的配置项"
}
```

---

## 39.3 更新单个配置项

```
POST /api/plugins/{plugin_name}/config/{config_name}/{item_name}
```

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `plugin_name` | string | ✅ | 插件名称 |
| `config_name` | string | ✅ | 配置组名称 |
| `item_name` | string | ✅ | 配置项名称 |

**请求体**：

```json
"新值"
```

> 注意：请求体直接为配置项的值（非 JSON 对象），使用 `embed=True` 时为 `{"value": "新值"}`。

**响应**：

```json
{
    "status": 0,
    "msg": "配置项已保存"
}
```

---

## 39.4 配置类型详解

所有配置类型均继承自 `GsConfig(msgspec.Struct)`，必须包含 `title`（标题）和 `desc`（描述）字段。API 返回的 `type` 字段值为类名去掉 `Gs` 前缀和 `Config` 后缀后转小写，并加上 `gs` 前缀。

### 通用字段

每个配置项在 API 返回中都包含以下通用字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `value` | any | 当前值 |
| `default` | any | 默认值 |
| `type` | string | 配置类型标识（见下表） |
| `title` | string | 配置项标题 |
| `desc` | string | 配置项描述 |

### 类型标识对照表

| Python 类 | API type 值 | 说明 |
|-----------|-------------|------|
| `GsStrConfig` | `gsstr` | 字符串配置 |
| `GsBoolConfig` | `gsbool` | 布尔开关配置 |
| `GsIntConfig` | `gsint` | 整数配置 |
| `GsFloatConfig` | `gsfloat` | 浮点数配置 |
| `GsListStrConfig` | `gsliststr` | 字符串列表配置 |
| `GsListConfig` | `gslist` | 整数列表配置 |
| `GsDictConfig` | `gsdict` | 字典配置 |
| `GsImageConfig` | `gsimage` | 图片配置 |
| `GsTimeRConfig` | `gstimer` | 时间点配置 |
| `GsDivider` | `gsdivider` | 分割线（可选标题，仅前端展示） |
| `GsFileUploadConfig` | `gsfileupload` | 文件上传配置 |
| `GsFilesUploadConfig` | `gsfilesupload` | 批量文件上传配置 |
| `GsDateConfig` | `gsdate` | 日期配置 |
| `GsTimeRangeConfig` | `gstimerange` | 时间范围配置 |
| `GsColorConfig` | `gscolor` | 颜色配置 |
| `GsTimeConfig` | `gstime` | 已废弃，请使用 GsTimeRConfig |

---

### GsStrConfig — 字符串配置

**Python 定义**：

```python
class GsStrConfig(GsConfig, tag=True):
    data: str
    options: List[str] = []
    regex: Optional[str] = None
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": "hello",
    "default": "hello",
    "type": "gsstr",
    "title": "问候语",
    "desc": "机器人发送的问候语",
    "options": ["hello", "hi", "hey"],
    "regex": "^[a-zA-Z]+$"
}
```

**额外字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `options` | `string[]` | 否 | 可选值列表，存在时前端应渲染为下拉选择 |
| `regex` | `string` | 否 | 前端正则校验表达式，用于输入校验（非后端校验） |

**保存时请求值**：`string`

---

### GsBoolConfig — 布尔开关配置

**Python 定义**：

```python
class GsBoolConfig(GsConfig, tag=True):
    data: bool
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": true,
    "default": true,
    "type": "gsbool",
    "title": "启用缓存",
    "desc": "是否缓存查询结果"
}
```

**保存时请求值**：`boolean`

---

### GsIntConfig — 整数配置

**Python 定义**：

```python
class GsIntConfig(GsConfig, tag=True):
    data: int
    max_value: Optional[int] = None
    options: List[int] = []
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": 10,
    "default": 10,
    "type": "gsint",
    "title": "最大查询数量",
    "desc": "单次最多返回多少条结果",
    "max_value": 100,
    "options": [5, 10, 20, 50]
}
```

**额外字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `max_value` | `int` | 否 | 最大值限制 |
| `options` | `int[]` | 否 | 可选值列表，存在时前端应渲染为下拉选择 |

**保存时请求值**：`int`

---

### GsFloatConfig — 浮点数配置

**Python 定义**：

```python
class GsFloatConfig(GsConfig, tag=True):
    data: float
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": 0.75,
    "default": 0.75,
    "type": "gsfloat",
    "title": "匹配阈值",
    "desc": "模糊匹配的最低相似度",
    "min_value": 0.0,
    "max_value": 1.0
}
```

**额外字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `min_value` | `float` | 否 | 最小值限制 |
| `max_value` | `float` | 否 | 最大值限制 |

**保存时请求值**：`float`

---

### GsListStrConfig — 字符串列表配置

**Python 定义**：

```python
class GsListStrConfig(GsConfig, tag=True):
    data: List[str]
    options: List[str] = []
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": ["user1", "user2"],
    "default": ["user1", "user2"],
    "type": "gsliststr",
    "title": "屏蔽用户列表",
    "desc": "不响应的用户 ID",
    "options": ["user1", "user2", "user3"]
}
```

**额外字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `options` | `string[]` | 否 | 可选值列表 |

**保存时请求值**：`string[]`

---

### GsListConfig — 整数列表配置

**Python 定义**：

```python
class GsListConfig(GsConfig, tag=True):
    data: List[int]
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": [1, 2, 3],
    "default": [1, 2, 3],
    "type": "gslist",
    "title": "允许的等级",
    "desc": "允许查询的等级列表"
}
```

**保存时请求值**：`int[]`

---

### GsDictConfig — 字典配置

**Python 定义**：

```python
class GsDictConfig(GsConfig, tag=True):
    data: Dict[str, List]
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": {"group1": ["a", "b"], "group2": ["c"]},
    "default": {"group1": ["a", "b"], "group2": ["c"]},
    "type": "gsdict",
    "title": "分组映射",
    "desc": "分组到标签的映射"
}
```

> **注意**：`GsDictConfig` 结构复杂，建议通过 WebConsole 修改，聊天命令中不支持直接设置。

**保存时请求值**：`Dict[str, List]`

---

### GsImageConfig — 图片配置

**Python 定义**：

```python
class GsImageConfig(GsConfig, tag=True):
    data: str
    upload_to: str
    filename: str
    suffix: str = "jpg"
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": "/path/to/image.jpg",
    "default": "/path/to/image.jpg",
    "type": "gsimage",
    "title": "背景图片",
    "desc": "卡片渲染的背景图",
    "upload_to": "/path/to/upload/dir",
    "filename": "bg",
    "suffix": "jpg"
}
```

**额外字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `upload_to` | `string` | ✅ | 上传目标目录的绝对路径，须通过 `get_res_path()` 获取，且只能指向本插件目录下的子文件夹 |
| `filename` | `string` | ✅ | 保存的文件名（不含后缀） |
| `suffix` | `string` | 否 | 文件后缀，默认 `"jpg"` |

> **注意**：
> - `upload_to` 必须通过 `get_res_path()` 获取绝对路径，例如 `str(get_res_path("MyPlugin") / "images")`，禁止写相对路径或跨插件路径。
> - 如果 `value` 指向的文件不存在，API 会返回空字符串 `""`。建议通过 WebConsole 上传修改。

**保存时请求值**：`string`（文件路径）

---

### GsTimeRConfig — 时间点配置

**Python 定义**：

```python
class GsTimeRConfig(GsConfig, tag=True):
    data: Tuple[int, int]
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": [8, 30],
    "default": [8, 30],
    "type": "gstimer",
    "title": "定时推送时间",
    "desc": "每天推送数据的时间（时:分）"
}
```

**`value` 格式**：`[hour, minute]`，其中 `hour` 范围 0-23，`minute` 范围 0-59。

**保存时请求值**：`[int, int]`（如 `[8, 30]`）

**聊天命令格式**：`HH:MM`（如 `08:30`）

---

### GsDivider — 分割线配置

**Python 定义**：

```python
class GsDivider(GsConfig, tag=True):
    data: Optional[str] = None
    """分割线标题, 为None时前端仅渲染分割线, 非None时渲染带标题的分割线"""
```

**API 返回示例（无标题分割线，data=None）**：

```json
{
    "type": "gsdivider",
    "title": "高级设置",
    "desc": "以下为高级配置项"
}
```

**API 返回示例（带标题分割线，data非空）**：

```json
{
    "value": "高级选项",
    "default": "高级选项",
    "type": "gsdivider",
    "title": "高级设置",
    "desc": "以下为高级配置项"
}
```

**额外字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `data` | `string` | 否 | 分割线标题，为 `null` 时前端仅渲染分割线，非 `null` 时渲染带标题的分割线 |

> **注意**：`GsDivider` 主要用于前端渲染分割线以优化用户体验。当 `data` 为 `None` 时，API 不返回 `value`/`default` 字段；当 `data` 非空时，返回 `value` 和 `default` 字段作为分割线标题。不可通过聊天命令设置值。

---

### GsFileUploadConfig — 文件上传配置

**Python 定义**：

```python
class GsFileUploadConfig(GsConfig, tag=True):
    data: str
    upload_to: str
    filename: str
    suffix: str = "html"
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": "/path/to/template.html",
    "default": "/path/to/template.html",
    "type": "gsfileupload",
    "title": "自定义模板",
    "desc": "上传自定义渲染模板",
    "upload_to": "/path/to/upload/dir",
    "filename": "custom_template",
    "suffix": "html"
}
```

**额外字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `upload_to` | `string` | ✅ | 上传目标目录的绝对路径，须通过 `get_res_path()` 获取，且只能指向本插件目录下的子文件夹 |
| `filename` | `string` | ✅ | 保存的文件名（不含后缀） |
| `suffix` | `string` | 否 | 文件后缀，默认 `"html"` |

> **注意**：`upload_to` 必须通过 `get_res_path()` 获取绝对路径，禁止写相对路径或跨插件路径。建议通过 WebConsole 上传修改，聊天命令中不支持直接设置。

**保存时请求值**：`string`（文件路径）

---

### GsFilesUploadConfig — 批量文件上传配置

**Python 定义**：

```python
class GsFilesUploadConfig(GsConfig, tag=True):
    data: str
    suffix: str = "html"
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": "/path/to/templates/dir",
    "default": "/path/to/templates/dir",
    "type": "gsfilesupload",
    "title": "模板目录",
    "desc": "批量上传模板文件的目录",
    "suffix": "html"
}
```

**额外字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `suffix` | `string` | 否 | 允许的文件后缀，默认 `"html"` |

> **注意**：`data` 即批量上传目标目录的绝对路径，须通过 `get_res_path()` 获取，且只能指向本插件目录下的子文件夹。建议通过 WebConsole 上传修改，聊天命令中不支持直接设置。

**保存时请求值**：`string`（目录路径）

---

### GsDateConfig — 日期配置

**Python 定义**：

```python
class GsDateConfig(GsConfig, tag=True):
    data: datetime.date
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": "2025-01-01",
    "default": "2025-01-01",
    "type": "gsdate",
    "title": "活动开始日期",
    "desc": "活动生效的起始日期"
}
```

**`value` 格式**：ISO 8601 日期字符串 `"YYYY-MM-DD"`。

**保存时请求值**：`string`（格式 `"YYYY-MM-DD"`，如 `"2025-06-15"`）

**聊天命令格式**：`YYYY-MM-DD`（如 `2025-06-15`）

---

### GsTimeRangeConfig — 时间范围配置

**Python 定义**：

```python
class GsTimeRangeConfig(GsConfig, tag=True):
    data: Tuple[Tuple[int, int], Tuple[int, int]]
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": [[8, 0], [20, 0]],
    "default": [[8, 0], [20, 0]],
    "type": "gstimerange",
    "title": "允许访问时间段",
    "desc": "仅在此时间段内响应命令"
}
```

**`value` 格式**：`[[start_hour, start_minute], [end_hour, end_minute]]`，表示从 `start` 到 `end` 的时间范围。

**保存时请求值**：`[[int, int], [int, int]]`（如 `[[8, 0], [20, 0]]`）

**聊天命令格式**：`HH:MM-HH:MM`（如 `08:00-20:00`）

---

### GsColorConfig — 颜色配置

**Python 定义**：

```python
class GsColorConfig(GsConfig, tag=True):
    data: str
```

**API 返回示例**：

```json
{
    "value": "#FF5733",
    "default": "#FF5733",
    "type": "gscolor",
    "title": "主题色",
    "desc": "卡片渲染的主题颜色"
}
```

**`value` 格式**：HEX 颜色字符串 `#RRGGBB` 或 `#RRGGBBAA`（RGBA 含透明度）。

**保存时请求值**：`string`（如 `"#FF5733"` 或 `"#FF573380"`）

**聊天命令格式**：`#RRGGBB` 或 `#RRGGBBAA`（如 `#FFFFFF`）

---

### GsTimeConfig — 已废弃

> ⚠️ **已废弃**，请使用 [`GsTimeRConfig`](#gstimerconfig--时间点配置) 代替。

**Python 定义**：

```python
@deprecated("GsTimeConfig 已废弃，请使用 GsTimeRConfig 代替")
class GsTimeConfig(GsConfig, tag=True):
    data: str
    secret: bool = False
```

**API 返回示例**：

```json
{
    "value": "08:30",
    "default": "08:30",
    "type": "gstime",
    "title": "推送时间",
    "desc": "已废弃，请迁移至 GsTimeRConfig"
}
```

---

## 39.5 secret 字段说明

除 `GsDivider` 和 `GsColorConfig` 外，所有配置类型均支持 `secret: bool = False` 字段。当某配置项 `secret=True` 时：

- **API 响应**会额外返回 `"secret": true` 字段（非敏感项及 `GsDivider` / `GsColorConfig` 不返回该字段），前端应据此将该项渲染为密码输入框（`type="password"`）
- 聊天命令中查看配置时，该项的值会被脱敏显示为 `<已隐藏>`

---

## 39.6 前端渲染建议

| 配置类型 | 建议前端控件 | 说明 |
|---------|-------------|------|
| `gsstr` | 文本输入框 / 下拉选择 | 有 `options` 时用下拉；有 `regex` 时做正则校验 |
| `gsbool` | 开关 / 复选框 | — |
| `gsint` | 数字输入框 / 下拉选择 | 有 `options` 时用下拉；有 `max_value` 时限制上限 |
| `gsfloat` | 数字输入框（支持小数） | 有 `min_value`/`max_value` 时限制范围 |
| `gsliststr` | 多选 / 标签输入 | 有 `options` 时用多选；否则用逗号分隔输入 |
| `gslist` | 数字标签输入 | 逗号分隔的整数输入 |
| `gsdict` | 键值对编辑器 | 结构复杂，建议表格形式编辑 |
| `gsimage` | 图片上传 + 预览 | 使用 `upload_to`/`filename`/`suffix` 构建上传请求 |
| `gstimer` | 时间选择器 | 时:分选择 |
| `gsdivider` | 分割线 / 带标题分割线 | 展示 `title` 和 `desc`；`data` 非空时渲染带标题分割线，无输入控件 |
| `gsfileupload` | 文件上传 | 使用 `upload_to`/`filename`/`suffix` 构建上传请求 |
| `gsfilesupload` | 批量文件上传 | 使用 `data` 作为上传目录，`suffix` 限制文件类型 |
| `gsdate` | 日期选择器 | YYYY-MM-DD 格式 |
| `gstimerange` | 时间范围选择器 | 两个时间选择器组合 |
| `gscolor` | 颜色选择器 | 支持 HEX 和 RGBA |
| `gstime` | 文本输入框 | 已废弃，建议迁移提示 |
