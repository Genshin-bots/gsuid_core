# GsCore 代码编写红线与边界规范

> 本文档面向 AI Agent，用于指导对 GsCore 项目的代码编辑工作。所有代码修改必须严格遵循本文档规定的红线规则。
>
> 需要框架架构 / 触发链路 / 启动时序 / AI 子系统 / 已知坑的系统级说明，见
> [`docs/skills/gscore-development/SKILL.md`](skills/gscore-development/SKILL.md)。

---

## 一、绝对红线（Strict Red Lines）

以下规则为**绝对禁止**，违反将导致代码质量严重下降，必须从根源上避免：

### 1.1 禁止使用 try-except 兜底

```python
# ❌ 错误示例
try:
    result = data.get("key")
except (AttributeError, KeyError):
    result = None
```

遇到类型错误或属性访问问题，**必须从类型提示标注和代码逻辑上解决**，而非用 try-except 吞掉异常。

### 1.2 禁止使用 cast() 类型强制转换

```python
# ❌ 错误示例
result = cast(str, some_unknown_type)
```

cast 是类型层面的欺骗，掩盖了真实的类型问题。遇到类型冲突应该：
- 使用 `Union` 标记多种可能的类型
- 使用 `isinstance` 进行类型守卫
- 调整函数签名以反映真实的类型约束

### 1.3 禁止使用 type: ignore 抑制类型检查

```python
# ❌ 错误示例
data = some_function()  # type: ignore
```

type: ignore 是最后手段，仅在**第三方库类型标注错误且无法绕过**时使用（如某些 ORM 框架的已知问题）。**不得用于掩盖自身代码的类型问题**。

### 1.4 禁止使用 getattr/dict.get 等兜底语法

```python
# ❌ 错误示例
name = getattr(user, "name", None)
value = data.get("key", None)
```

这些语法暗示了对类型的不确定。应该：
- 明确定义类型（使用 TypedDict、数据类、泛型）
- 使用 `isinstance` 进行类型守卫后安全访问
- 如果确定某个键/属性存在，直接访问并让类型检查器验证

### 1.5 遇到类型标红的正确解决思路

```
标红 → 分析原因 → 类型定义是否正确 → 逻辑是否有漏洞 → Union + isinstance → 最后才考虑 assert
```

**核心原则**：类型标红是类型系统在告诉你代码存在潜在问题，而不是让你去压制它。

### 1.6 禁止冗长注释（`#` 注释必须精简直接）

**`#` 注释最多两行，每行不超过 88 个字符。** 注释的价值在于"用最精确的一句话点明为什么 /
有什么坑"，而不是把代码翻译成中文、或写五六行长篇大论。

```python
# ❌ 错误示例：五六行长篇大论 + 单行超长
# 这个函数用来处理用户发来的消息，首先解析消息内容，然后判断消息类型，接着根据类型分发到不同
# 的处理器，如果是命令就走命令处理器，不是命令就走 AI 处理器，最后把结果返回给调用方，由调用
# 方决定怎么发送出去……（把代码又用中文复述一遍，毫无信息增量，且第二行已超 88 字）
async def handle(msg): ...

# ✅ 正确示例：一句话点明非显而易见的关键点
# 群聊 user_id 置空以保证同群共享同一个 deque（见 §3.1.1）
async def handle(msg): ...
```

**写注释的判据**：

- 代码已能自解释的**不写**注释——不要把 Python 翻译成中文。
- 要写就写**为什么这样做 / 有什么坑 / 边界条件**，不写"做了什么"。
- 一条注释两行讲不清，说明这段逻辑该拆函数 / 改命名，而不是堆注释。
- 超过两行 = 信号：要么删到两行内，要么这段代码本身需要重构。

> 该上限（每行 88 字）比代码 `line-length=120` 更严，是**刻意**的：注释越短越会被读，长注释
> 会被跳过、且极易随代码改动过期变成误导。用最精确的注释给后续维护者指导，而非长篇大论。

---

## 二、类型提示规范

### 2.1 完全类型提示原则

所有函数、方法的参数和返回值**必须有类型注解**：

```python
# ✅ 正确示例
async def process_user(user_id: str, bot_id: str) -> User | None:
    ...

# ❌ 错误示例
async def process_user(user_id, bot_id):
    ...
```

### 2.2 Union vs Optional 的选择

```python
# ✅ 推荐：当存在多种具体类型时使用 Union
result: str | int | None  # 三种可能

# ✅ 推荐：当值可能不存在时使用 Optional (即 | None)
name: str | None  # 要么是字符串，要么是 None
```

### 2.3 复杂数据结构的类型定义

使用 `TypedDict` 定义结构化字典：

```python
from typing import TypedDict

class UserProfile(TypedDict):
    user_id: str
    nickname: str
    level: int
    items: list[str]
```

使用 `@dataclass` 定义配置类：

```python
from dataclasses import dataclass

@dataclass
class BotConfig:
    bot_id: str
    auto_retry: bool
    max_retry: int
```

### 2.4 多类型冲突的正当处理方式

当一个值确实可能返回多种类型，且无法在类型层面统一时：

```python
# ✅ 正确：使用 Union + isinstance 守卫
def process_result(result: str | int | dict) -> str:
    if isinstance(result, dict):
        return result.get("message", "")
    elif isinstance(result, int):
        return str(result)
    else:
        return result

# ✅ 正确：谨慎使用 assert（仅当确定运行时类型时）
assert isinstance(data, str), "data must be str at this point"
```

---

## 三、数据库操作规范

### 3.1 数据库基类继承体系

GsCore 使用 SQLModel 作为 ORM，数据库模型应继承 `gsuid_core/utils/database/base_models.py` 中的基类：

```
BaseIDModel          # 最基础，只有 id 字段
    └── BaseBotIDModel  # 包含 bot_id 字段
            └── BaseModel   # 包含 bot_id + user_id 字段
```

```python
from gsuid_core.utils.database.base_models import BaseIDModel, BaseBotIDModel, BaseModel

class MyData(BaseModel, table=True):
    """需要 bot_id 和 user_id 的数据表"""
    name: str = Field(title="名称")
```

### 3.1.1 SQLModel 表命名规范

SQLModel **不使用** `__tablename__` 属性。表名由类名自动推导，规则为**全小写、无下划线**：

```python
# ✅ 正确：类名 AiMemeRecord → 表名 aimemerecord
class AiMemeRecord(SQLModel, table=True):
    meme_id: str = Field(primary_key=True)

# ❌ 错误：不要使用 __tablename__
class MemeRecord(SQLModel, table=True):
    __tablename__ = "ai_meme_records"  # 禁止！
```

**命名示例**：

| 类名 | 表名（自动推导） |
|------|-----------------|
| `AiMemeRecord` | `aimemerecord` |
| `AIMemEpisode` | `aimemepisode` |
| `CoreUser` | `coreuser` |
| `AIScheduledTask` | `aischeduledtask` |

**规则**：
1. 类名使用 PascalCase（大驼峰）
2. 表名自动为类名的全小写形式
3. **禁止**使用 `__tablename__` 覆盖
4. 如果需要自定义表名约束（如索引），使用 `__table_args__`

### 3.2 @with_session 装饰器的使用

所有数据库类方法必须使用 `@with_session` 装饰器，它会自动：
- 创建 session
- 处理事务提交
- 异常时回滚
- 归还连接池

```python
from gsuid_core.utils.database.base_models import with_session
from sqlalchemy.ext.asyncio import AsyncSession

class User(BaseModel):
    @classmethod
    @with_session
    async def get_user_by_name(cls, session: AsyncSession, name: str) -> User | None:
        stmt = select(cls).where(cls.name == name)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
```

**注意**：使用 `@with_session` 时，函数签名必须包含 `session: AsyncSession` 参数作为第二个参数（紧跟 cls 或 self）。

### 3.3 复杂场景下的 async_maker()

当需要在类方法外手动管理 session，或需要更精细控制事务时：

```python
from gsuid_core.utils.database.base_models import async_maker

async def batch_operation():
    async with async_maker() as session:
        # 手动管理 session
        result = await session.execute(select(Data))
        await session.commit()
        return result.scalars().all()
```

### 3.4 数据库方法必须写在类中

所有与特定表相关的数据库操作应封装在该表对应的模型类中，而非散落在各处：

```python
class CoreUser(BaseBotIDModel, table=True):
    @classmethod
    @with_session
    async def clean_repeat_user(cls, session: AsyncSession):
        # 数据库操作写在类中
        ...
```

### 3.5 SQLModel / SQLAlchemy 查询的类型安全写法

ORM 查询是 `cast` / `type: ignore` / `getattr` 的重灾区。下面三条是**从根源消除**这些兜底的正确写法，
违反 §1.2~1.4 去糊弄 basedpyright 的标红，先回到这里。

#### 3.5.1 比较表达式一律用 `col()` 包裹列

SQLModel 字段注解是 Python 类型（如 `created_at: int`），所以 `cls.created_at >= ts` 被类型检查器判为
**`bool`**，传进 `where()` 会标红：`"bool" 不能赋值给 _ColumnExpressionArgument[bool]`。用 `col()` 把列
还原成 `ColumnElement`，比较结果才是 `ColumnElement[bool]`：

```python
# ❌ 错误：cls.created_at >= ts 是 bool
stmt = delete(cls).where(cls.created_at >= since_ts)

# ✅ 正确：col() 包裹得到 ColumnElement[bool]
stmt = delete(cls).where(col(cls.created_at) >= since_ts)
```

`where` / `order_by` / `group_by` / `!=` / `.is_(False)` **全部**适用。

> 陷阱：`select(cls).where(cls.x == v)` 恰好**不报错**——SQLModel 的 `Select.where` 重载把 `bool` 也
> 收进了 union；但 `delete()`/`update()` 是 SQLAlchemy 原生、`where` 严格只收 `ColumnElement[bool]` 就会
> 报错。**不要依赖前者的宽松，一律 `col()` 包裹**，写法统一且不踩 DML 的坑。

#### 3.5.2 `rowcount` 用 `isinstance(result, CursorResult)` 守卫

`session.execute()` 静态返回 `Result[Any]`，**没有** `rowcount`（标红 `reportAttributeAccessIssue`）。但
DML（`delete`/`update`）运行时真实返回的是 `CursorResult`。用类型守卫安全取值，而不是
`cast(CursorResult, ...)` / `getattr(result, "rowcount", 0)` / `# type: ignore`：

```python
from sqlalchemy.engine import CursorResult

result = await session.execute(delete(cls).where(col(cls.created_at) < before_ts))
deleted = result.rowcount if isinstance(result, CursorResult) else 0
```

#### 3.5.3 不要把运行时变长列表 splat 进 `select()`

`select(*cols, total)`（`cols` 是运行时按分支拼出来的 `list`）无法匹配 `select` 的重载
（`No overloads for "select" match`），且 `row[i]` 退化成 `Any`、行下标全无类型。**按分支写出列数确定的
`select()`**，结果类型收敛为 `Select[tuple[...]]`，行下标自动带类型：

```python
# ❌ 错误：变长 splat，select 无重载匹配、row 退化为 Any
stmt = select(*group_cols, total).where(...)

# ✅ 正确：列数确定 → Select[tuple[str, str, int]]，r[0]/r[1]/r[2] 有类型
conds: List[ColumnElement[bool]] = [col(cls.created_at) >= since_ts]
stmt = select(col(cls.group_id), col(cls.user_id), total).where(*conds)
```

---

## 四、异步编程规范

### 4.1 全部使用异步方法

这是一个**完全异步的项目**，所有可能阻塞的方法都必须定义为 `async def`：

```python
# ✅ 正确
async def fetch_data(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        return (await client.get(url)).json()

# ❌ 错误：同步阻塞
def fetch_data(url: str) -> dict:
    with requests.get(url) as response:
        return response.json()
```

### 4.2 同步代码的异步桥接

如有确实需要使用的同步代码，使用 `to_thread` 工具：

```python
from gsuid_core.pool import to_thread

@to_thread
def sync_calculation(data: list) -> int:
    # 同步的 CPU 密集型操作
    return sum(data)

# 调用时无需 await
result = await sync_calculation(my_list)
```

---

## 五、项目核心模块概览

### 5.1 ai_core 模块

AI 核心模块，位于 `gsuid_core/ai_core/`，包含：

| 子模块 | 用途 |
|--------|------|
| `memory/` | 记忆系统，包含 entity、edge、hiergraph、retrieval、vector 等 |
| `rag/` | RAG 知识库系统 |
| `scheduled_task/` | 定时任务系统 |
| `persona/` | 人设系统 |
| `history/` | 对话历史管理 |
| `web_search/` | 网页搜索功能 |

### 5.2 webconsole 模块

Web 控制台 API，位于 `gsuid_core/webconsole/`。所有 API 路由使用 FastAPI：

```python
from fastapi import APIRouter, Depends
from gsuid_core.webconsole.auth_api import require_auth

app = APIRouter()

@app.get("/api/example")
async def example_endpoint(_user: Dict = Depends(require_auth)):
    ...
```

### 5.3 utils 模块

工具模块，位于 `gsuid_core/utils/`：

| 子模块 | 用途 |
|--------|------|
| `database/` | 数据库相关，base_models.py 包含核心基类 |
| `image/` | 图片处理工具 |
| `api/` | 第三方 API 请求封装 |
| `plugins_config/` | 插件配置管理 |
| `upload/` | 文件上传工具 |

---

## 六、配置管理

### 6.1 插件配置类

使用 `gsuid_core/utils/plugins_config/` 下的配置类管理插件配置：

```python
from gsuid_core.utils.plugins_config.gs_config import GsConfig

class MyPluginConfig(GsConfig):
    """插件配置类"""

    @property
    def config_name(self) -> str:
        return "my_plugin"

    def setup_config(self) -> Dict[str, GSC]:
        return {
            "api_key": GsStrConfig(
                title="API Key",
                description="输入 API Key",
                default=""
            ),
            "max_count": GsIntConfig(
                title="最大数量",
                description="最大处理数量",
                default=10
            )
        }
```

### 6.2 资源配置路径

```python
from gsuid_core.utils.resource_manager import get_res_path

# 获取资源目录路径
res_path = get_res_path()
config_path = res_path / "config"
data_path = res_path / "data"
```

---

## 七、日志规范

使用项目封装的日志器：

```python
from gsuid_core.logger import Logger

logger = Logger("MyModule")

logger.info("操作开始...")
logger.warning("需要注意的情况")
logger.error("错误信息", exc_info=True)
```

---

## 八、Bot 与 _Bot 类区分（关键知识）

> **⚠️ 这是高频混淆点**：`_Bot` 和 `Bot` 是两个完全不同的类，混用会导致运行时错误。

### 8.1 `_Bot` — 底层 Bot 实现

**文件**: `gsuid_core/bot.py`，**构造函数**: `_Bot(_id: str, ws: Optional[WebSocket] = None)`

底层实现，负责 WebSocket 连接管理、消息队列、发送调度。**不依赖 Event**。

```python
class _Bot:
    def __init__(self, _id: str, ws: Optional[WebSocket] = None):
        self.bot_id = _id
        self.bot = ws              # WebSocket 连接（可为 None）
        self.queue = asyncio.queues.PriorityQueue()
        self.sem = asyncio.Semaphore(10)
        self._send_queue = asyncio.queues.Queue()
```

**使用场景**: 框架内部连接管理、HTTP API 模式（`_Bot("HTTP")`）。

### 8.2 `Bot` — 高层包装器

**文件**: `gsuid_core/bot.py`，**构造函数**: `Bot(bot: _Bot, ev: Event)`

高层包装器，封装 `_Bot` + `Event`，供插件和触发器使用。提供 `send()`、`receive_resp()` 等业务 API。

```python
class Bot:
    def __init__(self, bot: _Bot, ev: Event):
        self.bot = bot              # 底层 _Bot 实例
        self.ev = ev                # 当前事件
        self.bot_id = ev.bot_id
        self.bot_self_id = ev.bot_self_id
```

**使用场景**: 插件触发器函数参数 `bot: Bot`、AI Agent 调用、MockBot 包装。

### 8.3 关键区别

| 特性 | `_Bot` | `Bot` |
|------|--------|-------|
| 构造参数 | `_id: str, ws: Optional[WebSocket]` | `bot: _Bot, ev: Event` |
| 依赖 Event | ❌ | ✅ 强依赖 |
| send 方法 | `target_send()` 需完整参数 | `send()` 自动从 ev 提取 |
| 交互式等待 | ❌ | ✅ `receive_resp()` |
| 适用场景 | 框架内部、连接管理 | 插件开发、触发器函数 |

### 8.4 禁止混用

```python
# ❌ 错误：在需要 Bot 的地方传入 _Bot
from gsuid_core.bot import _Bot
mock_bot = _Bot("MCP_Server")  # 缺少 Event，send() 会崩溃

# ✅ 正确：创建完整的 Bot 实例
from gsuid_core.bot import _Bot, Bot
_bot = _Bot("MCP_Server")
mock_ev = Event()
bot = Bot(_bot, mock_ev)  # 包含 _Bot + Event
```

---

## 九、总结

编辑本项目代码时，记住以下优先级：

1. **类型问题 → 从类型标注和代码逻辑解决，不使用兜底语法**
2. **数据库操作 → 继承基类 + @with_session 装饰器**
3. **异步要求 → 所有可能阻塞的方法都用 async def**
4. **代码组织 → 相关方法封装在类中，使用 dataclass/TypedDict 定义数据结构**
5. **Bot 类型 → 插件/触发器用 `Bot`（高层），框架内部用 `_Bot`（底层），禁止混用**
6. **注释精简 → `#` 注释最多两行、每行 ≤88 字，只写"为什么/坑/边界"，不复述代码**

如遇到无法解决的多类型冲突，且运行时类型确定可以使用 assert 守卫，必要时可用 Union 标注后配合 isinstance 处理。
