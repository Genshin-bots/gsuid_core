# 五、数据库操作

GsCore 使用 SQLModel 作为 ORM，**所有数据库操作必须在模型类内部**，使用 `@with_session` 装饰器管理会话。

## 5.1 三级基类

```python
from gsuid_core.utils.database.base_models import (
    BaseIDModel,      # 只有 id 字段（自增主键）
    BaseBotIDModel,   # id + bot_id
    BaseModel,        # id + bot_id + user_id  ← 最常用
)
```

## 5.2 定义数据模型

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

## 5.3 `@with_session` 装饰器

所有数据库操作方法必须使用 `@with_session` 装饰器：

```python
from gsuid_core.utils.database.base_models import BaseModel, with_session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field

class UserData(BaseModel, table=True):
    name: str = Field(title="名称")
    level: int = Field(default=1, title="等级")

    @classmethod
    @with_session
    async def get_user_by_name(
        cls, session: AsyncSession, name: str
    ) -> 'UserData | None':
        """根据名称查询用户"""
        from sqlalchemy import select
        stmt = select(cls).where(cls.name == name)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def create_user(
        cls, session: AsyncSession, name: str, level: int = 1
    ) -> 'UserData':
        """创建新用户"""
        user = cls(name=name, level=level)
        session.add(user)
        # @with_session 会自动 commit
        return user
```

**规则要点**：

- **必须是 `classmethod`** 且 **`async def`**
- `session: AsyncSession` 必须是第二个参数（紧跟 `cls`）
- 装饰器自动 commit，异常自动回滚
- `@with_session` 已处理事务，**不要**在方法内手动 `await session.commit()`

## 5.4 `async_maker` — 手动管理 Session

当需要在类方法外手动管理 session 时（例如批量操作、定时任务中的数据库清理等）：

```python
from gsuid_core.utils.database.base_models import async_maker

async def batch_cleanup():
    async with async_maker() as session:
        from sqlalchemy import delete
        stmt = delete(GameBind).where(GameBind.cookie == None)
        await session.execute(stmt)
        await session.commit()  # ⚠️ 使用 async_maker 时必须手动 commit
```

> **⚠️ 警告**：使用 `async_maker` 时需要手动调用 `await session.commit()`，这与 `@with_session` 装饰器自动 commit 不同。

## 5.5 把数据库表注册到 Web 控制台

参照 ZZZeroUID / SayuStock 的写法，给业务表加一个 `@site.register_admin` 装饰的
`GsAdminModel` 子类，Web 控制台启动后会自动出现该表的可视化管理页（增删改查 + 字段过滤）。

```python
# MyPlugin/utils/database/models.py
from typing import Optional
from sqlmodel import Field

from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site
from gsuid_core.utils.database.base_models import Push  # 或 Bind / BaseModel


class MyPush(Push, table=True):
    __table_args__ = {"extend_existing": True}
    bot_id: str = Field(title="平台")
    my_uid: str = Field(default=None, title="游戏 UID")

    # title / schema_extra.hint 会在 webconsole 表格中作为列头 / 提示文字展示
    energy_push: Optional[str] = Field(
        title="体力推送",
        default="off",
        schema_extra={"json_schema_extra": {"hint": "mp 开启体力推送"}},
    )
    energy_value: Optional[int] = Field(title="电量阈值", default=180)
    energy_is_push: Optional[str] = Field(title="电量是否已推送", default="off")


@site.register_admin
class MyPushAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="MyPlugin 推送管理",   # 左侧菜单显示文本
        icon="fa fa-bullhorn",       # Font Awesome 图标
    )  # type: ignore

    # 把上面的 SQLModel 表绑定到这个 admin 页
    model = MyPush
```

**注意要点**：
- `Push` / `Bind` / `BaseModel` 等基类已经在 `utils/database/base_models.py` 中包含 `bot_id` / `user_id` / `uid` 等
  公共字段；按业务实际需要选基类。
- `__table_args__ = {"extend_existing": True}` 必加——允许同名表在多次 reload 时重新绑定。
- `Field` 的 `title=...` 同时是 SQLModel 字段元数据和 webconsole 列标题。
- `page_schema.icon` 取 [Font Awesome 4](https://fontawesome.com/v4/icons/) 图标名（不带版本号）。
- 业务字段的 `schema_extra={"json_schema_extra": {"hint": "..."}}` 会渲染为输入框下方的提示。

## 5.6 在触发器中使用数据库

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

## 5.7 为已定义的表添加新列

当数据库模型已经定义好并被用户使用后，如果需要新增字段，开发者可以直接修改模型代码，但对于已部署的用户（部署者），他们可能没有数据库迁移的能力。因此需要一种方法，在 Bot 启动时自动为已有表添加新列。

### 方法概述

使用 `exec_list` 机制，在 `on_core_start_before` 阶段（WS 服务启动之前）执行 SQL 语句，确保数据库 Schema 变更在消息处理前完成。

### 实现步骤

**第一步：修改数据模型，添加新字段**

```python
# MyPlugin/utils/database/models.py
from typing import Optional, Dict, Any
from sqlmodel import Field
from gsuid_core.utils.database.base_models import BaseModel, with_session

class MyUser(BaseModel, table=True):
    __table_args__: Dict[str, Any] = {"extend_existing": True}

    uid: str = Field(title="游戏 UID")
    region: str = Field(default="cn", title="大区")
    cookie: Optional[str] = Field(default=None, title="Cookie")
    # === 新增字段 ===
    platform: str = Field(default="", title="平台")
    stamina_bg_value: str = Field(default="", title="体力背景")
    auto_sign: str = Field(default="off", title="自动签到开关")
```

**第二步：在模型文件末尾添加 SQL 迁移语句**

```python
# MyPlugin/utils/database/models.py（文件末尾）
from gsuid_core.utils.database.startup import exec_list

# 添加新列的 SQL 语句
# 注意：类型必须与 Python 字段类型对应（str -> TEXT, int -> INTEGER）
# DEFAULT 后面跟的是默认值
exec_list.extend(
    [
        'ALTER TABLE MyUser ADD COLUMN platform TEXT DEFAULT ""',
        'ALTER TABLE MyUser ADD COLUMN stamina_bg_value TEXT DEFAULT ""',
        'ALTER TABLE MyUser ADD COLUMN auto_sign TEXT DEFAULT "off"',
    ]
)
```

### 类型对应关系

| Python 类型 | SQL 类型 | DEFAULT 示例 |
|-------------|----------|--------------|
| `str`       | `TEXT`   | `DEFAULT ""` |
| `int`       | `INTEGER` | `DEFAULT 0` |
| `float`     | `REAL`   | `DEFAULT 0.0` |
| `bool`      | `INTEGER` | `DEFAULT 0` |
| `Optional[str]` | `TEXT` | `DEFAULT NULL` |

### 完整示例

```python
# MyPlugin/utils/database/models.py
from typing import Optional, Dict, Any
from sqlmodel import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site
from gsuid_core.utils.database.base_models import BaseModel, with_session
from gsuid_core.utils.database.startup import exec_list


class MyUser(BaseModel, table=True):
    """用户数据表"""
    __table_args__: Dict[str, Any] = {"extend_existing": True}

    uid: str = Field(title="游戏 UID")
    region: str = Field(default="cn", title="大区")
    cookie: Optional[str] = Field(default=None, title="Cookie")
    platform: str = Field(default="", title="平台")
    stamina_bg_value: str = Field(default="", title="体力背景")
    auto_sign: str = Field(default="off", title="自动签到")

    @classmethod
    @with_session
    async def get_user(
        cls, session: AsyncSession, user_id: str, bot_id: str
    ) -> Optional["MyUser"]:
        stmt = select(cls).where(cls.user_id == user_id, cls.bot_id == bot_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


# 为已部署用户的数据库自动添加新列
# 这些 SQL 语句会在 on_core_start_before 阶段执行
# 对于新用户，表会自动包含所有字段，ALTER TABLE 会静默失败（列已存在）
# 对于老用户，新列会被自动添加
exec_list.extend(
    [
        'ALTER TABLE MyUser ADD COLUMN platform TEXT DEFAULT ""',
        'ALTER TABLE MyUser ADD COLUMN stamina_bg_value TEXT DEFAULT ""',
        'ALTER TABLE MyUser ADD COLUMN auto_sign TEXT DEFAULT "off"',
    ]
)


# Web 控制台注册
@site.register_admin
class MyUserAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="MyPlugin 用户管理",
        icon="fa fa-users",
    )  # type: ignore
    model = MyUser
```

### 注意事项

1. **SQL 语句会在启动时执行**：`exec_list` 中的语句在 `on_core_start_before` 阶段执行，早于任何用户消息处理。

2. **列已存在时的行为**：如果表中已有该列，`ALTER TABLE ... ADD COLUMN` 会失败，但框架会捕获异常并继续执行，不会影响启动。

3. **类型必须匹配**：SQL 类型必须与 Python 字段类型正确对应，否则可能导致数据异常。

4. **默认值必须提供**：`DEFAULT` 子句是必须的，确保已有数据行的该列有合理的默认值。

5. **`extend_existing: True` 必须**：允许同名表在多次 reload 时重新绑定，避免 SQLAlchemy 报错。
