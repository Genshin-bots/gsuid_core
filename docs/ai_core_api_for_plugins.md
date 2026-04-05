# GsCore AI Core 插件开发者 API 文档

## 概述

本文档面向插件开发者，介绍如何使用 `gsuid_core/ai_core` 模块为机器人 AI 提供工具函数、知识库和自定义 Agent 支持。

**核心模块路径**: `gsuid_core/ai_core/`

---

## 目录

1. [模块导入速查](#1-模块导入速查)
2. [@ai_tools 装饰器](#2-ai_tools-装饰器)
3. [create_agent 临时 Agent](#3-create_agent-临时-agent)
4. [知识库注册](#4-知识库注册)
5. [别名注册](#5-别名注册)
6. [内置工具一览](#6-内置工具一览)
7. [Skills 系统](#7-skills-系统)
8. [类型定义](#8-类型定义)
9. [完整示例](#9-完整示例)

---

## 1. 模块导入速查

```python
# 工具注册装饰器
from gsuid_core.ai_core.register import ai_tools, ai_entity, ai_alias, add_manual_knowledge

# 临时 Agent 创建
from gsuid_core.ai_core.gs_agent import create_agent

# 工具上下文
from gsuid_core.ai_core.models import ToolContext

# PydanticAI RunContext
from pydantic_ai import RunContext

# 内置工具
from gsuid_core.ai_core.buildin_tools import (
    search_knowledge,      # 知识库检索
    web_search,            # Web搜索
    send_message_by_ai,    # 发送消息
    query_user_favorability,   # 查询好感度
    query_user_memory,     # 查询用户记忆
    set_user_favorability,     # 设置好感度
    update_user_favorability,  # 更新好感度
)
```

---

## 2. @ai_tools 装饰器

### 2.1 入口函数

```python
from gsuid_core.ai_core.register import ai_tools
```

### 2.2 函数签名

```python
def ai_tools(
    func: Optional[Callable] = None,
    /,
    *,
    check_func: Optional[Callable[..., Awaitable[Tuple[bool, str]]]] = None,
    **check_kwargs,
) -> Callable[[F], F] | F
```

### 2.3 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `func` | `Callable` | 否 | 被装饰的异步函数（装饰器模式自动传入） |
| `check_func` | `Callable` | 否 | 权限校验函数，签名为 `async def check_xxx(...) -> Tuple[bool, str]` |
| `**check_kwargs` | `Any` | 否 | 额外传递给 `check_func` 的参数 |

### 2.4 被装饰函数的签名要求

```python
@ai_tools()
async def my_tool(
    ctx: RunContext[ToolContext],   # 可选: RunContext / ToolContext / 不传
    param1: str,                    # 业务参数
    param2: int = 10,              # 可选参数
) -> str:
    """工具描述 - 将作为 AI 看到的工具说明"""
    # ctx.deps.bot  - Bot 对象
    # ctx.deps.ev   - Event 对象
    return f"结果: {param1}"
```

**三种上下文模式**：

| 模式 | 函数签名 | 说明 |
|------|----------|------|
| RunContext | `async def tool(ctx: RunContext[ToolContext], ...)` | PydanticAI 推荐方式，可访问 `ctx.deps.bot` 和 `ctx.deps.ev` |
| ToolContext | `async def tool(ctx: ToolContext, ...)` | 直接使用上下文对象 |
| 无上下文 | `async def tool(param1: str, ...)` | 简单工具不需要上下文 |

### 2.5 check_func 参数注入

`check_func` 的参数会根据类型自动注入：

| 参数类型 | 注入值 |
|----------|--------|
| `Event` | `ctx.deps.ev` |
| `Bot` | `ctx.deps.bot` |

### 2.6 返回值类型处理

| 返回类型 | 处理方式 |
|----------|----------|
| `str` | 直接返回 |
| `Message` | 调用 `bot.send()` 发送，并返回描述 |
| `dict` | JSON 序列化后返回 |
| `Image.Image` | 转换为图片发送，返回资源ID |
| `bytes` | 作为资源发送 |

---

## 3. create_agent 临时 Agent

### 3.1 入口函数

```python
from gsuid_core.ai_core.gs_agent import create_agent
```

### 3.2 函数签名

```python
def create_agent(
    model_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> GsCoreAIAgent
```

### 3.3 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model_name` | `str` | 否 | 模型名称，默认使用配置中的 `model_name` |
| `system_prompt` | `str` | 否 | 系统提示词，控制 AI 行为 |

### 3.4 GsCoreAIAgent.run() 方法

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
| `user_message` | `str` | 是 | 用户输入消息 |
| `bot` | `Bot` | 否 | Bot 对象，用于发送消息 |
| `ev` | `Event` | 否 | 事件对象 |
| `rag_context` | `str` | 否 | 额外的 RAG 上下文 |

**返回**: AI 响应字符串

---

## 4. 知识库注册

### 4.1 ai_entity - 插件知识注册

**入口函数**：

```python
from gsuid_core.ai_core.register import ai_entity
```

**函数签名**：

```python
def ai_entity(entity: Union[KnowledgePoint, KnowledgeBase]) -> None
```

**参数说明**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | `str` | 是 | 唯一标识符 |
| `plugin` | `str` | 是 | 插件名称 |
| `title` | `str` | 是 | 知识点标题 |
| `content` | `str` | 是 | 知识点内容（支持 Markdown） |
| `tags` | `List[str]` | 是 | 标签列表 |
| `_hash` | `str` | 自动 | 不需传入，自动计算 |

**示例**：

```python
from gsuid_core.ai_core.register import ai_entity
from gsuid_core.ai_core.models import KnowledgePoint

# 在插件初始化时调用
ai_entity(KnowledgePoint(
    id="genshin_character_001",
    plugin="Genshin",
    title="雷电将军 - 角色介绍",
    content="""
    # 雷电将军

    ## 角色信息
    - 元素: 雷
    - 武器: 长枪
    - 命之座: 万世之座

    ## 技能简介
    ### 普通攻击 - 源流
    枪类的普通攻击...

    ### 元素战技 - 奥义 - 梦想真说
    展现梦想的力量...
    """,
    tags=["原神", "雷电将军", "雷神", "角色"],
))
```

**特点**：
- 在 `plugins/` 目录下的插件注册会自动归属到对应插件名
- 启动时自动同步到向量数据库
- 内容变化时会自动更新

---

### 4.2 add_manual_knowledge - 手动知识添加

**入口函数**：

```python
from gsuid_core.ai_core.register import add_manual_knowledge
```

**函数签名**：

```python
def add_manual_knowledge(entity: ManualKnowledgeBase) -> bool
```

**参数说明**：与 `ai_entity` 相同，但 `source` 固定为 `"manual"`

**示例**：

```python
from gsuid_core.ai_core.register import add_manual_knowledge
from gsuid_core.ai_core.models import ManualKnowledgeBase

add_manual_knowledge(ManualKnowledgeBase(
    id="manual_faq_001",
    plugin="custom",
    title="常见问题回答",
    content="Q: 如何绑定账号？\nA: 发送 '绑定 123456' 即可。",
    tags=["FAQ", "绑定"],
))
```

**特点**：
- 不会在启动时同步
- 适用于通过前端 API 手动添加的内容
- 需要手动管理增删改

---

### 4.3 手动知识管理 API

| 函数 | 签名 | 说明 |
|------|------|------|
| `add_manual_knowledge` | `(entity) -> bool` | 添加知识，已存在返回 `False` |
| `update_manual_knowledge` | `(entity_id: str, updates: dict) -> bool` | 更新知识 |
| `delete_manual_knowledge` | `(entity_id: str) -> bool` | 删除知识 |
| `get_manual_entities` | `() -> List[ManualKnowledgeBase]` | 获取所有手动知识 |
| `get_manual_entity` | `(entity_id: str) -> Optional[ManualKnowledgeBase]` | 获取指定知识 |

---

## 5. 别名注册

### 5.1 入口函数

```python
from gsuid_core.ai_core.register import ai_alias
```

### 5.2 函数签名

```python
def ai_alias(name: str, alias: Union[str, List[str]]) -> None
```

### 5.3 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | `str` | 是 | 标准名称 |
| `alias` | `str` | 是 | 别名（单个或列表） |

### 5.4 示例

```python
from gsuid_core.ai_core.register import ai_alias

# 单个别名
ai_alias("雷电将军", "雷神")

# 多个别名
ai_alias("雷电将军", ["雷神", "将军", "影", "屑"])

# 在插件初始化时注册
def on_init():
    ai_alias("丝柯克", ["skk", "斯柯克", "SKK"])
```

---

## 6. 内置工具一览

### 6.1 search_knowledge

检索知识库内容。

```python
from gsuid_core.ai_core.buildin_tools import search_knowledge

async def search_knowledge(
    ctx: RunContext[ToolContext],
    query: str,                      # 自然语言查询
    category: Optional[str] = None,  # 知识类别筛选
    plugin: Optional[str] = None,    # 插件来源筛选
    limit: int = 10,                 # 最大返回数量
    score_threshold: float = 0.45,   # 相似度阈值
) -> str
```

### 6.2 send_message_by_ai

AI 主动发送消息。

```python
from gsuid_core.ai_core.buildin_tools import send_message_by_ai

async def send_message_by_ai(
    ctx: RunContext[ToolContext],
    message_type: Literal["text", "image"],  # 消息类型
    text: Optional[str] = None,              # 文本内容
    image_id: Optional[str] = None,          # 图片资源ID
    user_id: Optional[str] = None,          # 目标用户ID
) -> str
```

### 6.3 query_user_favorability

查询用户好感度。

```python
from gsuid_core.ai_core.buildin_tools import query_user_favorability

async def query_user_favorability(
    ctx: RunContext[ToolContext],
    user_id: Optional[str] = None,
) -> str
```

### 6.4 query_user_memory

查询用户记忆条数。

```python
from gsuid_core.ai_core.buildin_tools import query_user_memory

async def query_user_memory(
    ctx: RunContext[ToolContext],
    user_id: Optional[str] = None,
) -> str
```

### 6.5 set_user_favorability / update_user_favorability

设置或更新用户好感度。

```python
from gsuid_core.ai_core.buildin_tools import (
    set_user_favorability,
    update_user_favorability,
)

async def set_user_favorability(
    ctx: RunContext[ToolContext],
    value: int,                        # 好感度绝对值
    user_id: Optional[str] = None,
) -> str

async def update_user_favorability(
    ctx: RunContext[ToolContext],
    delta: int,                        # 好感度变化量
    user_id: Optional[str] = None,
) -> str
```

---

## 7. Skills 系统

Skills 是 PydanticAI 的技能系统，用于为 AI 提供额外的指令和能力。

### 7.1 入口

```python
from gsuid_core.ai_core.skills import SKILLS_PATH, skills, skills_toolset
```

| 变量 | 类型 | 说明 |
|------|------|------|
| `SKILLS_PATH` | `Path` | Skills 文件目录 |
| `skills` | `dict[str, Skill]` | 已加载的技能字典 |
| `skills_toolset` | `SkillsToolset` | 技能工具集实例 |

### 7.2 添加自定义 Skill

在 `res/ai_core/skills/` 目录下添加 YAML 或 Markdown 文件：

```
res/ai_core/skills/
├── my_skill.yaml
└── custom_instruction.md
```

---

## 8. 类型定义

### 8.1 ToolContext

```python
@dataclass
class ToolContext:
    bot: Optional[Bot] = None   # Bot 对象
    ev: Optional[Event] = None  # 事件对象
```

### 8.2 KnowledgeBase

```python
class KnowledgeBase(TypedDict):
    id: str                     # 唯一标识
    plugin: str                 # 插件名称
    title: str                  # 标题
    content: str                # 内容
    tags: List[str]             # 标签
    source: str                 # "plugin" 或 "manual"
```

### 8.3 KnowledgePoint

```python
class KnowledgePoint(KnowledgeBase):
    _hash: str                   # 自动计算的哈希值
```

### 8.4 ManualKnowledgeBase

```python
class ManualKnowledgeBase(TypedDict):
    id: str
    plugin: str
    title: str
    content: str
    tags: List[str]
    source: str  # 固定为 "manual"
```

---

## 9. 完整示例

### 9.1 示例一：注册工具并使用 check_func

```python
from pydantic_ai import RunContext
from gsuid_core.ai_core.register import ai_tools, ai_entity
from gsuid_core.ai_core.models import ToolContext, KnowledgePoint, Event

# 定义权限校验函数
async def check_admin(ev: Event) -> tuple[bool, str]:
    """仅允许管理员使用此工具"""
    if ev.user_id == "123456":
        return True, ""
    return False, "仅管理员可用"

# 注册带权限校验的工具
@ai_tools(check_func=check_admin)
async def admin_query(
    ctx: RunContext[ToolContext],
    uid: str,
) -> str:
    """
    管理员查询接口

    Args:
        uid: 要查询的用户ID
    """
    return f"管理员查询了用户 {uid} 的信息"

# 注册无需上下文的工具
@ai_tools()
async def simple_calc(a: int, b: int, operation: str = "add") -> str:
    """
    简单计算器

    Args:
        a: 第一个数
        b: 第二个数
        operation: 操作类型，add/sub/mul/div
    """
    if operation == "add":
        return str(a + b)
    elif operation == "sub":
        return str(a - b)
    elif operation == "mul":
        return str(a * b)
    elif operation == "div":
        if b == 0:
            return "错误：除数不能为0"
        return str(a / b)
    return "未知操作"

# 注册知识库
ai_entity(KnowledgePoint(
    id="plugin_help_001",
    plugin="MyPlugin",
    title="插件帮助文档",
    content="""
    # MyPlugin 插件帮助

    ## 命令列表
    - /help - 显示帮助
    - /admin - 管理员命令

    ## 功能说明
    本插件提供...功能
    """,
    tags=["帮助", "命令", "文档"],
))
```

### 9.2 示例二：创建临时 Agent 处理自定义任务

```python
from gsuid_core.ai_core.gs_agent import create_agent

# 创建自定义 Agent
agent = create_agent(
    system_prompt="""
你是一个翻译助手，负责将中文翻译成英文。
只翻译用户输入的内容，不要添加任何解释。
""",
)

# 在异步函数中使用
async def translate_text(text: str) -> str:
    result = await agent.run(text)
    return result

# 使用结果
result = await translate_text("你好，世界！")
print(result)  # Hello, world!
```

### 9.3 示例三：完整的插件初始化示例

```python
# my_plugin/__init__.py

from gsuid_core.sv import SV
from gsuid_core.plugin import Plugin

from gsuid_core.ai_core.register import ai_tools, ai_entity, ai_alias
from gsuid_core.ai_core.models import KnowledgePoint

# 注册别名
ai_alias("我的插件", ["MyPlugin", "mp"])

# 注册知识库
ai_entity(KnowledgePoint(
    id="my_plugin_info",
    plugin="MyPlugin",
    title="我的插件介绍",
    content="""
# MyPlugin

## 功能特性
1. 功能A
2. 功能B

## 使用方法
发送 /help 获取帮助
""",
    tags=["插件", "功能", "帮助"],
))

# 定义 AI 工具
@ai_tools()
async def my_plugin_query(query: str) -> str:
    """
    查询插件相关信息

    Args:
        query: 查询内容
    """
    return f"查询结果: {query}"

# 注册插件
sv_myplugin = SV("我的插件")
pd_mp = Plugin(
    name="我的插件",
    pm=1,
    sv=sv_myplugin,
)

@pd_mp.on_fullmatch("/myplugin help")
async def show_help(bot, ev):
    await bot.send("我的插件帮助信息...")
```

---

## 相关文档

- [AI 处理流程](./ai_handle_flow.md)
- [知识库重构设计](./knowledge_base_restructuring.md)
- [WebConsole API](./webconsole/API.md)
