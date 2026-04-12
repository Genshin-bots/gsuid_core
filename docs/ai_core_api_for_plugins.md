# GsCore AI Core 插件开发者 API 文档

## 概述

本文档面向插件开发者，介绍如何使用 `gsuid_core/ai_core` 模块为机器人 AI 提供工具函数、知识库和自定义 Agent 支持。

**核心模块路径**: `gsuid_core/ai_core/`

---

## 目录

1. [模块导入速查](#1-模块导入速查)
2. [@ai_tools 装饰器](#2-ai_tools-装饰器)
3. [工具分类系统（category）](#3-工具分类系统category)
4. [create_agent 与 Agent 架构](#4-create_agent-与-agent-架构)
5. [知识库注册](#5-知识库注册)
6. [别名注册](#6-别名注册)
7. [图片实体注册](#7-图片实体注册)
8. [内置工具一览](#8-内置工具一览)
9. [System Prompt 管理](#9-system-prompt-管理)
10. [工具注册表查询 API](#10-工具注册表查询-api)
11. [类型定义参考](#11-类型定义参考)
12. [完整示例](#12-完整示例)

---

## 1. 模块导入速查

```python
# 工具注册装饰器
from gsuid_core.ai_core.register import (
    ai_tools,            # 工具注册装饰器
    ai_entity,           # 知识库注册
    ai_alias,            # 别名注册
    ai_image,            # 图片实体注册
    add_manual_knowledge,    # 手动知识添加
    update_manual_knowledge, # 手动知识更新
    delete_manual_knowledge, # 手动知识删除
    get_manual_entities,     # 获取所有手动知识
    get_manual_entity,       # 获取指定手动知识
    get_registered_tools,    # 获取所有已注册工具（按分类）
    get_all_tools,           # 获取所有已注册工具（平铺结构）
)

# Agent 创建
from gsuid_core.ai_core.gs_agent import create_agent, get_main_agent_tools

# 工具上下文
from gsuid_core.ai_core.models import ToolContext

# PydanticAI RunContext
from pydantic_ai import RunContext

# 数据模型
from gsuid_core.ai_core.models import (
    KnowledgeBase,       # 知识库基类
    KnowledgePoint,      # 知识点（插件注册）
    ManualKnowledgeBase, # 手动知识
    ImageEntity,         # 图片实体
    ToolBase,            # 工具元数据
)

# 内置工具（可直接导入使用）
from gsuid_core.ai_core.buildin_tools import (
    # 主Agent工具 (category="buildin")
    search_knowledge,           # 知识库检索
    web_search,                 # Web搜索
    send_message_by_ai,         # 发送消息
    query_user_favorability,    # 查询好感度
    query_user_memory,          # 查询用户记忆
    update_user_favorability,   # 更新好感度（增量）
    set_user_favorability,      # 设置好感度（绝对值）
    create_subagent,            # 创建子Agent
    get_self_persona_info,      # 获取自身Persona信息
    execute_shell_command,      # 执行系统命令

    # 子Agent工具 (category="default")
    get_current_date,           # 获取当前日期时间
    read_file_content,          # 读取文件
    write_file_content,         # 写入文件
    execute_file,               # 执行脚本
    diff_file_content,          # 文件对比
    list_directory,             # 列出目录
)
```

---

## 2. @ai_tools 装饰器

### 2.1 函数签名

```python
@overload
def ai_tools(func: F, /) -> F: ...

@overload
def ai_tools(
    func: None = None,
    /,
    *,
    category: str = "default",
    check_func: Optional[CheckFunc] = None,
    **check_kwargs,
) -> Callable[[F], F]: ...
```

### 2.2 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `category` | `str` | `"default"` | 工具分类，决定工具放入哪个分类字典。`"buildin"` 为主Agent工具，`"default"` 为子Agent工具 |
| `check_func` | `Callable` | `None` | 可选的权限校验函数，签名为 `async def check(ev: Event) -> Tuple[bool, str]` |
| `**check_kwargs` | `Any` | — | 额外传递给 `check_func` 的参数 |

### 2.3 被装饰函数要求

被装饰的函数**必须是异步函数**，第一个参数支持三种上下文模式：

| 上下文模式 | 函数签名示例 | 说明 |
|------------|-------------|------|
| RunContext | `async def tool(ctx: RunContext[ToolContext], ...)` | 推荐方式，通过 `ctx.deps.bot` 和 `ctx.deps.ev` 访问上下文 |
| ToolContext | `async def tool(ctx: ToolContext, ...)` | 直接使用上下文对象 |
| 无上下文 | `async def tool(param1: str, ...)` | 简单工具，不需要上下文 |

> **特殊注入**：函数参数如果类型注解为 `Bot` 或 `Event`，会被自动注入，**不会暴露给 LLM**。

```python
from pydantic_ai import RunContext
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

# 模式一：RunContext（推荐）
@ai_tools(category="default")
async def my_tool_v1(ctx: RunContext[ToolContext], query: str) -> str:
    """工具描述，AI 会看到这段文档"""
    bot = ctx.deps.bot   # Bot 对象
    ev = ctx.deps.ev     # Event 对象
    return f"查询结果: {query}"

# 模式二：ToolContext
@ai_tools(category="default")
async def my_tool_v2(ctx: ToolContext, query: str) -> str:
    """工具描述"""
    return f"查询结果: {query}"

# 模式三：无上下文
@ai_tools(category="default")
async def my_tool_v3(query: str) -> str:
    """工具描述"""
    return f"查询结果: {query}"

# 模式四：自动注入 Bot/Event（不暴露给 LLM）
@ai_tools(category="default")
async def my_tool_v4(query: str, ev: Event) -> str:
    """工具描述"""
    return f"查询者: {ev.user_id}, 查询结果: {query}"
```

### 2.4 使用方式

支持两种调用方式：

```python
# 方式一：直接装饰（使用默认参数）
@ai_tools
async def my_tool(query: str) -> str:
    """工具描述"""
    return query

# 方式二：带参数装饰
@ai_tools(category="buildin", check_func=my_check_func)
async def my_tool(ctx: RunContext[ToolContext], query: str) -> str:
    """工具描述"""
    return query
```

### 2.5 check_func 权限校验

`check_func` 参数会根据类型注解**自动注入** `Bot`/`Event` 对象：

```python
from gsuid_core.models import Event
from gsuid_core.bot import Bot

# 同步 check_func
def check_admin(ev: Event) -> tuple[bool, str]:
    if ev.user_id in ADMIN_LIST:
        return True, ""
    return False, "⚠️ 权限不足：仅管理员可用"

# 异步 check_func
async def check_level(bot: Bot, ev: Event) -> tuple[bool, str]:
    level = await get_user_level(ev.user_id)
    if level >= 10:
        return True, ""
    return False, f"⚠️ 等级不足：需要10级，当前{level}级"

# 使用 check_func
@ai_tools(check_func=check_admin)
async def admin_tool(ctx: RunContext[ToolContext], uid: str) -> str:
    """管理员专用工具"""
    return f"查询用户: {uid}"
```

### 2.6 返回值类型

| 返回类型 | 处理方式 |
|----------|----------|
| `str` | 直接返回给 AI |
| `Message` | 调用 `bot.send()` 发送，并返回描述字符串 |
| `dict` | JSON 序列化后返回给 AI |
| `Image.Image` | 转换为图片并发送，返回资源ID |
| `bytes` | 作为资源发送 |

---

## 3. 工具分类系统（category）

### 3.1 架构概览

工具注册表采用**分类字典**结构：

```python
# 内部结构
_TOOL_REGISTRY: Dict[str, Dict[str, ToolBase]] = {
    "buildin": {
        "search_knowledge": ToolBase(...),
        "create_subagent": ToolBase(...),
        # ... 其他 buildin 工具
    },
    "default": {
        "get_current_date": ToolBase(...),
        "read_file_content": ToolBase(...),
        # ... 其他 default 工具
    },
    "my_plugin": {
        "my_custom_tool": ToolBase(...),
    }
}
```

### 3.2 分类说明

| 分类名 | 说明 | 谁可以调用 |
|--------|------|-----------|
| `"buildin"` | 核心内置工具，主Agent直接调用 | 主Agent（Main Agent） |
| `"default"` | 通用工具，需通过 `create_subagent` 调用 | 子Agent（Sub Agent） |
| `"<自定义>"` | 插件自定义分类 | 根据配置决定 |

### 3.3 Agent 调用架构

```
┌─────────────────────────────────────────────────────┐
│              主Agent (Main Agent)                   │
│          使用 category="buildin" 的工具              │
│                                                     │
│  - search_knowledge    - create_subagent            │
│  - web_search          - get_self_persona_info      │
│  - send_message_by_ai  - execute_shell_command      │
│  - query_user_*        - set_user_favorability      │
└─────────────────────────┬───────────────────────────┘
                          │ create_subagent()
                          ▼
┌─────────────────────────────────────────────────────┐
│              子Agent (Sub Agent)                    │
│          使用 category="default" 的工具              │
│                                                     │
│  - get_current_date    - write_file_content         │
│  - read_file_content   - diff_file_content          │
│  - execute_file        - list_directory             │
└─────────────────────────────────────────────────────┘
```

### 3.4 插件工具分类建议

插件开发时，工具注册推荐使用：

```python
# 简单工具（通过子Agent调用）
@ai_tools(category="default")
async def my_simple_tool(query: str) -> str:
    """简单查询工具"""
    ...

# 高频/核心工具（主Agent直接调用，需谨慎）
@ai_tools(category="buildin")
async def my_core_tool(ctx: RunContext[ToolContext], uid: str) -> str:
    """核心工具，主Agent直接调用"""
    ...

# 插件专属分类
@ai_tools(category="genshin")
async def genshin_query(ctx: RunContext[ToolContext], character: str) -> str:
    """原神角色查询"""
    ...
```

---

## 4. create_agent 与 Agent 架构

### 4.1 create_agent - 创建临时 Agent

```python
from gsuid_core.ai_core.gs_agent import create_agent
```

**函数签名**：

```python
def create_agent(
    model_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    persona_name: Optional[str] = None,
) -> GsCoreAIAgent
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_name` | `str` | `None` | 模型名称，`None` 时使用全局配置 |
| `system_prompt` | `str` | `None` | 系统提示词 |
| `persona_name` | `str` | `None` | 绑定的 Persona 名称（用于热重载检测） |

**示例**：

```python
from gsuid_core.ai_core.gs_agent import create_agent

# 创建翻译 Agent
translator = create_agent(
    system_prompt="你是一个翻译助手，只负责将中文翻译成英文，不做任何解释。",
)

# 在异步函数中使用
async def translate(text: str) -> str:
    result = await translator.run(user_message=text)
    return result
```

### 4.2 GsCoreAIAgent.run() 方法

```python
async def run(
    self,
    user_message: Union[str, Sequence[UserContent]],
    bot: Optional[Bot] = None,
    ev: Optional[Event] = None,
    rag_context: Optional[str] = None,
) -> str
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_message` | `str \| Sequence` | 是 | 用户输入消息 |
| `bot` | `Bot` | 否 | Bot 对象，工具调用时注入 `ctx.deps.bot` |
| `ev` | `Event` | 否 | 事件对象，工具调用时注入 `ctx.deps.ev` |
| `rag_context` | `str` | 否 | 额外的 RAG 上下文，追加到 system_prompt |

**返回**: AI 响应字符串

### 4.3 get_main_agent_tools - 获取主Agent工具列表

```python
from gsuid_core.ai_core.gs_agent import get_main_agent_tools

def get_main_agent_tools() -> ToolList
```

返回所有 `category="buildin"` 的工具列表，用于构建主Agent。

---

## 5. 知识库注册

### 5.1 ai_entity - 插件知识注册

**入口**：

```python
from gsuid_core.ai_core.register import ai_entity
from gsuid_core.ai_core.models import KnowledgePoint
```

**函数签名**：

```python
def ai_entity(entity: Union[KnowledgePoint, KnowledgeBase]) -> None
```

**KnowledgePoint 字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | `str` | 是 | 全局唯一标识符，建议格式：`{plugin}_{类型}_{编号}` |
| `plugin` | `str` | 是 | 插件名称（在 `plugins/` 下会自动推断） |
| `title` | `str` | 是 | 知识点标题，用于 RAG 检索 |
| `content` | `str` | 是 | 知识点内容，支持 Markdown，内容越详细越好 |
| `tags` | `List[str]` | 是 | 标签列表，用于过滤和检索 |
| `source` | `str` | 自动 | 固定为 `"plugin"`，系统自动设置 |
| `_hash` | `str` | 自动 | 内容哈希，系统自动计算，不需传入 |

**示例**：

```python
from gsuid_core.ai_core.register import ai_entity
from gsuid_core.ai_core.models import KnowledgePoint

ai_entity(KnowledgePoint(
    id="genshin_character_shogun",
    plugin="GenshinUID",
    title="雷电将军 - 角色介绍",
    content="""
# 雷电将军

## 基本信息
- 元素：雷
- 武器类型：长枪
- 命之座：万世之座
- 稀有度：5星

## 技能说明
### 普通攻击 - 源流
进行五段枪类普通攻击。

### 元素战技 - 奥义·梦想真说
创造「愿力」储蓄机制，并召唤眼之核心。

## 适用阵容
雷电将军在超导、感电等队伍中效果优异。
""",
    tags=["原神", "雷电将军", "雷神", "角色", "长枪", "雷元素"],
))
```

**注意**：
- 启动时自动同步到向量数据库
- 内容发生变化时（通过 `_hash` 检测）自动增量更新
- 在 `plugins/` 目录下注册时 `plugin` 字段会被自动推断

### 5.2 add_manual_knowledge - 手动知识添加

**入口**：

```python
from gsuid_core.ai_core.register import add_manual_knowledge
from gsuid_core.ai_core.models import ManualKnowledgeBase
```

**函数签名**：

```python
def add_manual_knowledge(entity: ManualKnowledgeBase) -> bool
```

**返回**: `True` 表示添加成功，`False` 表示 ID 已存在

**示例**：

```python
success = add_manual_knowledge(ManualKnowledgeBase(
    id="faq_bind_account",
    plugin="custom",
    title="如何绑定账号",
    content="Q: 如何绑定游戏账号？\nA: 发送 '绑定 你的UID' 即可完成绑定。",
    tags=["FAQ", "绑定", "账号"],
    source="manual",
))
```

### 5.3 手动知识管理 API

| 函数 | 签名 | 说明 |
|------|------|------|
| `add_manual_knowledge` | `(entity: ManualKnowledgeBase) -> bool` | 添加，ID 已存在返回 `False` |
| `update_manual_knowledge` | `(entity_id: str, updates: dict) -> bool` | 更新指定字段（不能修改 `id`、`source`） |
| `delete_manual_knowledge` | `(entity_id: str) -> bool` | 删除，不存在返回 `False` |
| `get_manual_entities` | `() -> List[ManualKnowledgeBase]` | 获取所有手动知识（副本） |
| `get_manual_entity` | `(entity_id: str) -> Optional[ManualKnowledgeBase]` | 获取指定知识 |

**与 ai_entity 的区别**：

| 特性 | `ai_entity` | `add_manual_knowledge` |
|------|-------------|----------------------|
| 启动同步 | ✅ 自动同步 | ❌ 不自动同步 |
| 增量更新 | ✅ 自动检测 | ❌ 手动管理 |
| 适用场景 | 插件固定知识 | 前端 API 动态添加 |
| source 字段 | `"plugin"` | `"manual"` |

---

## 6. 别名注册

### 6.1 入口

```python
from gsuid_core.ai_core.register import ai_alias
```

### 6.2 函数签名

```python
def ai_alias(name: str, alias: Union[str, List[str]]) -> None
```

别名系统用于 LLM 调用前进行**专有名词归一化**，将用户输入的别名统一替换为标准名称。

### 6.3 参数说明

| 参数 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 标准名称（归一化目标） |
| `alias` | `str \| List[str]` | 别名，可以是单个字符串或列表 |

### 6.4 示例

```python
from gsuid_core.ai_core.register import ai_alias

# 单个别名
ai_alias("雷电将军", "雷神")

# 多个别名
ai_alias("胡桃", ["小胡桃", "HuTao", "胡桃儿"])
ai_alias("丝柯克", ["skk", "斯柯克", "SKK", "丝绸之路"])

# 在插件初始化时批量注册
ALIASES = {
    "雷电将军": ["雷神", "将军", "影"],
    "纳西妲": ["草神", "小草神", "Lesser Lord Kusanali"],
}

for name, aliases in ALIASES.items():
    ai_alias(name, aliases)
```

---

## 7. 图片实体注册

### 7.1 入口

```python
from gsuid_core.ai_core.register import ai_image
from gsuid_core.ai_core.models import ImageEntity
```

### 7.2 函数签名

```python
def ai_image(entity: ImageEntity) -> None
```

### 7.3 ImageEntity 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | `str` | 是 | 唯一标识符 |
| `plugin` | `str` | 是 | 插件名称 |
| `path` | `str` | 是 | 图片路径（绝对路径或相对路径） |
| `tags` | `List[str]` | 是 | 描述标签，用于语义检索 |
| `content` | `str` | 是 | 详细描述文本 |
| `source` | `str` | 自动 | 固定为 `"plugin"` |
| `_hash` | `str` | 自动 | 内容哈希，传入空字符串即可 |

### 7.4 示例

```python
from gsuid_core.ai_core.register import ai_image
from gsuid_core.ai_core.models import ImageEntity

ai_image(ImageEntity(
    id="genshin_hutao_illustration",
    plugin="GenshinUID",
    path="./resources/characters/hutao.png",
    tags=["胡桃", "原神", "角色立绘", "火元素"],
    content="胡桃角色立绘图片，往生堂第七十七代堂主，性格活泼爱捉弄人。",
    source="plugin",
    _hash="",
))
```

### 7.5 图片检索使用

注册图片后，AI 可以通过 `search_image` 工具或 RAG API 进行语义检索：

```python
from gsuid_core.ai_core.rag.image_rag import search_and_load_image

# 在插件命令处理中使用
async def show_character_image(bot, ev):
    image = await search_and_load_image("给我看看胡桃的图片")
    if image:
        await bot.send(image)
```

---

## 8. 内置工具一览

所有内置工具均已注册到全局工具注册表，可直接在插件中使用或让 AI 自动调用。

### 8.1 主Agent工具（category="buildin"）

#### search_knowledge - 知识库检索

```python
@ai_tools(category="buildin")
async def search_knowledge(
    ctx: RunContext[ToolContext],
    query: str,                      # 自然语言查询
    category: Optional[str] = None, # 知识类别筛选（可选）
    plugin: Optional[str] = None,   # 插件来源筛选（可选）
    limit: int = 10,                # 最大返回数量
    score_threshold: float = 0.45,  # 相似度阈值（0~1）
) -> str
```

#### web_search - Web 搜索

```python
@ai_tools(category="buildin")
async def web_search(
    ctx: RunContext[ToolContext],
    query: str,          # 搜索关键词
    max_results: int = 5, # 最大结果数
) -> str
```

> **注意**：需要配置 Tavily API Key

#### send_message_by_ai - 主动发送消息

```python
@ai_tools(category="buildin")
async def send_message_by_ai(
    ctx: RunContext[ToolContext],
    message_type: Literal["text", "image"],  # 消息类型
    text: Optional[str] = None,              # 文本内容
    image_id: Optional[str] = None,         # 图片资源ID
    user_id: Optional[str] = None,          # 指定目标用户ID（可选）
) -> str
```

#### query_user_favorability - 查询好感度

```python
@ai_tools(category="buildin")
async def query_user_favorability(
    ctx: RunContext[ToolContext],
    user_id: Optional[str] = None,  # 用户ID，None时查询当前用户
) -> str
```

#### query_user_memory - 查询用户记忆

```python
@ai_tools(category="buildin")
async def query_user_memory(
    ctx: RunContext[ToolContext],
    user_id: Optional[str] = None,  # 用户ID，None时查询当前用户
) -> str
```

#### update_user_favorability - 更新好感度（增量）

```python
@ai_tools(category="buildin")
async def update_user_favorability(
    ctx: RunContext[ToolContext],
    delta: int,                     # 好感度变化量（可为负数）
    user_id: Optional[str] = None,
) -> str
```

#### set_user_favorability - 设置好感度（绝对值）

```python
@ai_tools(category="buildin")
async def set_user_favorability(
    ctx: RunContext[ToolContext],
    value: int,                     # 好感度绝对值
    user_id: Optional[str] = None,
) -> str
```

#### create_subagent - 创建子Agent

```python
@ai_tools(category="buildin")
async def create_subagent(
    ctx: RunContext[ToolContext],
    task: str,                      # 任务描述，请详细说明
    tags: Optional[str] = None,     # 逗号分隔的标签，用于匹配 System Prompt
    max_tokens: int = 1800,         # 子Agent最大输出 token 数
) -> str
```

**工作流程**：
1. 根据 `task` 和 `tags` 向量检索最匹配的 System Prompt
2. 使用匹配的 System Prompt 创建临时子 Agent
3. 子 Agent 执行任务并返回结果

**`tags` 参数格式**：逗号分隔字符串，如 `"代码,Python"` 或 `"摘要,总结"`

```python
# 代码任务
result = await create_subagent(ctx, "写一个Python快速排序", tags="代码,Python")

# 文本摘要（>2000字符自动触发）
result = await create_subagent(ctx, f"请总结：\n{long_text}", tags="摘要,总结", max_tokens=500)

# 角色扮演
result = await create_subagent(ctx, "以傲娇的语气回复：今天天气真好", tags="角色扮演")
```

#### get_self_persona_info - 获取自身 Persona 信息

```python
@ai_tools(category="buildin")
async def get_self_persona_info(
    ctx: RunContext[ToolContext],
    info_type: Literal["config", "image", "avatar", "audio"],
    persona_name: str,              # Persona 名称
) -> str
```

**info_type 说明**：

| info_type | 返回内容 |
|-----------|---------|
| `"config"` | `config.json` 配置内容（JSON 字符串，不含 introduction） |
| `"image"` | 立绘图片路径 |
| `"avatar"` | 头像图片路径 |
| `"audio"` | 音频文件路径 |

#### execute_shell_command - 执行系统命令

```python
@ai_tools(category="buildin")
async def execute_shell_command(
    ctx: RunContext[ToolContext],
    command: str,                   # 要执行的命令
    timeout: int = 30,              # 超时时间（秒）
) -> str
```

> ⚠️ **安全警告**：此工具执行系统命令，需要通过 `check_func` 严格控制权限，建议仅在沙箱环境中开放。

---

### 8.2 子Agent工具（category="default"）

子Agent工具由主Agent通过 `create_subagent` 调用，或在临时 Agent 中使用。

#### get_current_date - 获取当前日期时间

```python
@ai_tools(category="default")
async def get_current_date(
    ctx: RunContext[ToolContext],
    timezone: str = "Asia/Shanghai",  # 时区
) -> str
```

#### read_file_content - 读取文件

```python
@ai_tools()  # category="default"
async def read_file_content(
    ctx: RunContext[ToolContext],
    file_path: str,  # 相对于 FILE_PATH 的路径，如 "data/config.json"
) -> str
```

> **安全**：有路径遍历攻击防护，只能读取 `FILE_PATH` 目录下的文件

#### write_file_content - 写入文件

```python
@ai_tools()  # category="default"
async def write_file_content(
    ctx: RunContext[ToolContext],
    file_path: str,         # 相对于 FILE_PATH 的路径
    content: str,           # 要写入的内容
    overwrite: bool = True, # 是否覆盖已存在的文件
) -> str
```

#### execute_file - 执行脚本

```python
@ai_tools()  # category="default"
async def execute_file(
    ctx: RunContext[ToolContext],
    file_path: str,     # 相对于 FILE_PATH 的脚本路径
    timeout: int = 30,  # 超时时间（秒）
) -> str
```

#### diff_file_content - 文件对比

```python
@ai_tools()  # category="default"
async def diff_file_content(
    ctx: RunContext[ToolContext],
    file_path_a: str,  # 第一个文件路径
    file_path_b: str,  # 第二个文件路径
) -> str
```

#### list_directory - 列出目录

```python
@ai_tools()  # category="default"
async def list_directory(
    ctx: RunContext[ToolContext],
    dir_path: str = "",        # 相对于 FILE_PATH 的目录路径，空字符串表示根目录
    recursive: bool = False,   # 是否递归列出子目录
) -> str
```

---

## 9. System Prompt 管理

System Prompt 模块提供系统提示词的 CRUD 管理和向量检索功能，主要供 `create_subagent` 使用。

### 9.1 模块导入

```python
from gsuid_core.ai_core.system_prompt import (
    SystemPrompt,          # 数据模型
    get_all_prompts,       # 获取所有 System Prompt
    get_prompt_by_id,      # 根据 ID 获取单个
    add_prompt,            # 添加
    update_prompt,         # 更新
    delete_prompt,         # 删除
    search_system_prompt,  # 向量检索
    get_best_match,        # 获取最佳匹配（供 create_subagent 使用）
)
```

### 9.2 数据模型

```python
class SystemPrompt(TypedDict):
    id: str            # 唯一标识，推荐格式: "plugin-name-purpose"
    title: str         # 标题，如"代码专家"
    desc: str          # 描述，用于向量检索匹配
    content: str       # 完整系统提示词内容（作为 system_prompt 传给 AI）
    tags: List[str]    # 标签列表，支持标签过滤检索
```

### 9.3 存储位置

- JSON 文件：`AI_CORE_PATH / "system_prompts.json"`
- 向量库 Collection：`system_prompts`

### 9.4 CRUD 操作

```python
from gsuid_core.ai_core.system_prompt import (
    SystemPrompt, add_prompt, get_prompt_by_id,
    update_prompt, delete_prompt, get_all_prompts,
)

# 添加新的 System Prompt
prompt = SystemPrompt(
    id="my-plugin-math-expert",
    title="数学专家",
    desc="专业的数学解题专家，擅长各类数学问题",
    content="""你是一个专业的数学老师，代号MathMaster。

## 核心能力
- 解答各类数学问题（代数、几何、微积分）
- 提供清晰的解题步骤
- 用通俗的语言解释复杂概念

## 回复格式
- 给出解题步骤
- 最后给出答案
- 必要时用 LaTeX 公式""",
    tags=["数学", "解题", "教育"]
)
add_prompt(prompt)

# 查询
all_prompts = get_all_prompts()
single = get_prompt_by_id("my-plugin-math-expert")

# 更新
update_prompt("my-plugin-math-expert", {"title": "高级数学专家"})

# 删除
delete_prompt("my-plugin-math-expert")
```

### 9.5 向量检索

```python
from gsuid_core.ai_core.system_prompt import search_system_prompt, get_best_match

# 搜索匹配的 System Prompt
results = await search_system_prompt(
    query="写一个Python快速排序函数",
    tags=["代码"],     # 可选，标签过滤
    limit=5,           # 最大返回数量
    use_vector=True,   # 使用向量检索（默认）
)

# 获取最佳匹配（返回匹配度最高的一个）
best = await get_best_match(
    query="帮我写一段代码",
    tags=["代码"]
)
if best:
    print(best["title"])    # 代码专家
    print(best["content"])  # 系统提示词内容
```

### 9.6 默认内置 System Prompt

系统自带以下 System Prompt（在 `defaults.py` 中定义）：

| ID | 标题 | 适用场景 |
|----|------|---------|
| `default-code-expert` | 代码专家 | 编写各种编程语言的代码 |
| `default-finance-expert` | 财经专家 | 股票分析、投资建议 |
| `default-design-expert` | 图片设计专家 | UI/UX、海报、Logo设计 |
| `default-text-summarizer` | 文本摘要专家 | 长文本压缩摘要 |

---

## 10. 工具注册表查询 API

```python
from gsuid_core.ai_core.register import get_registered_tools, get_all_tools

# 获取按分类组织的工具字典
# 返回: Dict[str, Dict[str, ToolBase]]
all_by_category = get_registered_tools()
# {
#   "buildin": {"search_knowledge": ToolBase(...), ...},
#   "default": {"get_current_date": ToolBase(...), ...},
# }

# 查看某分类的工具
buildin_tools = all_by_category.get("buildin", {})
for name, tool_base in buildin_tools.items():
    print(f"{name}: {tool_base.description}")

# 获取平铺结构（所有分类合并）
# 返回: Dict[str, ToolBase]
all_flat = get_all_tools()
for name, tool_base in all_flat.items():
    print(f"{name} (plugin={tool_base.plugin})")
```

**ToolBase 属性**：

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 工具函数名 |
| `description` | `str` | 工具描述（来自函数 `__doc__`） |
| `plugin` | `str` | 插件来源（在 `plugins/` 下自动推断，核心工具为 `"core"`） |
| `tool` | `Tool[ToolContext]` | PydanticAI Tool 对象 |

---

## 11. 类型定义参考

### 11.1 ToolContext

```python
@dataclass
class ToolContext:
    """工具执行上下文"""
    bot: Optional[Bot] = None   # Bot 实例，用于发送消息
    ev: Optional[Event] = None  # 事件实例，包含用户ID、群组ID等
```

**访问方式**：

```python
# 通过 RunContext
async def my_tool(ctx: RunContext[ToolContext], ...) -> str:
    bot = ctx.deps.bot
    ev = ctx.deps.ev
    user_id = ev.user_id if ev else None
    group_id = ev.group_id if ev else None

# 通过 ToolContext
async def my_tool(ctx: ToolContext, ...) -> str:
    bot = ctx.bot
    ev = ctx.ev
```

### 11.2 KnowledgeBase

```python
class KnowledgeBase(TypedDict):
    id: str          # 唯一标识符
    plugin: str      # 插件名称
    title: str       # 标题
    content: str     # 内容（支持 Markdown）
    tags: List[str]  # 标签列表
    source: str      # "plugin" 或 "manual"
```

### 11.3 KnowledgePoint

```python
class KnowledgePoint(KnowledgeBase):
    _hash: str  # 自动计算的内容哈希
```

### 11.4 ManualKnowledgeBase

```python
class ManualKnowledgeBase(TypedDict):
    id: str
    plugin: str
    title: str
    content: str
    tags: List[str]
    source: str  # 固定为 "manual"
```

### 11.5 ImageEntity

```python
class ImageEntity(TypedDict):
    id: str               # 唯一标识符
    plugin: str           # 插件名称
    path: str             # 图片文件路径
    tags: List[str]       # 描述标签
    content: str          # 详细描述文本
    source: str           # "plugin"
    _hash: Optional[str]  # 内容哈希
```

### 11.6 ToolBase

```python
class ToolBase:
    name: str                    # 工具名
    description: str             # 工具描述
    plugin: str                  # 所属插件（"core" 或插件名）
    tool: Tool[ToolContext]       # PydanticAI Tool 对象
```

### 11.7 CheckFunc 类型

```python
# 支持同步和异步
CheckFunc = Callable[..., Union[
    Tuple[bool, str],
    Awaitable[Tuple[bool, str]],
]]

# 返回值含义
# (True, "")        -> 校验通过
# (False, "原因")   -> 校验失败，"原因" 作为工具返回值告知 AI
```

---

## 12. 完整示例

### 12.1 示例一：基础工具注册

```python
# my_plugin/ai_tools.py

from pydantic_ai import RunContext
from gsuid_core.models import Event
from gsuid_core.bot import Bot
from gsuid_core.ai_core.register import ai_tools, ai_entity, ai_alias
from gsuid_core.ai_core.models import ToolContext, KnowledgePoint

# ============================================================
# 1. 注册别名（插件加载时自动执行）
# ============================================================
ai_alias("雷电将军", ["雷神", "将军", "影", "屑"])
ai_alias("纳西妲", ["草神", "小草神"])

# ============================================================
# 2. 注册知识库
# ============================================================
ai_entity(KnowledgePoint(
    id="myplugin_intro",
    plugin="MyPlugin",
    title="MyPlugin 插件介绍",
    content="""
# MyPlugin 使用指南

## 命令列表
- `/query <角色名>` - 查询角色信息
- `/bind <uid>` - 绑定账号
- `/help` - 显示帮助

## 注意事项
1. 需要先绑定账号才能使用查询功能
2. 每日查询上限为100次
""",
    tags=["帮助", "命令", "MyPlugin"],
))

# ============================================================
# 3. 权限校验函数
# ============================================================
async def check_bound(ev: Event) -> tuple[bool, str]:
    """检查用户是否已绑定账号"""
    from my_plugin.database import is_user_bound
    if await is_user_bound(ev.user_id):
        return True, ""
    return False, "⚠️ 请先绑定账号：发送 /bind <你的UID>"

async def check_admin(ev: Event) -> tuple[bool, str]:
    """检查是否管理员"""
    ADMIN_IDS = ["123456789", "987654321"]
    if ev.user_id in ADMIN_IDS:
        return True, ""
    return False, "⚠️ 此工具仅管理员可用"

# ============================================================
# 4. 注册 AI 工具
# ============================================================

# 简单工具（无上下文）
@ai_tools(category="default")
async def calculate(
    expression: str,
) -> str:
    """
    计算数学表达式

    Args:
        expression: 数学表达式，如 "1+2*3"
    """
    try:
        result = eval(expression, {"__builtins__": {}})
        return f"计算结果：{expression} = {result}"
    except Exception as e:
        return f"计算失败：{str(e)}"


# 需要上下文的工具
@ai_tools(category="default", check_func=check_bound)
async def query_character(
    ctx: RunContext[ToolContext],
    character_name: str,
) -> str:
    """
    查询游戏角色详细信息

    Args:
        character_name: 角色名称，如"雷电将军"、"胡桃"
    """
    ev = ctx.deps.ev
    uid = await get_bound_uid(ev.user_id)
    data = await fetch_character_data(uid, character_name)
    return f"角色 {character_name} 的数据：\n{data}"


# 管理员工具
@ai_tools(category="default", check_func=check_admin)
async def admin_reset_cd(
    ctx: RunContext[ToolContext],
    user_id: str,
) -> str:
    """
    重置指定用户的冷却时间（管理员专用）

    Args:
        user_id: 要重置的用户ID
    """
    await reset_cooldown(user_id)
    return f"✅ 已重置用户 {user_id} 的冷却时间"


# 自动注入 Bot（不暴露给 LLM）
@ai_tools(category="default")
async def send_custom_message(
    bot: Bot,
    message: str,
) -> str:
    """
    发送自定义文本消息

    Args:
        message: 要发送的消息内容
    """
    await bot.send(message)
    return "✅ 消息已发送"
```

### 12.2 示例二：创建临时 Agent 做专项任务

```python
from gsuid_core.ai_core.gs_agent import create_agent

# 创建翻译 Agent
translator = create_agent(
    system_prompt="""你是一个翻译助手，仅负责将中文翻译成英文。
规则：
1. 直接输出翻译结果，不加任何解释
2. 保持原文的语气和风格
3. 专有名词保持原样""",
)

async def translate_to_english(text: str) -> str:
    return await translator.run(user_message=text)


# 创建代码审查 Agent
code_reviewer = create_agent(
    system_prompt="""你是一个严格的代码审查专家。
请对用户提供的代码进行审查，关注：
1. 潜在的 Bug
2. 性能问题
3. 代码风格
4. 安全漏洞

输出格式：
## 审查结论
[总体评价]

## 问题列表
1. [问题描述] - [严重程度：高/中/低]

## 改进建议
[具体建议]""",
)

async def review_code(code: str, bot, ev) -> str:
    return await code_reviewer.run(
        user_message=f"请审查以下代码：\n```\n{code}\n```",
        bot=bot,
        ev=ev,
    )
```

### 12.3 示例三：完整插件入口文件

```python
# my_plugin/__init__.py

from gsuid_core.sv import SV
from gsuid_core.ai_core.register import ai_alias

# 注册别名（导入时自动执行）
ai_alias("我的插件", ["MyPlugin", "mp"])

# 导入 AI 工具模块（触发工具注册）
from my_plugin import ai_tools  # noqa: F401

# 注册插件命令
sv = SV("我的插件")

@sv.on_fullmatch("/help")
async def show_help(bot, ev):
    await bot.send("""
我的插件使用指南：
- /help: 显示此帮助
- /bind <uid>: 绑定账号
- /query <角色>: 查询角色

AI功能：
- 直接@机器人并描述需求，AI会自动调用相关工具
""")
```

### 12.4 示例四：注册并使用自定义 System Prompt

```python
from gsuid_core.ai_core.system_prompt import add_prompt, SystemPrompt

# 在插件初始化时注册 System Prompt
def register_prompts():
    add_prompt(SystemPrompt(
        id="myplugin-game-guide",
        title="游戏攻略助手",
        desc="专业的游戏攻略助手，擅长解答原神等游戏的相关问题",
        content="""你是一个专业的游戏攻略助手，代号 GameGuide。

## 专长领域
- 原神：角色培养、圣遗物搭配、队伍组合
- 崩坏：星穹铁道：遗器搭配、角色技能
- 鸣潮：共鸣者培养

## 回复规范
1. 提供具体数据和建议
2. 考虑玩家实际资源情况
3. 标注信息时效性（如版本号）
4. 用简洁的格式呈现""",
        tags=["游戏", "攻略", "原神", "星穹铁道"]
    ))

register_prompts()

# 之后 create_subagent 会自动找到这个 System Prompt
# @ai_tools 工具中调用：
# result = await create_subagent(ctx, "雷电将军圣遗物怎么搭？", tags="游戏,原神")
```

---

## 附录：常见问题

### Q1: 工具注册后 AI 能直接使用吗？

取决于 `category`：
- `category="buildin"`：主Agent直接可用
- `category="default"` 或其他：需通过 `create_subagent` 在子Agent中使用

### Q2: 如何让插件工具被 AI 主Agent直接调用？

将 `category` 设置为 `"buildin"`，但要谨慎——主Agent的工具越多，token 消耗越大。推荐将高频核心工具注册为 `"buildin"`，其他通过子Agent完成。

### Q3: check_func 和工具自身的错误处理有什么区别？

- `check_func` 在工具执行**前**校验，失败时返回错误消息给 AI，工具函数**不会被执行**
- 工具函数内部的 `try/except` 处理执行过程中的异常

### Q4: 工具函数的 docstring 有格式要求吗？

推荐使用 Google 风格的 docstring：

```python
async def my_tool(ctx: RunContext[ToolContext], param: str) -> str:
    """
    简短的工具描述（AI 将看到这行）

    详细说明工具的作用、限制和使用场景。

    Args:
        param: 参数说明（AI 会参考这里理解如何调用工具）

    Returns:
        返回值说明
    """
```

### Q5: `ai_entity` 和 `add_manual_knowledge` 什么时候同步到向量库？

- `ai_entity`：系统启动时，`rag/startup.py` 的 `init_all()` 自动同步
- `add_manual_knowledge`：不自动同步，需要手动调用向量库 API

---

## 相关文档

- [AI 触发流转文档](./AI_TRIGGER_FLOW.md)
- [toAgent 设计文档](./toAgent.md)
- [WebConsole API 文档](../gsuid_core/webconsole/API.md)
