---
name: gscore-plugin-development
description: >
  为 GsCore 机器人框架编写插件的完整指南。涵盖插件结构、触发器注册、消息收发、数据库操作、
  定时任务、配置管理、AI 工具集成（@ai_tools、to_ai、ai_return）、知识库注册、启动钩子等所有核心 API。
  当用户要求"帮我写一个 GsCore 插件"、"给这个插件加功能"、"改造触发器支持 AI"、
  "怎么用 to_ai"、"注册 ai_tools"、"写一个游戏查询插件"时触发此 SKILL。
  对所有 GsCore 插件开发任务都应优先读取此 SKILL。
---

# GsCore 插件开发完整指南

## 目录
- [一、插件基础结构](#一插件基础结构)
- [二、SV 与触发器](#二sv-与触发器)
- [三、消息收发](#三消息收发)
- [四、配置管理](#四配置管理)
- [五、数据库操作](#五数据库操作)
- [六、定时任务](#六定时任务)
- [七、启动钩子](#七启动钩子)
- [八、AI 集成：to_ai 与 ai_return](#八ai-集成to_ai-与-ai_return)
- [九、AI 集成：@ai_tools 装饰器](#九ai-集成ai_tools-装饰器)
- [十、AI 集成：知识库与别名注册](#十ai-集成知识库与别名注册)
- [十一、AI 集成：create_agent](#十一ai-集成create_agent)
- [十二、完整插件示例](#十二完整插件示例)
- [十三、代码规范红线](#十三代码规范红线)

---

## 一、插件基础结构

### 1.1 目录结构

```
gsuid_core/plugins/<插件名>/
├── __init__.py              # 插件入口，负责注册所有触发器
├── <插件名>_command.py      # 主命令逻辑（可选，按需分文件）
├── config.json              # 插件配置（运行时自动生成）
├── utils/
│   ├── __init__.py
│   ├── database/
│   │   ├── __init__.py
│   │   └── models.py        # 数据库模型
│   └── api.py               # 第三方 API 请求封装
└── resource/                # 静态资源（图片、模板等）
```

### 1.2 插件入口 `__init__.py` 标准写法

```python
# my_plugin/__init__.py
from gsuid_core.sv import SV
from gsuid_core.logger import logger
from gsuid_core.bot import Bot
from gsuid_core.models import Event

# 可在此注册别名，模块加载时自动执行
from gsuid_core.ai_core.register import ai_alias
ai_alias("我的插件", ["MyPlugin", "mp"])

# 创建 SV 实例
sv = SV("my_plugin")

# 在此文件内直接写触发器，或从子模块导入
@sv.on_fullmatch("帮助")
async def show_help(bot: Bot, ev: Event) -> None:
    await bot.send("这是帮助信息")
```

### 1.3 pyproject.toml（声明插件依赖）

```toml
[project]
name = "my-plugin"
version = "0.1.0"
dependencies = [
    "httpx>=0.24.0",
    "pillow>=9.0.0",
]
```

启动时自动安装 `dependencies` 中声明的依赖。`python`、`fastapi`、`pydantic`、`gsuid-core` 等基础依赖无需声明。

---

## 二、SV 与触发器

### 2.1 创建 SV 实例

```python
from gsuid_core.sv import SV

sv = SV(
    name="查询用户信息",     # 必填，和插件名不同, 代表这一组功能的统称
    pm=6,                  # 允许什么权限等级的用户触发, 默认为6, 最高为0
                           # 0代表仅限主人用户触发, 2代表允许群主及以上权限触发, 3代表允许管理员及其以上权限触发
    priority=5,            # 优先级，数字越小越先执行，默认 5
    enabled=True,          # 是否启用，默认 True
    area="ALL",            # 作用范围：GROUP / DIRECT / ALL
    black_list=[],         # 用户 ID 黑名单
    white_list=[],         # 用户 ID 白名单
)

add_sv = SV(name="查询帮助信息", pm=6, area="ALL") # 一般只需要定义这三项即可
```

同一 `name` 的 SV 是单例，多次创建会返回同一个实例，可跨文件共享。

### 2.2 触发器类型速查

| 装饰器 | 匹配方式 | 适用场景 |
|--------|---------|---------|
| `on_command` | 前缀匹配命令 | `/查询 xxx`、`查询 xxx` |
| `on_fullmatch` | 完全匹配 | `帮助`、`菜单` |
| `on_prefix` | 前缀匹配（保留后续内容） | 以某词开头的所有消息 |
| `on_suffix` | 后缀匹配 | 以某词结尾的所有消息 |
| `on_keyword` | 包含关键词 | 消息中包含某词即触发 |
| `on_regex` | 正则匹配 | 复杂格式匹配 |
| `on_file` | 文件消息 | 用户上传文件时触发 |
| `on_message` | 所有消息 | 监听全部消息（慎用） |

### 2.3 触发器注册示例

```python
# on_command：用户发 "查询 雷电将军" 时触发，ev.text = "雷电将军"
# 和 on_prefix 不同的是, on_command 允许 用户发送 "查询" 时也触发
@sv.on_command("查询")
async def query_handler(bot: Bot, ev: Event) -> None:
    name = ev.text.strip()
    await bot.send(f"查询：{name}")

# on_prefix：前缀匹配, 用户发 "查询 雷电将军" 时触发，ev.text = "雷电将军"
# 和 on_command 不同的是, on_prefix强制要求必须加参数
# on_prefix 不允许 用户发送 "查询" 时也触发, 只允许用户发送 "查询 雷电将军" 时触发
@sv.on_prefix("查询")
async def help_handler(bot: Bot, ev: Event) -> None:
    name = ev.text.strip()
    await bot.send(f"查询：{name}")

# on_suffix：后缀匹配, 用户发送 "雷电将军 帮助" 时触发
@sv.on_suffix("帮助")
async def help_handler(bot: Bot, ev: Event) -> None:
    await bot.send("帮助信息")

# on_fullmatch：精确匹配 "帮助"
@sv.on_fullmatch("帮助")
async def help_handler(bot: Bot, ev: Event) -> None:
    await bot.send("帮助信息")

# on_command 支持 tuple：多个命令映射同一个函数
@sv.on_command(("绑定", "bind", "绑定UID"))
async def bind_handler(bot: Bot, ev: Event) -> None:
    uid = ev.text.strip()
    await bot.send(f"绑定 UID: {uid}")

# on_regex：匹配 "查询 某角色 的 某属性"
@sv.on_regex(r"查询\s*(?P<name>\S+)\s*的\s*(?P<attr>\S+)")
async def regex_handler(bot: Bot, ev: Event) -> None:
    name = ev.regex_dict.get("name", "")
    attr = ev.regex_dict.get("attr", "")
    await bot.send(f"角色: {name}, 属性: {attr}")

# on_file：用户上传 .png 文件时触发
@sv.on_file("png")
async def file_handler(bot: Bot, ev: Event) -> None:
    await bot.send(f"收到图片：{ev.file_name}")
```

### 2.4 通用参数

所有 `on_xxx` 装饰器都支持以下通用参数：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `block` | `bool` | `False` | 匹配后阻止事件继续传递给其他触发器 |
| `to_me` | `bool` | `False` | 是否必须 @ 机器人才触发 |
| `prefix` | `bool` | `True` | 是否应用插件全局前缀 |
| `to_ai` | `str` | `""` | 非空时自动注册为 AI 工具（详见第八章） |

### 2.5 处理函数签名规范

所有处理函数必须遵循此签名，**不得更改**：

```python
from gsuid_core.bot import Bot
from gsuid_core.models import Event

async def my_handler(bot: Bot, ev: Event) -> None:
    ...
```

---

## 三、消息收发

### 3.1 Event 对象常用属性

```python
ev.user_id         # str：发送者用户 ID
ev.group_id        # str | None：群 ID（私聊为 None）
ev.bot_id          # str：Bot 标识
ev.bot_self_id     # str：机器人自身 ID
ev.raw_text        # str：原始消息文本（含前缀+命令）
ev.text            # str：去掉前缀+命令后的参数部分
ev.command         # str：匹配到的命令关键词
ev.is_tome         # bool：是否 @ 了机器人
ev.at              # str | None：被 @ 的用户 ID
ev.message         # list：消息段列表
ev.user_nickname   # str：用户昵称
ev.file            # bool：是否有文件附件
ev.file_name       # str | None：文件名
ev.regex_dict      # dict：正则命名分组 (?P<name>...)
ev.regex_group     # tuple：正则位置分组
```

### 3.2 Bot 发送方法

```python
from gsuid_core.segment import MessageSegment
from gsuid_core.message_models import Button

# ----- 发送纯文本 -----
await bot.send("Hello!")                      # 直接传字符串
await bot.send(MessageSegment.text("Hello!")) # 或显式构造 Message
# 多段文本可放在列表中
await bot.send(["第一行", "第二行"])

# ----- 发送图片 -----
# 支持 bytes、base64 字符串、URL、Path、PIL.Image
await bot.send(MessageSegment.image(b"图片字节"))
await bot.send(MessageSegment.image("base64://...."))
await bot.send(MessageSegment.image("https://example.com/img.png"))
await bot.send(MessageSegment.image(Path("local.png")))

# ----- 发送 Markdown（仅部分平台支持）-----
await bot.send(MessageSegment.markdown("# 标题\n内容"))
# 也可携带按钮
await bot.send(MessageSegment.markdown(
    "请选择：",
    [[Button("选项1"), Button("选项2")]]
))

# ----- 发送按钮（仅部分平台支持）-----
await bot.send([
    MessageSegment.text("点击下方按钮："),
    MessageSegment.buttons([[Button("确认"), Button("取消")]])
])

# ----- @某人 -----
await bot.send([MessageSegment.text("提醒："), MessageSegment.at("123456")])

# ----- 合并转发节点 -----
await bot.send(MessageSegment.node(["消息1", "消息2"]))

# ----- 发送文件 / 语音 / 视频 -----
await bot.send(MessageSegment.file(Path("doc.pdf"), file_name="doc.pdf"))
await bot.send(MessageSegment.record("base64://..."))
await bot.send(MessageSegment.video("https://example.com/video.mp4"))
```

### 3.3 多步会话（Response）

用于需要用户多次交互的场景，分为**单用户响应**和**多用户响应**两种模式。

#### 单用户响应

仅接收触发命令的同一用户后续消息。

```python
@sv.on_fullmatch("开始测试")
async def get_resp_msg(bot: Bot, ev: Event):
    await bot.send("开始多步会话测试")

    # 发送提示并等待该用户回复（默认超时60秒）
    resp = await bot.receive_resp("接下来你说的话我都会提取出来噢？")
    if resp is not None:
        await bot.send(f"你说的是 {resp.text} 吧？")
```

#### 多用户响应

接收群内任意用户的后续消息，常用于游戏、投票等场景。

```python
@sv.on_fullmatch("开始多用户测试")
async def get_resp_msg(bot: Bot, ev: Event):
    await bot.send("开始多步会话测试")
    await bot.send("接下来开始游戏！？所有人的会话我都会收集起来的哦！")
    while True:
        resp = await bot.receive_mutiply_resp()
        if resp is not None:
            await bot.send(f"你说的是 {resp.text} 吧？")
```

如需限制收集时长，可配合 `asyncio.timeout`（Python 3.11+）或 `async_timeout`：

```python
@sv.on_fullmatch("开始一场60秒的游戏")
async def get_time_limit_resp_msg(bot: Bot, ev: Event):
    await bot.send("接下来开始60秒的游戏！？")
    try:
        async with asyncio.timeout(60):  # 限制时长60秒
            while True:
                resp = await bot.receive_mutiply_resp()
                if resp is not None:
                    await bot.send(f"你说的是 {resp.text} 吧？")
    except asyncio.TimeoutError:
        await bot.send("时间到!!现在开始计算每个人的分数...")
```

#### 主要方法对比

| 方法 | 说明 |
|------|------|
| `bot.send_option(...)` | 发送按钮或选项提示，**不等待**回复。 |
| `bot.receive_resp(...)` | 发送可选消息，并等待**触发命令用户**的下一条消息。 |
| `bot.receive_mutiply_resp(...)` | 发送可选消息，并等待**群内任意用户**的后续消息。 |

`receive_mutiply_resp` 和 `send_option` 内部均调用 `receive_resp`，因此参数基本一致。

#### 常用参数

- **`reply`**：可填入 `bot.send()` 接受的任何值（字符串、`Message`、`MessageSegment` 等），会在等待回复前先发送一次消息。
- **`option_list`**：类型 `List[str]`、`List[Button]`、`List[List[str]]` 或 `List[List[Button]]`，用于生成按钮或多选提示（部分平台支持）。
- **`timeout`**：等待回复的超时时间（秒），默认 `60`。
- **`unsuported_platform`**：当平台不支持按钮时，是否转为发送多选文本提示（默认 `False`）。
- **`sep`**、**`command_tips`**、**`command_start_text`**：在文本模式下自定义选项分隔符和提示语。

完整参数可参考代码中 `Bot.receive_resp` 的签名。

---

## 四、配置管理

### 4.1 定义插件配置项

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
}
```

> **注意**：所有配置类型的字段名是 `title`、`desc`、`data`，而非示例代码中的 `title`、`description`、`default`。

### 4.2 创建配置实例并注册到 Web 控制台

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

### 4.3 读取与修改配置

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

### 4.4 完整示例目录结构

```
my_plugin/
├── __init__.py          # 包含 my_config 实例和 __plugin_config_class__
├── config_default.py    # 配置项定义
├── ...
```

### 4.5 所有可用配置类型

从 `gsuid_core.utils.plugins_config.models` 导入：

```python
from gsuid_core.utils.plugins_config.models import (
    GsStrConfig,      # 字符串
    GsBoolConfig,     # 布尔
    GsIntConfig,      # 整数 (可限制 max_value)
    GsListStrConfig,  # 字符串列表
    GsListConfig,     # 整数列表
    GsDictConfig,     # 字典
    GsImageConfig,    # 图片配置 (上传相关)
    GsTimeRConfig,    # 时间点配置 (定时任务配置相关)
)
# 联合类型 GSC = Union[GsStrConfig, GsBoolConfig, GsIntConfig, GsListStrConfig, GsListConfig, GsDictConfig, GsImageConfig]
```

---

## 五、数据库操作

GsCore 使用 SQLModel 作为 ORM，**所有数据库操作必须在模型类内部**，使用 `@with_session` 装饰器管理会话。

### 5.1 三级基类

```python
from gsuid_core.utils.database.base_models import (
    BaseIDModel,      # 只有 id 字段（自增主键）
    BaseBotIDModel,   # id + bot_id
    BaseModel,        # id + bot_id + user_id  ← 最常用
)
```

### 5.2 定义数据模型

```python
# utils/database/models.py
from typing import Optional
from sqlmodel import Field
from gsuid_core.utils.database.base_models import BaseModel, with_session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


class GameBind(BaseModel, table=True):
    """游戏账号绑定表"""
    uid: str = Field(title="游戏 UID")
    region: str = Field(default="cn", title="大区")
    cookie: Optional[str] = Field(default=None, title="Cookie")

    @classmethod
    @with_session
    async def get_bind(
        cls, session: AsyncSession, user_id: str, bot_id: str
    ) -> Optional["GameBind"]:
        """根据用户 ID 查询绑定"""
        stmt = (
            select(cls)
            .where(cls.user_id == user_id)
            .where(cls.bot_id == bot_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def bind_uid(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
        region: str = "cn",
    ) -> "GameBind":
        """绑定或更新 UID"""
        existing = await cls.get_bind(user_id, bot_id)
        if existing:
            existing.uid = uid
            existing.region = region
            session.add(existing)
            return existing
        bind = cls(user_id=user_id, bot_id=bot_id, uid=uid, region=region)
        session.add(bind)
        return bind

    @classmethod
    @with_session
    async def get_uid_list(
        cls, session: AsyncSession, user_id: str, bot_id: str
    ) -> list[str]:
        """获取用户所有绑定的 UID 列表"""
        stmt = (
            select(cls.uid)
            .where(cls.user_id == user_id)
            .where(cls.bot_id == bot_id)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def delete_bind(
        cls, session: AsyncSession, user_id: str, bot_id: str, uid: str
    ) -> bool:
        """删除绑定"""
        stmt = (
            select(cls)
            .where(cls.user_id == user_id)
            .where(cls.bot_id == bot_id)
            .where(cls.uid == uid)
        )
        result = await session.execute(stmt)
        bind = result.scalar_one_or_none()
        if bind is None:
            return False
        await session.delete(bind)
        return True
```

### 5.3 `@with_session` 规则

- **必须是 `classmethod`** 且 **`async def`**
- `session: AsyncSession` 必须是第二个参数（紧跟 `cls`）
- 装饰器自动 commit，异常自动回滚
- `@with_session` 已处理事务，**不要**在方法内手动 `await session.commit()`

### 5.4 类方法外的手动 session

```python
from gsuid_core.utils.database.base_models import async_maker

async def batch_cleanup():
    async with async_maker() as session:
        from sqlalchemy import delete
        stmt = delete(GameBind).where(GameBind.cookie == None)
        await session.execute(stmt)
        await session.commit()
```

### 5.5 在触发器中使用数据库

```python
@sv.on_command(("绑定", "bind"))
async def bind_uid(bot: Bot, ev: Event) -> None:
    uid = ev.text.strip()
    if not uid or not uid.isdigit():
        return await bot.send("请输入正确的 UID（纯数字）")

    await GameBind.bind_uid(ev.user_id, ev.bot_id, uid)
    await bot.send(f"✅ 已绑定 UID: {uid}")

@sv.on_fullmatch("我的UID")
async def show_uid(bot: Bot, ev: Event) -> None:
    uid_list = await GameBind.get_uid_list(ev.user_id, ev.bot_id)
    if not uid_list:
        return await bot.send("您还没有绑定 UID，发送 '绑定 您的UID' 进行绑定")
    await bot.send("您绑定的 UID：\n" + "\n".join(uid_list))
```

---

## 六、定时任务

### 6.1 使用 APScheduler

```python
from gsuid_core.aps import scheduler

# cron 表达式：每天 8:30 执行
@scheduler.scheduled_job("cron", hour=8, minute=30)
async def daily_task():
    # 需要主动获取 bot 实例向用户推送
    from gsuid_core.gss import gss
    for bot_id, bot in gss.active_bot.items():
        await bot.target_send(
            bot_id=bot_id,
            target_type="group",
            target_id="目标群ID",
            message="今日早报",
        )

# interval：每 30 分钟执行一次
@scheduler.scheduled_job("interval", minutes=30)
async def refresh_cache():
    await do_cache_refresh()

# 一次性：在指定时间执行
from datetime import datetime, timedelta
scheduler.add_job(
    func=one_time_task,
    trigger="date",
    run_date=datetime.now() + timedelta(hours=1),
)
```

### 6.2 定时任务中获取 Bot 实例与订阅推送

定时任务不像插件回调那样自动注入 `bot`，你可以手动遍历在线 Bot 发送消息，
也可以并且我们推荐你使用 **订阅系统** 将目标会话持久化，在定时任务中直接调用 `send()`，省去手动获取和传递平台参数的麻烦。

#### 手动获取在线 Bot 发送

```python
from gsuid_core.gss import gss

@scheduler.scheduled_job("cron", hour=0, minute=20)
async def daily_cleanup():
    # 遍历所有在线 Bot
    for bot_id, bot in gss.active_bot.items():
        # 向特定群发送消息
        await bot.target_send(
            bot_id=bot_id,
            target_type="group",
            target_id="123456789",
            message="每日缓存清理完成",
        )
```

#### 使用订阅系统（推荐）

对于需要持续向某些用户/群聊推送消息的场景（公告、签到、数据更新等），你可以提前在插件命令中让用户“订阅”，然后在定时任务中通过 `gs_subscribe` 直接获取所有订阅记录并发送消息，无需关心 Bot 是否在线（框架会自动路由）。

**1. 注册订阅（在命令中调用）**

```python
from gsuid_core.subscribe import gs_subscribe

@sv.on_prefix("订阅公告")
async def subscribe_notice(bot: Bot, ev: Event):
    await gs_subscribe.add_subscribe(
        subscribe_type="session",   # 每个群/私聊仅保留一条记录
        task_name="每日公告",
        event=ev,
    )
    await bot.send("已订阅每日公告！")
```

**2. 定时任务中批量推送**

```python
@scheduler.scheduled_job("cron", hour=8)
async def send_daily_notice():
    subs = await gs_subscribe.get_subscribe("每日公告")
    if not subs:
        return
    for sub in subs:
        # sub.send() 自动识别平台、Bot、目标会话
        await sub.send("📢 每日公告：维护完成，各项服务已恢复。")
```

**3. 取消订阅**

```python
@sv.on_prefix("取消公告")
async def unsubscribe_notice(bot: Bot, ev: Event):
    await gs_subscribe.delete_subscribe("session", "每日公告", ev)
    await bot.send("已取消订阅。")
```

**订阅类型**

| `subscribe_type` | 行为 |
|-----------------|------|
| `"session"`     | 同一群聊/私聊只保留一条记录，适合公告、推送。 |
| `"single"`      | 同一群聊可有多条记录（如多个签到任务），私聊仍只保留一条。 |

你还可以通过 `extra_message` 参数在订阅时保存额外数据，并在发送时通过 `sub.extra_message` 读取。


---

## 七、启动钩子

用于在服务启动时执行初始化操作（数据库迁移、缓存预热等）。

### 7.1 两类钩子

```python
from gsuid_core.server import on_core_start, on_core_start_before

# 阶段一：WS 服务启动前阻塞执行（用于数据库迁移、必要初始化）
@on_core_start_before(priority=0)
async def before_start():
    # 例如：确保数据库表结构是最新的
    await migrate_schema()

# 阶段二：WS 服务启动后后台执行（用于缓存预热、资源加载等）
@on_core_start(priority=5)
async def after_start():
    # 例如：预热 API 缓存
    await warmup_cache()
```

`priority` 越小越先执行，同优先级并发执行。

### 7.2 常见使用场景

| 场景 | 使用哪个钩子 |
|------|------------|
| 数据库表结构变更 | `on_core_start_before` |
| 加载全局配置到内存 | `on_core_start_before` |
| 注册 AI 知识库内容 | `on_core_start`（priority=0，等 RAG 初始化后） |
| 预热 HTTP 缓存 | `on_core_start` |
| 启动后台监控任务 | `on_core_start` |

---

## 八、AI 集成：`to_ai` 与 `ai_return`

这是 GsCore 中将现有命令触发器零成本开放给 AI 调用的核心机制。

### 8.1 核心概念

**`to_ai` 参数**：在 `on_xxx` 装饰器上声明一段描述文字，启动时自动将触发器函数注册为 AI 工具（分类：`"by_trigger"`）。AI 按照这段描述理解"什么时候调用"以及"怎么构建参数"。

**`ai_return(text)`**：在触发器函数或其调用的数据处理函数中调用，向 AI 返回结构化文本摘要：
- **普通用户触发时**：完全静默，不影响任何逻辑
- **AI 调用时**：文本被收集，作为工具的返回值传回给 AI

**`MockBot`**：AI 调用触发器时，`bot` 对象被替换为 `MockBot`：
- `bot.send(bytes)` / `bot.send(Message(type="image"))` / `bot.send("base64://...")` → 通过 `RM.register()` 注册图片，返回资源 ID（如 `img_a1b2c3d4`），不传给 AI 也不发送给用户
- `bot.send(str)` / `bot.send(纯文字 Message)` → 文字被收集，作为工具返回值传回给 AI
- `bot.send_option(reply, buttons)` → reply 走 `send()` 拦截，buttons 忽略
- `bot.receive_resp(reply, ...)` → reply 走 `send()` 拦截，返回 `None`（AI 不支持交互式等待）
- AI 收到工具返回值（含资源 ID）后，决定是否调用 `send_trigger_images(resource_id)` 发送图片

**权限检查**：AI 调用触发器工具时，系统会自动检查 `plugins.pm` 和 `sv.pm` 权限，与用户直接触发一致。低权限用户通过 AI 调用高权限命令会收到 "❌ 权限不足" 错误。配置通过 webconsole 修改后实时生效。

### 8.2 `to_ai` 的 docstring 写法规范

**必须包含的内容**：

```
<一句话功能描述>
<用户在什么自然语言场景下会需要这个功能>

Args:
    text: <text 参数的完整格式，包括：>
          - <基础格式>
          - <可选前缀/后缀及其含义>
          - <多值分隔方式>
          - <至少两个具体例子>
          <如果是 on_fullmatch 且无参数：写"无需参数，留空即可">
```

**`to_ai` 写得好不好，决定 AI 能否正确调用触发器**。

### 8.3 基础用法示例

```python
from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.trigger_bridge import ai_return

sv = SV("游戏查询")

# ── 示例一：有参数的命令 ──────────────────────────────────────
@sv.on_command(
    ("查角色", "角色信息"),
    to_ai="""查询指定游戏角色的培养详情和属性数据。
    当用户询问某个角色的命座、圣遗物、天赋、伤害数据时调用。
    需要用户已绑定 UID。

    Args:
        text: 角色名称，支持昵称。
              例如 "雷电将军"、"雷神"、"胡桃"、"纳西妲"
    """,
)
async def get_char_info(bot: Bot, ev: Event) -> None:
    char_name = ev.text.strip()
    if not char_name:
        ai_return("错误：未提供角色名称，请在 text 中指定角色名")
        return await bot.send("请输入角色名，例如：查角色 雷电将军")
    uid = await GameBind.get_bind(ev.user_id, ev.bot_id)
    if not uid:
        ai_return("错误：用户未绑定 UID，请先发送 '绑定 你的UID'")
        return await bot.send("请先绑定 UID")
    im = await render_char_image(uid.uid, char_name)
    await bot.send(im)


# ── 示例二：无参数的 fullmatch ─────────────────────────────────
@sv.on_fullmatch(
    "我的角色",
    to_ai="""查看用户当前绑定账号的全部角色列表。
    当用户说"帮我看看我有哪些角色"、"我的角色列表"时调用。
    无需参数，自动读取当前用户的绑定账号。

    Args:
        text: 无需参数，留空即可
    """,
)
async def my_chars(bot: Bot, ev: Event) -> None:
    uid = await GameBind.get_bind(ev.user_id, ev.bot_id)
    if not uid:
        return await bot.send("请先绑定 UID")
    im = await render_char_list(uid.uid)
    await bot.send(im)


# ── 示例三：绑定操作（写操作，bot.send 的文字会被 MockBot 收集告知 AI）──
@sv.on_command(
    ("绑定", "bind"),
    to_ai="""绑定用户的游戏 UID 到账号。
    当用户说"帮我绑定UID"、"我的UID是xxx"时调用。

    Args:
        text: 游戏 UID，纯数字，例如 "123456789"
    """,
)
async def bind_uid_cmd(bot: Bot, ev: Event) -> None:
    uid = ev.text.strip()
    if not uid.isdigit():
        return await bot.send("UID 格式不正确，请输入纯数字")
    await GameBind.bind_uid(ev.user_id, ev.bot_id, uid)
    await bot.send(f"✅ 已成功绑定 UID: {uid}")
    # bot.send 的文字被 MockBot 收集，AI 会知道"绑定成功"
```

### 8.4 `ai_return` 在数据层的注入（推荐模式）

对于最终生成图片的触发器，在渲染层注入 `ai_return` 是最佳实践：

```python
# utils/renderer.py
from gsuid_core.ai_core.trigger_bridge import ai_return
from gsuid_core.logger import logger


async def render_char_image(uid: str, char_name: str) -> bytes:
    # 1. 获取数据
    char_data = await fetch_char_data(uid, char_name)

    # 2. 注入 AI 文本摘要（在数据拿到后、图片生成前）
    _ai_return_char(char_data, char_name)

    # 3. 生成图片
    fig = build_char_figure(char_data)
    return await render_image_by_pw(fig)


def _ai_return_char(char_data: dict, char_name: str) -> None:
    """提取角色关键数据作为 AI 可读文本摘要"""
    try:
        level = char_data.get("level", "N/A")
        constellation = char_data.get("constellation", 0)
        atk = char_data.get("fight_prop", {}).get("atk", "N/A")
        crit_rate = char_data.get("fight_prop", {}).get("crit_rate", 0.0)
        crit_dmg = char_data.get("fight_prop", {}).get("crit_dmg", 0.0)
        weapon = char_data.get("weapon", {}).get("name", "N/A")
        ai_return(
            f"【{char_name} 角色数据】\n"
            f"等级: {level}  命座: {constellation}命\n"
            f"攻击力: {atk:.0f}  暴击率: {crit_rate:.1%}  暴击伤害: {crit_dmg:.1%}\n"
            f"武器: {weapon}"
        )
    except Exception as e:
        # ai_return 的辅助函数允许 try/except，失败不影响图片生成
        logger.warning(f"[MyPlugin] ai_return 角色数据提取失败: {e}")
```

### 8.5 `ai_return` 应该包含什么内容

AI 拿到工具返回值后，会用这段文字来理解执行结果，并决定如何回复用户。

| 数据类型 | 应提取哪些字段 |
|---------|-------------|
| 游戏角色/装备 | 名称、等级、核心属性数值（至少3个）、关键装备 |
| 排行榜/列表 | 前 5 名 + 后 5 名 + 总计统计 |
| 行情/走势 | 名称、最新价、涨跌幅、开/高/低、关键指标 |
| K 线数据 | 名称、周期、最近 N 条记录（日期+核心数值） |
| 副本/任务 | 名称、进度（x/y）、完成状态、剩余次数 |
| 错误情况 | 错误原因，例如 `ai_return("错误：未找到角色 xxx")` |
| 写操作成功 | 不需要额外 `ai_return`，`bot.send` 的文字会被收集 |

### 8.6 哪些触发器不加 `to_ai`

| 情况 | 原因 |
|------|------|
| 管理员/超级用户专用命令 | 虽然系统会自动检查 `pm` 权限，但 AI 对大多数用户都会收到权限错误，浪费 token |
| 危险操作（清数据、重载配置） | AI 不应独立执行破坏性操作 |
| 需要多轮 Response 会话的命令 | `receive_resp` 在 AI 上下文中返回 `None`，交互流程会中断 |
| `on_file` 文件接收命令 | AI 无法构建文件输入 |
| 功能单一且 AI 无法获取有效信息 | 改造价值低 |

> **权限保障**：即使开发者错误地给高权限命令添加了 `to_ai`，系统也会在运行时检查 `plugins.pm` 和 `sv.pm`，低权限用户通过 AI 调用时会收到 "❌ 权限不足" 错误。

> **图片资源持久化**：AI 调用触发器时，图片通过 `RM.register()` 注册并返回资源 ID。资源 ID 在 RM 中持久存储，AI 可在后续轮次中通过 `send_trigger_images(resource_id)` 再次发送图片。

---

## 九、AI 集成：`@ai_tools` 装饰器

当触发器的 `to_ai` 桥接不够用（例如你需要一个纯数据查询接口、不返回图片），用 `@ai_tools` 直接注册工具函数。

### 9.1 四种函数模式

```python
from pydantic_ai import RunContext
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.bot import Bot
from gsuid_core.models import Event

# 模式一：RunContext（推荐，可同时访问 bot 和 ev）
@ai_tools(category="default")
async def query_char_data(
    ctx: RunContext[ToolContext],
    char_name: str,
    uid: str,
) -> str:
    """
    查询游戏角色的基础属性数据（文本格式）。

    Args:
        char_name: 角色名称
        uid: 游戏 UID
    """
    bot = ctx.deps.bot
    ev = ctx.deps.ev
    data = await fetch_char_data(uid, char_name)
    return f"【{char_name}】攻击: {data['atk']}  暴击: {data['crit']}"


# 模式二：自动注入 Event/Bot（不暴露给 LLM，LLM 不需要填这些参数）
@ai_tools
async def get_my_uid(ev: Event) -> str:
    """获取当前用户绑定的游戏 UID。无需任何参数。"""
    bind = await GameBind.get_bind(ev.user_id, ev.bot_id)
    if bind is None:
        return "您还没有绑定 UID"
    return f"您绑定的 UID：{bind.uid}"


# 模式三：无上下文（纯计算型工具）
@ai_tools(category="default")
async def calc_damage(
    atk: float,
    crit_rate: float,
    crit_dmg: float,
    multiplier: float = 1.0,
) -> str:
    """
    计算期望伤害。

    Args:
        atk: 攻击力
        crit_rate: 暴击率（0~1，如 0.7 表示 70%）
        crit_dmg: 暴击伤害（如 1.5 表示 150%）
        multiplier: 技能倍率，默认 1.0
    """
    expected = atk * multiplier * (1 + crit_rate * crit_dmg)
    return f"期望伤害：{expected:.0f}"
```

### 9.2 category 分类规则

| 分类 | 谁能调用 | 使用场景 |
|------|---------|---------|
| `"common"` | 主 Agent 直接调用 | 高频核心功能，主 Agent 直接可见 |
| `"default"` | 子 Agent（通过 `create_subagent`） | 复杂计算、文件操作等子任务 |
| `"<自定义>"` | 根据配置 | 插件专属分类 |

**主 Agent 工具越多 Token 消耗越大**，常用功能才放 `"common"`，其余放 `"default"` 或自定义分类（一般都放default）。

### 9.3 check_func 权限校验

```python
from gsuid_core.models import Event

async def check_bound(ev: Event) -> tuple[bool, str]:
    """校验用户是否已绑定账号"""
    bind = await GameBind.get_bind(ev.user_id, ev.bot_id)
    if bind is not None:
        return True, ""
    return False, "⚠️ 请先绑定账号：发送 '绑定 您的UID'"

def check_admin(ev: Event) -> tuple[bool, str]:
    """同步校验函数也支持"""
    ADMIN_LIST = ["123456789"]
    if ev.user_id in ADMIN_LIST:
        return True, ""
    return False, "⚠️ 此工具仅管理员可用"

# 使用 check_func：校验失败时不执行函数，直接返回错误消息给 AI
@ai_tools(category="common", check_func=check_bound)
async def query_my_data(ev: Event) -> str:
    """查询我的游戏数据（需要先绑定）"""
    bind = await GameBind.get_bind(ev.user_id, ev.bot_id)
    return f"UID: {bind.uid}"  # type: ignore[union-attr]  # check_func 已保证 bind 非 None
```

### 9.4 工具 docstring 规范

AI 工具的 docstring 是 AI 判断"是否调用"以及"如何传参"的依据，**必须清晰**：

```python
@ai_tools(category="common")
async def search_game_data(
    ctx: RunContext[ToolContext],
    query: str,
    category: str = "all",
    limit: int = 5,
) -> str:
    """
    搜索游戏内的数据（角色、装备、副本等）。
    当用户询问游戏相关信息但不知道具体名称时调用。

    Args:
        query: 搜索关键词，例如 "雷元素长枪角色" 或 "高暴击圣遗物套装"
        category: 搜索类别，可选 "character"/"weapon"/"artifact"/"all"，默认 "all"
        limit: 返回结果数量，默认 5，最大 20

    Returns:
        匹配结果的文本列表
    """
    ...
```

---

## 十、AI 集成：知识库与别名注册

### 10.1 注册知识库（`ai_entity`）

让 AI 在 RAG 检索时能找到插件相关的静态知识（命令说明、游戏数据等）：

```python
from gsuid_core.ai_core.register import ai_entity
from gsuid_core.ai_core.models import KnowledgePoint

# 在模块加载时调用，自动在启动时同步到向量数据库
ai_entity(KnowledgePoint(
    id="myplugin_commands",           # 全局唯一 ID，建议 {plugin}_{类型}_{编号}
    plugin="MyPlugin",
    title="MyPlugin 命令使用指南",
    content="""
# MyPlugin 使用指南

## 命令列表
- `查角色 <角色名>` — 查询角色培养详情，需要先绑定 UID
- `绑定 <UID>` — 绑定游戏账号
- `我的角色` — 查看全部角色列表
- `帮助` — 显示此帮助

## 注意事项
1. 所有查询功能需要先绑定账号
2. 每日查询上限为 100 次
3. 支持的游戏区域：cn（国服）、os（国际服）
""",
    tags=["MyPlugin", "帮助", "命令", "使用说明"],
))

ai_entity(KnowledgePoint(
    id="myplugin_genshin_shogun",
    plugin="MyPlugin",
    title="雷电将军 - 角色培养建议",
    content="""
# 雷电将军培养建议

## 推荐圣遗物
- 绝缘之旗印（4件套）：充能转化攻击，爆发效果极强

## 推荐武器
- 薙草之稻光（5星长枪）：充能提升+技能倍率加成

## 属性优先级
充能 160%+ → 暴击率 70%+ → 暴击伤害 → 攻击力
""",
    tags=["雷电将军", "雷神", "角色", "培养", "原神", "MyPlugin"],
))
```

**注意**：`id` 字段变化会触发重新索引，`content` 变化会通过 `_hash` 检测自动增量更新。

### 10.2 注册别名（`ai_alias`）

让 AI 在解析用户意图时进行专有名词归一化：

```python
from gsuid_core.ai_core.register import ai_alias

# 在模块级别调用（导入时即执行）
ai_alias("雷电将军", ["雷神", "将军", "影", "Raiden", "shogun"])
ai_alias("纳西妲", ["草神", "小草神", "Lesser Lord Kusanali"])
ai_alias("胡桃", ["小胡桃", "HuTao", "胡桃儿", "往生堂堂主"])

# 批量注册
GAME_ALIASES: dict[str, list[str]] = {
    "雷电将军": ["雷神", "将军"],
    "钟离": ["岩神", "摩拉克斯"],
    "万叶": ["楓原万叶", "枫原万叶"],
}
for name, aliases in GAME_ALIASES.items():
    ai_alias(name, aliases)
```

---

## 十一、AI 集成：`create_agent`

用于在触发器内部创建一个**临时的专用 AI Agent**，执行特定子任务（如文本分析、翻译、摘要）：

```python
from gsuid_core.ai_core.gs_agent import create_agent

# 模块级别创建（复用 Agent 实例）
summarizer = create_agent(
    system_prompt="""你是一个文本摘要专家。
将用户提供的文本压缩为不超过 100 字的摘要，保留核心信息，输出中文。
直接输出摘要，不加任何说明。""",
    max_tokens=500,
)

translator = create_agent(
    system_prompt="你是一个翻译助手，只负责将输入翻译为中文，不做解释。",
    max_tokens=1000,
)

# 在触发器中调用
@sv.on_command("摘要")
async def summarize_cmd(bot: Bot, ev: Event) -> None:
    text = ev.text.strip()
    if not text:
        return await bot.send("请在命令后提供要摘要的文本")
    result = await summarizer.run(user_message=text)
    await bot.send(f"摘要：\n{result}")

# 带结构化输出
from pydantic import BaseModel

class CharAnalysis(BaseModel):
    name: str
    element: str
    recommended: bool
    reason: str

char_analyzer = create_agent(
    system_prompt="你是原神角色分析专家，根据用户描述给出角色评价。"
)

@sv.on_command("分析角色")
async def analyze_char(bot: Bot, ev: Event) -> None:
    char_name = ev.text.strip()
    result: CharAnalysis = await char_analyzer.run(
        user_message=f"分析角色：{char_name}",
        bot=bot,
        ev=ev,
        output_type=CharAnalysis,  # 强制结构化输出
    )
    await bot.send(
        f"角色：{result.name}\n"
        f"元素：{result.element}\n"
        f"推荐：{'✅' if result.recommended else '❌'}\n"
        f"理由：{result.reason}"
    )
```

---

## 十二、完整插件示例

以下是一个包含全部核心功能的完整游戏查询插件示例，遵循 GsCore 插件命名规范。

### 12.1 命名规范

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

### 12.2 目录结构

省略插件所在文件夹（`gsuid_core/plugins/`）下的其他目录，只保留 `MyGameUID/` 目录。

```
/MyGameUID/          # 用户插件目录
├── __init__.py                        # 插件入口（可留空或导入子包）
├── __nest__.py                        # 嵌套加载入口
├── pyproject.toml                     # 插件依赖声明
└── MyGameUID/                         # 内部包（与插件目录同名）
    ├── __init__.py                    # 包初始化（导入各子模块触发注册）, 定义Plugins类
    ├── __full__.py                    # 空文件, 本身不含任何内容, 向框架标记嵌套加载：遍历子目录导入
    ├── version.py                     # 版本号
    ├── mygameuid_bind/                # 绑定功能子模块
    │   └── __init__.py
    ├── mygameuid_roleinfo/            # 角色查询子模块
    │   ├── __init__.py
    │   ├── draw_roleinfo.py           # 图片渲染
    │   └── texture2d/                 # 子模块专属资源
    │       └── bg.png
    ├── mygameuid_config/              # 配置子模块
    │   └── config.py
    └── utils/                         # 共享工具（不加前缀）
        ├── database/
        │   └── models.py              # 数据库模型
        ├── api.py                     # API 请求封装
        └── image.py                   # 图片工具
```

### 12.3 `MyGameUID/utils/database/models.py`

```python
from typing import Optional
from sqlmodel import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from gsuid_core.utils.database.base_models import BaseModel, with_session


class MyGameBind(BaseModel, table=True):
    """游戏账号绑定表"""
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
```

### 12.4 `MyGameUID/mygameuid_config/`

插件配置分为两个文件：`config_default.py` 定义默认配置项，`mygame_config.py` 创建 `StringConfig` 实例。

#### `config_default.py` — 配置项定义

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

#### `mygame_config.py` — 创建 StringConfig 实例

```python
from gsuid_core.utils.plugins_config.gs_config import StringConfig
from .config_default import CONFIG_DEFAULT
from ..utils.resource.RESOURCE_PATH import CONFIG_PATH

MYGAME_CONFIG = StringConfig("MyGameUID", CONFIG_PATH, CONFIG_DEFAULT)
```

> **配置类型一览**（`gsuid_core/utils/plugins_config/models.py`）：
>
> | 类型 | 说明 | `data` 类型 |
> |------|------|------------|
> | `GsStrConfig` | 字符串配置 | `str` |
> | `GsBoolConfig` | 布尔配置 | `bool` |
> | `GsIntConfig` | 整数配置 | `int` |
> | `GsListStrConfig` | 字符串列表 | `List[str]` |
> | `GsListConfig` | 整数列表 | `List[int]` |
> | `GsDictConfig` | 字典配置 | `Dict[str, List]` |
> | `GsImageConfig` | 图片配置 | `str` |
> | `GsTimeRConfig` | 时间范围 | `Tuple[int, int]` |
>
> 所有配置类型继承自 `GsConfig(msgspec.Struct)`，必须包含 `title`、`desc`、`data` 字段。

### 12.5 `MyGameUID/mygameuid_roleinfo/draw_roleinfo.py`

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

### 12.6 `MyGameUID/mygameuid_bind/__init__.py`

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

### 12.7 `MyGameUID/mygameuid_roleinfo/__init__.py`

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

### 12.8 `MyGameUID/__init__.py`（包入口）

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

### 12.9 `MyGameUID/__nest__.py`（嵌套加载入口）

空文件, 无需任何内容

```python
```

### 12.10 `__init__.py`（插件根目录入口）

```python
# 插件根目录的 __init__.py
# 对于 __nest__.py 模式，此文件可留空或仅做版本声明
```

---

## 十三、代码规范红线

GsCore 对代码质量有严格要求，以下规则**绝对禁止**：

### 13.1 禁止事项

```python
# ❌ 禁止：try-except 兜底（掩盖类型和逻辑问题）
try:
    result = data.get("key")
except (AttributeError, KeyError):
    result = None

# ❌ 禁止：cast() 类型强制转换
from typing import cast
result = cast(str, some_value)

# ❌ 禁止：type: ignore 抑制自身代码的类型错误
data = some_function()  # type: ignore

# ❌ 禁止：getattr/dict.get 兜底
name = getattr(user, "name", None)
value = data.get("key", None)

# ❌ 禁止：同步阻塞函数（整个项目是异步的）
def fetch_data(url: str) -> dict:
    import requests
    return requests.get(url).json()
```

### 13.2 正确做法

```python
# ✅ 正确：Union + isinstance 守卫
from typing import Union

def process(result: str | int | None) -> str:
    if isinstance(result, int):
        return str(result)
    if result is None:
        return ""
    return result

# ✅ 正确：所有函数必须有类型注解
async def get_user(user_id: str, bot_id: str) -> GameBind | None:
    return await GameBind.get_bind(user_id, bot_id)

# ✅ 正确：TypedDict 代替裸字典
from typing import TypedDict

class CharData(TypedDict):
    level: int
    constellation: int
    weapon: str

# ✅ 正确：全部使用异步 I/O
async def fetch_data(url: str) -> dict:
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        return resp.json()
```

### 13.3 `ai_return` 辅助函数的特殊说明

`_ai_return_xxx()` 系列辅助函数是**唯一允许使用 `try/except`** 的地方，因为：
1. 它们是观测性代码，不属于业务逻辑
2. 提取失败绝对不能影响图片生成和发送
3. 失败时只记录 `logger.warning`，不 raise

```python
# ✅ 唯一允许 try/except 的地方
def _ai_return_xxx(data: dict) -> None:
    try:
        result = f"..."
        ai_return(result)
    except Exception as e:
        logger.warning(f"[插件名] ai_return 数据提取失败: {e}")
```
