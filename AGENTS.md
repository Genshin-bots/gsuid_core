# AGENTS.md

> 本文件遵循 [AGENTS.md](https://agents.md/)：给编码 Agent 的仓库说明（README for agents）。
> 它取代已删除的 `docs/LLM.md`。**§1–§9 编号保持不变**，源码注释里的「§3.5.2」等仍指向这里。
>
> 架构 / 触发链路 / 启动时序 / AI 子系统 / 已知坑：按需读
> [`.agents/skills/gscore-development/SKILL.md`](.agents/skills/gscore-development/SKILL.md)，
> **不要**一次把所有 `references/` 塞进上下文。源码是唯一事实源。

## Project overview

GsCore（早柚核心 / `gsuid-core`）是 **FastAPI + WebSocket + APScheduler** 的**单进程**服务：

- **不是 Bot**。Core 不直连聊天平台。适配器以 WS 客户端连 `/ws/{bot_id}`，上报 `MessageReceive`，再把 `MessageSend` 发回平台。
- **插件宿主**。业务在 `gsuid_core/plugins/`；`@sv.on_xxx` 在 import 时登记触发器。
- **AI 中枢**。无命令匹配时由 `gsuid_core/ai_core/` 接管（Persona、工具、记忆、RAG、巡检、长任务）。
- **不能独立当聊天机器人用**；需配合 NoneBot2 / AstrBot / Koishi 等上游 Bot。

默认端口 `8765`，WebConsole：`http://<HOST>:<PORT>/app`。Python `>=3.11,<4.0`。

## Repository map

```
.
├── AGENTS.md                 # 本文件：Agent 必读（红线 + 结构 + 命令）
├── README.md                 # 人类用户 README（部署 / Docker）
├── pyproject.toml            # 依赖、入口 `core = gsuid_core.core:main`
├── ruff.toml                 # line-length=120，lint E/F/I/W
├── pyrightconfig.json        # basic；排除 plugins / data
├── .agents/skills/           # 开发文档 Skill（给 Agent 按任务加载）
├── docs/                     # 专题设计 / 历史评审（非入门）
├── gsuid_core/               # 框架包
│   ├── core.py               # 进程入口
│   ├── app_life.py           # FastAPI lifespan / 两阶段钩子
│   ├── server.py / gss.py    # 插件加载、连接、钩子注册表
│   ├── meta_plugins.py       # 基础设施插件先加载，并把 <包>.api 挂到 sys.modules
│   ├── handler.py            # handle_event：命令匹配 + AI 分流
│   ├── bot.py                # _Bot（底层）/ Bot（高层）
│   ├── sv.py / trigger.py    # Plugins / SV / 触发器
│   ├── models.py             # Event / MessageReceive / MessageSend
│   ├── config.py             # CoreConfig / PluginConfigStore
│   ├── utils/                # database / image / plugins_config / …
│   ├── webconsole/           # 网页控制台 API + dist
│   ├── plugins/              # 用户插件（运行时，不进类型检查）
│   ├── buildin_plugins/      # 内置命令（安装/重启/帮助/状态）
│   └── ai_core/              # AI 子系统（见下）
├── tests/                    # pytest
├── eval/                     # 实机评测（打运行中的 core，不要进程内重建世界）
└── data/                     # 运行时生成：配置 / DB / 日志 / AI 状态（不进 git）
```

`gsuid_core/ai_core/` 速查：

| 路径 | 职责 |
|------|------|
| `startup.py` | `init_ai_core`，按 `_INIT_STEPS` 串行初始化 |
| `handle_ai.py` | AI 聊天入口 |
| `ai_router.py` / `session_registry.py` | Session 路由与注册表 |
| `gs_agent.py` + `agent_run/` | Agent 环：准备 → 工具 → iter → 收尾 |
| `register.py` | `@ai_tools` / 工具注册表 / `ai_alias` / `ai_entity` / `ai_skill` |
| `output_gate.py` | `pre_send_gate`（尖括号 → OOC） |
| `context_assembly.py` | system prompt + 每轮注入的唯一装配点 |
| `memory/` `rag/` `persona/` `heartbeat/` `planning/` | 记忆 / 知识库 / 人设 / 巡检 / 长任务 |

更细的模块图与消息总链路：`.agents/skills/gscore-development/references/01-architecture-and-modules.md`。

**运行时 Skill**（模型 `list_skills` / `run_skill_script`）在 `data/ai_core/skills/` 或插件 repo 的 `skills/`，**不是** `.agents/skills/`。后者是给编码 Agent 的开发指南。

## Skills

任务对上后再 `Read` 对应 `SKILL.md`，再按索引打开**一篇** `references/`。

| 任务 | Skill |
|------|--------|
| 改框架核心（handler / ai_core / 启动 / 配置 / 数据库基类 / webconsole） | [gscore-development](.agents/skills/gscore-development/SKILL.md) |
| 写业务插件、触发器、`to_ai`、帮助、配置、库表 | [gscore-plugin-development](.agents/skills/gscore-plugin-development/SKILL.md) |
| 查 AI Core 给插件的 API 签名 | [gscore-ai-core-api](.agents/skills/gscore-ai-core-api/SKILL.md) |
| 写平台适配器（WS 协议 / MessageReceive） | [gscore-adapter-development](.agents/skills/gscore-adapter-development/SKILL.md) |
| 部署 / Docker / WebConsole / WS_TOKEN | [gscore-deploy](.agents/skills/gscore-deploy/SKILL.md) |

`docs/` 里还有生命周期、记忆系统、Takumi 出图等专题（见各 Skill「关联文档」）。改核心机制后同步对应章节。

## Setup commands

一律在仓库根目录（含 `pyproject.toml`）执行。推荐 **uv**。

```sh
# 安装
uv python install 3.12
uv sync --python 3.12
uv run python -m ensurepip

# 启动（等价：poetry run core / pdm run core / python -m gsuid_core.core）
uv run core
uv run core --dev                          # 只加载目录名以 -dev 结尾的插件
uv run core --host 0.0.0.0 --port 9527     # CLI 覆盖不写回文件
```

```sh
# 检查
uv run ruff check gsuid_core tests eval
uv run ruff format --check gsuid_core tests eval
uv run pytest tests/test_interaction_scaffold.py -q
```

实机评测必须打**已启动且加载全部插件**的 core（不要 `--dev`）：

```powershell
$env:GSUID_LOCAL_TEST_MODE="1"
$env:GSUID_LOCAL_TEST_TOKEN="<token>"
$env:PYTHONUTF8="1"
$env:NO_PROXY="localhost,127.0.0.1"
uv run core --port 8765
```

碰装配 / 闸门 / 每轮注入 / 启动顺序时，单测全绿不够，还要对照 `eval/agent` 群聊基准。

## Testing

- 测试在 `tests/`，`pytest`，文件 `test_*.py`。默认收集面必须离线、无 LLM。
- 需要已启动 core 的 WS/HTTP 脚本放 `eval/manual/`（不要 `test_` 前缀）或 `eval/agent/`；禁止再往 `tests/` 加。
- 类型：basedpyright / pyright，`typeCheckingMode=basic`；`gsuid_core/plugins` 与 `data` 已排除。
- 行宽 120（ruff）；`#` 注释更严，见 §1.6。
- 改交互脚手架必须跑 `tests/test_interaction_scaffold.py`（正反双向）。
- 改工具 docstring / covers：`tests/test_ai_tool_docstrings.py`。

## Security notes

- 公网必须配 `WS_TOKEN` 或 `TRUSTED_IPS`，否则外网 WS 会被拒。
- WebConsole：`REGISTER_CODE` 只能注册一个管理员；握手为 ECDH + AES-GCM。
- 密钥与 token 只走配置 / 环境，不要写进仓库或提交到 `data/`。

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

唯一例外：解析 LLM 自由文本 / Qdrant payload / 加密报文等**不可信外部输入**处，为「绝不打断主链路」按需保留。插件里 `_ai_return_xxx()` 也允许 try/except（观测性代码，失败只打 warning）。

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

**`#` 注释最多两行，每行不超过 88 个字符。** 注释的价值在于「用最精确的一句话点明为什么 / 有什么坑」，而不是把代码翻译成中文、或写五六行长篇大论。

```python
# ❌ 错误示例：五六行长篇大论 + 单行超长
# 这个函数用来处理用户发来的消息，首先解析消息内容，然后判断消息类型……

# ✅ 正确示例：一句话点明非显而易见的关键点
# 群聊 user_id 置空以保证同群共享同一个 deque（见 §3.1.1）
async def handle(msg): ...
```

**写注释的判据**：

- 代码已能自解释的**不写**注释——不要把 Python 翻译成中文。
- 要写就写**为什么这样做 / 有什么坑 / 边界条件**，不写「做了什么」。
- 一条注释两行讲不清，说明这段逻辑该拆函数 / 改命名，而不是堆注释。
- 超过两行 = 信号：要么删到两行内，要么这段代码本身需要重构。

> 该上限（每行 88 字）比代码 `line-length=120` 更严，是**刻意**的：注释越短越会被读，长注释会被跳过、且极易随代码改动过期变成误导。

### 1.7 前缀缓存红线（AI 会话）

改 `ai_core` 会话 / Agent loop / history 时**绝对禁止**破坏 provider 前缀缓存：

1. **禁止中途改写 `system_prompt` 字符串**（会话内默认 TTL=inf）。需要系统提醒 → 只向**当前 request 追加 `UserPromptPart`**，落盘前 `_relean` 剥掉（`（系统：` / `（系统校验：` 等）。
2. **禁止砍 history 头部**（`history[-n:]` 式 compact）。须用 `compact_session_history` / 保头裁中段；禁止把锚点消息插回头部。
3. **动态 per-turn 内容**（mood / 关系 / 记忆 / 精确时间）进 **user 侧**，不进 system。

详见 `gscore-development` §6.7.1 / §12.7。

### 1.8 禁止 `Any`：运行时变量必须有完整、可追踪的类型

`Any` 与 `cast` / `type: ignore` 同类：类型检查在这里停掉，后续读写全部失明。

**禁止**在本仓库代码里引入或传播 `Any`（含 `list[Any]`、`dict[str, Any]`、`Optional[Any]`、
`Callable[..., Any]`、`TypeVar` 无界到退化为 Any）。

所有**运行时变量**——函数参数 / 返回值 / 实例属性 / 跨语句使用的局部变量——必须有**完整、
可追踪**的类型：从定义点顺着赋值与调用，类型检查器能推出具体类型，读代码的人也能指到
`TypedDict` / dataclass / Protocol / Union / 泛型实参，而不是「这里是 Any」。

```python
# ❌ 错误：用 Any 把问题藏起来
from typing import Any

payload: dict[str, Any] = raw
result: Any = await session.execute(stmt)

# ✅ 正确：把结构写出来，边界用守卫收窄
class Payload(TypedDict):
    user_id: str
    text: str

payload: Payload = raw
result = await session.execute(stmt)
deleted = result.rowcount if isinstance(result, CursorResult) else 0
```

遇到「现在还不知道是什么」时：

- 多种已知形态 → `A | B` + `isinstance`（见 §2.4）
- 结构化 dict → `TypedDict`；配置 / 状态袋 → `@dataclass` / `NamedTuple`
- 只关心一组方法 → `Protocol`
- 容器元素同构 → `list[T]` / `dict[K, V]` / `Sequence[T]`，不要 `list[Any]`
- 第三方 stub 标成 `Any` / `Result[Any]`（如 SQLAlchemy）→ **在调用点立刻收窄**，
  不要把 `Any` 赋给下游变量、更不要写进本仓库的公开签名

`object` 当「万能袋」、裸 `dict` / `list` 当业务数据，等同于 Any，同样禁止。
测试里的假对象用 `Protocol` 或显式 stub 类，不要 `MagicMock` + `Any` 糊弄生产签名。

### 1.9 禁止人格锁定 / 能力锁定（通用框架）

GsCore 是**通用框架**：部署者自己写人格卡、自己装插件。框架运行时**不得**假定某个具体人格或某个业务垂直存在。

**人格锁定（禁止）**：框架代码、用户可见兜底、闸门、脚手架、分类器、规划提示 **不得**写死某个角色的姓名、口癖、自称、出身或道具梗（如「早柚」「唔…」「呼」「zzz」「卷轴」「本貉」）。口癖配额 / 结尾语气词必须从**当前人格卡**解析（Tone Markers 等）；卡上没有就当无口癖。末端兜底必须人格中性，禁止用默认角色的口头禅冒充中性。

**能力锁定（禁止）**：框架意图分类 / 规划 / 路由 / 提示词 **不得**内置业务垂直词表（游戏练度/圣遗物/命座、股票/研报/模拟盘、把某城市天气写进核心特判等）。插件能力只许插件自己声明：`covers` / 带领域前缀的 `aliases` / `capability_domain` / `ai_entity` / `ai_alias`。框架只做确定性查表 + 向量召回，不在核心维护「这个词属于哪款游戏 / 哪条业务线」。禁止为某个业务域写栏目词表、猜分隔约定、把插件名写进装配路径。

```python
# ❌ 框架把默认人格的口癖写进运行时
if text.endswith(("zzz", "呼", "唔")): ...
PERSONA_FALLBACK_TEXT = "唔…这个不太想说呢…"

# ❌ 框架意图词表收插件专属域词
KNOWLEDGE_NOUNS = {"圣遗物", "命座", "元素精通", "模拟盘"}

# ✅ 口癖从当前人格卡解析；兜底中性
markers = get_tone_markers(persona_name)
PERSONA_FALLBACK_TEXT = "这个不太想说呢。"

# ✅ 垂直能力由插件自描述，框架不内置词表
@ai_tools(covers=["…"], aliases=["领域A·能力X"], capability_domain="…")
```

**允许（不是锁定）**：

- 产品品牌「早柚核心」出现在日志 / WebConsole / 更新文案。
- 仓库可附带一份**默认人格卡**（`sayu_persona_prompt`；目录为空时种子一份）。运行时仍按当前启用人格工作，不把该卡的口癖抄进框架。
- 框架一等能力节点（`render_agent` / `research_agent` 等）是通用基础设施。
- 插件代码；评测 / 单测用某个已启用人格名做寻址 fixture；历史事故注释点名具体插件。
- HTML 模板库的通用版式名（metrics / ranking / weather 卡片）是可视化原语，不是路由特判。
- 遗留的游戏 UID / Cookie / Enka 工具在 `utils/`，服务已装插件，不进 AI 路由词表。

---

## 二、类型提示规范

### 2.1 完全类型提示原则

所有函数、方法的参数和返回值**必须有类型注解**，且注解必须是可追踪的具体类型
（见 §1.8）。写了 `: Any` 等于没写，不算合规。

```python
# ✅ 正确示例
async def process_user(user_id: str, bot_id: str) -> User | None:
    ...

# ❌ 错误示例
async def process_user(user_id, bot_id):
    ...

# ❌ 错误示例：注解是 Any，类型链在这里断掉
async def process_user(user_id: str, bot_id: str) -> Any:
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
        return result["message"] if "message" in result else ""
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

### 3.2 `@with_session` / `@with_read_session`

所有数据库类方法必须挂其中一个装饰器：

- `@with_session`：写入 / 读后写 / 删除（SQLite 走写槽）
- `@with_read_session`：纯 SELECT（SQLite 走独立读槽，不跟大写抢）

二者都会自动创建 session、提交、异常回滚、归还连接池。

```python
from gsuid_core.utils.database.base_models import with_session, with_read_session
from sqlalchemy.ext.asyncio import AsyncSession

class User(BaseModel):
    @classmethod
    @with_read_session
    async def get_user_by_name(cls, session: AsyncSession, name: str) -> User | None:
        stmt = select(cls).where(cls.name == name)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
```

**注意**：签名必须包含 `session: AsyncSession` 作为第二个参数（紧跟 cls 或 self）。
插件侧完整说明见 `gscore-plugin-development` §5.3。

### 3.3 复杂场景下的 async_maker()

当需要在类方法外手动管理 session，或需要更精细控制事务时：

```python
from gsuid_core.utils.database.base_models import async_maker

async def batch_operation():
    async with async_maker() as session:
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
        ...
```

### 3.5 SQLModel / SQLAlchemy 查询的类型安全写法

ORM 查询是 `cast` / `type: ignore` / `getattr` 的重灾区。下面三条是**从根源消除**这些兜底的正确写法，违反 §1.2~1.4 去糊弄 basedpyright 的标红，先回到这里。

#### 3.5.1 比较表达式一律用 `col()` 包裹列

SQLModel 字段注解是 Python 类型（如 `created_at: int`），所以 `cls.created_at >= ts` 被类型检查器判为 **`bool`**，传进 `where()` 会标红。用 `col()` 把列还原成 `ColumnElement`：

```python
# ❌ 错误：cls.created_at >= ts 是 bool
stmt = delete(cls).where(cls.created_at >= since_ts)

# ✅ 正确：col() 包裹得到 ColumnElement[bool]
stmt = delete(cls).where(col(cls.created_at) >= since_ts)
```

`where` / `order_by` / `group_by` / `!=` / `.is_(False)` **全部**适用。

> 陷阱：`select(cls).where(cls.x == v)` 恰好**不报错**——SQLModel 的 `Select.where` 重载把 `bool` 也收进了 union；但 `delete()`/`update()` 是 SQLAlchemy 原生、`where` 严格只收 `ColumnElement[bool]` 就会报错。**不要依赖前者的宽松，一律 `col()` 包裹**。

#### 3.5.2 `rowcount` 用 `isinstance(result, CursorResult)` 守卫

`session.execute()` 静态返回 `Result[Any]`，**没有** `rowcount`。DML（`delete`/`update`）运行时真实返回的是 `CursorResult`。用类型守卫安全取值，而不是 `cast` / `getattr` / `# type: ignore`：

```python
from sqlalchemy.engine import CursorResult

result = await session.execute(delete(cls).where(col(cls.created_at) < before_ts))
deleted = result.rowcount if isinstance(result, CursorResult) else 0
```

#### 3.5.3 不要把运行时变长列表 splat 进 `select()`

`select(*cols, total)`（`cols` 是运行时按分支拼出来的 `list`）无法匹配 `select` 的重载，且 `row[i]` 退化成 `Any`。**按分支写出列数确定的 `select()`**：

```python
# ❌ 错误：变长 splat
stmt = select(*group_cols, total).where(...)

# ✅ 正确：列数确定 → Select[tuple[str, str, int]]
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
    return sum(data)

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
| `web_search/` | 统一网页搜索（Tavily/Jina/Exa/AnySearch/MCP + 多源策略） |
| `web_fetch/` | 网页抓取转 Markdown（Jina Reader / local + 多源策略） |

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
        self.bot = ws
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
        self.bot = bot
        self.ev = ev
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
bot = Bot(_bot, mock_ev)
```

---

## 九、总结

编辑本项目代码时，记住以下优先级：

1. **类型问题 → 从类型标注和代码逻辑解决，不使用兜底语法；禁止 `Any`，运行时变量类型必须可追踪（§1.8）**
2. **人格 / 能力锁定 → 框架不写死某个人格的口癖，也不内置业务垂直词表（§1.9）**
3. **数据库操作 → 继承基类 + `@with_session`（写）/ `@with_read_session`（纯 SELECT）**
4. **异步要求 → 所有可能阻塞的方法都用 async def**
5. **代码组织 → 相关方法封装在类中，使用 dataclass/TypedDict 定义数据结构**
6. **Bot 类型 → 插件/触发器用 `Bot`（高层），框架内部用 `_Bot`（底层），禁止混用**
7. **注释精简 → `#` 注释最多两行、每行 ≤88 字，只写「为什么/坑/边界」，不复述代码**

专题细节按 Skills 表按需加载，不要把整本 `references/` 一次读完。
