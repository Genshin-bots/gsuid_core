# GsCore AI Core 插件开发者 API 文档

## 概述

本文档面向插件开发者，介绍如何使用 `gsuid_core/ai_core` 模块为机器人 AI 提供工具函数、知识库和自定义 Agent 支持。

**核心模块路径**: `gsuid_core/ai_core/`

---

## 目录

1. [模块导入速查](#1-模块导入速查)
2. [@ai_tools 装饰器](#2-ai_tools-装饰器)
3. [工具分类系统（category）](#3-工具分类系统category)
4. [触发器 → AI 工具桥接（to_ai）](#4-触发器--ai-工具桥接to_ai)
5. [create_agent 与 Agent 架构](#5-create_agent-与-agent-架构)
6. [知识库注册](#6-知识库注册)
7. [别名注册](#7-别名注册)
8. [图片实体注册](#8-图片实体注册)
9. [内置工具一览](#9-内置工具一览)
10. [System Prompt 管理](#10-system-prompt-管理)
11. [Persona 角色系统](#11-persona-角色系统)
12. [Memory 记忆系统](#12-memory-记忆系统)
13. [Scheduled Task 定时任务](#13-scheduled-task-定时任务)
14. [工具注册表查询 API](#14-工具注册表查询-api)
15. [类型定义参考](#15-类型定义参考)
16. [完整示例](#16-完整示例)
17. [MCP 工具集成](#17-mcp-工具集成)
18. [Image Understand 图片理解](#18-image-understand-图片理解)
19. [Web Search 统一搜索](#19-web-search-统一搜索)
20. [Meme 表情包模块](#20-meme-表情包模块)

---

## 1. 模块导入速查

```python
# ============================================================
# 工具注册装饰器
# ============================================================
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

# ============================================================
# 触发器 → AI 工具桥接
# ============================================================
from gsuid_core.ai_core.trigger_bridge import (
    ai_return,               # 在触发器函数内向 AI 返回纯文本中间结果
)

# ============================================================
# Agent 创建
# ============================================================
from gsuid_core.ai_core.gs_agent import (
    create_agent,           # 创建临时 Agent
)

from gsuid_core.ai_core.rag.tools import (
    get_main_agent_tools,   # 获取主Agent工具列表
)

# ============================================================
# AI 聊天入口
# ============================================================
from gsuid_core.ai_core.handle_ai import handle_ai_chat

# ============================================================
# 工具上下文
# ============================================================
from gsuid_core.ai_core.models import ToolContext

# ============================================================
# PydanticAI RunContext
# ============================================================
from pydantic_ai import RunContext

# ============================================================
# 数据模型
# ============================================================
from gsuid_core.ai_core.models import (
    KnowledgeBase,       # 知识库基类
    KnowledgePoint,      # 知识点（插件注册）
    ManualKnowledgeBase, # 手动知识
    ImageEntity,         # 图片实体
    ToolBase,            # 工具元数据
)

# ============================================================
# MCP 工具集成
# ============================================================
from gsuid_core.ai_core.mcp import (
    MCPClient,               # MCP 客户端
    MCPToolInfo,              # MCP 工具信息
    MCPToolResult,            # MCP 工具调用结果
    MCPConfig,                # MCP 配置数据类
    MCPToolDefinition,        # MCP 工具定义
    MCPConfigManager,         # MCP 配置管理器
    mcp_config_manager,       # 全局 MCP 配置管理器单例
    MCP_PRESETS,              # MCP 预设配置列表
    parse_mcp_tool_id,        # 解析 MCP 工具 ID
    format_mcp_tool_id,      # 格式化 MCP 工具 ID
    register_all_mcp_tools,   # 注册所有 MCP 工具
    register_single_mcp_server,  # 注册单个 MCP 服务器
    unregister_mcp_server,   # 注销 MCP 服务器
    get_mcp_server,           # 获取 MCP Server 实例
    get_mcp_trigger_count,    # 获取 MCP 触发器注册数量
)

from gsuid_core.ai_core.mcp.mcp_tool_caller import (
    call_mcp_tool,           # 通用 MCP 工具调用
)

from gsuid_core.ai_core.mcp.mcp_tools_config import (
    mcp_tools_config,        # MCP 工具配置（websearch/image_understand）
)

# ============================================================
# Image Understand 图片理解
# ============================================================
from gsuid_core.ai_core.image_understand import (
    understand_image,        # 统一图片理解接口
)

# ============================================================
# Web Search 统一搜索
# ============================================================
from gsuid_core.ai_core.web_search.search import (
    web_search,              # 统一搜索接口
    web_search_with_context, # 带上下文的搜索接口
)

# ============================================================
# 内置工具（可直接导入使用）
# ============================================================
from gsuid_core.ai_core.buildin_tools import (
    # --- Self 工具 (category="self") --- 保底池，无条件全部加载
    # 只有主Agent能调用，用于核心操作
    query_user_favorability,    # 查询用户好感度
    update_user_favorability,   # 更新用户好感度（增量）
    create_subagent,            # 创建子Agent完成特定任务
    send_message_by_ai,         # 发送消息给用户
    add_once_task,              # 添加一次性定时任务（创建入口，口语化触发）
    add_interval_task,          # 添加循环任务（创建入口，口语化触发）

    # --- Buildin 工具 (category="buildin") --- 保底池，无条件全部加载
    # 主Agent调用时也会加载，直接调用不会拒绝
    search_knowledge,           # 知识库检索
    web_search_tool,            # Web搜索
    web_fetch_tool,             # 网页抓取（转Markdown）
    query_user_memory,          # 查询用户记忆
    get_self_info,              # 获取完整自我认知（身份/能力边界/主人）
    state_get,                  # 读取通用持久状态
    state_set,                  # 写入通用持久状态
    state_delete,               # 删除通用持久状态
    state_list,                 # 列出通用持久状态键
    state_append,               # 向列表型持久状态追加元素

    # --- Common 工具 (category="common") ---
    # 不属于保底池，向量检索按需加载，用户明确需要相关功能时才出现
    search_image,               # 图片检索
    get_self_persona_info,      # 获取自身Persona资源信息
    set_user_favorability,      # 设置用户好感度（绝对值）
    send_meme,                  # 发送表情包
    collect_meme,               # 收藏表情包
    search_meme,                # 搜索表情包
    list_scheduled_tasks,       # 列出所有定时任务（管理类）
    query_scheduled_task,       # 查询任务详情（管理类）
    modify_scheduled_task,      # 修改任务（管理类）
    cancel_scheduled_task,      # 取消任务（管理类）
    pause_scheduled_task,       # 暂停任务（管理类）
    resume_scheduled_task,      # 恢复任务（管理类）
    create_persistent_agent_tool,  # 创建持久化子Agent
    send_agent_task_tool,       # 向持久化Agent发送任务
    list_agents_tool,           # 列出所有活跃的持久化Agent
    stop_agent_tool,            # 停止指定的持久化Agent

    # --- Media 工具 (category="media") ---
    # 多媒体渲染工具
    render_html_to_image,       # 将HTML渲染为图片
    render_markdown_to_image,    # 将Markdown渲染为图片

    # --- Default 工具 (category="default") ---
    # 通过 create_subagent 调用，用于文件操作、代码执行等
    execute_shell_command,      # 执行系统命令
    get_current_date,           # 获取当前日期时间
    read_file_content,          # 读取文件
    write_file_content,         # 写入文件
    execute_file,               # 执行脚本
    diff_file_content,          # 文件对比
    list_directory,             # 列出目录

    # --- 动态工具发现（未注册为AI工具，仅可手动调用） ---
    discover_tools,             # 发现可能需要的新工具
    list_available_tools,       # 列出可用工具
)

# ============================================================
# RAG / 向量检索
# ============================================================
from gsuid_core.ai_core.rag import (
    init_embedding_model,
    sync_knowledge,
    query_knowledge,
    sync_images,
    search_images,
    search_and_load_image,
    init_knowledge_collection,
    init_image_collection,
    get_reranker,
    rerank_results,
)

# ============================================================
# System Prompt 管理
# ============================================================
from gsuid_core.ai_core.system_prompt import (
    SystemPrompt,
    get_all_prompts,
    get_prompt_by_id,
    add_prompt,
    update_prompt,
    delete_prompt,
    search_system_prompt,
    get_best_match,
)

# ============================================================
# Persona 角色系统
# ============================================================
from gsuid_core.ai_core.persona import (
    Persona,
    PersonaMetadata,
    PersonaFiles,
    build_persona_prompt,
    load_persona,
    save_persona,
    list_available_personas,
    get_persona_metadata,
    get_persona_image_path,
    get_persona_avatar_path,
    get_persona_audio_path,
    persona_config_manager,
)

# ============================================================
# Memory 记忆系统
# ============================================================
from gsuid_core.ai_core.memory import (
    memory_config,
    ScopeType,
    make_scope_key,
    observe,
    get_observation_queue,
    ObservationRecord,
    dual_route_retrieve,
    MemoryContext,
    get_ingestion_worker,
)

# ============================================================
# Statistics 统计系统
# ============================================================
from gsuid_core.ai_core.statistics import (
    statistics_manager,         # 统计管理器单例，所有 record_* 方法都在此对象上
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
    context_tags: Optional[List[str]] = None,
    **check_kwargs,
) -> Callable[[F], F]: ...
```

### 2.2 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `category` | `str` | `"default"` | 工具分类，决定工具放入哪个分类字典。`"self"` 为主Agent核心工具，`"buildin"` 为内置工具，`"common"` 为通用工具，`"default"` 为子Agent工具 |
| `check_func` | `Callable` | `None` | 可选的权限校验函数，签名为 `async def check(ev: Event) -> Tuple[bool, str]` |
| `context_tags` | `List[str]` | `None` | 语境标签。声明后，框架会在匹配该语境的群聊中通过**语境工具池**自动加载本工具，无需依赖向量搜索命中 |
| `**check_kwargs` | `Any` | — | 额外传递给 `check_func` 的参数 |

> **语境工具池**：插件可通过 `context_tags` 声明工具的适用语境，例如：
> ```python
> @ai_tools(category="genshin", context_tags=["原神", "Genshin", "游戏"])
> async def get_genshin_characters(ctx: RunContext[ToolContext], user_id: str) -> str:
>     """获取指定用户的原神角色列表及练度信息"""
>     ...
> ```
> 当框架通过群组画像判定当前群聊语境为"原神"时，该群内所有声明了 `原神` 标签的工具会被自动加入工具列表（最多 8 个），解决"群里问游戏问题但向量搜索命中不到游戏工具"的问题。语境标签由记忆系统在摄入群组对话时自动维护，无需人工配置。

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
@ai_tools(category="common", check_func=my_check_func)
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
    "self": {
        "query_user_favorability": ToolBase(...),
        "update_user_favorability": ToolBase(...),
        "create_subagent": ToolBase(...),
        "send_message_by_ai": ToolBase(...),
        "add_once_task": ToolBase(...),       # 定时任务"创建"入口，口语化触发
        "add_interval_task": ToolBase(...),   # 定时任务"创建"入口，口语化触发
    },
    "buildin": {
        "search_knowledge": ToolBase(...),
        "web_search_tool": ToolBase(...),
        "web_fetch_tool": ToolBase(...),
        "query_user_memory": ToolBase(...),
        "get_self_info": ToolBase(...),
        "state_get": ToolBase(...),
        "state_set": ToolBase(...),
        "state_delete": ToolBase(...),
        "state_list": ToolBase(...),
        "state_append": ToolBase(...),
    },
    "common": {
        "search_image": ToolBase(...),
        "get_self_persona_info": ToolBase(...),
        "set_user_favorability": ToolBase(...),
        "send_meme": ToolBase(...),
        "collect_meme": ToolBase(...),
        "search_meme": ToolBase(...),
        # 定时任务"管理"类——用户显式提需求时按需向量检索加载
        "list_scheduled_tasks": ToolBase(...),
        "query_scheduled_task": ToolBase(...),
        "modify_scheduled_task": ToolBase(...),
        "cancel_scheduled_task": ToolBase(...),
        "pause_scheduled_task": ToolBase(...),
        "resume_scheduled_task": ToolBase(...),
        "create_persistent_agent_tool": ToolBase(...),
        "send_agent_task_tool": ToolBase(...),
        "list_agents_tool": ToolBase(...),
        "stop_agent_tool": ToolBase(...),
    },
    "media": {
        "render_html_to_image": ToolBase(...),
        "render_markdown_to_image": ToolBase(...),
    },
    "default": {
        "execute_shell_command": ToolBase(...),
        "get_current_date": ToolBase(...),
        "read_file_content": ToolBase(...),
        "write_file_content": ToolBase(...),
        "execute_file": ToolBase(...),
        "diff_file_content": ToolBase(...),
        "list_directory": ToolBase(...),
    },
    "my_plugin": {
        "my_custom_tool": ToolBase(...),
    }
}
```

### 3.2 分类说明

| 分类名 | 说明 | 加载方式 |
|--------|------|-----------|
| `"self"` | 核心自我操作工具，只有主Agent能调用 | **保底**：无条件全部加载进主Agent |
| `"buildin"` | 框架基础工具（搜索/记忆/自我认知/持久状态等） | **保底**：无条件全部加载进主Agent |
| `"common"` | 通用工具，有选择地调用 | 向量检索按需加载 |
| `"media"` | 多媒体渲染工具 | 向量检索按需加载 |
| `"default"` | 子Agent工具，需通过 `create_subagent` 调用 | 子Agent向量检索按需加载 |
| `"mcp"` | MCP 外部工具，启动时自动注册 | 向量检索按需加载 |
| `"<自定义>"` | 插件自定义分类 | 向量检索按需加载 |

> **框架保底工具池**：`self` 与 `buildin` 两个分类构成"框架保底工具池"，
> `get_main_agent_tools()` 会把这两个分类下的工具**无条件全部加载**进主Agent，不受向量搜索影响。
> 一个工具是否属于保底池，**完全由它注册时声明的 `category` 决定**，框架内不存在硬编码的工具名单。
> 因此插件若希望某个工具成为主Agent的保底工具，注册时使用 `category="buildin"` 即可。

### 3.3 Agent 调用架构

```
┌─────────────────────────────────────────────────────┐
│              主Agent (Main Agent)                   │
│    使用 category="self", "buildin", "common", "media" │
│                                                     │
│  Self工具（保底，全部加载）:                          │
│  - query_user_favorability - update_user_favorability│
│  - create_subagent        - send_message_by_ai      │
│  - add_once_task          - add_interval_task        │
│                                                     │
│  Buildin工具（保底，全部加载）:                       │
│  - search_knowledge       - web_search_tool          │
│  - web_fetch_tool         - query_user_memory        │
│  - get_self_info                                     │
│  - state_get/set/delete/list/append                  │
│                                                     │
│  Common工具（向量检索按需加载）:                      │
│  - search_image           - get_self_persona_info    │
│  - set_user_favorability                             │
│  - send_meme              - collect_meme             │
│  - search_meme                                      │
│  - list_scheduled_tasks   - query_scheduled_task     │
│  - modify_scheduled_task  - cancel_scheduled_task    │
│  - pause_scheduled_task   - resume_scheduled_task    │
│  - create_persistent_agent - send_agent_task         │
│  - list_agents            - stop_agent               │
│                                                     │
│  Media工具:                                          │
│  - render_html_to_image   - render_markdown_to_image │
└─────────────────────────┬───────────────────────────┘
                          │ create_subagent()
                          ▼
┌─────────────────────────────────────────────────────┐
│              子Agent (Sub Agent)                    │
│          使用 category="default" 的工具              │
│                                                     │
│  - execute_shell_command  - get_current_date         │
│  - read_file_content     - write_file_content        │
│  - execute_file          - diff_file_content         │
│  - list_directory                                    │
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

# 核心工具（主Agent直接调用，需谨慎）
@ai_tools(category="common")
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

## 4. 触发器 → AI 工具桥接（to_ai）

### 4.1 概述

`to_ai` 参数允许插件开发者将现有的触发器函数自动注册为 AI 工具，无需编写重复的 `@ai_tools` 函数。AI 调用时使用 `MockBot` 拦截 `bot.send()`，将图片/消息内容收集而非真正发送，由 AI 决定是否真正发给用户。

**核心模块**: [`gsuid_core/ai_core/trigger_bridge.py`](gsuid_core/ai_core/trigger_bridge.py)

### 4.2 `to_ai` 参数

在所有 `on_xxx` 装饰器上新增 `to_ai: str = ""` 参数：

| 参数值 | 行为 |
|--------|------|
| `""`（默认） | 不注册为 AI 工具，行为完全不变 |
| 非空字符串 | 将该字符串作为 AI 工具的 docstring，自动注册到 `_TOOL_REGISTRY["by_trigger"]` |

```python
from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

sv = SV("股票插件")

@sv.on_command(
    "个股",
    to_ai="""
    查询指定股票或ETF的K线图或分时图。
    当用户询问某只股票/ETF今天走势、分时图、日K、周K、月K时调用。

    Args:
        text: 查询内容，格式为 "[周期前缀] 股票名称或代码"
              - 无前缀：默认显示分时图，例如 "证券ETF"
              - "日k": 日K线，例如 "日k 证券ETF"
              - 多个标的以空格分隔，例如 "证券ETF 白酒ETF"
    """,
)
async def send_stock_img(bot: Bot, ev: Event):
    content = ev.text.strip().lower()
    if not content:
        return await bot.send("请后跟股票代码使用")
    # ... 原有逻辑完全不变 ...
    await bot.send(im)
```

### 4.3 `ai_return()` — 向 AI 返回中间文本

在触发器函数内调用，向 AI 返回纯文本中间结果（如错误提示、进度信息）。

```python
from gsuid_core.ai_core.trigger_bridge import ai_return

@sv.on_command("个股", to_ai="查询股票走势...")
async def send_stock_img(bot: Bot, ev: Event):
    content = ev.text.strip()
    if not content:
        ai_return("错误：未提供股票代码")
        return await bot.send("请后跟股票代码使用")
    # ...
```

| 调用场景 | 行为 |
|----------|------|
| 真实用户触发 | 静默忽略，不影响触发器正常执行 |
| AI 工具调用 | 文本被收集，最终作为工具返回值返回给 AI |

### 4.4 `MockBot` — AI 调用时的消息拦截

AI 调用触发器时，包装函数使用 `MockBot` 代理真实 Bot：

| 方法 | 行为 |
|------|------|
| `send(str)` | 纯文字存入 `bot_messages`；`base64://`/`http://` 等图片字符串通过 `RM.register()` 注册，返回资源 ID |
| `send(bytes)` | 通过 `RM.register()` 注册，返回资源 ID |
| `send(Message(type="image"))` | 提取图片数据通过 `RM.register()` 注册，返回资源 ID |
| `reply()` | 同 `send()` |
| `send_option(reply, buttons)` | reply 走 `send()` 拦截，buttons 忽略 |
| `receive_resp(reply, ...)` | reply 走 `send()` 拦截，返回 `None`（AI 不支持交互式等待） |
| 其他属性 | 代理到真实 Bot（如 `bot_self_id`） |

### 4.4.1 权限检查

AI 调用触发器工具时，会执行与用户直接触发相同的权限检查：

- `plugins.enabled` — 插件是否启用
- `sv.enabled` — SV 是否启用
- `user_pm <= plugins.pm` — 插件级权限
- `user_pm <= sv.pm` — SV 级权限

权限不足时返回错误文本给 AI，AI 据此向用户解释。配置通过 webconsole 修改后实时生效（运行时读取，非注册时快照）。

### 4.5 `send_message_by_ai` 工具发送图片

位于 `gsuid_core.ai_core.buildin_tools.message_sender`，`self` 分类。
AI 通过资源 ID 将图片发送给用户：

```python
# AI 工具返回值示例：
# "[已生成 1 张图片，资源ID: img_a1b2c3d4。请调用 send_message_by_ai 工具传入 image_id 将图片发送给用户]"
```

AI 可以：
- 调用 `send_message_by_ai(image_id="img_a1b2c3d4")` → 通过 `RM.get()` 取回图片并发送
- 不调用 → 图片保留在 RM 中（用户只收到 AI 的文字回复）

资源 ID 通过 `RM.register()` 生成，格式为 `img_xxxxxxxx`，在 RM 中持久存储，支持跨轮次使用。

### 4.6 交互流程对比

| 场景 | bot 对象 | bot.send 行为 |
|------|---------|--------------|
| 用户直接触发 | 真实 Bot | 立即发送给用户 |
| AI 调用，AI 决定发 | MockBot | `RM.register()` → 返回资源 ID → AI 调用 `send_message_by_ai(image_id=...)` → 发出 |
| AI 调用，AI 决定不发 | MockBot | `RM.register()` → 返回资源 ID → AI 不调用 → 图片保留在 RM 中 |

### 4.7 注册时序

```
插件加载阶段 (cached_import)
    │
    ├── 执行 @sv.on_command("个股", to_ai="...")
    │       │
    │       └── _on() 检测 to_ai 非空
    │               │
    │               └── _register_trigger_as_ai_tool(func, keyword, to_ai_doc, sv, trigger_type)
    │                       │
    │                       └── 写入 _TOOL_REGISTRY["by_trigger"]["send_stock_img"]
    │
    └── 插件加载完成
```

---

## 5. create_agent 与 Agent 架构

### 5.1 create_agent - 创建临时 Agent

```python
from gsuid_core.ai_core.gs_agent import create_agent
```

**函数签名**：

```python
def create_agent(
    system_prompt: Optional[str] = None,
    max_tokens: int = 20000,
    max_iterations: Optional[int] = None,
    persona_name: Optional[str] = None,
    create_by: str = "LLM",
    max_history: int = 20,
    task_level: Literal["high", "low"] = "high",
) -> GsCoreAIAgent
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `system_prompt` | `str` | `None` | 系统提示词 |
| `max_tokens` | `int` | `20000` | 最大输出 token 数 |
| `max_iterations` | `int` | `None` | 最大迭代次数，`None` 时使用配置默认值 |
| `persona_name` | `str` | `None` | 绑定的 Persona 名称（用于热重载检测） |
| `create_by` | `str` | `"LLM"` | 创建者标识，影响工具加载策略 |
| `max_history` | `int` | `20` | 最大历史消息数 |
| `task_level` | `str` | `"high"` | 任务级别，`"high"` 或 `"low"`，用于选择对应模型配置 |

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

### 5.2 GsCoreAIAgent.run() 方法

```python
async def run(
    self,
    user_message: Union[str, Sequence[UserContent]],
    bot: Optional[Bot] = None,
    ev: Optional[Event] = None,
    rag_context: Optional[str] = None,
    tools: Optional[ToolList] = None,
    return_mode: Literal["always", "return", "by_bot"] = "by_bot",
    output_type: Optional[type[_T]] = None,
) -> Union[str, _T]
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_message` | `str \| Sequence` | 是 | 用户输入消息 |
| `bot` | `Bot` | 否 | Bot 对象，工具调用时注入 `ctx.deps.bot` |
| `ev` | `Event` | 否 | 事件对象，工具调用时注入 `ctx.deps.ev` |
| `rag_context` | `str` | 否 | 额外的 RAG 上下文，追加到 system_prompt |
| `tools` | `ToolList` | 否 | 自定义工具列表 |
| `return_mode` | `str` | 否 | 返回模式：`"always"` 始终返回、`"return"` 仅返回不发送、`"by_bot"` 由Bot决定发送 |
| `output_type` | `type[_T]` | 否 | 指定 Pydantic 模型类时，强制结构化输出 |

**返回**: AI 响应字符串，或指定的 Pydantic 模型实例

### 5.3 get_main_agent_tools - 获取主Agent保底工具集

```python
from gsuid_core.ai_core.rag.tools import get_main_agent_tools

async def get_main_agent_tools(query: str = "") -> ToolList
```

返回**框架保底工具池**——即 `category="self"` 和 `category="buildin"` 两个分类下的**全部**工具，
无条件加载、不受向量搜索影响。`query` 参数已废弃保留（仅作签名兼容），保底工具不再依赖 query 筛选。

主Agent 的完整工具列表 = 保底工具池（本函数）+ 语境工具池（`get_tools_by_context_tags`）
+ 查询工具池（`search_tools`），后两者合并去重后受附加数量上限约束。

### 5.4 handle_ai_chat - AI聊天入口

```python
from gsuid_core.ai_core.handle_ai import handle_ai_chat

async def handle_ai_chat(bot: Bot, event: Event)
```

**工作流程**：
1. 双层长度防护：硬截断 + 智能摘要
2. 意图识别：使用分类器判断用户意图（闲聊/工具/问答）
3. 获取 AI Session（含 system_prompt/Persona）
4. 准备上下文（历史记录 + 记忆检索）
5. 调用 Agent 生成回复
6. 发送回复给用户

---

## 6. 知识库注册

### 6.1 ai_entity - 插件知识注册

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

### 6.2 add_manual_knowledge - 手动知识添加

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

### 6.3 手动知识管理 API

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

## 7. 别名注册

### 7.1 入口

```python
from gsuid_core.ai_core.register import ai_alias
```

### 7.2 函数签名

```python
def ai_alias(name: str, alias: Union[str, List[str]]) -> None
```

别名系统用于 LLM 调用前进行**专有名词归一化**，将用户输入的别名统一替换为标准名称。

### 7.3 参数说明

| 参数 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 标准名称（归一化目标） |
| `alias` | `str \| List[str]` | 别名，可以是单个字符串或列表 |

### 7.4 示例

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

## 8. 图片实体注册

### 8.1 入口

```python
from gsuid_core.ai_core.register import ai_image
from gsuid_core.ai_core.models import ImageEntity
```

### 8.2 函数签名

```python
def ai_image(entity: ImageEntity) -> None
```

### 8.3 ImageEntity 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | `str` | 是 | 唯一标识符 |
| `plugin` | `str` | 是 | 插件名称 |
| `path` | `str` | 是 | 图片路径（绝对路径或相对路径） |
| `tags` | `List[str]` | 是 | 描述标签，用于语义检索 |
| `content` | `str` | 是 | 详细描述文本 |
| `source` | `str` | 自动 | 固定为 `"plugin"` |
| `_hash` | `str` | 自动 | 内容哈希，传入空字符串即可 |

### 8.4 示例

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

### 8.5 图片检索使用

注册图片后，AI 可以通过 RAG API 进行语义检索：

```python
from gsuid_core.ai_core.rag import search_and_load_image

# 在插件命令处理中使用
async def show_character_image(bot, ev):
    image = await search_and_load_image("给我看看胡桃的图片")
    if image:
        await bot.send(image)
```

---

## 9. 内置工具一览

所有内置工具均已注册到全局工具注册表，可直接在插件中使用或让 AI 自动调用。

### 9.1 Self 工具（category="self"）

只有主Agent能调用，用于核心自我操作。

#### query_user_favorability - 查询用户好感度

```python
@ai_tools(category="self")
async def query_user_favorability(
    ctx: RunContext[ToolContext],
    user_id: Optional[str] = None,  # 用户ID，None时查询当前用户
) -> str
```

#### update_user_favorability - 更新用户好感度（增量）

```python
@ai_tools(category="self")
async def update_user_favorability(
    ctx: RunContext[ToolContext],
    delta: int,                     # 好感度变化量（可为负数）
    user_id: Optional[str] = None,
) -> str
```

#### create_subagent - 创建子Agent

```python
@ai_tools(category="self")
async def create_subagent(
    ctx: RunContext[ToolContext],
    task: str,                      # 任务描述，请详细说明
    max_tokens: int = 10000,        # 子Agent最大输出 token 数
    max_iterations: int = 15,       # 子Agent最大迭代次数
) -> str
```

**工作流程**：
1. 根据 `task` 向量检索最匹配的工具
2. 使用内置的 Plan-and-Solve System Prompt 创建临时子 Agent
3. 子 Agent 执行任务并返回结果

#### send_message_by_ai - 主动发送消息

```python
@ai_tools(category="self")
async def send_message_by_ai(
    ctx: RunContext[ToolContext],
    message_type: Literal["text", "image"],  # 消息类型
    text: Optional[str] = None,              # 文本内容
    image_id: Optional[str] = None,         # 图片资源ID
    user_id: Optional[str] = None,          # 指定目标用户ID（可选）
) -> str
```

#### add_once_task - 添加一次性定时任务

```python
@ai_tools(category="self")
async def add_once_task(
    ctx: RunContext[ToolContext],
    run_time: str,               # 执行时间，格式 "YYYY-MM-DD HH:MM:SS"
    task_prompt: str,            # 任务描述
) -> str
```

#### add_interval_task - 添加循环任务

```python
@ai_tools(category="self")
async def add_interval_task(
    ctx: RunContext[ToolContext],
    interval_value: int,         # 间隔值
    task_prompt: str,            # 任务描述
    interval_type: str = "minutes",  # 间隔类型: "minutes"/"hours"/"days"
    max_executions: int = 10,    # 最大执行次数（上限10）
) -> str
```

---

### 9.1.1 定时任务管理工具（category="common"）

定时任务的"管理"类工具（列出/查询/修改/取消/暂停/恢复）**不属于保底池**，由 `search_tools()`
按用户 query 向量检索按需加载——用户使用这些功能时通常会显式带任务 ID 或明确表达"取消任务""暂停任务"
等需求，向量命中率高。而"创建"入口 `add_once_task` / `add_interval_task` 因口语化触发（"每天下午三点
半给我推送新闻"）向量难命中，故保留在 `self` 保底池。

#### list_scheduled_tasks - 列出所有定时任务

```python
@ai_tools(category="common")
async def list_scheduled_tasks(
    ctx: RunContext[ToolContext],
) -> str
```

#### query_scheduled_task - 查询任务详情

```python
@ai_tools(category="common")
async def query_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,                # 任务ID
) -> str
```

#### modify_scheduled_task - 修改任务

```python
@ai_tools(category="common")
async def modify_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,                # 任务ID
    task_prompt: Optional[str] = None,    # 新的任务描述
    max_executions: Optional[int] = None, # 新的最大执行次数（仅循环任务）
) -> str
```

#### cancel_scheduled_task - 取消任务

```python
@ai_tools(category="common")
async def cancel_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,                # 任务ID
) -> str
```

#### pause_scheduled_task - 暂停任务

```python
@ai_tools(category="common")
async def pause_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,                # 任务ID
) -> str
```

#### resume_scheduled_task - 恢复任务

```python
@ai_tools(category="common")
async def resume_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,                # 任务ID
) -> str
```

---

### 9.2 Buildin 工具（category="buildin"）—— 框架保底工具池

`buildin` 分类下的工具属于**框架保底工具池**，主Agent 无条件全部加载，不受向量搜索影响。

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

#### web_search_tool - Web 搜索

```python
@ai_tools(category="buildin")
async def web_search_tool(
    ctx: RunContext[ToolContext],
    query: str,          # 搜索关键词
    limit: int = 10,     # 最大结果数
) -> str
```

> **注意**：支持 Tavily / Exa / MCP 三种搜索提供方，通过 `ai_config.websearch_provider` 配置切换。使用 MCP 时需配置 `mcp_tools_config.websearch_mcp_tool_id`。

#### web_fetch_tool - 网页抓取

```python
@ai_tools(category="buildin")
async def web_fetch_tool(
    ctx: RunContext[ToolContext],
    url: str,            # 要抓取的网页 URL
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

#### get_self_info - 获取完整自我认知

```python
@ai_tools(category="buildin")
async def get_self_info(ctx: RunContext[ToolContext]) -> str
```

返回身份、运行框架、能力边界（按分类汇总的已注册工具）、主人列表、当前会话语境标签。
当用户问"你是谁""你能做什么""你的主人是谁"，或需要判断任务是否在能力范围内时调用。

#### state_get / state_set / state_delete / state_list / state_append - 通用持久状态存储

框架级别的跨会话键值存储，让复杂任务（虚拟账户、任务进度、报名名单等）的结构化状态在会话结束后依然存活。属于框架保底工具，任何 session 默认注入。

```python
@ai_tools(category="buildin")
async def state_set(
    ctx: RunContext[ToolContext],
    key: str,                       # 键名，建议格式 "插件名:业务名"
    value: str,                     # JSON 字符串
    scope: str = "auto",            # 隔离范围，见下表
    ttl_days: Optional[int] = None, # 可选，保留天数
) -> str

@ai_tools(category="buildin")
async def state_get(ctx, key: str, scope: str = "auto") -> str

@ai_tools(category="buildin")
async def state_delete(ctx, key: str, scope: str = "auto") -> str

@ai_tools(category="buildin")
async def state_list(ctx, prefix: str = "", scope: str = "auto") -> str

@ai_tools(category="buildin")
async def state_append(
    ctx, key: str, item: str, scope: str = "auto",
    max_length: Optional[int] = None, ttl_days: Optional[int] = None,
) -> str   # 向列表型状态追加元素，原子操作，避免 get→改→set 的竞态
```

`scope` 取值：

| scope | 说明 |
|-------|------|
| `"auto"` | 按当前会话自动判断：群聊 → `group:{群ID}`，私聊 → `user:{用户ID}` |
| `"user:{id}"` | 指定用户的私有数据 |
| `"group:{id}"` | 指定群组的共享数据 |
| `"global"` | 全局共享数据 |

> 插件可直接复用底层 API（`from gsuid_core.ai_core.state_store import state_get_value, state_set_value, ...`）来构建有状态功能，无需关心存储细节。

---

### 9.3 Common 工具（category="common"）

不属于保底工具池，通过向量检索按需加载，当用户明确需要相关功能时才会出现在工具列表中。

#### search_image - 图片检索

```python
@ai_tools(category="common")
async def search_image(
    ctx: RunContext[ToolContext],
    query: str,                      # 自然语言查询
    limit: int = 10,                # 最大返回数量
    score_threshold: float = 0.45,  # 相似度阈值（0~1）
) -> str
```

#### get_self_persona_info - 获取自身 Persona 资源信息

```python
@ai_tools(category="common")
async def get_self_persona_info(
    ctx: RunContext[ToolContext],
    info_type: Literal["config", "image", "avatar", "audio"],
    persona_name: str,              # Persona 名称
) -> str
```

**info_type 说明**：`"config"` 返回 config.json 配置；`"image"` / `"avatar"` / `"audio"` 返回对应资源文件路径。

> 完整的自我认知请使用保底工具 `get_self_info`；本工具仅用于获取具体的 Persona 资源文件路径。

#### set_user_favorability - 设置用户好感度（绝对值）

```python
@ai_tools(category="common")
async def set_user_favorability(
    ctx: RunContext[ToolContext],
    value: int,                     # 好感度绝对值
    user_id: Optional[str] = None,
) -> str
```

#### send_meme - 发送表情包

```python
@ai_tools(category="common")
async def send_meme(
    ctx: RunContext[ToolContext],
    mood: Optional[str] = None,    # 情绪标签
    scene: Optional[str] = None,   # 场景标签
) -> str
```

#### collect_meme - 收藏表情包

```python
@ai_tools(category="common")
async def collect_meme(
    ctx: RunContext[ToolContext],
    url: str,                      # 图片URL
    tags: Optional[str] = None,   # 自定义标签
) -> str
```

#### search_meme - 搜索表情包

```python
@ai_tools(category="common")
async def search_meme(
    ctx: RunContext[ToolContext],
    query: str,                    # 搜索关键词
) -> str
```

#### create_persistent_agent_tool - 创建持久化子Agent

```python
@ai_tools(category="common")
async def create_persistent_agent_tool(
    ctx: RunContext[ToolContext],
    name: str,                     # Agent 名称
    system_prompt: str,            # 系统提示词
    idle_timeout_minutes: int = 60, # 空闲超时时间（分钟）
) -> str
```

#### send_agent_task_tool - 向持久化Agent发送任务

```python
@ai_tools(category="common")
async def send_agent_task_tool(
    ctx: RunContext[ToolContext],
    agent_name: str,               # Agent 名称
    task: str,                     # 任务描述
) -> str
```

#### list_agents_tool - 列出所有活跃的持久化Agent

```python
@ai_tools(category="common")
async def list_agents_tool(
    ctx: RunContext[ToolContext],
) -> str
```

#### stop_agent_tool - 停止指定的持久化Agent

```python
@ai_tools(category="common")
async def stop_agent_tool(
    ctx: RunContext[ToolContext],
    agent_name: str,               # Agent 名称
) -> str
```

---

### 9.4 Media 工具（category="media"）

多媒体渲染工具，主Agent可调用。

#### render_html_to_image - 将HTML渲染为图片

```python
@ai_tools(category="media")
async def render_html_to_image(
    ctx: RunContext[ToolContext],
    html: str,                     # HTML 内容
    width: int = 800,             # 渲染宽度
) -> str
```

#### render_markdown_to_image - 将Markdown渲染为图片

```python
@ai_tools(category="media")
async def render_markdown_to_image(
    ctx: RunContext[ToolContext],
    markdown: str,                 # Markdown 内容
    width: int = 800,             # 渲染宽度
) -> str
```

---

### 9.5 Default 工具（category="default"）

通过 `create_subagent` 调用，用于文件操作、代码执行等。

#### execute_shell_command - 执行系统命令

```python
@ai_tools(category="default")
async def execute_shell_command(
    ctx: RunContext[ToolContext],
    command: str,                   # 要执行的命令
    timeout: int = 30,              # 超时时间（秒）
) -> str
```

> ⚠️ **安全警告**：此工具执行系统命令，需要通过 `check_func` 严格控制权限，建议仅在沙箱环境中开放。

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
@ai_tools(category="default")
async def read_file_content(
    ctx: RunContext[ToolContext],
    file_path: str,  # 相对于 FILE_PATH 的路径，如 "data/config.json"
) -> str
```

> **安全**：有路径遍历攻击防护，只能读取 `FILE_PATH` 目录下的文件

#### write_file_content - 写入文件

```python
@ai_tools(category="default")
async def write_file_content(
    ctx: RunContext[ToolContext],
    file_path: str,         # 相对于 FILE_PATH 的路径
    content: str,           # 要写入的内容
    overwrite: bool = True, # 是否覆盖已存在的文件
) -> str
```

#### execute_file - 执行脚本

```python
@ai_tools(category="default")
async def execute_file(
    ctx: RunContext[ToolContext],
    file_path: str,     # 相对于 FILE_PATH 的脚本路径
    timeout: int = 30,  # 超时时间（秒）
) -> str
```

#### diff_file_content - 文件对比

```python
@ai_tools(category="default")
async def diff_file_content(
    ctx: RunContext[ToolContext],
    file_path_a: str,  # 第一个文件路径
    file_path_b: str,  # 第二个文件路径
) -> str
```

#### list_directory - 列出目录

```python
@ai_tools(category="default")
async def list_directory(
    ctx: RunContext[ToolContext],
    dir_path: str = "",        # 相对于 FILE_PATH 的目录路径，空字符串表示根目录
    recursive: bool = False,   # 是否递归列出子目录
) -> str
```

---

### 9.6 动态工具发现（未注册为 AI 工具）

> **注意**：以下两个函数的 `@ai_tools` 装饰器已被注释掉，**不会自动注册为 AI 工具**。它们仅作为可手动调用的辅助函数存在。

#### discover_tools - 发现可能需要的新工具

```python
async def discover_tools(
    ctx: RunContext[ToolContext],
    task: str,               # 任务描述
    limit: int = 5,          # 最大返回工具数量
) -> str
```

#### list_available_tools - 列出可用工具

```python
async def list_available_tools(
    ctx: RunContext[ToolContext],
    category: Optional[str] = None,  # 可选，按分类筛选
) -> str
```

---

## 10. System Prompt 管理

System Prompt 模块提供系统提示词的 CRUD 管理和向量检索功能，主要供 `create_subagent` 使用。

### 10.1 模块导入

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

### 10.2 数据模型

```python
class SystemPrompt(TypedDict):
    id: str            # 唯一标识，推荐格式: "plugin-name-purpose"
    title: str         # 标题，如"代码专家"
    desc: str          # 描述，用于向量检索匹配
    content: str       # 完整系统提示词内容（作为 system_prompt 传给 AI）
    tags: List[str]    # 标签列表，支持标签过滤检索
```

### 10.3 存储位置

- JSON 文件：`AI_CORE_PATH / "system_prompts.json"`
- 向量库 Collection：`system_prompts`

### 10.4 CRUD 操作

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

### 10.5 向量检索

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

---

## 11. Persona 角色系统

Persona 模块提供人格角色的提示词管理和资料存储功能。

### 10.1 模块导入

```python
from gsuid_core.ai_core.persona import (
    Persona,
    PersonaMetadata,
    PersonaFiles,
    build_persona_prompt,
    load_persona,
    save_persona,
    list_available_personas,
    get_persona_metadata,
    get_persona_image_path,
    get_persona_avatar_path,
    get_persona_audio_path,
    persona_config_manager,
)
```

### 10.2 核心类

```python
class Persona(TypedDict):
    id: str              # 唯一标识
    name: str            # 角色名称
    description: str     # 角色描述
    image_path: str      # 立绘图片路径
    avatar_path: str     # 头像图片路径
    audio_path: str      # 音频文件路径
    config_path: str     # 配置文件路径
    introduction: str   # 角色介绍（长文本）
```

### 10.3 构建 Persona 提示词

```python
from gsuid_core.ai_core.persona import build_persona_prompt

# 构建完整的 persona 提示词
prompt = await build_persona_prompt(
    persona_name="my_persona",
    user_name="用户",
    context="当前对话上下文"
)
```

### 10.4 Persona 资源管理

```python
from gsuid_core.ai_core.persona import (
    list_available_personas,
    get_persona_metadata,
    get_persona_image_path,
    get_persona_avatar_path,
    get_persona_audio_path,
)

# 列出所有可用 Persona
personas = list_available_personas()

# 获取 Persona 元数据
metadata = get_persona_metadata("my_persona")

# 获取各种资源路径
image_path = get_persona_image_path("my_persona")
avatar_path = get_persona_avatar_path("my_persona")
audio_path = get_persona_audio_path("my_persona")
```

---

## 12. Memory 记忆系统

基于 Mnemis 双路检索思想的多群组/多用户 Agent 记忆系统。

### 11.1 模块导入

```python
from gsuid_core.ai_core.memory import (
    memory_config,
    ScopeType,
    make_scope_key,
    observe,
    get_observation_queue,
    ObservationRecord,
    dual_route_retrieve,
    MemoryContext,
    get_ingestion_worker,
)
```

### 11.2 记忆检索

```python
# 双路检索获取记忆上下文
mem_ctx = await dual_route_retrieve(
    query="用户之前提到的游戏偏好",
    group_id="群组ID",
    user_id="用户ID",
    top_k=5,
    enable_system2=True,
    enable_user_global=True,
)

# 转换为提示词文本
memory_text = mem_ctx.to_prompt_text(max_chars=2000)
```

### 11.3 记忆配置

```python
from gsuid_core.ai_core.memory import memory_config

# 记忆系统配置
memory_config.enable_retrieval    # 是否启用检索
memory_config.enable_system2      # 是否启用 System-2 检索
memory_config.enable_user_global_memory  # 是否启用用户全局记忆
memory_config.retrieval_top_k     # 检索返回数量
```

### 11.4 消息观察

```python
from gsuid_core.ai_core.memory import observe, ObservationRecord

# 观察消息并记录到记忆
observe(
    group_id="群组ID",
    user_id="用户ID",
    content="用户说想养一只猫",
    message_type="text"
)

# 获取观察队列
queue = get_observation_queue()
```

---

## 13. Scheduled Task 定时任务

定时任务系统支持一次性任务和循环任务。

### 13.1 模块导入

定时任务工具位于 `buildin_tools/scheduler.py`，数据库模型位于 `scheduled_task/models.py`：

```python
# 定时任务工具（通过 buildin_tools 导入）
from gsuid_core.ai_core.buildin_tools import (
    add_once_task,
    add_interval_task,
    list_scheduled_tasks,
    query_scheduled_task,
    modify_scheduled_task,
    cancel_scheduled_task,
    pause_scheduled_task,
    resume_scheduled_task,
)

# 数据库模型
from gsuid_core.ai_core.scheduled_task import AIScheduledTask
```

### 13.2 数据模型

```python
class AIScheduledTask(SQLModel, table=True):
    task_id: str                    # 任务ID
    bot_id: str                     # Bot ID
    user_id: str                    # 用户ID
    group_id: Optional[str]         # 群组ID
    task_type: str                  # "once" 或 "interval"
    task_prompt: str                # 任务描述
    trigger_time: Optional[datetime]  # 一次性任务的触发时间
    interval_seconds: Optional[int]   # 循环任务的间隔秒数
    max_executions: Optional[int]     # 最大执行次数
    current_executions: Optional[int] # 已执行次数
    status: str                     # "pending", "paused", "executed", "failed", "cancelled"
    persona_name: Optional[str]     # Persona 名称
    session_id: Optional[str]       # Session ID
    next_run_time: Optional[datetime] # 下次执行时间
    result: Optional[str]           # 上次执行结果
    error_message: Optional[str]    # 错误信息
```

---

## 14. 工具注册表查询 API

```python
from gsuid_core.ai_core.register import get_registered_tools, get_all_tools

# 获取按分类组织的工具字典
# 返回: Dict[str, Dict[str, ToolBase]]
all_by_category = get_registered_tools()
# {
#   "self": {"query_user_favorability": ToolBase(...), ...},
#   "buildin": {"search_knowledge": ToolBase(...), ...},
#   "buildin": {"get_self_persona_info": ToolBase(...), ...},
#   "default": {"execute_shell_command": ToolBase(...), ...},
# }

# 查看某分类的工具
self_tools = all_by_category.get("self", {})
for name, tool_base in self_tools.items():
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

## 15. 类型定义参考

### 14.1 ToolContext

```python
@dataclass
class ToolContext:
    """工具执行上下文"""
    bot: Optional[Bot] = None   # Bot 实例，用于发送消息
    ev: Optional[Event] = None  # 事件实例，包含用户ID、群组ID等
    extra: Dict[str, Any] = field(default_factory=dict)  # 工具间临时传递状态
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

### 14.2 KnowledgeBase

```python
class KnowledgeBase(TypedDict):
    id: str
    plugin: str
    title: str
    content: str
    tags: List[str]
    source: str  # "plugin" 或 "manual"
```

### 14.3 KnowledgePoint

```python
class KnowledgePoint(KnowledgeBase):
    _hash: str  # 自动计算的内容哈希
```

### 14.4 ManualKnowledgeBase

```python
class ManualKnowledgeBase(TypedDict):
    id: str
    plugin: str
    title: str
    content: str
    tags: List[str]
    source: str  # 固定为 "manual"
```

### 14.5 ImageEntity

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

### 14.6 ToolBase

```python
class ToolBase:
    name: str                    # 工具名
    description: str             # 工具描述
    plugin: str                  # 所属插件（"core" 或插件名）
    tool: Tool[ToolContext]       # PydanticAI Tool 对象
```

### 14.7 CheckFunc 类型

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

## 16. 完整示例

### 15.1 示例一：基础工具注册

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
@ai_tools(category="common", check_func=check_bound)
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
@ai_tools(category="common", check_func=check_admin)
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

### 15.2 示例二：创建临时 Agent 做专项任务

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

### 15.3 示例三：完整插件入口文件

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

### 15.4 示例四：注册并使用自定义 System Prompt

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

## 17. MCP 工具集成

### 17.1 概述

MCP (Model Context Protocol) 模块允许通过 MCP 协议集成外部工具服务器。用户可通过 WebConsole API 添加 MCP 服务器配置，框架启动时自动连接服务器并将工具注册为 AI 工具。

### 17.2 模块导入

```python
from gsuid_core.ai_core.mcp import (
    MCPClient,               # MCP 客户端
    MCPConfig,               # MCP 配置数据类
    MCPToolDefinition,       # MCP 工具定义
    mcp_config_manager,      # 全局配置管理器单例
    parse_mcp_tool_id,       # 解析 "{mcp_id} - {tool_name}" 格式
    format_mcp_tool_id,      # 格式化 MCP 工具 ID
    register_all_mcp_tools,  # 注册所有 MCP 工具
    register_single_mcp_server,  # 注册单个 MCP 服务器
    unregister_mcp_server,   # 注销 MCP 服务器
)

from gsuid_core.ai_core.mcp.mcp_tool_caller import call_mcp_tool
from gsuid_core.ai_core.mcp.mcp_tools_config import mcp_tools_config
```

### 17.3 MCPConfig 数据类

```python
@dataclass
class MCPConfig:
    name: str                                    # 服务器名称
    command: str                                 # 启动命令（如 "uvx"）
    args: list[str] = field(default_factory=list)  # 命令参数
    env: dict[str, str] = field(default_factory=dict)  # 环境变量
    enabled: bool = True                         # 是否启用
    register_as_ai_tools: bool = False           # 是否注册为 AI Tools
    tools: list[MCPToolDefinition] = field(default_factory=list)  # 工具列表
```

### 17.4 MCP 工具 ID 格式

MCP 工具 ID 格式为 `{mcp_id} - {tool_name}`，例如 `minimax - web_search`。

```python
from gsuid_core.ai_core.mcp import parse_mcp_tool_id, format_mcp_tool_id

mcp_id, tool_name = parse_mcp_tool_id("minimax - web_search")
# mcp_id = "minimax", tool_name = "web_search"

tool_id = format_mcp_tool_id("minimax", "web_search")
# "minimax - web_search"
```

### 17.5 通用 MCP 工具调用

无需将工具注册为 AI Tools，直接通过 `call_mcp_tool()` 调用：

```python
from gsuid_core.ai_core.mcp.mcp_tool_caller import call_mcp_tool

result = await call_mcp_tool(
    mcp_tool_id="minimax - web_search",
    arguments={"query": "Python 教程"},
)
print(result.text)  # 工具返回的文本结果
```

### 17.6 配置管理

```python
from gsuid_core.ai_core.mcp import mcp_config_manager

# 列出所有配置
configs = mcp_config_manager.list_configs()

# 获取启用的配置
enabled = mcp_config_manager.get_enabled_configs()

# 获取指定配置
config = mcp_config_manager.get_config("minimax")

# 创建配置
from gsuid_core.ai_core.mcp import MCPConfig
success, msg = mcp_config_manager.create_config("my_server", MCPConfig(
    name="MyServer",
    command="uvx",
    args=["my-mcp-server"],
    env={"API_KEY": "xxx"},
))

# 列出所有工具
tools = mcp_config_manager.list_all_tools()
```

---

## 18. Image Understand 图片理解

### 18.1 概述

提供统一的图片理解接口，将图片内容转述为文本描述。当 LLM 模型不支持图片输入时，`GsCoreAIAgent._prepare_user_message()` 会自动调用此模块。

### 18.2 核心函数

```python
from gsuid_core.ai_core.image_understand import understand_image

async def understand_image(
    image_url: str,           # 图片来源（HTTP URL 或 base64 DataURI）
    prompt: str | None = None,  # 对图片的提问，默认为通用描述
) -> str:                     # 图片内容的文本描述
```

### 18.3 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `image_understand_provider` | str | `"MCP"` | 图片理解服务提供方（目前仅支持 MCP） |
| `mcp_tools_config.image_understand_mcp_tool_id` | str | `""` | MCP 工具 ID，格式 `"{mcp_id} - {tool_name}"` |

### 18.4 使用示例

```python
from gsuid_core.ai_core.image_understand import understand_image

# 通用描述
description = await understand_image("https://example.com/image.png")

# 自定义提问
answer = await understand_image(
    "https://example.com/chart.png",
    prompt="这张图表显示了什么数据趋势？",
)
```

---

## 19. Web Search 统一搜索

### 19.1 概述

提供统一的 Web 搜索接口，根据用户配置自动选择搜索引擎（Tavily / Exa / MCP）。

### 19.2 核心函数

```python
from gsuid_core.ai_core.web_search.search import web_search, web_search_with_context

async def web_search(
    query: str,
    max_results: int | None = None,
) -> list[dict]

async def web_search_with_context(
    query: str,
    max_results: int = 5,
) -> dict  # {"results": [...], "answer": "..."}
```

### 19.3 配置

| 配置项 | 类型 | 默认值 | 选项 | 说明 |
|--------|------|--------|------|------|
| `websearch_provider` | str | `"Tavily"` | `Tavily` / `Exa` / `MCP` | Web 搜索服务提供方 |
| `mcp_tools_config.websearch_mcp_tool_id` | str | `""` | — | MCP 搜索工具 ID（provider=MCP 时必填） |

### 19.4 使用示例

```python
from gsuid_core.ai_core.web_search.search import web_search

results = await web_search("Python 教程", max_results=5)
for r in results:
    print(r["title"], r["url"], r["content"])
```

---

## 20. Meme 表情包模块

### 20.1 概述

让 AI 具备「表情包意识」：自动采集群聊图片、智能打标、分类存储、智能发送。

> **详细设计文档**: 见 [MEME_MODULE.md](./MEME_MODULE.md)

### 20.2 AI 工具

| 工具 | category | 说明 |
|------|----------|------|
| `send_meme` | `buildin` | 根据情绪/场景智能选取并发送表情包 |
| `collect_meme` | `buildin` | 手动收集表情包入库 |
| `search_meme` | `buildin` | 搜索表情包库 |

### 20.3 集成点

- `handle_ai.py` 中通过 `asyncio.create_task(observe_message_for_memes(event, ""))` 异步采集群聊图片
- `handle_ai.py` 中导入 `meme.startup` 和 `meme_tools` 以注册 `@on_core_start` 钩子和 `@ai_tools`

---

## 附录：常见问题

### Q1: 工具注册后 AI 能直接使用吗？

取决于 `category`：
- `category="self"`, `"buildin"`, `"common"`：主Agent直接可用
- `category="default"` 或其他：需通过 `create_subagent` 在子Agent中使用

### Q2: 如何让插件工具被 AI 主Agent直接调用？

将 `category` 设置为 `"common"`，但要谨慎——主Agent的工具越多，token 消耗越大。推荐将高频核心工具注册为 `"common"`，其他通过子Agent完成。

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

### Q6: RAG 知识库检索的工作方式是什么？

RAG 知识库检索不再作为强制前置流程。`search_knowledge` 工具注册为 `buildin` 分类，主Agent会根据对话内容自主决定是否调用该工具进行知识库检索。

---

## 相关文档

- [AI 触发流转文档](./AI_TRIGGER_FLOW.md)
- [WebConsole API 文档](../gsuid_core/webconsole/API.md)
