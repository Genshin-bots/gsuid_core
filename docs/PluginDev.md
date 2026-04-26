# GsCore 插件开发指南

> 本文档面向插件开发者，介绍 GsCore 插件开发的核心概念、常用 API 和最佳实践。

---

## 一、插件基础结构

### 1.1 插件目录结构

```
gsuid_core/plugins/<插件名>/
├── __init__.py          # 插件入口（可留空）
├── <插件名>.py          # 主逻辑文件
├── config.json          # 插件配置项（可选）
├── utils/               # 工具模块目录
│   ├── __init__.py
│   ├── database/        # 数据库模型
│   │   └── models.py
│   └── ...
└── resource/            # 静态资源目录
    └── ...
```

### 1.2 SV 服务模块

SV 是插件的核心类，用于注册触发器和配置管理：

```python
from gsuid_core.sv import SV

# 创建 SV 实例，name 应与插件目录名一致
sv = SV(
    name="我的插件",
    pm=6,                    # 权限等级（1-10，数字越大权限越高）
    priority=5,              # 优先级，数字越小越先执行
    enabled=True,            # 是否启用
    area="ALL",             # 作用范围：GROUP(群聊) / DIRECT(私聊) / ALL
    black_list=[],          # 黑名单
    white_list=[],          # 白名单
)
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | str | - | SV 名称，应与插件目录名一致 |
| `pm` | int | 6 | 权限等级，1-10 |
| `priority` | int | 5 | 优先级，数字越小越先执行 |
| `enabled` | bool | True | 是否启用 |
| `area` | str | "ALL" | 作用范围：GROUP/DIRECT/ALL |
| `black_list` | list | [] | 用户 ID 黑名单 |
| `white_list` | list | [] | 用户 ID 白名单 |

---

## 二、触发器（Trigger）

### 2.1 触发器类型

GsCore 支持多种触发器类型：

| 类型 | 说明 | 示例 |
|------|------|------|
| `prefix` | 前缀匹配 | 用户说 "帮助" 触发 |
| `suffix` | 后缀匹配 | 用户说 "是什么" 触发 |
| `keyword` | 关键词匹配 | 用户消息包含 "原神" 触发 |
| `fullmatch` | 完全匹配 | 用户消息完全等于 "绑定" 触发 |
| `command` | 命令匹配 | 用户说 "/帮助" 触发 |
| `regex` | 正则匹配 | 按正则表达式匹配 |
| `file` | 文件匹配 | - |
| `message` | 消息类型匹配 | - |

### 2.2 绑定触发器到 SV

```python
from gsuid_core.trigger import Trigger
from gsuid_core.sv import SV

sv = SV(name="我的插件")

# 方式一：在 SV 创建时通过 on_command 绑定
@sv.on_command(command="帮助", prefix="/", alias=["help", "？"])
async def help_handler(bot, event):
    ...

# 方式二：手动创建 Trigger 并注册
sv.append_trigger(
    Trigger(
        type="keyword",      # 触发器类型
        keyword="原神",       # 匹配的关键词
        func=my_handler,     # 处理函数
        prefix="",           # 前缀（命令前导符）
        block=True,          # 是否阻止事件继续传递
        to_me=False,         # 是否需要 @机器人
    )
)
```

### 2.3 处理函数签名

所有触发器处理函数必须遵循以下签名：

```python
from gsuid_core.bot import Bot
from gsuid_core.models import Event

async def my_handler(bot: Bot, event: Event) -> None:
    """
    处理函数

    Args:
        bot: Bot 实例，用于发送消息
        event: Event 实例，包含用户消息和上下文信息
    """
    user_id = event.user_id
    group_id = event.group_id
    message = event.raw_text

    # 回复消息
    await bot.send(message="处理完成！")
```

---

## 三、消息收发

### 3.1 Bot 实例方法

```python
# 发送文字消息
await bot.send(message="Hello!")

# 发送图片（支持 base64、URL、文件路径）
await bot.send(image="base64://xxxxx")
await bot.send(image="https://example.com/image.png")

# 发送图片消息段
from gsuid_core.message import MessageSegment
await bot.send(MessageSegment.image(image_id))

# 回复（引用原消息）
await bot.reply(message="这是回复")

# 发送 Markdown
await bot.send_markdown(content="# 标题\n这是内容")
```

### 3.2 Event 对象常用属性

```python
event.user_id        # 用户 ID
event.group_id       # 群 ID（私聊为 None）
event.raw_text       # 原始消息文本
event.text           # 处理后的文本
event.bot_self_id    # 机器人自身 ID
event.is_tome        # 是否 @机器人
event.message        # 消息对象列表
event.user_nickname  # 用户昵称
```

### 3.3 多步会话（Response）

GsCore 支持多步会话，用于需要用户多次交互的场景：

```python
from gsuid_core.models import Response

# 创建会话上下文
resp = Response(
    event=event,
    context={},           # 存储上下文数据
    timeout=60,            # 超时时间（秒）
    delete_after_use=True  # 使用后是否删除
)

# 设置下一步处理函数
resp.set_next_handler(next_handler_func)

# 发送消息并等待用户输入
await bot.send(
    message="请输入您的姓名：",
    response=resp          # 携带会话上下文
)
```

---

## 四、配置管理

### 4.1 配置模型类

GsCore 提供了统一的配置模型类：

```python
from gsuid_core.utils.plugins_config.gs_config import GsConfig
from gsuid_core.utils.plugins_config.models import (
    GsStrConfig,    # 字符串配置
    GsBoolConfig,   # 布尔配置
    GsIntConfig,     # 整数配置
    GsListConfig,    # 列表配置（整数）
    GsListStrConfig, # 字符串列表配置
    GsDictConfig,    # 字典配置
    GsImageConfig,   # 图片配置
    GsTimeConfig,    # 时间配置（已废弃）
    GsTimeRConfig,   # 时间范围配置
)

class MyPluginConfig(GsConfig):
    @property
    def config_name(self) -> str:
        return "my_plugin"

    def setup_config(self) -> dict[str, GsConfig]:
        return {
            "api_key": GsStrConfig(
                title="API Key",
                description="输入您的 API Key",
                default=""
            ),
            "max_count": GsIntConfig(
                title="最大数量",
                description="最大处理数量",
                default=10
            ),
            "enable_feature": GsBoolConfig(
                title="启用功能",
                description="是否启用该功能",
                default=True
            )
        }
```

### 4.2 获取配置值

```python
# 获取插件配置实例
config = sv.config  # 或通过 Plugins 实例获取
config = sv.plugins

# 获取配置值（已实例化后，data 属性包含实际值）
api_key = config.api_key.data
max_count = config.max_count.data
```

### 4.3 修改配置值

```python
# 运行时修改配置（会自动持久化）
config.api_key.data = "new_api_key"

# 或使用 set 方法
config.set(api_key="new_api_key")
```

---

## 五、数据库操作

### 5.1 模型基类

GsCore 使用 SQLModel 作为 ORM，提供三级基类：

```python
from gsuid_core.utils.database.base_models import (
    BaseIDModel,      # 仅 id 字段
    BaseBotIDModel,   # id + bot_id
    BaseModel,        # id + bot_id + user_id
)
from sqlmodel import Field

class UserData(BaseModel, table=True):
    """用户数据表，包含 bot_id 和 user_id"""
    name: str = Field(title="名称")
    level: int = Field(default=1, title="等级")
```

### 5.2 @with_session 装饰器

所有数据库操作方法必须使用 `@with_session` 装饰器：

```python
from gsuid_core.utils.database.base_models import with_session
from sqlalchemy.ext.asyncio import AsyncSession

class UserData(BaseModel):

    @classmethod
    @with_session
    async def get_user_by_name(cls, session: AsyncSession, name: str) -> UserData | None:
        """根据名称查询用户"""
        from sqlalchemy import select
        stmt = select(cls).where(cls.name == name)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def create_user(cls, session: AsyncSession, name: str, level: int = 1) -> UserData:
        """创建新用户"""
        user = cls(name=name, level=level)
        session.add(user)
        # @with_session 会自动 commit
        return user
```

**注意**：
- 方法签名必须包含 `session: AsyncSession` 参数
- `@with_session` 会自动处理 commit 和异常回滚
- 方法必须是 `async def`

### 5.3 复杂场景下的 async_maker

当需要在类方法外手动管理 session 时：

```python
from gsuid_core.utils.database.base_models import async_maker

async def batch_operation():
    async with async_maker() as session:
        from sqlalchemy import select
        stmt = select(Data)
        result = await session.execute(stmt)
        await session.commit()
        return result.scalars().all()
```

---

## 六、定时任务

### 6.1 APScheduler 定时任务

GsCore 内部使用 APScheduler 实现定时任务：

```python
from gsuid_core.aps import scheduler

# 添加定时任务
scheduler.add_job(
    func=my_task,                    # 任务函数
    trigger="cron",                  # 触发器类型
    second=0,                        # 每分钟的第 0 秒执行
    # 或使用其他参数：
    # hour=8, minute=30             # 每天 8:30 执行
    # day=1, hour=0, minute=0      # 每月 1 日执行
    # args=(arg1, arg2),            # 位置参数
    # kwargs={"key": value},        # 关键字参数
)

# 添加间隔任务
scheduler.add_job(
    func=my_task,
    trigger="interval",
    minutes=30,                      # 每 30 分钟执行一次
)

async def my_task():
    """定时任务函数"""
    ...
```

### 6.2 任务调度器触发器类型

| 触发器 | 说明 | 常用参数 |
|--------|------|----------|
| `date` | 一次性任务 | `run_date` |
| `interval` | 间隔任务 | `seconds`/`minutes`/`hours`/`days` |
| `cron` | Cron 表达式 | `second`/`minute`/`hour`/`day`/`month`/`day_of_week` |

---

## 七、资源路径

### 7.1 获取资源目录

```python
from gsuid_core.utils.resource_manager import get_res_path

# 获取插件资源根目录
res_path = get_res_path()

# 拼接资源子目录
config_path = res_path / "config"
data_path = res_path / "data"
image_path = res_path / "images"

# 检查路径是否存在
if data_path.exists():
    ...
```

### 7.2 插件专属资源目录

```python
from gsuid_core.utils.resource_manager import get_plugin_data_path

# 获取插件专属数据目录
plugin_data_path = get_plugin_data_path(plugin_name)
```

---

## 八、日志

### 8.1 使用日志器

```python
from gsuid_core.logger import Logger

logger = Logger("MyPlugin")

logger.info("操作信息")
logger.warning("警告信息")
logger.error("错误信息", exc_info=True)  # exc_info=True 打印完整堆栈
logger.debug("调试信息")
```

---

## 九、订阅功能

### 9.1 订阅消息机制

GsCore 支持消息订阅功能，用于定时向用户推送消息：

```python
from gsuid_core.subscribe import Subscribe

# 创建订阅实例
subscribe = Subscribe(
    name="原神便签",           # 订阅名称
    config_name="notice_config" # 配置文件名
)

# 添加订阅用户
await subscribe.add_subscriber(
    bot_id=bot_id,
    user_id=user_id,
    group_id=group_id,
    extra_data={}  # 额外数据
)

# 发送订阅消息
await subscribe.send_msg(
    bot_id=bot_id,
    content="您的便签内容",
    user_id=user_id,
    group_id=group_id
)
```

---

## 十、最佳实践

### 10.1 插件初始化

```python
from gsuid_core.sv import SV
from gsuid_core.logger import Logger

logger = Logger("MyPlugin")
sv = SV(name="my_plugin")

# 导出插件配置类
from .config import MyPluginConfig

__plugin_config_class__ = MyPluginConfig

# 初始化工作（如有需要）
async def on_load():
    """插件加载时执行"""
    logger.info("插件加载完成")

# 清理工作（如有需要）
async def on_unload():
    """插件卸载时执行"""
    logger.info("插件已卸载")
```

### 10.2 错误处理

```python
async def my_handler(bot, event):
    try:
        # 业务逻辑
        result = await do_something()
    except ValueError as e:
        # 业务异常，发送友好提示
        await bot.send(message=f"操作失败：{e}")
    except Exception as e:
        # 其他异常，记录日志
        logger.error(f"Unexpected error: {e}", exc_info=True)
        await bot.send(message="发生未知错误，请联系管理员")
```

### 10.3 权限检查

```python
from gsuid_core.models import Permission

async def admin_handler(bot, event):
    # 检查权限
    if event.permission < Permission.ADMIN:
        await bot.send(message="此操作需要管理员权限")
        return

    # 管理员操作
    ...
```

---

## 附录：类型提示参考

GsCore 项目**严格要求**类型提示，详见 [LLM.md](./LLM.md)。核心要点：

1. **禁止使用 try-except、cast()、type: ignore、getattr/dict.get 兜底**
2. **遇到类型问题从类型标注和代码逻辑解决**
3. **复杂类型用 Union + isinstance 守卫**
4. **所有函数必须 async def，返回值必须有类型注解**
