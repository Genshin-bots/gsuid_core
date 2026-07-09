# 十五、完整插件示例

以下是一个包含全部核心功能的完整游戏查询插件示例，遵循 GsCore 插件命名规范。

## 15.1 命名规范

参照 GenshinUID、SayuStock 等成熟插件的目录结构：

| 规则 | 说明 | 示例 |
|------|------|------|
| 插件目录名 | `_PluginName/`（下划线前缀表示 buildin 插件）或 `PluginName/`（用户插件） | `_GenshinUID/`、`SayuStock/` |
| 内部包名 | 与插件目录名同名（不含下划线前缀） | `GenshinUID/`、`SayuStock/` |
| 入口文件 | `__init__.py` + `__nest__.py`（嵌套加载模式） | `GenshinUID/__init__.py` |
| 子模块目录 | `{prefix}_{feature}/`，prefix 为插件名小写缩写 | `genshinuid_roleinfo/`、`stock_info/` |
| 共享工具 | `utils/`、`tools/`（不加前缀） | `utils/database/models.py` |
| 配置模块 | `{prefix}_config/` | `genshinuid_config/`、`stock_config/` |
| 资源目录 | 子模块内的 `texture2d/` 或 `texture2D/` | `genshinuid_enka/texture2D/` |

## 15.2 目录结构

省略插件所在文件夹（`gsuid_core/plugins/`）下的其他目录，只保留 `MyGameUID/` 目录。

```
/MyGameUID/                            # 用户插件目录
├── __init__.py                        # 插件入口（可留空）
├── __nest__.py                        # 空文件，标记启用嵌套加载
├── pyproject.toml                     # 插件依赖声明
├── README.md
├── LICENSE
├── ICON.png                           # 插件图标（帮助 / webconsole）
└── MyGameUID/                         # 内部包（与插件目录同名）
    ├── __init__.py                    # 定义 Plugins(...) + 可选 import 子模块
    ├── __full__.py                    # 空文件，向框架标记嵌套加载
    ├── version.py                     # MyGameUID_version = "1.0.0"
    ├── mygameuid_bind/                # 绑定功能子模块
    │   └── __init__.py
    ├── mygameuid_roleinfo/            # 角色查询子模块
    │   ├── __init__.py
    │   ├── draw_roleinfo.py           # 图片渲染（PIL）
    │   └── texture2d/                 # 子模块专属静态资源
    │       └── bg.png
    ├── mygameuid_help/                # 帮助子模块（推荐）
    │   ├── __init__.py
    │   ├── get_help.py
    │   ├── help.json
    │   ├── icon_path/
    │   └── texture2d/
    ├── mygameuid_config/              # 配置子模块
    │   ├── __init__.py                # 开启 / 关闭 / 设置阈值 命令
    │   ├── config_default.py
    │   └── mygame_config.py
    └── utils/                         # 共享工具（不加前缀）
        ├── database/
        │   ├── __init__.py
        │   └── models.py              # 表 + @site.register_admin
        ├── resource/
        │   └── RESOURCE_PATH.py       # 各类路径常量
        ├── api.py                     # 第三方 API 请求封装
        ├── image.py                   # get_footer / 颜色辅助
        └── hint.py                    # 公共提示文案
```

## 15.3 `MyGameUID/utils/database/models.py`

```python
from typing import Optional
from sqlmodel import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site
from gsuid_core.utils.database.base_models import BaseModel, with_session


class MyGameBind(BaseModel, table=True):
    """游戏账号绑定表"""
    __table_args__ = {"extend_existing": True}

    uid: str = Field(title="游戏 UID")
    server: str = Field(default="cn", title="服务器")

    @classmethod
    @with_session
    async def get_bind(
        cls, session: AsyncSession, user_id: str, bot_id: str
    ) -> Optional["MyGameBind"]:
        stmt = select(cls).where(cls.user_id == user_id, cls.bot_id == bot_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def save_bind(
        cls, session: AsyncSession, user_id: str, bot_id: str, uid: str
    ) -> None:
        existing = await cls.get_bind(user_id, bot_id)
        if existing:
            existing.uid = uid
            session.add(existing)
        else:
            session.add(cls(user_id=user_id, bot_id=bot_id, uid=uid))


# 把这张表暴露到 Web 控制台，供管理员可视化管理
@site.register_admin
class MyGameBindAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="MyGame 绑定管理",
        icon="fa fa-link",
    )  # type: ignore
    model = MyGameBind
```

## 15.4 `MyGameUID/mygameuid_config/`

插件配置分为两个文件：`config_default.py` 定义默认配置项，`mygame_config.py` 创建 `StringConfig` 实例。

### `config_default.py` — 配置项定义

```python
from typing import Dict
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsStrConfig,
    GsIntConfig,
    GsBoolConfig,
)

CONFIG_DEFAULT: Dict[str, GSC] = {
    "api_key": GsStrConfig(
        title="API Key",
        desc="游戏数据 API Key",
        data="",
    ),
    "cache_ttl": GsIntConfig(
        title="缓存时长（分钟）",
        desc="数据缓存时长",
        data=30,
    ),
    "enable_cache": GsBoolConfig(
        title="启用缓存",
        desc="是否启用数据缓存",
        data=True,
    ),
}
```

### `mygame_config.py` — 创建 StringConfig 实例

```python
from gsuid_core.utils.plugins_config.gs_config import StringConfig
from .config_default import CONFIG_DEFAULT
from ..utils.resource.RESOURCE_PATH import CONFIG_PATH

MYGAME_CONFIG = StringConfig("MyGameUID", CONFIG_PATH, CONFIG_DEFAULT)
```

> **配置类型一览**（`gsuid_core/utils/plugins_config/models.py`）：
>
> | 类型 | 说明 | `data` 类型 | 特有字段 |
> |------|------|------------|---------|
> | `GsStrConfig` | 字符串配置 | `str` | `options`, `regex`, `secret` |
> | `GsBoolConfig` | 布尔配置 | `bool` | `secret` |
> | `GsIntConfig` | 整数配置 | `int` | `max_value`, `options`, `secret` |
> | `GsFloatConfig` | 浮点数配置 | `float` | `min_value`, `max_value`, `secret` |
> | `GsListStrConfig` | 字符串列表 | `List[str]` | `options`, `secret` |
> | `GsListConfig` | 整数列表 | `List[int]` | `secret` |
> | `GsDictConfig` | 字典配置 | `Dict[str, List]` | `secret` |
> | `GsImageConfig` | 图片配置 | `str` | `upload_to`, `filename`, `suffix`, `secret` |
> | `GsTimeRConfig` | 时间点 | `Tuple[int, int]` | `secret` |
> | `GsDivider` | 分割线 | `Optional[str]` | `data` (分割线标题, None=无标题) |
> | `GsFileUploadConfig` | 文件上传 | `str` | `upload_to`, `filename`, `suffix`, `secret` |
> | `GsFilesUploadConfig` | 批量文件上传 | `str` | `suffix`, `secret` |
> | `GsDateConfig` | 日期 | `datetime.date` | `secret` |
> | `GsTimeRangeConfig` | 时间范围 | `Tuple[Tuple[int,int], Tuple[int,int]]` | `secret` |
> | `GsColorConfig` | 颜色 | `str` | — |
> | `GsRepeatGroupConfig` | 可重复配置组 | `List[Dict[str, GSC]]` | `template`, `secret` |
>
> 所有配置类型继承自 `GsConfig(msgspec.Struct)`，必须包含 `title`、`desc` 字段。除 `GsDivider` 和 `GsColorConfig` 外，均支持 `secret` 字段。⚠️ `GsTimeConfig` 已废弃，请使用 `GsTimeRConfig`。
> `GsRepeatGroupConfig`（可重复配置组，`data` 为记录列表 + `template` 原型）用法详见 [四、配置管理 §4.6](04-config-management.md#46-可重复配置组-gsrepeatgroupconfig)。

## 15.5 `MyGameUID/mygameuid_roleinfo/draw_roleinfo.py`

```python
from gsuid_core.logger import logger
from gsuid_core.ai_core.trigger_bridge import ai_return
from MyGameUID.utils.api import fetch_char_data


async def render_char_image(uid: str, char_name: str) -> bytes | str:
    """渲染角色图片，同时注入 AI 可读数据"""
    data = await fetch_char_data(uid, char_name)
    if isinstance(data, str):
        ai_return(f"错误：{data}")
        return data
    _ai_return_char(data, char_name)
    return await _build_image(data)


def _ai_return_char(data: dict, char_name: str) -> None:
    level = data.get("level", "N/A")
    const = data.get("constellation", 0)
    props = data.get("properties", {})
    atk = props.get("atk", 0)
    crit_rate = props.get("crit_rate", 0.0)
    crit_dmg = props.get("crit_dmg", 0.0)
    weapon = data.get("weapon", {}).get("name", "N/A")
    ai_return(
        f"【{char_name} 角色详情】\n"
        f"等级: {level}  命座: {const}命\n"
        f"攻击力: {atk:.0f}  暴击率: {crit_rate:.1%}  暴击伤害: {crit_dmg:.1%}\n"
        f"武器: {weapon}"
    )


async def _build_image(data: dict) -> bytes:
    # 实际图片生成逻辑（略）
    ...
```

## 15.6 `MyGameUID/mygameuid_bind/__init__.py`

```python
from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.trigger_bridge import ai_return
from MyGameUID.utils.database.models import MyGameBind

sv = SV("MyGameUID")


@sv.on_command(
    ("绑定", "bind"),
    to_ai="""绑定用户的游戏 UID。
    当用户说"帮我绑定UID"、"我的UID是xxx"时调用。

    Args:
        text: 游戏 UID，纯数字，例如 "123456789"
    """,
)
async def bind_uid(bot: Bot, ev: Event) -> None:
    uid = ev.text.strip()
    if not uid.isdigit():
        return await bot.send("UID 格式不正确，请输入纯数字")
    await MyGameBind.save_bind(ev.user_id, ev.bot_id, uid)
    await bot.send(f"已成功绑定 UID: {uid}")


@sv.on_fullmatch(
    "我的绑定",
    to_ai="""查看当前用户绑定的游戏 UID。
    当用户询问"我绑定了什么"、"我的UID是多少"时调用。
    无需参数。

    Args:
        text: 无需参数，留空即可
    """,
)
async def show_bind(bot: Bot, ev: Event) -> None:
    bind = await MyGameBind.get_bind(ev.user_id, ev.bot_id)
    if not bind:
        return await bot.send("您还没有绑定 UID")
    await bot.send(f"您绑定的 UID：{bind.uid}")
```

## 15.7 `MyGameUID/mygameuid_roleinfo/__init__.py`

```python
from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.trigger_bridge import ai_return
from MyGameUID.utils.database.models import MyGameBind
from MyGameUID.mygameuid_roleinfo.draw_roleinfo import render_char_image

sv = SV("MyGameUID")


@sv.on_command(
    ("查角色", "角色信息"),
    to_ai="""查询游戏角色的培养详情和属性数据。
    当用户询问某个角色的命座、圣遗物、天赋、属性面板时调用。
    需要用户已绑定 UID。

    Args:
        text: 角色名称，支持昵称。
              例如 "雷电将军"、"雷神"（等同于雷电将军）、"胡桃"
    """,
)
async def get_char_info(bot: Bot, ev: Event) -> None:
    char_name = ev.text.strip()
    if not char_name:
        ai_return("错误：未提供角色名，请在 text 中指定角色名称")
        return await bot.send("请输入角色名，例如：查角色 雷电将军")
    bind = await MyGameBind.get_bind(ev.user_id, ev.bot_id)
    if not bind:
        ai_return("错误：用户未绑定 UID，请先发送 '绑定 你的UID'")
        return await bot.send("请先绑定 UID：发送 '绑定 你的UID'")
    im = await render_char_image(bind.uid, char_name)
    await bot.send(im)
```

## 15.8 `MyGameUID/__init__.py`（包入口）

包入口文件的核心职责：
1. **定义 `Plugins` 类** — 声明插件的全局配置（前缀、权限、别名等）
2. **导入子模块** — 触发各子模块中 `@sv.on_xxx` 装饰器的注册

```python
from gsuid_core.sv import Plugins

# ── Plugins 类：声明插件全局配置 ──────────────────────────────────
# Plugins 是单例模式，同名插件只创建一次。
# 它定义了插件内所有 SV 实例共享的前缀、权限等配置。
#
# 关键参数：
#   name:             插件名称，必须与目录名一致
#   force_prefix:     强制前缀列表，用户必须以此开头才能触发命令
#   allow_empty_prefix: 是否允许无前缀触发（默认根据 prefix/force_prefix 自动推断）
#   alias:            插件别名列表
#   pm:               权限等级（0-6，数字越小权限越高）
#   prefix:           可选前缀列表（与 force_prefix 的区别：force_prefix 强制，prefix 可选）
#   disable_force_prefix: 是否禁用强制前缀
#
# 实际示例（参照 GenshinUID）：
#   Plugins(name="GenshinUID", force_prefix=["gs"], allow_empty_prefix=False, alias=["gsuid"])
#
# 实际示例（参照 SayuStock）：
#   Plugins(name="SayuStock", force_prefix=["a", "股票"], allow_empty_prefix=True)

Plugins(
    name="MyGameUID",
    force_prefix=["mygame", "游戏"],
    allow_empty_prefix=False,
    alias=["mygame"],
)

# ── 导入子模块，触发 @sv.on_xxx 装饰器注册 ───────────────────────
from MyGameUID import mygameuid_bind  # noqa: F401
from MyGameUID import mygameuid_roleinfo  # noqa: F401
```

> **`Plugins` vs `SV` 的关系**：
> - `Plugins` 是**插件级**配置，定义整个插件的前缀、权限等共享设置
> - `SV` 是**服务模块级**配置，定义单个功能模块的触发器和权限
> - 一个 `Plugins` 下可以有多个 `SV` 实例
> - `SV` 创建时会自动查找同名 `Plugins` 实例，继承其前缀配置

## 15.9 `MyGameUID/__nest__.py`（嵌套加载入口）

空文件, 无需任何内容

```python
```

## 15.10 `__init__.py`（插件根目录入口）

```python
# 插件根目录的 __init__.py
# 对于 __nest__.py 模式，此文件可留空或仅做版本声明
```
