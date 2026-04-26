# GsCore 代码编写红线与边界规范

> 本文档面向 AI Agent，用于指导对 GsCore 项目的代码编辑工作。所有代码修改必须严格遵循本文档规定的红线规则。

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

## 八、总结

编辑本项目代码时，记住以下优先级：

1. **类型问题 → 从类型标注和代码逻辑解决，不使用兜底语法**
2. **数据库操作 → 继承基类 + @with_session 装饰器**
3. **异步要求 → 所有可能阻塞的方法都用 async def**
4. **代码组织 → 相关方法封装在类中，使用 dataclass/TypedDict 定义数据结构**

如遇到无法解决的多类型冲突，且运行时类型确定可以使用 assert 守卫，必要时可用 Union 标注后配合 isinstance 处理。
