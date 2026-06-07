# AI 触发流转图文档

## 目录
1. [系统概述](#1-系统概述)
2. [核心入口](#2-核心入口)
3. [触发模式详解](#3-触发模式详解)
4. [Persona 配置系统](#4-persona-配置系统)
5. [AI 路由与 Session 管理](#5-ai-路由与-session-管理)
   - [5.5 工具注册系统与 Agent 架构](#55-工具注册系统与-agent-架构)
   - [5.6 设计缺陷与潜在问题](#56-设计缺陷与潜在问题)
6. [Heartbeat 定时巡检机制](#6-heartbeat-定时巡检机制)
   - [6.7 设计缺陷与潜在问题](#67-设计缺陷与潜在问题)
      - [6.7.1 定时巡检会引发"LLM Token 破产"与并发雪崩](#671-定时巡检会引发llm-token-破产与并发雪崩-性能漏洞-✅-已修复)
      - [6.7.2 _Bot 与 Bot 混淆导致 bot_self_id 缺失](#672-bot-与-bot-混淆导致-bot_self_id-缺失-致命错误-✅-已修复)
7. [Scheduled Task 定时任务系统](#7-scheduled-task-定时任务系统)
   - [7.1 概述](#71-概述)
   - [7.2 模块结构](#72-模块结构)
   - [7.3 任务类型](#73-任务类型)
      - [7.3.1 一次性任务 (once)](#731-一次性任务-once)
      - [7.3.2 循环任务 (interval)](#732-循环任务-interval)
   - [7.4 核心组件](#74-核心组件)
      - [7.4.1 数据库模型 - `AIScheduledTask`](#741-数据库模型---aischeduledtask)
      - [7.4.2 工具函数 - `manage_scheduled_task`](#742-工具函数---manage_scheduled_task)
      - [7.4.3 执行器 - `execute_scheduled_task`](#743-执行器---execute_scheduled_task)
   - [7.5 安全限制](#75-安全限制)
   - [7.6 架构设计](#76-架构设计)
   - [7.7 使用流程](#77-使用流程)
   - [7.8 任务状态机](#78-任务状态机)
   - [7.9 重启恢复](#79-重启恢复)
   - [7.10 触发方式统计](#710-触发方式统计)
   - [7.11 启用方式](#711-启用方式)
   - [7.12 WebConsole API](#712-webconsole-api)
8. [WebConsole API 与配置热重载](#8-webconsole-api-与配置热重载)
9. [AI Statistics 统计系统](#9-ai-statistics-统计系统)
10. [Memory 记忆系统](#10-memory-记忆系统)
    - [10.14 Meme 表情包模块](#1014-meme-表情包模块)
    - [10.15 Image Understand 图片理解模块](#1015-image-understand-图片理解模块)
    - [10.16 Web Search 统一搜索接口](#1016-web-search-统一搜索接口)
    - [10.1 概述](#101-概述)
    - [10.2 模块结构](#102-模块结构)
    - [10.3 核心架构](#103-核心架构)
    - [10.4 Scope Key 隔离体系](#104-scope-key-隔离体系)
    - [10.5 Observer 观察者管道](#105-observer-观察者管道)
    - [10.6 Ingestion 摄入引擎](#106-ingestion-摄入引擎)
    - [10.7 双路检索引擎](#107-双路检索引擎)
    - [10.8 分层语义图](#108-分层语义图)
    - [10.9 数据库模型](#109-数据库模型)
    - [10.10 向量存储](#1010-向量存储)
    - [10.11 配置项](#1011-配置项)
    - [10.12 与现有模块的集成](#1012-与现有模块的集成)
    - [10.13 记忆统计](#1013-记忆统计)
11. [嵌入模型提供方抽象层](#11-嵌入模型提供方抽象层)
12. [完整流程图](#12-完整流程图)
13. [附录](#附录)
   - [D. 已知问题汇总](#d-已知问题汇总)

---

## 1. 系统概述

### 1.1 AI Core 模块结构

```
gsuid_core/ai_core/
├── __init__.py          # 核心初始化入口
├── ai_router.py         # Session 路由管理
├── check_func.py        # 检查函数
├── gs_agent.py          # AI Agent 实现（含 _prepare_user_message 图片处理）
├── handle_ai.py         # AI 聊天处理入口
├── models.py            # 数据模型
├── normalize.py         # 查询规范化 (已移至子模块)
├── register.py          # 工具注册
├── resource.py          # 资源管理 (含 MCP_CONFIGS_PATH)
├── trigger_bridge.py    # 触发器→AI工具桥接 (MockBot/ai_return)
├── utils.py             # 工具函数（含 prepare_content_payload、send_chat_result）
├── configs/             # 配置文件
│   ├── __init__.py
│   ├── ai_config.py     # AI 全局配置
│   └── models.py        # 配置数据模型
├── buildin_tools/       # 内建 AI 工具
│   ├── __init__.py
│   ├── command_executor.py  # 执行系统命令
│   ├── database_query.py    # 数据库查询（好感度/记忆）
│   ├── dynamic_tool_discovery.py  # 动态工具发现
│   ├── favorability_manager.py  # 好感度管理（增量/绝对值）
│   ├── file_manager.py      # 文件管理 (read/write/execute/diff/list)
│   ├── get_time.py          # 获取时间
│   ├── html_render_tools.py # HTML/Markdown 渲染为图片
│   ├── meme_tools.py        # 表情包工具（send/collect/search）
│   ├── message_sender.py    # 消息发送
│   ├── rag_search.py        # RAG 检索 (knowledge/image)
│   ├── scheduler.py         # 预约定时任务（独立工具函数）
│   ├── self_info.py         # 获取自身 Persona 信息
│   ├── subagent.py          # 创建子Agent
│   ├── web_search.py        # Web 搜索
│   └── web_fetch.py         # 网页抓取（转 Markdown）
├── planning/               # 长任务编排层（C5/C7）—— 取代已移除的 agent_mesh
│   ├── models.py            # AIAgentTask / AIAgentTaskStep / AIAgentTaskLog 三表
│   ├── manager.py           # 框架内部编排函数（不暴露为 LLM 工具）
│   ├── resolver.py          # 自然语言任务引用解析
│   ├── runtime.py           # contextvars 绑定 current_task
│   ├── executor.py          # 定时唤醒执行器 + 崩溃恢复 + 人格转译播报
│   ├── context.py           # 每轮注入活跃任务摘要
│   ├── tools.py             # 暴露给 LLM 的无 UUID 工具
│   └── startup.py           # 初始化与僵尸任务恢复
├── capability_agents/      # 能力代理层 —— 执行/表达分离（无人格专职执行体）
│   ├── registry.py          # CapabilityAgentProfile 注册表 + resolve_profile + unregister_capability_agent
│   ├── profiles.py          # 内置 research/code 能力代理画像
│   ├── persistence.py       # 用户自定义画像 JSON 持久化 + source 三态（builtin/plugin/user）
│   └── runner.py            # 能力代理运行器（无人格 Plan-Solve）
├── multimodal/             # 多模态消息处理模块
│   ├── __init__.py          # 模块导出
│   ├── asr.py               # 语音转文字（ASR）
│   ├── tts.py               # 文字转语音（TTS）
│   ├── video.py             # 视频关键帧提取 + 多帧理解
│   └── document.py          # 文档内容提取管道（PDF/Word/Excel → Markdown）
├── scheduled_task/       # 定时任务系统
│   ├── __init__.py
│   ├── models.py          # AIScheduledTask 数据模型
│   ├── executor.py        # 定时执行器
│   ├── scheduler.py       # APScheduler 任务注册辅助
│   ├── startup.py         # 启动/关闭回调
│   └── README.md          # 设计文档
├── classifier/           # 意图分类器
│   ├── __init__.py
│   └── mode_classifier.py
├── database/            # 数据库模型
│   ├── __init__.py
│   └── models.py
├── heartbeat/           # 定时巡检系统
│   ├── __init__.py
│   ├── inspector.py     # 巡检器核心
│   └── decision.py      # LLM 决策逻辑
├── history/              # 历史记录管理
│   ├── __init__.py
│   ├── manager.py
│   └── README.md
├── persona/              # Persona 角色系统
│   ├── __init__.py
│   ├── config.py        # Persona 配置管理
│   ├── models.py        # 数据模型
│   ├── persona.py       # Persona 类
│   ├── processor.py     # Prompt 构建
│   ├── prompts.py       # 提示词模板
│   ├── resource.py      # 资源管理
│   ├── startup.py       # 初始化
│   └── README.md
├── rag/                  # RAG 知识库
│   ├── __init__.py
│   ├── base.py
│   ├── embedding.py      # 嵌入模型提供方抽象层（local/openai）
│   ├── image_rag.py
│   ├── knowledge.py
│   ├── reranker.py
│   ├── startup.py
│   └── tools.py
├── memory/               # 记忆系统（Mnemis 双路检索）
│   ├── __init__.py       # 模块导出
│   ├── config.py         # 记忆系统全局配置
│   ├── scope.py          # Scope Key 隔离体系
│   ├── observer.py       # 观察者管道
│   ├── startup.py        # 初始化入口
│   ├── database/         # 图结构存储（SQLAlchemy）
│   │   ├── __init__.py   # Session 工厂
│   │   └── models.py     # MemEpisode/Entity/Edge/Category 模型
│   ├── ingestion/        # 摄入引擎
│   │   ├── __init__.py
│   │   ├── worker.py     # IngestionWorker 后台消费
│   │   ├── entity.py     # Entity 去重与写入
│   │   ├── edge.py       # Edge 写入与冲突检测
│   │   └── hiergraph.py  # 分层语义图构建
│   ├── retrieval/        # 检索引擎
│   │   ├── __init__.py
│   │   ├── system1.py    # System-1 向量相似度检索
│   │   ├── system2.py    # System-2 分层图遍历
│   │   └── dual_route.py # 双路合并 + Reranker
│   ├── vector/           # 向量存储（Qdrant）
│   │   ├── __init__.py
│   │   ├── collections.py # Collection 名称常量
│   │   ├── startup.py    # Collection 初始化
│   │   └── ops.py        # 向量写入/读取操作
│   └── prompts/          # LLM 提示词模板
│       ├── __init__.py
│       ├── extraction.py  # Entity/Edge 提取
│       ├── categorization.py # Category 分类
│       ├── selection.py   # 节点选择
│       └── summary.py    # 摘要生成
├── skills/               # Skills 技能系统
│   ├── __init__.py
│   ├── operations.py
│   └── resource.py
├── statistics/           # AI 统计系统
│   ├── __init__.py
│   ├── manager.py       # 统计管理器
│   ├── models.py        # 数据库模型
│   ├── dataclass_models.py  # 内存数据结构（BotState/LatencyStats/TokenUsage）
│   └── startup.py       # 启动/关闭/零点重置回调
├── mcp/                  # MCP (Model Context Protocol) 工具集成
│   ├── __init__.py       # 模块导出（MCPClient, MCPConfig, mcp_config_manager 等）
│   ├── client.py         # MCP 客户端（基于 fastmcp，stdio 传输）
│   ├── config_manager.py # MCP 配置管理器（增删改查 + MCPToolDefinition）
│   ├── mcp_tool_caller.py # 通用 MCP 工具调用模块（call_mcp_tool）
│   ├── mcp_tools_config.py # MCP 工具配置（websearch/image_understand 的 MCP 工具 ID）
│   ├── mcp_presets.py    # MCP 预设配置（MiniMax、Firecrawl 等）
│   ├── server.py         # MCP Server（将 to_ai 触发器对外暴露为 MCP 服务）
│   └── startup.py        # 启动时自动注册 MCP 工具 + 热重载
├── meme/                 # 表情包模块
│   ├── config.py         # 配置项（StringConfig）
│   ├── database_model.py # AiMemeRecord SQLModel 表
│   ├── filter.py         # 去重 + 质量过滤
│   ├── library.py        # 文件 + DB + Qdrant 操作
│   ├── observer.py       # 消息流监听
│   ├── selector.py       # 检索 + 决策
│   ├── startup.py        # @on_core_start 钩子
│   └── tagger.py         # VLM 打标引擎
├── image_understand/     # 图片理解模块
│   ├── __init__.py       # 模块导出（understand_image）
│   └── understand.py     # 统一图片理解接口（MCP 驱动）
└── web_search/           # Web 搜索
    ├── __init__.py
    └── search.py         # 统一搜索接口（Tavily/Exa/MCP 三选一）
```

### 1.2 核心组件关系

```
┌─────────────────────────────────────────────────────────────────┐
│                         handler.py                               │
│                    (事件处理入口函数)                              │
│                    handle_event()                                 │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    消息事件处理流程                                │
│  1. msg_process() - 解析消息                                     │
│  2. 黑名单/白名单检查                                             │
│  3. 命令前缀匹配                                                  │
│  4. 触发器匹配 (SL.lst)                                          │
└─────────┬─────────────────────────┬───────────────────────────┘
          │ 有命令匹配               │ 无命令匹配
          ▼                         ▼
┌─────────────────────┐     ┌─────────────────────────────────────┐
│   执行命令触发器      │     │          AI 处理流程                 │
│   (trigger.func)    │     │  handle_ai_chat()                   │
└─────────────────────┘     └──────────────────┬──────────────────┘
                                               │
                          ┌────────────────────┴────────────────────┐
                          ▼                                         ▼
          ┌───────────────────────────┐           ┌───────────────────────────┐
          │     提及应答模式           │           │     定时巡检模式           │
          │  (HeartbeatInspector)     │           │                           │
          └───────────────────────────┘           └───────────────────────────┘
```

---

## 2. 核心入口

### 2.1 handler.py - 事件处理入口

**文件位置**: [`gsuid_core/handler.py`](gsuid_core/handler.py)

**主入口函数**: `handle_event(ws, msg, is_http=False)`

```python
async def handle_event(ws: _Bot, msg: MessageReceive, is_http: bool = False):
    # 核心处理流程
```

**处理流程**:

```
1. IS_HANDDLE 全局开关检查 (第 66-68 行)
   └── if not IS_HANDDLE: return

2. 黑名单/屏蔽列表检查 (第 70-73 行)
   ├── black_list: 插件黑名单
   ├── shield_list: 屏蔽的机器人列表
   └── same_user_cd: 相同用户事件冷却

3. 消息解析 msg_process() (第 77 行)
   └── 返回 Event 对象

4. 用户消息记录到历史 (第 82-119 行)
   └── history_manager.add_message()

5. 主人识别 (第 121-131 行)
   └── 如果 user_pm == 0 且未订阅，自动订阅"主人用户"

6. 用户/群组数据库记录 (第 143-154 行)
   ├── CoreUser.insert_user()
   └── CoreGroup.insert_group()

7. Session ID 生成 (第 156-164 行)
   └── session_id = f"{bot_id}%%%{temp_gid}%%%{uid}"

8. 重复消息检查 (第 170-183 行)
   ├── instances 检查 (单实例)
   └── mutiply_instances 检查 (多实例)

9. 相同消息冷却检查 (第 186-191 行)
   └── cooldown_tracker.is_on_cooldown()

10. 命令前缀处理 (第 193-201 行)
    └── 移除 command_start 前缀

11. 触发器匹配检查 (第 203-253 行)
    └── _check_command() 检查所有 SL.lst 中的触发器

12. 命令执行 (第 255-297 行)
    └── 如果有匹配的触发器，执行 trigger.func()

13. AI 处理 (第 298-357 行)
    └── 如果没有命令匹配，进入 AI 处理流程
```

### 2.2 AI 触发条件 (handler.py: 432-502)

```python
# 检查顺序
1. enable_ai 全局开关检查（运行时动态读取）
   └── ai_config.get_config("enable").data

2. 黑白名单检查
   ├── user_in_black_list = event.user_id in ai_black_list
   ├── group_in_black_list = event.group_id in ai_black_list
   ├── user_in_white_list = event.user_id in ai_white_list
   └── group_in_white_list = event.group_id in ai_white_list

3. Persona 配置检查
   ├── session_id = event.session_id
   └── persona_name = persona_config_manager.get_persona_for_session(session_id)

4. AI Mode 检查
   ├── "提及应答" in ai_mode: 检查 @机器人 或 关键词
   └── 其他模式...

5. 任务入队
   └── ws.queue.put_nowait(TaskContext(coro=handle_ai_chat(...)))
```

> **注意**: `enable_ai` 在 `handle_ai.py` 中改为**函数内动态读取**（`ai_config.get_config("enable").data`），
> 而非模块级常量。这确保用户在 WebConsole 中切换 AI 总开关后，**无需重启框架即可生效**。

---

### 2.3 双层长度防护机制（D-9、D-10 修复）

**问题**: 原代码对超大文本缺乏硬上限保护。恶意用户发送 10 万字文本时，系统会把原始文本直接塞给子Agent摘要，导致 OpenAI 单次输入超限或消耗数万 Token。

**修复方案**: 在 `handle_ai_chat()` 中引入**双层长度防护**：

```python
# handle_ai.py
ABSOLUTE_MAX_LENGTH = 60000  # 第一层：绝对上限，超过直接硬截断
MAX_SUMMARY_LENGTH = 15000    # 第二层：摘要阈值，超过则调用子Agent智能摘要

# 第一层：硬截断（防止子Agent Token爆炸）
if len(event.raw_text) > ABSOLUTE_MAX_LENGTH:
    query = query[:ABSOLUTE_MAX_LENGTH] + "...[文本过长，已自动截断]"
    event.raw_text = query  # 同步到 event

# 第二层：智能摘要（在安全范围内压缩长文本）
if len(event.raw_text) > MAX_SUMMARY_LENGTH:
    from gsuid_core.ai_core.buildin_tools.subagent import create_subagent
    summarized = await create_subagent(
        ctx=None,
        task=f"请总结以下用户输入，保留关键信息：\n\n{event.raw_text}",
        tags="摘要,总结",
        max_tokens=500,
    )
    user_messages = summarized
```

**防护层级说明**：

| 层级 | 触发条件 | 处理方式 | 目的 |
|------|---------|---------|------|
| 第一层 | `> 60000` 字符 | 硬截断至 60000 字符 + 截断提示 | 防止子Agent Token爆炸、API超限 |
| 第二层 | `> 15000` 字符 | 调用子Agent智能摘要 | 压缩长文本，保留关键信息 |
| 无需处理 | `≤ 15000` 字符 | 直接传递给主Agent | 正常短消息处理 |

> **说明**：第二层阈值从 2000 调整为 15000，因为现代 LLM 上下文窗口动辄 128K（约 10 万汉字），2000 字符对 LLM 来说毫无压力。对于代码、报错日志等长文本，摘要会丢失细节，应尽量避免自动摘要。
`n
### 2.5 AI 并发控制机制

**问题**: 原代码在用户触发路径（`handle_ai_chat`）没有并发控制，恶意用户可能瞬间发送大量请求导致 Rate Limit。

**修复方案**: 使用全局信号量限制并发 AI 调用数：

```python
# handler.py
MAX_CONCURRENT_AI_CALLS = 10  # 全局最大并发AI调用数
_ai_semaphore = asyncio.Semaphore(MAX_CONCURRENT_AI_CALLS)

# handle_ai.py
async def handle_ai_chat(bot: Bot, event: Event):
    from gsuid_core.handler import _ai_semaphore

    async with _ai_semaphore:
        try:
            # ... AI 处理逻辑
        except Exception as e:
            logger.exception(f"🧠 [GsCore][AI] 聊天异常: {e}")
```

**与 Heartbeat 的对比**：
| 模块 | 并发控制 | 信号量值 |
|------|----------|----------|
| Heartbeat | `Semaphore(5)` | 5 |
| handle_ai (用户触发) | `Semaphore(10)` | 10 |

**效果**: 全局最多同时有 10 个用户触发的 AI 调用，防止 Rate Limit。

### 2.6 RAG 知识库检索：由强制前置改为工具按需调用（D-11 修复）

**问题**: 原 `handle_ai_chat()` 在所有意图下强制执行 RAG 知识库检索，然后将结果拼入 `rag_context` 传给 LLM。

**问题场景**：用户只说了"你好啊"或"你真可爱"，系统依然：
1. 对"你好啊"向量化（Embedding 调用）
2. 去 Qdrant 检索知识库
3. 把不相关的检索结果塞入 `rag_context`
4. 发送给 LLM（多余 Token 消耗）

**带来的问题**：
- 每次 AI 响应都会额外增加 1~2 秒 RAG 检索延迟
- 不相关内容污染 LLM 上下文，影响回复质量
- 浪费无用 Token（输入费用）

**修复方案**：将 RAG 检索改为主 Agent 的 `buildin` 工具 `search_knowledge`，由 LLM 自主判断是否调用：

```python
# 旧设计（已移除）：
# if intent == "问答":
#     knowledge_results = await query_knowledge(query=normalized_query)
#     rag_context += "【参考资料】\n" + ...

# 新设计（handle_ai.py）：
# RAG 检索由主Agent的 search_knowledge 工具按需调用，handle_ai_chat 不再强制检索
# rag_context 只包含历史对话上下文
rag_context = f"【历史对话】\n{history_context}\n"

# 主Agent工具列表（gs_agent.py）：
# - search_knowledge: 当用户需要查询知识时，LLM 主动调用此工具
# - 用户问"你好"时，LLM 直接回复，不触发 RAG
```

**对比**：

| 场景 | 旧设计 | 新设计 |
|------|--------|--------|
| 用户问"你好" | 强制 RAG 检索（延迟+无意义消耗） | 直接回复，不触发 RAG |
| 用户问"配置方法" | 强制 RAG 检索（可能相关） | AI 自主调用 `search_knowledge` 工具 |
| 用户问"怎么绑定" | 强制 RAG 检索 | AI 自主决定是否查询知识库 |

**效果**：
- 闲聊消息响应延迟减少约 1~2 秒
- 消除无谓的 Embedding 调用和数据库查询
- LLM 根据对话上下文智能决定是否需要检索知识库

---

## 3. 触发模式详解

### 3.1 提及应答模式

**触发条件**:
- 用户 @机器人 (`event.is_tome = True`)
- 或 消息包含关键词 (`keywords` 配置)

**handler.py 中的判断逻辑** (第 336-345 行):

```python
if "提及应答" in ai_mode:
    should_respond = event.is_tome  # 检查是否@机器人
    if not should_respond and keywords:
        # 检查关键词
        msg_text = getattr(event, "raw_text", "") or ""
        should_respond = any(kw in msg_text for kw in keywords)

    if not should_respond:
        return  # 不触发 AI
```

**AI 处理流程** (`handle_ai.py`):

```
1. 双层长度防护
   ├── > 60000 字符: 硬截断（防子Agent Token爆炸）
   └── > 15000 字符: 调用 create_subagent 智能摘要

2. 意图识别
   └── classifier_service.predict_async(query)
       ├── "闲聊" - 闲聊模式
       ├── "工具" - 工具执行模式
       └── "问答" - 问答模式

3. 获取 AI Session
   └── session = await get_ai_session(event)

4. 记忆上下文检索（Memory Retrieval）
   └── dual_route_retrieve() - 双路检索相关记忆

5. 历史记录上下文
   ├── format_history_for_agent() - 格式化近 30 条历史
   └── rag_context = "【历史对话】\n{history_context}"
   注意：RAG 知识库检索不再是前置强制步骤，由主Agent通过 search_knowledge 工具按需调用

6. 调用 Agent 生成回复
   └── chat_result = await session.run(
           user_message=user_messages,
           bot=bot,
           ev=event,
           rag_context=full_context,  # 含历史记录 + 长期记忆
       )

7. 发送回复
   └── await send_chat_result(bot, chat_result)
       └── 支持 @用户ID 语法解析和打字延迟

8. 记忆观察（AI 回复后）
   └── observe() - 将 AI 回复入队记忆系统
```

### 3.2 定时巡检模式

**配置项**:
- `ai_mode` 包含 "定时巡检"
- `inspect_interval`: 巡检间隔 (5/10/15/30/60 分钟)

**详细流程见 [第 6 节](#6-heartbeat-定时巡检机制)

### 3.3 其他模式 (暂不可用)

- `趣向捕捉` - 暂不可用
- `困境救场` - 暂不可用

---

## 4. Persona 配置系统

### 4.1 配置文件位置

```
RESOURCE_PATH/
└── persona/
    └── {persona_name}/
        ├── config.json          # Persona 配置 (不含 introduction)
        ├── persona.md           # 角色设定 (Markdown 格式)
        ├── avatar.png           # 头像图片 (可选)
        ├── image.png            # 立绘图片 (可选)
        ├── audio.mp3            # 音频文件 (可选，优先级最高)
        ├── audio.ogg            # 音频文件 (可选)
        ├── audio.wav            # 音频文件 (可选)
        ├── audio.m4a            # 音频文件 (可选)
        └── audio.flac           # 音频文件 (可选)
```

**音频格式优先级**：mp3 > ogg > wav > m4a > flac

### 4.2 配置项定义 (`persona/config.py`)

**DEFAULT_PERSONA_CONFIG**:

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `ai_mode` | List[str] | `["提及应答"]` | AI行动模式 |
| `scope` | str | `"disabled"` | 启用范围 |
| `target_groups` | List[str] | `[]` | 目标群聊列表 |
| `inspect_interval` | int | `30` | 巡检间隔(分钟) |
| `keywords` | List[str] | `[]` | 唤醒关键词 |

**ai_mode 选项**:
- `提及应答` - @机器人或关键词触发
- `定时巡检` - 定时主动发言
- `趣向捕捉(暂不可用)` - 未实现
- `困境救场(暂不可用)` - 未实现

**scope 选项**:
- `disabled` - 不对任何群聊启用
- `global` - 对所有群/角色启用 (全局唯一，只能有一个)
- `specific` - 仅对指定群聊启用

### 4.3 PersonaConfigManager 核心方法

```python
class PersonaConfigManager:
    def get_config(self, persona_name: str) -> StringConfig:
        """获取 Persona 配置实例"""

    def get_all_configs(self) -> Dict[str, StringConfig]:
        """获取所有 Persona 配置"""

    def get_global_persona(self) -> Optional[str]:
        """获取当前 global 的 Persona"""

    def validate_global_uniqueness(self, persona_name, scope) -> tuple[bool, Optional[str]]:
        """验证 global 全局唯一性约束"""

    def set_scope(self, persona_name: str, scope: str) -> tuple[bool, str]:
        """设置启用范围"""

    def set_target_groups(self, persona_name: str, target_groups: List[str]):
        """设置目标群聊"""

    def set_ai_mode(self, persona_name: str, ai_mode: List[str]):
        """设置 AI 行动模式"""

    def set_inspect_interval(self, persona_name: str, inspect_interval: int):
        """设置巡检间隔"""

    def set_keywords(self, persona_name: str, keywords: List[str]):
        """设置唤醒关键词"""

    def get_persona_for_session(self, session_id: str) -> Optional[str]:
        """根据 Session ID 获取对应 Persona"""
```

### 4.4 Persona 匹配规则

`get_persona_for_session()` 的匹配优先级:

```
1. 首先查找 scope="specific" 且 target_groups 包含该 group_id 的 Persona
2. 如果没有找到，查找 scope="global" 的 Persona
3. 如果没有找到，返回 None (不触发 AI)

注意: 全局只能有一个 scope="global" 的 Persona
```

---

## 5. AI 路由与 Session 管理

### 5.1 AI Router (`ai_router.py`)

**核心函数**:

```python
async def get_ai_session(event: Event) -> GsCoreAIAgent:
    """通过 event 获取 AI Session"""
    return await _get_or_create_ai_session(event)

async def get_ai_session_by_id(
    session_id: str,
    user_id: str,
    group_id: Optional[str] = None,
    is_group_chat: bool = False,
) -> Optional[GsCoreAIAgent]:
    """通过 session_id 获取或创建 AI Session（兼容接口）"""
    from gsuid_core.models import Event
    ev = Event(
        bot_id="",
        user_id=user_id,
        group_id=group_id,
        user_type="group" if is_group_chat else "direct",
    )
    return await _get_or_create_ai_session(ev, session_id=session_id)
```

**Session 创建流程**:

```python
async def _get_or_create_ai_session(
    event: Event,
    session_id: Optional[str] = None,
) -> GsCoreAIAgent:
    """内部函数：获取或创建 AI Session 的核心逻辑"""
    if session_id is None:
        session_id = event.session_id

    history_manager = get_history_manager()
    history_manager.update_session_access(event)

    registry = get_ai_session_registry()

    # 检查是否已存在 AI session
    session = registry.get_ai_session(session_id)
    is_group_chat = event.user_type != "direct"
    if session is not None:
        persona_name = persona_config_manager.get_persona_for_session(session_id)
        if persona_name and _check_persona_changed(session, persona_name):
            # Persona 已修改，热重载 Session
            ...

    # 如果 session 不存在或需要重建，创建新 Session
    persona_name = persona_config_manager.get_persona_for_session(session_id)
    if persona_name is None:
        raise ValueError(f"没有为 session {session_id} 配置 persona")

    # 构建 Persona Prompt
    base_persona = await build_persona_prompt(persona_name)
    _persona_mtime_cache[persona_name] = _get_persona_mtime(persona_name)

    # 创建 Agent
    session = create_agent(
        system_prompt=base_persona,
        persona_name=persona_name,
        create_by="Chat",
    )

    # 保存到 AISessionRegistry
    registry.set_ai_session(session_id, session)
    history_manager.update_session_access(event)

    return session
```

**Session ID 格式**:
```
# 群聊时: 以 {WS_BOT_ID}:{bot_id}:{bot_self_id}: 为前缀，同时包含 WS 链接标识和平台标识
session_id = f"{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}"
示例: "ws-onebot:onebot:bot_001:group:789012"

# 私聊时: 以 {WS_BOT_ID}:{bot_id}:{bot_self_id}: 为前缀，同时包含 WS 链接标识、平台标识和机器人账号标识
session_id = f"{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}"
示例: "ws-onebot:onebot:bot_001:private:345678"
```

### 5.2 Session 存储

消息历史与 AI 会话对象已拆分为两个独立模块：

- 通用消息历史 `HistoryManager`（`gsuid_core/message_history/manager.py`）—— 不涉及 AI，
  负责记录 Bot 的消息输入/输出历史。
- AI 会话对象注册表 `AISessionRegistry`（`gsuid_core/ai_core/session_registry.py`）—— 仅在
  AI 开启时使用，负责 `GsCoreAIAgent` 对象的注册与生命周期。

AI Session 对象存储在 `AISessionRegistry` 中：

```python
class AISessionRegistry:
    def __init__(self):
        self._ai_sessions: Dict[str, Any] = {}  # session_id -> GsCoreAIAgent

    def get_ai_session(self, session_id: str) -> Optional[Any]:
        return self._ai_sessions.get(session_id)

    def set_ai_session(self, session_id: str, session: Any):
        self._ai_sessions[session_id] = session
```

### 5.3 内存保护机制 (滑动窗口 + 自动清理)

消息历史与 AI 会话对象各自包含内存保护机制，**不存在 OOM 风险**：

#### 5.3.1 滑动窗口机制（`HistoryManager`）

```python
# 每个 Session 使用 deque 限制消息数量
DEFAULT_MAX_MESSAGES = 40  # 每 Session 最多保留 40 条消息
MAX_AI_HISTORY_LENGTH = 30  # AI 对话历史最大长度

# 在 __init__ 中
self._histories[storage_event] = deque(maxlen=self._max_messages)
```

**效果**: 每个 Session 的消息历史被限制在 `deque(maxlen=40)` 中，超过限制的旧消息自动被丢弃。

> **注意**：群聊场景下，`storage_event` 的 `user_id` 被设为空字符串，确保同一群聊的所有用户消息共享同一个 deque。

#### 5.3.2 空闲 Session 清理（`AISessionRegistry`）

空闲清理由 `AISessionRegistry` 负责，依据 `HistoryManager` 的 session 元数据
判断 session 是否空闲：

```python
IDLE_THRESHOLD = 1800  # 空闲阈值（秒），默认 30 分钟
CLEANUP_INTERVAL = 3600  # 清理检查间隔（秒），默认 1 小时

# 启动清理循环（由 ai_core/statistics/startup.py 调用）
async def start_cleanup_loop(self):
    self._cleanup_task = asyncio.create_task(self._cleanup_loop())

# 清理逻辑：移除空闲 session 对应的 AI 会话对象
async def cleanup_idle_sessions(self, idle_threshold: int = None):
    history_manager = get_history_manager()
    for session_id, info in history_manager.get_all_sessions_info().items():
        if current_time - info["last_access"] > idle_threshold:
            self.remove_ai_session(session_id)
```

**效果**: 超过 30 分钟未活跃的 Session 对应的 AI 会话对象自动从内存中清除。

#### 5.3.3 内存保护总结

| 机制 | 所属模块 | 配置 | 效果 |
|------|---------|------|------|
| 滑动窗口 | `HistoryManager` | `deque(maxlen=40)` | 每 Session 最多 40 条消息 |
| Token 上限 | `HistoryManager` | `MAX_HISTORY_TOKENS=160000` | 单 Session Token 总量超限时淘汰最旧消息 |
| AI 历史限制 | `AISessionRegistry` | `MAX_AI_HISTORY_LENGTH=30` | AI 对话历史不超过 30 条 |
| Agent 内部截断 | `GsCoreAIAgent` | `max_history=50` | `GsCoreAIAgent.history` 超过 50 条时安全截断（含 ToolCall/ToolReturn 配对保护） |
| 空闲清理 | `AISessionRegistry` | `IDLE_THRESHOLD=1800` (30分钟) | 30 分钟不活跃的 Session 自动清除 |
| 定时清理 | `AISessionRegistry` | `CLEANUP_INTERVAL=3600` (1小时) | 每小时检查一次空闲 Session |

> **⚠️ 重要改进**：`deque(maxlen=40)` 仅按消息条数截断，存在"隐形 Token 爆炸"风险。
> 如果在群聊中，5 个人连续发了 10 篇 5000 字的长文，虽然只有 50 条消息（未触发限制），
> 但合计 25 万字会瞬间突破主流 LLM 的 Token 上限或产生极其昂贵的费用。
>
> `GsCoreAIAgent` 内部使用 `_truncate_history_with_tool_safety()` 进行安全截断，
> 确保 ToolCallPart 和 ToolReturnPart 始终配对，避免 "tool result's tool id not found" 错误。

### 5.4 Persona Prompt 热重载

Session 一旦创建，`system_prompt` (base persona) 会通过 mtime 检测实现热重载。

详见 [5.6.2 节](#562-persona-prompt-热重载的缓存陷阱-已修复)

### 5.6 设计缺陷与潜在问题

#### 5.6.1 Session ID 设计导致"群聊上下文割裂" (致命漏洞) ✅ 已修复

**问题所在**: 原 Session ID 绑定到具体用户，导致群聊中失去全局记忆。

```python
# 原代码 (handler.py)
session_id = f"{bid}%%%{temp_gid}%%%{uid}"
```

**场景重现**:
- 群聊 gid=1001 中，用户 A（uid=01）问 AI："我叫什么名字？" → Session 1
- 接着用户 B（uid=02）问 AI："刚才那个跟你说话的人叫什么？" → Session 2

**后果**: AI 会回答"不知道，这是我们第一次对话"。因为 Session ID 绑定了具体的 user_id，导致 AI 在群聊中失去了"群组全局记忆"，它变成了分别和每个人在群里进行毫无关联的 1v1 单聊。

**修复方案** (已实现):

Session ID 格式修改为：
```python
# 群聊: {WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}
# 私聊: {WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}
```

```python
# models.py - Event.session_id 属性
# session_id 由 Event 的属性自动生成
# 群聊: f"{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}"
# 私聊: f"{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}"
```

**关键区别**：
- Session ID 不再包含 `user_id`（群聊场景），群内所有用户共享同一个 Session
- 私聊时使用 `user_id` 作为标识，确保一对一对话的独立性

**实际 Session 路由逻辑** (`ai_router.py`):
```python
# session_id 格式: "{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}" 或 "{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}"
# AI Router 使用 event.session_id 获取或创建 Session
# HistoryManager 以 Event 为 key 存储历史记录（群聊时 user_id 置空以保证一致性）
```

修改后的架构：
- `Event.session_id` 格式为 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}` 或 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}`
- `get_persona_for_session()` 解析 session_id 提取 `group_id` 或 `user_id` 用于 Persona 匹配
- `HistoryManager`（`gsuid_core/message_history`）以 `Event` 对象为 key 存储历史记录，群聊时 `user_id` 置空确保同一群聊共享 deque
- AI Session 的共享由 `AISessionRegistry._ai_sessions` 决定，按 `session_id` 字符串存储

#### 5.6.2 Persona Prompt 热重载的"缓存陷阱" (设计缺陷) ✅ 已修复

**问题所在**: 原 Session 一旦创建，`system_prompt` (base persona) 保持不变。

**场景重现**:
1. 管理员在后台把"傲娇萝莉"的人设改成"温柔御姐"，点击保存
2. 新加入的用户看到温柔御姐
3. 但之前一直在跟 AI 聊天的老用户，AI 依然是个傲娇萝莉

**后果**: 状态不一致，管理员以为修改没生效。

**修复方案** (已实现): 引入 Persona 文件修改时间检测机制：

```python
# ai_router.py
_persona_mtime_cache: dict[str, float] = {}  # mtime 缓存

def _check_persona_changed(session: GsCoreAIAgent, persona_name: str) -> bool:
    """检查 Persona 是否已修改，需要热重载"""
    if session.persona_name != persona_name:
        return True

    current_mtime = _get_persona_mtime(persona_name)
    cached_mtime = _persona_mtime_cache.get(persona_name, 0.0)

    if current_mtime > cached_mtime:
        _persona_mtime_cache[persona_name] = current_mtime
        return True
    return False
```

修改后的 `ai_router.py` 现在：
- 在 `_get_or_create_ai_session()` 中检查 Persona 文件的修改时间
- 如果检测到文件变更，自动移除旧 Session 并重建
- `GsCoreAIAgent` 新增 `persona_name` 属性用于追踪
- `create_agent()` 工厂函数支持 `persona_name` 参数

#### 5.6.3 强制总结偏离用户问题 (设计缺陷) ✅ 已修复

**问题所在**: 当 Agent 达到 `UsageLimitExceeded`（思考轮数上限）时，系统的 fallback 处理逻辑无法让 AI 真正回答用户最初的问题，而是让 AI "自我总结思考过程"。

**症状**: 用户问"今天北京天气怎么样"，AI 搜索了多轮工具后超限，然后回复"根据我前面的搜索，我调用了天气查询工具，获取了相关数据，现在总结如下..."——用户听到的是 AI 的工作汇报，而非天气答案。

**根本原因**（三重缺陷叠加）：

| 缺陷 | 说明 | 后果 |
|------|------|------|
| A：无问题锚定 | fallback prompt 不包含用户原始问题 | LLM 在多轮工具调用后遗忘原始意图 |
| B：工具 schema 残留 | 传入的 `message_history` 含完整工具定义 | schema 的"模式惯性"诱导 LLM 继续输出工具调用格式 |
| C：措辞歧义 | "总结性的最终回答"被解读为"总结思考过程" | LLM 复盘工具调用而非回答问题 |

**修复方案** (`gs_agent.py`，v4 最终方案)：

核心思想：**不传递杂乱历史**，而是从中提取"用户原问题 + 已知事实 + LLM 中间推理"，按轮次组织后打包为一条干净的消息，`message_history` 置空，fallback Agent 不带 `deps_type/deps`。

```python
# 1. 记录原始用户问题
self._last_user_question = user_message.strip() if isinstance(user_message, str) else ""

# 2. 按轮次提取事实+推理（保留 TextPart 中间结论）
run_context = _extract_run_context(self.history)

# 3. 打包成一条干净的消息
if run_context:
    final_message = (
        f"【用户的问题】\n{user_question}\n\n"
        f"【已获取的信息和推理过程】\n{run_context}\n\n"
        "请根据以上已知信息，直接回答用户的问题。"
        "禁止调用任何工具，只输出自然语言文本。"
    )
else:
    final_message = (
        f"【用户的问题】\n{user_question}\n\n"
        "请直接回答这个问题（根据你的已有知识和角色性格），不要调用任何工具。"
    )

# 4. 创建无工具精简 Agent（去掉 deps_type/deps：tools=[] 时不需要依赖注入）
_fallback_agent = Agent(
    model=self.model,
    system_prompt=self.system_prompt or "你是一个智能助手。",
    model_settings={"max_tokens": self.max_tokens},
    tools=[],       # 空工具列表 = 从根源上消除 schema 注入
    toolsets=[],
    retries=0,
    output_type=str,
)

# message_history 为空：所有上下文已聚焦到 final_message 中
fallback_result = await _fallback_agent.run(
    final_message,
    message_history=[],
    usage_limits=UsageLimits(request_limit=1),
)
```

**原理说明**：
- **按轮次组织、保留推理**：`_extract_run_context()` 不仅提取 `ToolReturnPart`（工具返回），还保留 `TextPart`（LLM 中间推理）并按"第N轮 → 工具调用 → 返回"组织，LLM 能看到完整的因果链条，而非散装的返回列表
- **message_history = []**：彻底排除了上一轮"工具调用模式"的行为惯性，LLM 以"全新会话"的姿态进入
- **无工具 Agent**：`tools=[]` 从根源上消除 schema，LLM 没有工具可调；同时去掉 `deps_type/deps`，避免 pydantic_ai 版本中不匹配参数的隐患
- **错误处理一致性**：`except Exception` 中，有 `bot` 时通过 `bot.send()` 发出最终错误并 `return ""`；无 `bot` 时返回字符串由调用方处理，避免"安抚消息+错误消息"双发

> ⚠️ **方案演进**：v1 → 错误地用 `_strip_tool_schema_from_history()`（无效，schema 在 Agent 内部 system prompt）；v2 → 创建新 Agent 但仍传完整 `self.history`（LLM 需自行梳理）；v3 → 提取事实 + 打包消息 + message_history 置空，但丢失了 LLM 中间推理、保留了 deps_type/deps；**v4 最终：按轮次提取事实+推理、去掉冗余参数、修正错误处理**。

> 详细技术报告见 `docs/FORCED_SUMMARY_OPTIMIZATION_REPORT.md`（如文件存在）

### 5.5 工具注册系统与 Agent 架构

#### 5.5.1 工具注册表结构

工具注册表位于 [`register.py`](gsuid_core/ai_core/register.py)，采用分类字典结构：

```python
# 工具注册表: Dict[分类名, Dict[工具名, ToolBase]]
_TOOL_REGISTRY: Dict[str, Dict[str, ToolBase]] = {}
```

#### 5.5.2 @ai_tools 装饰器

```python
@ai_tools(category: str = "default", check_func: Optional[CheckFunc] = None, **check_kwargs)
async def my_tool(ctx: RunContext[ToolContext], ...) -> str:
    ...
```

**参数说明**：
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `category` | `str` | `"default"` | 工具分类名称，用于分组管理 |
| `check_func` | `Optional[CheckFunc]` | `None` | 可选的权限校验函数，支持同步/异步 |
| `**check_kwargs` | - | - | 传递给 check_func 的额外参数 |

> **智能参数注入**：`@ai_tools` 装饰器会自动分析原函数的参数签名，将 `RunContext[ToolContext]`、`Event`、`Bot` 类型的参数自动注入，不暴露给 LLM。同时重写 `__signature__` 以确保 PydanticAI Schema 兼容。

#### 5.5.3 工具分类与渐进式加载

系统采用**渐进式加载**机制，工具按用途和重要性分为五个层级：

| 分类 | 说明 | 加载方式 | 示例 |
|------|------|----------|------|
| `self` | 仅为自身服务的能力 | 主Agent专属，始终加载 | 好感度管理、发送消息、创建子Agent |
| `buildin` | 默认内置工具 | 主Agent始终加载 | 知识库检索、Web搜索、查询记忆 |
| `by_trigger` | 触发器桥接工具 | 按需加载，通过向量检索匹配 | 插件触发器通过 `to_ai` 自动注册的 AI 工具 |
| `common` | 通常工具 | 按需加载，通过向量检索匹配 | 定时任务管理、获取自身信息 |
| `default` | 子Agent工具 | 由子Agent使用 | 文件操作、日期获取、系统命令 |
| `mcp` | MCP 外部工具 | 启动时自动注册，按需加载 | 用户自定义的 MCP 服务器工具 |

**加载优先级**: `self` > `buildin` > `by_trigger` / `common`（按需向量检索）

#### 5.5.4 渐进式加载架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      主Agent (Main Agent)                   │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ self 工具 (始终加载)                                  │   │
│  │ - 好感度查询/更新                                    │   │
│  │ - 发送消息                                          │   │
│  │ - 创建子Agent                                       │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ buildin 工具 (始终加载)                              │   │
│  │ - 知识库检索                                       │   │
│  │ - Web搜索                                          │   │
│  │ - 查询用户记忆                                      │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ buildin + by_trigger + common 工具 (按需向量检索)     │   │
│  │ - 插件触发器通过 to_ai 自动注册的 AI 工具             │   │
│  │ - send_message_by_ai (发送拦截到的图片, self)         │   │
│  │ - 定时任务管理 (add/list/query/modify/cancel...)    │   │
│  │ - 获取自身Persona信息                                │   │
│  │ - 通过 search_tools() 向量检索按需加载               │   │
│  └─────────────────────────────────────────────────────┘   │
│                          │                                │
│                          │ create_subagent() 调用         │
│                          ▼                                │
│  ┌─────────────────────────────────────────────────────┐   │
│  │                    子Agent (Sub Agent)                │   │
│  │           default 工具 (由子Agent使用)                 │   │
│  │ - get_current_date                                   │   │
│  │ - read_file_content / write_file_content             │   │
│  │ - execute_file / execute_shell_command               │   │
│  │ - list_directory / diff_file_content               │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

#### 5.5.5 主Agent与子Agent的工具加载差异

系统中的工具加载分为两个独立的上下文：

**主Agent (Main Agent)** — 工具列表按**三层工具池**组装：

1. **保底工具池**：`get_main_agent_tools()` 无条件全部加载 `self` + `buildin` 两个分类的工具（搜索、记忆、自我认知 `get_self_info`、消息发送、持久状态 `state_*`、好感度、子Agent、定时任务**创建**入口 `add_once_task` / `add_interval_task` 等）。是否属于保底池**完全由工具注册时的 `category` 决定**，不依赖任何硬编码工具名单。定时任务的**管理**类工具（列出/查询/修改/取消/暂停/恢复）注册为 `common`，不在保底池，由查询工具池按需检索。
2. **语境工具池**：群聊场景下，由 `get_scope_context_tags()` 读取群组画像的语境标签，再由 `get_tools_by_context_tags()` 自动加载声明了匹配 `context_tags` 的工具（最多 8 个）。
3. **查询工具池**：由 `search_tools(non_category=["self", "buildin"])` 按用户 query 向量检索加载 `by_trigger`、`common`、`media`、`mcp` 工具。

保底工具池全部保留；语境 + 查询工具池合并去重后限制附加数量上限（12 个）。

- **不会调用 `default` 分类的工具**（子Agent专用）

**子Agent (Sub Agent)**
- 由 `create_subagent()` 创建
- 使用 `search_tools(non_category="self")` 搜索工具
- 加载 `buildin`、`common`、`default` 分类的工具
- **不会调用 `self` 分类的工具**（如 `query_user_favorability`、`send_message_by_ai` 等）

这种设计确保了工具调用的安全性：
- `self` 工具仅限主Agent使用，防止子Agent直接操作用户数据
- `default` 工具（如文件操作、系统命令）仅通过子Agent使用

> 三层工具池机制解决了"口语化提问命中不到基础工具"与"群里问游戏问题命中不到游戏工具"的问题。
> 当前最终说明以 `docs/AGENT_CAPABILITY_AGENT_MERGED_20260521.md` 为准；历史拆分稿已归档到 `docs/backups/`。

#### 5.5.6 Self 工具 (`category="self"`)

主Agent专属工具，属于保底工具池，始终加载。

| 工具 | 说明 |
|------|------|
| `query_user_favorability` | 查询用户好感度 |
| `update_user_favorability` | 更新用户好感度（增量） |
| `send_message_by_ai` | 发送消息给用户 |
| `create_subagent` | 创建子Agent |
| `add_once_task` | 添加一次性定时任务（创建入口，口语化触发，需常驻保底池） |
| `add_interval_task` | 添加循环任务（创建入口，口语化触发，需常驻保底池） |

#### 5.5.7 主Agent内置工具 (`category="buildin"`)

主Agent默认加载的核心工具，属于保底工具池，直接调用。

| 工具 | 说明 |
|------|------|
| `search_knowledge` | 检索知识库内容 |
| `web_search_tool` | Web搜索（支持 Tavily / Exa / MCP 三种提供方，通过 `websearch_provider` 配置切换） |
| `web_fetch_tool` | 网页抓取（转 Markdown） |
| `query_user_memory` | 查询用户记忆条数 |
| `get_self_info` | 获取完整自我认知（身份/能力边界/主人） |
| `state_get` / `state_set` / `state_delete` / `state_list` / `state_append` | 通用持久状态存储（跨会话键值数据） |

#### 5.5.8 通常工具 (`category="common"`)

不属于保底池，当用户明确需要相关功能时由查询工具池向量检索按需加载。

| 工具 | 说明 |
|------|------|
| `search_image` | 检索图片资源 |
| `get_self_persona_info` | 获取自身Persona资源信息 |
| `set_user_favorability` | 设置用户好感度（绝对值） |
| `send_meme` | 发送表情包（根据情绪/场景智能选取） |
| `collect_meme` | 手动收集表情包 |
| `search_meme` | 搜索表情包库 |
| `list_scheduled_tasks` | 列出所有定时任务（管理类） |
| `query_scheduled_task` | 查询任务详情（管理类） |
| `modify_scheduled_task` | 修改任务（管理类） |
| `cancel_scheduled_task` | 取消任务（管理类） |
| `pause_scheduled_task` | 暂停任务（管理类） |
| `resume_scheduled_task` | 恢复任务（管理类） |
| `evaluate_agent_mesh_capability` | 创建 Kanban 任务树前置——评估现有画像能否覆盖（必须 covered=true 才可创建） |
| `register_kanban_task` | 注册一棵 Kanban 任务树（根 + N 子任务节点），事件驱动并发推进 |
| `respawn_subtask` | 复活 failed 子任务（达 3 次自动转 waiting_approval） |
| `fail_task_tree` | 明确终结整棵任务树 + 级联未完成子任务 |
| `respond_subtask_approval` | 转达主人对 waiting_approval 子任务的同意 / 拒绝 |
| `artifact_put` / `artifact_get` / `artifact_list` | 任务树内 Artifact Hub 增 / 取 / 列 |
| `artifact_get_recent` | 取根任务最近一份 artifact 原文，专给主人格追问溯源用 |

> ⚠️ 原 `create_persistent_agent_tool`（agent_mesh）与 C5 长任务工具
> （`register_long_task` / `task_commit_step` / `task_*` 系列）已全部移除——
> 多步任务统一收敛到 Kanban 任务树（事件驱动 + 子任务依赖）。当前最终语义见
> `docs/AGENT_MESH_KANBAN_IMPLEMENTATION_20260522.md` 与
> `docs/AGENT_CAPABILITY_AGENT_MERGED_20260521.md`。
>
> 🆕 调度模型：Kanban 纯**事件驱动**。需要"明天 6 点触发""每天复盘"等时间
> 触发条件，用 `add_once_task` / `add_interval_task` 在那个时刻把主人格唤醒，
> 唤醒时主人格视情况调 `register_kanban_task`；不要把时间塞进子任务字段。
>
> 🆕 主人格在追问溯源场景（"你为什么选 X / 为什么这样做"）应**先调
> `artifact_get_recent`** 把对应任务树的最近一份 artifact 原文查回来，再用
> 角色口吻转告主人；严禁自行 web_search 重新拼凑解释。决策树 3.6 强制走这条路径。

#### 5.5.9 触发器桥接工具 (`category="by_trigger"`)

由插件触发器通过 `to_ai` 参数自动注册的 AI 工具，通过 `search_tools()` 向量检索按需加载（受 limit 数量限制），不再无条件全部加载。

**文件位置**: [`gsuid_core/ai_core/trigger_bridge.py`](gsuid_core/ai_core/trigger_bridge.py)

| 工具 | 说明 |
|------|------|
| `<触发器函数名>` | 插件通过 `@sv.on_command(..., to_ai="...")` 自动注册的工具 |


**工作原理**：

1. 插件开发者在 `@sv.on_command()` 等装饰器上声明 `to_ai` 参数（非空字符串作为 AI 工具的 docstring）
2. 插件加载时，`_on()` 方法自动调用 `_register_trigger_as_ai_tool()` 将触发器函数包装为 AI 工具
3. AI 调用时，包装函数先执行**权限检查**（与用户直接触发一致）：
   - `plugins.enabled` / `sv.enabled` — 插件/SV 是否启用
   - `user_pm <= plugins.pm` / `user_pm <= sv.pm` — 权限等级检查
   - 权限不足时返回错误文本给 AI，配置通过 webconsole 修改后实时生效
4. 权限通过后，使用 `MockBot` 代理 `bot.send()`：
   - **图片/资源 (bytes, Message(type="image"), base64://字符串)**: 通过 `RM.register()` 注册，返回资源 ID（如 `img_a1b2c3d4`）
   - **纯文本 (str, Message(type="text"))**: 被收集，作为工具返回值传回给 AI
   - **`send_option(reply, buttons)`**: reply 走 `send()` 拦截，buttons 忽略
   - **`receive_resp(reply, ...)`**: reply 走 `send()` 拦截，返回 `None`（AI 不支持交互式等待）
5. 触发器函数内可通过 `ai_return()` 向 AI 返回纯文本中间结果
6. AI 拿到工具返回值（纯文本摘要 + 资源 ID），决定是否调用 `send_message_by_ai(image_id=...)` 将图片发送给用户

**交互流程**：

```
用户直接触发（pm=2 的 SV，pm=3 的用户）：
  用户 → handler 检查 user_pm(3) <= sv.pm(2) → False → 触发器不匹配 → 落入 AI 流程

AI 调用（权限不足）：
  AI → 调用触发器工具(text="证券ETF")
    → 权限检查: ev.user_pm(3) > sv.pm(2) → True
    → 返回 "❌ 权限不足：该功能需要权限等级 2，当前用户权限等级为 3。"
  AI → 向用户解释权限不足 ✅

AI 调用（权限通过，AI 决定发图）：
  AI → 调用触发器工具(text="证券ETF")
    → 权限检查通过
    → MockBot 拦截 send(im) → RM.register(im) → 资源 ID: img_a1b2c3d4
    → 工具返回 "查询完成\n[已生成 1 张图片，资源ID: img_a1b2c3d4。请调用 send_message_by_ai 工具传入 image_id 将图片发送给用户]"
  AI → 调用 send_message_by_ai(image_id="img_a1b2c3d4") → RM.get() → real_bot.send() → 图片发出 ✅

AI 调用（权限通过，AI 决定不发图）：
  AI → 调用触发器工具(text="证券ETF")
    → 工具返回含资源 ID
  AI → 判断用户只要数据 → 不调用 send_message_by_ai → 图片保留在 RM 中 ✅

用户后续请求图片：
  用户 → "我想看看图"
  AI → 从历史对话中找到资源 ID → 调用 send_message_by_ai(image_id="img_a1b2c3d4") → 图片发出 ✅
```

#### 5.5.10 子Agent工具 (`category="default"`)

通过 `create_subagent` 调用，用于文件操作、代码执行等。

| 工具 | 说明 |
|------|------|
| `get_current_date` | 获取当前日期时间 |
| `read_file_content` | 读取文件内容 |
| `write_file_content` | 写入文件内容 |
| `execute_file` | 执行脚本文件 |
| `diff_file_content` | 对比两个文件 |
| `list_directory` | 列出目录内容 |
| `execute_shell_command` | 执行系统命令 (需权限) |

> **⚠️ 死循环防护**：
> 子Agent 执行时可能有错误的命令导致报错，然后尝试修复导致无限循环。
> 现在 `create_subagent` 实现了 `max_iterations=3` 的硬限制：
> - 子Agent 最多执行 3 次工具调用
> - 达到上限后强制终止并返回错误日志
> - 防止 思考 -> 执行 -> 报错 -> 思考 的无限循环

#### 5.5.11 动态工具发现

**文件位置**: [`gsuid_core/ai_core/buildin_tools/dynamic_tool_discovery.py`](gsuid_core/ai_core/buildin_tools/dynamic_tool_discovery.py)

系统提供动态工具发现能力，允许 AI 根据任务需求搜索可能用到的新工具。

| 工具 | 说明 | 状态 |
|------|------|------|
| `discover_tools` | 根据任务描述搜索相关工具 | 已定义但未注册（装饰器被注释） |
| `list_available_tools` | 列出当前系统中所有可用的AI工具 | 已定义但未注册（装饰器被注释） |

> **注意**：这两个工具函数已实现但 `@ai_tools` 装饰器被注释掉，暂未作为 AI 工具注册。主Agent 通过 `gs_agent.py` 中的 `search_tools()` 函数实现类似的动态工具发现能力。

#### 5.5.12 MCP 工具集成 (Model Context Protocol)

**文件位置**: [`gsuid_core/ai_core/mcp/`](gsuid_core/ai_core/mcp/)

系统支持通过 MCP (Model Context Protocol) 协议集成外部工具服务器。用户可以通过 WebConsole API 自由添加 MCP 服务器配置，框架启动时自动连接服务器并将 MCP 工具注册为 AI 工具，使 AI 可以自由调用。

**模块结构**:

```
mcp/
├── __init__.py           # 模块导出（MCPClient, MCPConfig, mcp_config_manager 等）
├── client.py             # MCP 客户端（基于 fastmcp，stdio 传输）
├── config_manager.py     # MCP 配置管理器（JSON 文件存储 + MCPToolDefinition）
├── mcp_tool_caller.py    # 通用 MCP 工具调用模块（call_mcp_tool）
├── mcp_tools_config.py   # MCP 工具配置（websearch/image_understand 的 MCP 工具 ID）
└── startup.py            # 启动时自动注册 MCP 工具 + 热重载
```

**配置存储**: `data/ai_core/mcp_configs/{config_id}.json`

```json
{
    "name": "MiniMax",
    "command": "uvx",
    "args": ["minimax-coding-plan-mcp"],
    "env": {"MINIMAX_API_KEY": "your_key"},
    "enabled": true,
    "register_as_ai_tools": false,
    "tools": [
        {
            "name": "web_search",
            "description": "Web search tool",
            "parameters": {
                "query": {"type": "string", "required": true},
                "max_results": {"type": "integer", "required": false}
            }
        }
    ]
}
```

**新增配置字段**:

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `register_as_ai_tools` | `bool` | `false` | 是否将该 MCP 服务器的工具注册为 AI Tools（供主Agent/子Agent 调用） |
| `tools` | `list[MCPToolDefinition]` | `[]` | 该 MCP 服务器的所有可用工具及其参数定义 |

**MCP 工具 ID 格式**: `{mcp_id} - {tool_name}`，例如 `minimax - web_search`。用于在 `mcp_tools_config` 中指定 Web Search 和 Image Understand 使用的 MCP 工具。

**MCP 工具配置** (`data/ai_core/mcp_tools_config.json`):

| 配置项 | 说明 | 示例值 |
|--------|------|--------|
| `websearch_mcp_tool_id` | Web Search 使用的 MCP 工具 ID | `minimax - web_search` |
| `image_understand_mcp_tool_id` | 图片理解使用的 MCP 工具 ID | `minimax - understand_image` |

**启动注册流程**:

```
框架启动 (on_core_start, priority=5)
    │
    ├── 1. mcp_config_manager.get_enabled_configs()
    │   └── 读取 data/ai_core/mcp_configs/*.json 中 enabled=true 的配置
    │
    ├── 2. 对每个配置创建 MCPClient
    │   └── MCPClient(name, command, args, env)
    │
    ├── 3. client.list_tools() 获取 MCP 服务器工具列表
    │   └── 通过 stdio 传输连接 MCP 服务器
    │
    ├── 4. 为每个 MCP 工具动态创建包装函数
    │   ├── 解析 input_schema 生成正确的函数签名
    │   ├── 注入 RunContext[ToolContext] 上下文
    │   └── 注册到 _TOOL_REGISTRY["mcp"] 分类
    │
    └── 5. 工具命名规则: mcp_{server_name}_{tool_name}
        └── 避免不同 MCP 服务器之间的工具名冲突
```

**工具调用流程**:

```
AI 决策调用 MCP 工具
    │
    ├── 1. PydanticAI 从 _TOOL_REGISTRY 获取工具
    │
    ├── 2. 调用 mcp_tool_wrapper(ctx, **kwargs)
    │   └── 过滤 None 值的可选参数
    │
    ├── 3. MCPClient.call_tool(tool_name, arguments)
    │   ├── 创建 StdioTransport
    │   ├── 建立连接
    │   ├── 执行工具调用
    │   └── 返回 MCPToolResult
    │
    └── 4. 返回文本结果给 AI
        ├── 成功: 返回工具输出文本
        └── 失败: 返回错误信息
```

**通用 MCP 工具调用** (`mcp_tool_caller.py`):

`call_mcp_tool()` 函数允许通过 MCP 工具 ID 直接调用 MCP 服务器上的工具，无需将工具注册为 AI Tools。Web Search 和 Image Understand 模块通过此函数调用 MCP 工具。

```python
from gsuid_core.ai_core.mcp.mcp_tool_caller import call_mcp_tool

result = await call_mcp_tool(
    mcp_tool_id="minimax - web_search",
    arguments={"query": "Python 教程"},
)
```

**WebConsole API 端点**:

| 方法 | 端点 | 功能 |
|------|------|------|
| GET | `/api/ai/mcp/list` | 获取所有 MCP 配置列表 |
| GET | `/api/ai/mcp/{config_id}` | 获取指定配置详情 |
| POST | `/api/ai/mcp` | 创建新 MCP 配置 |
| PUT | `/api/ai/mcp/{config_id}` | 更新 MCP 配置 |
| DELETE | `/api/ai/mcp/{config_id}` | 删除 MCP 配置 |
| POST | `/api/ai/mcp/{config_id}/toggle` | 切换启用/禁用状态 |
| POST | `/api/ai/mcp/reload` | 热重载所有配置并重新注册工具 |
| GET | `/api/ai/mcp/{config_id}/tools` | 从已配置的 MCP 服务器发现工具 |
| POST | `/api/ai/mcp/tools/discover` | 从临时配置发现工具（不保存） |
| POST | `/api/ai/mcp/tools/import` | 从 JSON 导入 MCP 配置 |
| GET | `/api/ai/mcp/presets` | 获取 MCP 预设列表 |

**MCP 预设配置**: 系统内置 5 个 MCP 预设（MiniMax、Firecrawl、Tavily、GitHub、Filesystem），用户可通过预设快速添加 MCP 服务器。

**热重载**: 通过 `POST /api/ai/mcp/reload` 可以在运行时重新加载所有 MCP 配置并重新注册工具，无需重启服务。

> **详细文档**: 见 [MCP_TOOL_INTEGRATION_CHANGELOG.md](./MCP_TOOL_INTEGRATION_CHANGELOG.md)（变更记录）、[MCP_TOOL_PERMISSIONS.md](./MCP_TOOL_PERMISSIONS.md)（权限配置）、[MCP_SERVER.md](./MCP_SERVER.md)（MCP Server 对外暴露）

#### 5.5.12 核心函数

```python
def get_main_agent_tools() -> ToolList:
    """获取主Agent基础工具集（仅 self + buildin 分类，始终加载）

    by_trigger 分类的工具不再无条件加载，而是通过 search_tools() 向量检索按需加载，
    避免插件数量膨胀导致工具列表过大（100+ 工具）浪费 Token 并降低 LLM 选工具准确率。
    """
    all_tools_cag = get_registered_tools()
    all_tools = {}
    for cat in ["self", "buildin"]:
        if cat in all_tools_cag:
            all_tools.update(all_tools_cag[cat])
    return [all_tools[tool].tool for tool in all_tools]

async def search_tools(
    query: str,
    limit: int = 5,
    category: Union[str, list[str]] = "all",
    non_category: Union[str, list[str]] = "",
) -> ToolList:
    """根据自然语言意图检索关联工具（向量搜索）"""
    ...

def get_all_tools() -> Dict[str, ToolBase]:
    """获取所有工具（平铺结构）"""
    result = {}
    for category_tools in _TOOL_REGISTRY.values():
        result.update(category_tools)
    return result

def get_registered_tools() -> Dict[str, Dict[str, ToolBase]]:
    """获取所有已注册的工具（按分类）"""
    return _TOOL_REGISTRY
```

---

## 6. Heartbeat 定时巡检机制

### 6.1 概述

Heartbeat 是 AI 主动发言的定时巡检系统，当 `ai_mode` 包含 "定时巡检" 时启用。

### 6.2 核心组件

```
heartbeat/
├── __init__.py
├── inspector.py     # HeartbeatInspector 巡检器
└── decision.py      # LLM 决策逻辑
```

**巡检器类** (`inspector.py`):

```python
class HeartbeatInspector:
    def __init__(self):
        self._running = False
        self._history_manager = get_history_manager()
        self._scheduled_jobs: dict[str, str] = {}  # persona_name -> job_id

    def start_for_persona(self, persona_name: str) -> bool:
        """为指定 persona 启动巡检任务"""

    def stop_for_persona(self, persona_name: str) -> bool:
        """为指定 persona 停止巡检任务"""

    def start_all(self) -> bool:
        """启动所有启用了定时巡检的 persona"""

    def stop(self) -> bool:
        """停止所有巡检任务"""

    async def _inspect_all_sessions_for_persona(self, persona_name: str):
        """巡检所有与指定 persona 相关的会话"""
```

### 6.3 定时任务配置

使用 `aps.py` 中的 `scheduler` 添加定时任务:

```python
job_id = f"ai_heartbeat_inspector_{persona_name}"
scheduler.add_job(
    func=self._inspect_all_sessions_for_persona,
    trigger="interval",
    minutes=inspect_interval,  # 5/10/15/30/60
    id=job_id,
    name=f"AI 定时巡检任务 - {persona_name}",
    replace_existing=True,
    kwargs={"persona_name": persona_name},
)
```

### 6.4 巡检流程

```
定时任务触发
    │
    ▼
_inspect_all_sessions_for_persona(persona_name)
    │
    ├── 1. 获取该 persona 的 scope 和 target_groups
    │
    ├── 2. 获取所有活跃会话
    │   └── sessions = history_manager.list_sessions()  # 返回 List[Event]
    │
    └── 3. 遍历每个会话
            │
            ├── _should_inspect_session(event, scope, target_groups, persona_name)
            │   ├── scope="disabled" -> 不巡检
            │   ├── scope="global" -> 巡检所有
            │   └── scope="specific" -> 只巡检 target_groups 中的群
            │
            ├── _pre_check_session(event) - 前置轻量级规则过滤
            │   ├── 无历史记录 -> 跳过
            │   ├── 最后消息来自 AI -> 跳过
            │   ├── 群已 1 小时不活跃 -> 跳过
            │   └── AI 最近已发言(防刷屏) -> 跳过
            │
            └── _inspect_session_with_semaphore(event, persona_name)
                    │
                    ├── 信号量控制 (Semaphore(5))
                    │
                    └── _inspect_session(event, persona_name)
                            │
                            ├── 1. 获取历史记录
                            │   └── history = _get_history(event)
                            │
                            ├── 2. 检查最近 AI 是否已发言 (防刷屏)
                            │   └── _has_recent_ai_response(history)
                            │
                            ├── 3. 获取 AI Session
                            │   └── ai_session = await get_ai_session_by_id()
                            │
                            ├── 4. LLM 决策 + 生成 (run_heartbeat)
                            │   └── run_heartbeat(event, history, ai_session)
                            │       ├── 阶段一：决策 (DECISION_PROMPT_TEMPLATE)
                            │       │   └── 返回 {should_speak, mood, context_hook}
                            │       └── 阶段二：生成发言 (PROACTIVE_MESSAGE_PROMPT)
                            │           └── 返回 (mood, message)
                            │
                            └── 5. 发送消息并记录
                                └── _send_proactive_message(event, user_id, message, reason)
                                    ├── _get_bot_for_session(event) - 获取 Bot 实例
                                    ├── _bot.target_send() - 发送消息
                                    └── history_manager.add_message(metadata={proactive: True})
```

### 6.5 LLM 决策 (`decision.py`)

**决策 Prompt** (`DECISION_PROMPT_TEMPLATE`):

```python
"""
{persona_text}
---

现在你独自看着群里的聊天记录，思考自己要不要说点什么。

【当前时间】
{current_time}

【群里最近发生的事】
{history_context}

---

做决定前，先问自己几件事：

- 现在几点？这个时间点，我这种人会在干嘛？会想开口吗？
- 群里最后一条消息是什么时候发的？现在算冷场吗？
- 大家聊的东西我有没有兴趣？或者有没有人需要我？
- 我上次说话是什么时候？有没有必要再说？

结合自己的性格做判断，不要为了说话而说话。

以严格 JSON 格式输出，禁止包含任何 Markdown 标记：
{{"should_speak": true 或 false, "mood": "此刻角色的内心状态，一句话，用第一人称", "context_hook": "如果决定说话，简述你打算接哪个话头或借什么由头；不说话则留空"}}
"""
```

**决策输出解析** (`_parse_decision_json`):

```python
# 容忍 Markdown 代码块包裹、首尾多余空白、字段缺失
clean = re.sub(r"```(?:json)?", "", response).strip()
data = json.loads(clean)

decision = {
    "should_speak": bool(data.get("should_speak", False)),
    "mood": str(data.get("mood", "")),
    "context_hook": str(data.get("context_hook", "")),
}
```

**生成发言 Prompt** (`PROACTIVE_MESSAGE_PROMPT`):

```python
"""
{persona_text}

---

【群里最近发生的事】
{history_context}

【此刻你的状态】
{mood}

---

你决定开口了。
直接输出你想说的话，不要任何前缀、引号或解释。
"""
```

**发言后处理** (`_strip_message_quotes`): 去除生成消息首尾可能出现的引号包裹。

### 6.6 防刷屏机制

`_has_recent_ai_response()` 检查最近 5 条消息:

```python
def _has_recent_ai_response(self, history: List[Any]) -> bool:
    """如果最近 5 条消息里 AI 已经开过口了，就不再发言，防刷屏"""
    for record in reversed(history[-5:]):
        if record.role == "assistant":
            if (record.metadata or {}).get("proactive", False):
                return True
    return False
```

**标记方式**: 主动发送的消息带有 `metadata={"proactive": True}` 标记。

### 6.7 设计缺陷与潜在问题

#### 6.7.1 定时巡检会引发"LLM Token 破产"与并发雪崩 (性能漏洞) ✅ 已修复

**问题所在**: 原定时任务会遍历所有活跃会话，针对每个会话调用 LLM 进行决策。

```python
# 原代码 - inspector.py: _inspect_all_sessions_for_persona
for session_key in sessions:
    await self._inspect_session(session_key, persona_name)  # 直接串行调用
```

**场景重现**:
- 如果机器人加了 100 个群，inspect_interval 设为 5 分钟
- 每隔 5 分钟，系统会瞬间向 OpenAI 发起 100 次并发请求
- 仅为了询问"我要不要说话？"

**后果**:
1. 瞬间触发 API 厂商的 Rate Limit（并发限制），导致大量报错
2. 如果对话历史很长，这 100 次的输入 Token 消耗极为恐怖
3. 钱包会被快速抽干

**修复方案** (已实现): 引入前置轻量级规则过滤 + 并发控制：

```python
# inspector.py
MAX_CONCURRENT_LLM_CALLS = 5  # 信号量限制并发
INACTIVE_THRESHOLD_HOURS = 1   # 冷场阈值（1小时不活跃则跳过）

async def _inspect_all_sessions_for_persona(self, persona_name: str) -> None:
    # 前置规则过滤
    for session_key in sessions:
        should_check, skip_reason = self._pre_check_session(session_key)
        if not should_check:
            continue  # 快速跳过，避免 LLM 调用

        # 使用信号量控制并发
        task = asyncio.create_task(
            self._inspect_session_with_semaphore(session_key, persona_name)
        )
        tasks.append(task)

    # 带超时保护
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=300)

def _pre_check_session(self, session_key) -> Tuple[bool, str]:
    """前置轻量级规则过滤"""
    # 检查最后消息是否来自 AI
    if last_message.role == "assistant":
        return False, "最后消息来自 AI"

    # 检查冷场时间
    if time_diff > timedelta(hours=INACTIVE_THRESHOLD_HOURS):
        return False, f"群已 {INACTIVE_THRESHOLD_HOURS} 小时不活跃"

    # 检查最近是否已发言（防刷屏）
    if self._has_recent_ai_response(history):
        return False, "AI 最近已发言"

    return True, ""
```

#### 6.7.2 _Bot 与 Bot 混淆导致 bot_self_id 缺失 (致命错误) ✅ 已修复

**问题所在**: `_get_bot_for_session()` 错误地用 `bot_id`（平台 ID）去 `gss.active_bot` 查找，但 `gss.active_bot` 的 key 是 `WS_BOT_ID`（WS 连接 ID），两者不匹配。

```python
# 原错误代码 - inspector.py: _get_bot_for_session
async def _get_bot_for_session(self, session_key: SessionKey) -> Optional[Any]:
    from gsuid_core.gss import gss
    # bot_id 是平台 ID，但 gss.active_bot 的 key 是 WS 连接 ID
    return gss.active_bot[bot_id]  # 永远找不到
```

**原因分析**:
- `Event.bot_id` = **平台 ID**（如 QQ 号），是 Session 标识
- `Event.WS_BOT_ID` = **WS 连接 ID**（`_Bot.bot_id`），是 `gss.active_bot` 的 key
- `gss.active_bot` 的 key 是 `_Bot.bot_id`（WS 连接 ID），不是 `bot_id`（平台 ID）

**修复方案**:

```python
async def _get_bot_for_session(self, event: Event) -> Optional["_Bot"]:
    """获取用于发送消息的 _Bot 实例"""
    from gsuid_core.gss import gss

    # 方式1（最优先）：直接用 WS_BOT_ID 查找 WS 连接
    if event.WS_BOT_ID and event.WS_BOT_ID in gss.active_bot:
        return gss.active_bot[event.WS_BOT_ID]

    # 方式2（兜底）：遍历历史消息的 metadata 尝试找 bot_id
    bot_id: Optional[str] = None
    history = self._history_manager._histories.get(event, [])
    for record in reversed(history):
        metadata = record.metadata or {}
        if _bot_id := metadata.get("bot_id"):
            bot_id = _bot_id
            break

    if bot_id and bot_id in gss.active_bot:
        return gss.active_bot[bot_id]

    # 方式3（最后的兜底）：返回任意一个可用的 _Bot
    if gss.active_bot:
        return list(gss.active_bot.values())[0]
    return None
```

**修复要点**:
1. 参数从 `session_key` 改为 `event: Event`，可直接访问 `WS_BOT_ID`
2. 三级查找策略：WS_BOT_ID 直接查找 → 历史 metadata 兜底 → 任意可用 Bot
3. 全部使用 `gss.active_bot` 而非 `Bot.instances`

---

**改进后的巡检流程**:

```
定时任务触发
    │
    ▼
前置规则过滤
    ├── 无历史记录? → 直接跳过
    ├── 最后消息来自 AI? → 直接跳过
    ├── 群已 1+ 小时不活跃? → 直接跳过
    ├── AI 最近已发言? → 直接跳过
    └── 通过 → LLM 决策 (Semaphore(5) 控制并发)
```

---

## 7. Scheduled Task 定时任务系统

### 7.1 概述

Scheduled Task 模块提供定时/循环 AI 任务能力，允许主 Agent 预约未来某个时间执行的复杂任务，或设定循环执行的任务。当时间到达时，系统会加载当时的 persona 和 session，使用与主 Agent 一致的语气执行任务。

**设计理念**：现代 AI 框架（如 AutoGen, LangChain）处理这类问题的标准做法是：
- **Scheduled Prompt（定时提示词）+ 唤醒 Sub-Agent（子智能体）**

**新增功能**（v2.0）：
- 支持**循环任务**：按固定间隔重复执行
- 支持**任务管理**：增删改查启停
- 内置**安全限制**：防止无限循环和资源耗尽

### 7.2 模块结构

```
gsuid_core/ai_core/
├── buildin_tools/
│   ├── __init__.py
│   └── scheduler.py     # 独立工具函数：add_once_task / add_interval_task / list_scheduled_tasks 等
└── scheduled_task/
    ├── __init__.py      # 模块初始化（导入 startup 注册回调）
    ├── models.py        # 数据库模型 AIScheduledTask
    ├── executor.py      # execute_scheduled_task 执行器 + reload_pending_tasks + cleanup_completed_tasks
    ├── scheduler.py     # APScheduler 任务注册辅助
    ├── startup.py       # @on_core_start / @on_core_shutdown 回调
    └── README.md        # 设计文档
```

### 7.3 任务类型

#### 7.3.1 一次性任务 (once)

在指定时间点执行一次，执行后状态变为 `executed`。

**适用场景**：
- "明天早上 6 点叫我起床"
- "周五晚上 8 点提醒我交报告"

#### 7.3.2 循环任务 (interval)

按固定间隔重复执行，达到最大执行次数后自动结束。

**适用场景**：
- "每半小时帮我查一下股市行情"
- "每天早上 8 点给我发天气预报"

**循环间隔单位**：
- `minutes` - 分钟
- `hours` - 小时
- `days` - 天

### 7.4 核心组件

#### 7.4.1 数据库模型 - `AIScheduledTask`

**文件位置**: [`gsuid_core/ai_core/scheduled_task/models.py`](gsuid_core/ai_core/scheduled_task/models.py)

```python
class AIScheduledTask(BaseBotIDModel, table=True):
    """定时 AI 任务模型"""

    task_id: str             # 唯一ID
    task_type: str           # 任务类型：once=一次性，interval=循环任务

    # Event 相关字段（用于发送消息）
    user_id: str             # 用户ID
    group_id: Optional[str]  # 群ID（私聊则为空）
    bot_self_id: str         # 机器人自身ID
    user_type: str           # 用户类型 (group/direct)
    WS_BOT_ID: Optional[str] # WS机器人ID

    # Persona 相关字段（用于执行时加载 persona）
    persona_name: Optional[str]  # Persona 名称
    session_id: str           # Session ID

    # 一次性任务字段
    trigger_time: Optional[datetime]  # 触发时间

    # 任务相关字段
    task_prompt: str         # 任务描述

    status: str              # pending / executed / failed / cancelled / paused

    created_at: datetime     # 创建时间
    executed_at: Optional[datetime]  # 执行时间

    result: Optional[str]    # 执行结果
    error_message: Optional[str]  # 错误信息

    # 循环任务字段
    interval_seconds: Optional[int]  # 间隔秒数
    max_executions: Optional[int]   # 最大执行次数
    current_executions: Optional[int]  # 当前执行次数
    start_time: Optional[datetime]  # 开始时间
    next_run_time: Optional[datetime]  # 下次执行时间
```

#### 7.4.2 工具函数 - 独立工具函数

**文件位置**: [`gsuid_core/ai_core/buildin_tools/scheduler.py`](gsuid_core/ai_core/buildin_tools/scheduler.py)

每个 action 对应一个独立的 AI 工具函数。其中"创建"入口（`add_once_task` / `add_interval_task`）
注册为 `@ai_tools(category="self")`，属保底工具池常驻加载——因其触发高度口语化（"每天下午三点半
推送新闻"），向量检索难以命中；"管理"类工具（增删查改启停中的查改启停）注册为
`@ai_tools(category="common")`，由查询工具池按 query 向量检索按需加载。

**工具列表**：

| 工具函数 | category | 说明 | 必需参数 |
|----------|----------|------|----------|
| `add_once_task` | `self` | 添加一次性定时任务 | run_time, task_prompt |
| `add_interval_task` | `self` | 添加循环任务 | interval_value, task_prompt, interval_type, max_executions |
| `list_scheduled_tasks` | `common` | 列出当前用户的所有定时任务 | - |
| `query_scheduled_task` | `common` | 查询指定任务的详细信息 | task_id |
| `modify_scheduled_task` | `common` | 修改定时任务 | task_id, task_prompt?, max_executions? |
| `cancel_scheduled_task` | `common` | 取消定时任务 | task_id |
| `pause_scheduled_task` | `common` | 暂停循环任务 | task_id |
| `resume_scheduled_task` | `common` | 恢复已暂停的循环任务 | task_id |

**安全限制**（全局常量）：

```python
MAX_PENDING_TASKS_PER_USER = 20  # 单用户最多 20 个待执行任务
MAX_EXECUTION_LIMIT = 10         # 循环任务最大执行次数为 10 次
MIN_INTERVAL_SECONDS = 300       # 循环任务最小间隔为 5 分钟（300秒）
```

**使用示例**：

```python
# 添加一次性任务
await add_once_task(
    ctx,
    run_time="2024-05-15 06:30:00",
    task_prompt="查询英伟达(NVDA)的实时股价和最新新闻",
)

# 添加循环任务（每30分钟执行一次）
await add_interval_task(
    ctx,
    interval_value=30,
    interval_type="minutes",
    task_prompt="帮我关注股市行情",
    max_executions=10,  # 最多执行10次
)

# 列出所有任务
await list_scheduled_tasks(ctx)

# 取消任务
await manage_scheduled_task(ctx, action="cancel", task_id="xxx")

# 修改任务
await manage_scheduled_task(
    ctx,
    action="modify",
    task_id="xxx",
    task_prompt="新的任务描述",
)

# 暂停任务
await manage_scheduled_task(ctx, action="pause", task_id="xxx")

# 恢复任务
await manage_scheduled_task(ctx, action="resume", task_id="xxx")
```

#### 7.4.3 执行器 - `execute_scheduled_task`

**文件位置**: [`gsuid_core/ai_core/scheduled_task/executor.py`](gsuid_core/ai_core/scheduled_task/executor.py)

被 APScheduler 触发时调用的统一执行器。

```python
async def execute_scheduled_task(task_id: str):
    # 1. 从数据库读取任务信息
    task = await AIScheduledTask.select_rows(task_id=task_id)

    # 2. 构建 Event 对象
    ev = Event(...)

    # 3. 使用 get_ai_session 加载 persona 和 session
    session = await get_ai_session(ev)

    # 4. 通过 session 执行任务
    result = await session.run(user_message=..., bot=bot_instance, ev=ev)

    # 5. 根据任务类型处理
    #    - 一次性任务：状态变为 executed
    #    - 循环任务：更新 current_executions，检查是否达到最大次数

    # 6. 记录触发方式
    statistics_manager.record_trigger(trigger_type="scheduled")

    # 7. 将结果推送给用户
    await bot_instance.send(result)
```

### 7.5 安全限制

为防止恶意用户创建无限循环任务或耗尽系统资源，系统内置以下安全限制：

| 限制项 | 默认值 | 说明 |
|--------|--------|------|
| 单用户最大待执行任务数 | 20 | 防止创建过多任务 |
| 循环任务最大执行次数 | 10 | 防止无限循环 |
| 循环任务最小间隔 | 5 分钟 | 防止过于频繁执行 |

**特殊处理**：
- 即使用户要求"无限循环"，系统也会强制设置 `max_executions=10`
- 达到最大执行次数后，任务状态自动变为 `executed`
- 单用户待执行任务数超限时，添加任务操作会被拒绝

### 7.6 架构设计

```
┌─────────────────────────────────────────────────────────────────┐
│                         用户请求                                  │
│   "每隔半小时帮我查一下英伟达的股价"                              │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      主 Agent (LLM)                              │
│     识别意图 → 提取间隔和任务 → 调用 add_once_task / add_interval_task │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              buildin_tools/scheduler.py                           │
│            独立工具函数 (add_once_task / add_interval_task)       │
│  1. 安全检查：用户任务数、最大次数、最小间隔                        │
│  2. 存入数据库 AIScheduledTask（包含循环任务字段）                │
│  3. 注册到 APScheduler (date/interval 触发器)                    │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      数据库 (持久化)                              │
│  任务状态: pending / paused / executed / failed / cancelled    │
│  循环任务: interval_seconds, max_executions, current_executions  │
└─────────────────────────────────────────────────────────────────┘

                          ...

┌─────────────────────────────────────────────────────────────────┐
│              间隔到达 → APScheduler 触发                         │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              scheduled_task/executor.py                           │
│              execute_scheduled_task (执行器)                      │
│  1. 从数据库读取任务信息                                          │
│  2. 使用 get_ai_session(event) 加载 persona 和 session           │
│  3. 向 session 发送任务消息                                      │
│  4. 更新 current_executions                                      │
│  5. 检查是否达到最大次数                                          │
│     - 未达到：计算下次执行时间，重新注册 APScheduler              │
│     - 已达到：状态变为 executed，停止调度                          │
│  6. 记录触发方式为 "scheduled"                                  │
│  7. 将结果推送给用户                                              │
└─────────────────────────────────────────────────────────────────┘
```

### 7.7 使用流程

**场景：用户设定循环任务**

1. **用户输入**
   ```
   "每隔半小时帮我查一下英伟达的股价，有异常波动时提醒我"
   ```

2. **主 Agent 思考**
   - 意图识别发现这是一个循环任务
   - 提取间隔：`interval_type="minutes", interval_value=30`
   - 提炼提示词：查询英伟达(NVDA)的实时股价和最新新闻
   - 检查安全限制：max_executions 默认为 10

3. **调用工具**
   主 Agent 调用 `add_interval_task(interval_value=30, ...)`，系统：
   - 验证安全限制（用户任务数、最大次数、最小间隔）
   - 将任务存入数据库（task_type="interval"）
   - 往 APScheduler 注册了一个 interval 触发器

4. **定时触发**（每 30 分钟）
   APScheduler 触发 `execute_scheduled_task`

5. **执行任务**
   - `execute_scheduled_task` 使用 `get_ai_session(ev)` 加载 persona
   - 保持与主 Agent 一致的语气和风格
   - 调用 web_search 等工具完成任务
   - 更新 `current_executions = 1`
   - 检查是否达到 `max_executions=10`
   - 如果未达到，计算下次执行时间，重新注册 APScheduler
   - 记录触发方式 `scheduled`

6. **推送结果**
   系统把 AI 生成的结果，主动发给用户

7. **循环往复**
   - 第 10 次执行后，`current_executions >= max_executions`
   - 任务状态变为 `executed`，调度器不再触发

### 7.8 任务状态机

```
                    ┌─────────────┐
                    │   创建任务   │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
         ┌─────────│   pending   │─────────┐
         │         └──────┬──────┘         │
         │                │                │
         ▼                ▼                ▼
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│  cancelled  │   │   paused    │   │  executed   │
│  (手动取消)  │   │  (仅循环任务) │   │  (执行完毕)  │
└─────────────┘   └──────┬──────┘   └─────────────┘
                         │                ▲
                         │                │
                         │         ┌──────┴──────┐
                         │         │             │
                         │         ▼             │
                         │   ┌───────────┐       │
                         └──▶│  resume   │───────┘
                             └───────────┘
                             (恢复 pending)
```

### 7.9 重启恢复

在系统启动时，调用 `reload_pending_tasks()` 可以重新加载所有待执行的任务：

```python
from gsuid_core.ai_core.scheduled_task import reload_pending_tasks

# 在启动流程中
await reload_pending_tasks()
```

此函数会：
1. 查询所有 `pending` 状态的任务
2. 对于**一次性任务**：
   - 已过期则立即执行
   - 未过期则重新注册到 APScheduler
3. 对于**循环任务**：
   - 检查 `next_run_time`，已到期则立即执行
   - 未到期则重新注册到 APScheduler

### 7.10 触发方式统计

定时任务的触发方式记录为 `scheduled`，与现有触发方式一致：

| 触发方式 | 说明 | 记录位置 |
|---------|------|----------|
| `mention` | 用户@机器人触发 | handler.py |
| `keyword` | 关键词触发 | - |
| `heartbeat` | 心跳巡检触发 | heartbeat/inspector.py |
| `scheduled` | 定时/循环任务触发 | scheduled_task/executor.py |

### 7.11 启用方式

在 `buildin_tools/__init__.py` 中导入即可：
```python
from gsuid_core.ai_core.buildin_tools.scheduler import (
    add_once_task,
    add_interval_task,
    list_scheduled_tasks,
    query_scheduled_task,
    modify_scheduled_task,
    cancel_scheduled_task,
    pause_scheduled_task,
    resume_scheduled_task,
)
```

### 7.12 WebConsole API

前端可以通过 WebConsole API 管理 AI 定时任务。

**文件位置**: [`gsuid_core/webconsole/ai_scheduled_task_api.py`](gsuid_core/webconsole/ai_scheduled_task_api.py)

| 方法 | 端点 | 功能 |
|------|------|------|
| GET | `/api/ai/scheduled_tasks` | 获取任务列表（支持筛选） |
| GET | `/api/ai/scheduled_tasks/{task_id}` | 获取任务详情 |
| POST | `/api/ai/scheduled_tasks` | 创建任务 |
| PUT | `/api/ai/scheduled_tasks/{task_id}` | 修改任务 |
| DELETE | `/api/ai/scheduled_tasks/{task_id}` | 删除任务 |
| POST | `/api/ai/scheduled_tasks/{task_id}/pause` | 暂停任务 |
| POST | `/api/ai/scheduled_tasks/{task_id}/resume` | 恢复任务 |
| GET | `/api/ai/scheduled_tasks/stats/overview` | 获取统计概览 |

详细 API 文档见 [API.md](../gsuid_core/webconsole/API.md#21-ai-scheduled-task-api---apiaischeduled_tasks)

---

## 8. WebConsole API 与配置热重载

### 8.0 MCP 配置 API

**文件位置**: [`gsuid_core/webconsole/mcp_config_api.py`](gsuid_core/webconsole/mcp_config_api.py)

MCP 配置 API 允许用户通过前端自由管理 MCP 服务器配置，支持增删改查、启用/禁用、工具发现、JSON 导入和热重载。

| 方法 | 端点 | 功能 |
|------|------|------|
| GET | `/api/ai/mcp/list` | 获取所有 MCP 配置列表（含 tools 字段） |
| GET | `/api/ai/mcp/{config_id}` | 获取指定配置详情 |
| POST | `/api/ai/mcp` | 创建新 MCP 配置 |
| PUT | `/api/ai/mcp/{config_id}` | 更新 MCP 配置 |
| DELETE | `/api/ai/mcp/{config_id}` | 删除 MCP 配置 |
| POST | `/api/ai/mcp/{config_id}/toggle` | 切换启用/禁用状态 |
| POST | `/api/ai/mcp/reload` | 热重载所有配置并重新注册工具 |
| GET | `/api/ai/mcp/{config_id}/tools` | 从已配置的 MCP 服务器发现工具 |
| POST | `/api/ai/mcp/tools/discover` | 从临时配置发现工具（不保存） |
| POST | `/api/ai/mcp/tools/import` | 从 JSON 导入 MCP 配置（支持 `mcpServers` 格式） |
| GET | `/api/ai/mcp/presets` | 获取 MCP 预设列表（MiniMax/Firecrawl/Tavily/GitHub/Filesystem） |

**创建配置请求体**:

```json
{
    "name": "MiniMax",
    "command": "uvx",
    "args": ["minimax-coding-plan-mcp"],
    "env": {"MINIMAX_API_KEY": "your_key"},
    "enabled": true,
    "register_as_ai_tools": false,
    "tools": [
        {
            "name": "web_search",
            "description": "Web search tool",
            "parameters": {
                "query": {"type": "string", "required": true}
            }
        }
    ]
}
```

**JSON 导入**: `POST /api/ai/mcp/tools/import` 支持导入标准 MCP JSON 格式（含 `mcpServers` 字段），自动解析配置、连接服务器发现工具并创建配置。

**热重载**: `POST /api/ai/mcp/reload` 会清除已注册的 MCP 工具，重新加载配置文件，并重新连接所有启用的 MCP 服务器注册工具。

> **详细文档**: 见 [MCP_TOOL_INTEGRATION_CHANGELOG.md](./MCP_TOOL_INTEGRATION_CHANGELOG.md)（变更记录）、[MCP_TOOL_PERMISSIONS.md](./MCP_TOOL_PERMISSIONS.md)（权限配置）、[MCP_SERVER.md](./MCP_SERVER.md)（MCP Server 对外暴露）

### 8.0.1 嵌入模型配置 API

**文件位置**: [`gsuid_core/webconsole/embedding_config_api.py`](gsuid_core/webconsole/embedding_config_api.py)

嵌入模型配置 API 用于管理嵌入模型提供方（local/openai）及其配置。支持在本地 fastembed 模型和 OpenAI 兼容格式的远程 API 之间自由切换。

| 方法 | 端点 | 功能 |
|------|------|------|
| GET | `/api/embedding_config/provider` | 获取当前嵌入模型提供方 |
| POST | `/api/embedding_config/provider` | 设置嵌入模型提供方 |
| GET | `/api/embedding_config/local` | 获取本地嵌入模型配置 |
| POST | `/api/embedding_config/local` | 保存本地嵌入模型配置 |
| GET | `/api/embedding_config/openai` | 获取 OpenAI 嵌入模型配置 |
| POST | `/api/embedding_config/openai` | 保存 OpenAI 嵌入模型配置 |
| GET | `/api/embedding_config/summary` | 获取嵌入模型配置摘要（一次性获取所有信息） |

**切换提供方请求体**:
```json
{
    "provider": "openai"
}
```

**OpenAI 嵌入配置请求体**:
```json
{
    "base_url": "https://api.siliconflow.cn/v1",
    "api_key": ["sk-xxx"],
    "embedding_model": "BAAI/bge-m3"
}
```

> **前端建议**：使用 `GET /api/embedding_config/summary` 一次性获取所有嵌入模型配置信息，根据 `provider` 字段决定显示哪一组配置表单。

### 8.1 Persona API 端点

**文件位置**: [`gsuid_core/webconsole/persona_api.py`](gsuid_core/webconsole/persona_api.py)

| 方法 | 端点 | 功能 |
|------|------|------|
| GET | `/api/persona/list` | 获取所有 Persona 列表 |
| GET | `/api/persona/{persona_name}` | 获取 Persona 详情 |
| GET | `/api/persona/{persona_name}/avatar` | 获取头像 |
| GET | `/api/persona/{persona_name}/image` | 获取立绘 |
| GET | `/api/persona/{persona_name}/audio` | 获取音频 |
| POST | `/api/persona/{persona_name}/avatar` | 上传头像 |
| POST | `/api/persona/{persona_name}/image` | 上传立绘 |
| POST | `/api/persona/{persona_name}/audio` | 上传音频 |
| POST | `/api/persona/{persona_name}` | 创建 Persona |
| DELETE | `/api/persona/{persona_name}` | 删除 Persona |
| GET | `/api/persona/{persona_name}/config` | 获取配置 |
| PUT | `/api/persona/{persona_name}/config` | 更新配置 |

### 7.2 配置更新 API

**端点**: `PUT /api/persona/{persona_name}/config`

**请求体**:

```json
{
    "scope": "global",           // optional
    "target_groups": ["123456"],  // optional
    "ai_mode": ["提及应答", "定时巡检"],  // optional
    "inspect_interval": 30,       // optional
    "keywords": ["关键词1", "关键词2"]  // optional
}
```

**响应示例**:

```json
{
    "status": 0,
    "msg": "已更新: scope: global, ai_mode: ['提及应答', '定时巡检']",
    "data": {
        "ai_mode": ["提及应答", "定时巡检"],
        "scope": "global",
        "target_groups": [],
        "inspect_interval": 30,
        "keywords": []
    }
}
```

### 7.3 配置热重载机制

**配置写入流程** (`gs_config.py`):

```python
class StringConfig:
    def set_config(self, key: str, value) -> bool:
        if key in self.config_list:
            # 1. 更新内存中的值
            self.config[key].data = value

            # 2. 立即持久化到磁盘
            self.write_config()
            return True
        return False

    def write_config(self):
        """将配置写回磁盘文件"""
        with open(self.CONFIG_PATH, "w", encoding="UTF-8") as f:
            json.dump(self.config, f, indent=4, ensure_ascii=False)
```

**热重载效果**:

| 配置项 | 何时生效 | 说明 |
|--------|----------|------|
| `scope` | 立即 | 下次 `get_persona_for_session()` 时生效 |
| `target_groups` | 立即 | 下次会话匹配时生效 |
| `ai_mode` | 立即 | 下次消息处理时生效 |
| `inspect_interval` | 需重启巡检 | 自动重启该 persona 的巡检任务 |
| `keywords` | 立即 | 下次消息处理时生效 |

### 7.4 巡检间隔更新特殊处理

在 `persona_api.py` 的 `update_persona_config()` 中:

```python
# 更新 inspect_interval（如果提供）
if "inspect_interval" in data:
    # ... 验证 ...
    success, msg = persona_config_manager.set_inspect_interval(...)

    # 如果该 persona 已启用定时巡检，重新启动以应用新间隔
    if "定时巡检" in config.get_config("ai_mode").data:
        inspector = get_inspector()
        inspector.stop_for_persona(persona_name)  # 停止旧任务
        inspector.start_for_persona(persona_name)  # 启动新任务
```

### 7.5 AI 模式更新特殊处理

```python
# 更新 ai_mode（如果提供）
if "ai_mode" in data:
    # ... 验证 ...
    success, msg = persona_config_manager.set_ai_mode(...)

    # 如果启用了定时巡检，启动巡检任务
    if "定时巡检" in ai_mode:
        from gsuid_core.ai_core.heartbeat import start_heartbeat_inspector
        start_heartbeat_inspector()  # 启动所有启用了定时巡检的 persona
```

### 7.6 配置热重载限制

**不会热重载的配置**:

| 配置项 | 原因 | 解决方案 |
|--------|------|----------|
| `model_name` | 在 `configs/models.py` 的 `get_openai_chat_model()` 中动态获取 | 修改后下次创建 Session 即生效 |
| Session system_prompt | Session 创建后通过 mtime 检测实现热重载 | 修改 persona 文件后自动重载（见 5.6.2） |

> **注意**：原文档称 `model_name` 需重启服务，但实际代码中 `create_agent()` 每次都通过 `get_openai_chat_model()` 动态获取模型，因此修改配置后新创建的 Session 会立即使用新模型。

---

## 8. AI Statistics 统计系统

### 8.1 概述

AI Statistics 模块负责收集、聚合和持久化 AI 模块的各类统计数据，支持前端展示面板的数据需求。

**文件位置**: [`gsuid_core/ai_core/statistics/`](gsuid_core/ai_core/statistics/)

```
statistics/
├── __init__.py          # 模块导出（StatisticsManager, statistics_manager, init_ai_core_statistics）
├── models.py            # 数据库模型（7个表）
├── manager.py           # 统计管理器（单例模式）
├── dataclass_models.py  # 内存数据结构（BotState, LatencyStats, TokenUsage）
└── startup.py           # @on_core_start / @on_core_shutdown / 零点重置回调
```

### 8.2 统计数据分类

#### 8.2.1 Token 消耗统计

| 统计项 | 说明 |
|--------|------|
| 分模型统计 | 统计不同模型（如 GPT-4, GPT-3.5, DeepSeek）的 Input 和 Output Token |

#### 8.2.2 Session 内存占用

| 统计项 | 说明 |
|--------|------|
| 活跃 Session 总数 | 当前内存中活跃的 Session 总数 |
| 平均消息数 | 平均每个 Session 包含的消息条数（监控 deque 的填充率） |

#### 8.2.3 活跃度与受欢迎程度

| 统计项 | 说明 |
|--------|------|
| Persona 排行榜 | 统计各个 Persona（人设）的调用次数 |
| 触发方式占比 | @机器人 触发 vs 关键词 触发 vs 主动巡检 触发 vs 定时任务 触发 |
| 用户/群组活跃榜 | 哪个群是"话痨群"？哪个用户是"深度使用者"？ |

#### 8.2.4 系统性能与质量统计

| 统计项 | 说明 |
|--------|------|
| P95 延迟 | 95% 的请求在多少秒内完成 |
| 环节耗时分析 | 分类器耗时、RAG 检索耗时、LLM 生成耗时 |
| 意图分布 | 统计"闲聊"、"工具"、"问答"各自的占比 |
| 失败率/错误码统计 | API 超时次数、Rate Limit 次数、网络错误次数、使用限制次数、Agent执行错误次数 |

#### 8.2.5 Heartbeat 巡检专项统计

| 统计项 | 说明 |
|--------|------|
| should_speak 次数 | AI 判定应该发言的次数 |
| should_not_speak 次数 | AI 判定不应该发言的次数 |

#### 8.2.6 RAG 知识库效果统计

| 统计项 | 说明 |
|--------|------|
| 检索命中率 | 统计多少比例的问题成功从知识库获取了参考资料 |
| 检索未命中率 | 统计多少比例的问题未能从知识库获取参考资料 |
| 知识库引用分布 | 哪些文档/知识点被 AI 引用的次数最多 |

### 8.3 数据库模型

#### AIRAGMissStatistics - RAG 未命中统计表

```python
class AIRAGMissStatistics(BaseIDModel, table=True):
    """RAG 未命中统计表"""
    date: str                    # 统计日期 (YYYY-MM-DD)
    hit_count: int              # 命中次数
    miss_count: int             # 未命中次数
```

#### AIRAGDocumentStatistics - RAG 文档命中统计表

```python
class AIRAGDocumentStatistics(BaseIDModel, table=True):
    """RAG 文档命中统计表"""
    document_name: str          # 文档名称
    hit_count: int              # 命中次数
```

#### AIDailyStatistics - 每日 AI 统计数据表

```python
class AIDailyStatistics(BaseIDModel, table=True):
    """每日 AI 统计数据表（全局统计，无 bot_id）"""
    date: str                    # 统计日期 (YYYY-MM-DD)
    total_input_tokens: int     # 总输入Token
    total_output_tokens: int     # 总输出Token
    avg_latency: float          # 平均延迟(秒)
    p95_latency: float          # P95延迟(秒)
    intent_chat_count: int      # 闲聊次数
    intent_tool_count: int      # 工具次数
    intent_qa_count: int        # 问答次数
    api_timeout_count: int       # API超时次数
    api_rate_limit_count: int   # RateLimit次数
    api_529_count: int          # API负载过高次数
    api_network_error_count: int # 网络错误次数
    api_usage_limit_count: int   # 使用限制次数
    api_agent_error_count: int  # Agent执行错误次数
    active_session_count: int   # 活跃Session数
    avg_messages_per_session: float  # 平均每Session消息数
    trigger_mention_count: int   # @触发次数
    trigger_keyword_count: int   # 关键词触发次数
    trigger_heartbeat_count: int # 主动巡检触发次数
    trigger_scheduled_count: int  # 定时任务触发次数
    # 记忆系统统计
    memory_observations: int    # 记忆观察入队数
    memory_ingestions: int      # 记忆摄入完成数
    memory_ingestion_errors: int # 记忆摄入失败数
    memory_retrievals: int      # 记忆检索请求数
    memory_entities_created: int # 新建Entity数
    memory_edges_created: int   # 新建Edge数
    memory_episodes_created: int # 新建Episode数
    created_at: int             # 创建时间戳
    updated_at: int             # 更新时间戳
```

#### AITokenUsageByModel - 按模型分组的 Token 消耗

```python
class AITokenUsageByModel(BaseIDModel, table=True):
    """按模型分组的 Token 消耗统计（全局统计）"""
    date: str
    model_name: str              # 模型名称
    input_tokens: int
    output_tokens: int
    # 唯一约束: (date, model_name)
```

#### AITokenUsageByType - 按使用类型分组的 Token 消耗

```python
class AITokenUsageByType(BaseIDModel, table=True):
    """按使用类型分组的 Token 消耗统计（全局统计）"""
    date: str
    chat_type: str               # 消耗类型（Chat/SubAgent/BuildPersona等）
    input_tokens: int
    output_tokens: int
    # 唯一约束: (date, chat_type)
```

#### AIHeartbeatMetrics - Heartbeat 巡检统计

```python
class AIHeartbeatMetrics(BaseIDModel, table=True):
    """Heartbeat 巡检详细指标（全局统计）"""
    date: str                    # 统计日期
    group_id: str                # 群组ID
    should_speak_count: int      # 应该发言次数
    should_not_speak_count: int  # 不应该发言次数
    # 唯一约束: (date, group_id)
```

#### AIGroupUserActivityStats - 群组/用户活跃统计

```python
class AIGroupUserActivityStats(BaseIDModel, table=True):
    """群组/用户活跃统计（全局统计）"""
    date: str
    group_id: str
    user_id: str
    ai_interaction_count: int   # AI互动次数
    message_count: int          # 消息总数
    # 唯一约束: (date, group_id, user_id)
```

### 8.4 每日数据持久化机制

#### 8.4.1 启动时

```python
# statistics/startup.py
@on_core_start
async def init_ai_core_statistics():
    """初始化AI Core的Session管理器和定时巡检"""
    # 启动 AISessionRegistry 的空闲清理任务
    registry = get_ai_session_registry()
    await registry.start_cleanup_loop()

    # 检查AI总开关，仅在启用时启动定时巡检
    if ai_config.get_config("enable").data:
        start_heartbeat_inspector()
    else:
        logger.info("🧠 [AI] AI总开关已关闭，跳过定时巡检启动")

    statistics_manager._today = datetime.now().strftime("%Y-%m-%d")
    await statistics_manager._load_today_data_from_db()  # 从数据库加载今日数据
```

> **注意**: 所有 AI 相关模块的 `@on_core_start` 钩子都增加了 `enable_ai` 总开关检查。
> 当 AI 总开关关闭时，以下模块的初始化会被跳过：
> - `rag/startup.py` - RAG 模块
> - `persona/startup.py` - Persona 默认角色
> - `memory/startup.py` - 记忆系统
> - `statistics/startup.py` - 定时巡检（Heartbeat）
> - `scheduled_task/startup.py` - 定时任务加载
> - `mcp/startup.py` - MCP 工具注册
> - `mcp/server.py` - MCP Server 启动

#### 8.4.2 关闭时

```python
# statistics/startup.py
@on_core_shutdown
async def shutdown_ai_core_statistics():
    """关闭AI Core统计管理器"""
    await statistics_manager._persist_all_stats_to_db()  # 持久化当前数据到数据库
```

#### 8.4.3 零点自动重置

统计系统使用 APScheduler 的 cron 定时任务实现每日零点重置：

```python
# statistics/startup.py
@scheduler.scheduled_job("cron", hour=0, minute=0)
async def _scheduled_ai_core_reset():
    """每日零点重置"""
    await statistics_manager._persist_all_stats_to_db()
    statistics_manager._reset_daily_counters()
    today = datetime.now().strftime("%Y-%m-%d")
    statistics_manager._today = today  # 更新当前日期
    logger.success(f"📊 [StatisticsManager] 每日重置完成，新日期: {today}")

# statistics/manager.py
@scheduler.scheduled_job("cron", minute="*/30")
async def _persist_loop():
    """每30分钟持久化一次统计数据"""
    await statistics_manager._persist_all_stats_to_db()
    logger.info("📊 [StatisticsManager] 每30分钟定时持久化完成")
```

**定时任务说明**：
| 任务 | 触发条件 | 功能 |
|------|----------|------|
| `_scheduled_ai_core_reset` | `cron, hour=0, minute=0` | 每日零点持久化数据并重置计数器 |
| `_persist_loop` | `cron, minute=*/30` | 每30分钟持久化当前统计数据到数据库 |

### 8.5 前端 API 接口

统计模块提供以下 RESTful API（详见 API.md 第 20 节）：

| API 端点 | 说明 |
|----------|------|
| `GET /api/ai/statistics/summary` | 获取统计数据摘要 |
| `GET /api/ai/statistics/token-by-model` | 获取按模型分组的 Token 消耗 |
| `GET /api/ai/statistics/persona-leaderboard` | 获取 Persona 排行榜 |
| `GET /api/ai/statistics/active-users` | 获取活跃用户/群组排行 |
| `GET /api/ai/statistics/trigger-distribution` | 获取触发方式占比 |
| `GET /api/ai/statistics/intent-distribution` | 获取意图分布统计 |
| `GET /api/ai/statistics/errors` | 获取错误统计 |
| `GET /api/ai/statistics/heartbeat` | 获取 Heartbeat 巡检统计 |
| `GET /api/ai/statistics/rag` | 获取 RAG 知识库效果统计（全局） |
| `GET /api/ai/statistics/rag/documents` | 获取 RAG 文档命中统计（全局） |
| `GET /api/ai/statistics/history` | 获取历史统计数据 |

### 8.6 使用示例

```python
from gsuid_core.ai_core.statistics import statistics_manager

# 直接使用统计管理器单例（已全局初始化）

# 记录 Token 使用（chat_type 区分 Chat/SubAgent/BuildPersona 等）
statistics_manager.record_token_usage(
    model_name="gpt-4",
    chat_type="chat",
    input_tokens=1000,
    output_tokens=500,
)

# 记录响应延迟
statistics_manager.record_latency(latency=1.5)

# 记录意图分类
statistics_manager.record_intent(intent="chat")

# 记录触发方式
statistics_manager.record_trigger(trigger_type="mention")

# 获取统计摘要
summary = statistics_manager.get_summary()
```
```

---

## 10. Memory 记忆系统

### 10.1 概述

Memory 模块是基于 Mnemis 双路检索思想的多群组/多用户 Agent 记忆系统，适配 gsuid_core 单进程架构。AI 可以"记住"群聊中发生的事情，在后续对话中利用这些记忆提供个性化响应。

**设计理念**：
- **Observer 与发言决策正交**：AI 可以读取所有消息以构建认知，但不需要因此回复任何一条。即使 Persona 配置为纯静默模式，记忆依然在后台积累。
- **双路检索（Dual-Route Retrieval）**：System-1（向量相似度快速匹配）+ System-2（分层图遍历全局选择），合并后经 Reranker 重排序。
- **Scope Key 隔离**：群组间严格隔离，同时支持用户跨群全局画像。
- **线程隔离**：IngestionWorker 在独立线程的事件循环中运行，LLM 调用不阻塞主事件循环，避免 WebSocket 心跳超时断连。
- **线程安全队列**：使用 `queue.Queue`（线程安全）替代 `asyncio.Queue`，支持跨线程通信。

**核心数据流**：

```
用户消息 → handler.py (observe 入队)
         → handle_ai.py (dual_route_retrieve 检索记忆上下文)
         → AI 回复 → handle_ai.py (observe 入队)
         → IngestionWorker 后台消费
           → LLM 提取 Entity/Edge
           → 写入 SQLAlchemy + Qdrant
           → 触发分层图增量重建
```

### 10.2 模块结构

```
gsuid_core/ai_core/memory/
├── __init__.py           # 模块导出（observe, dual_route_retrieve, MemoryContext 等）
├── config.py             # MemoryConfig 全局配置（dataclass 单例）
├── scope.py              # ScopeType 枚举 + make_scope_key() 函数
├── observer.py           # 观察者管道（queue.Queue 线程安全 + 过滤逻辑）
├── startup.py            # @on_core_start 初始化入口
├── database/             # 图结构存储（SQLAlchemy，独立 MemBase）
│   ├── __init__.py       # _MemorySessionFactory + get_async_session
│   └── models.py         # 6 个模型 + 2 个关联表
├── ingestion/            # 摄入引擎（后台消费 + LLM 提取）
│   ├── __init__.py
│   ├── worker.py         # IngestionWorker（独立线程事件循环）+ _ingest_batch()
│   ├── entity.py         # extract_and_upsert_entities() 两阶段去重
│   ├── edge.py           # extract_and_upsert_edges() 冲突检测
│   └── hiergraph.py      # HierarchicalGraphBuilder + AIMemHierarchicalGraphMeta + 分层图构建
├── retrieval/            # 检索引擎
│   ├── __init__.py
│   ├── system1.py        # System-1 向量相似度 + RRF 融合
│   ├── system2.py        # System-2 BFS + LLM 节点选择
│   └── dual_route.py     # 双路合并 + Reranker 重排序
├── vector/               # 向量存储（Qdrant，复用 rag/base.py 客户端）
│   ├── __init__.py
│   ├── collections.py    # 3 个 Collection 名称常量
│   ├── startup.py        # ensure_memory_collections()
│   └── ops.py            # upsert/search 操作（query_points API）
└── prompts/              # LLM 提示词模板
    ├── __init__.py
    ├── extraction.py     # Entity/Edge 提取 Prompt
    ├── categorization.py # Category 分类 Prompt
    ├── selection.py      # 节点选择 Prompt
    └── summary.py        # 摘要生成 Prompt
```

### 10.3 核心架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Memory System Architecture                    │
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │  handler.py  │    │ handle_ai.py │    │ handle_ai.py         │  │
│  │  (消息入口)   │    │ (AI回复后)    │    │ (AI回复前)           │  │
│  │  observe()   │    │ observe()    │    │ dual_route_retrieve()│  │
│  └──────┬───────┘    └──────┬───────┘    └──────────┬───────────┘  │
│         │                   │                       │              │
│         ▼                   ▼                       │              │
│  ┌──────────────────────────────────┐               │              │
│  │     queue.Queue (maxsize=10000)   │               │              │
│  │     _observation_queue            │               │              │
│  │     (线程安全，跨线程通信)          │               │              │
│  └──────────────┬───────────────────┘               │              │
│                 │                                    │              │
│                 ▼  (跨线程传递)                       │              │
│  ┌──────────────────────────────────┐               │              │
│  │  IngestionWorker (独立线程事件循环) │               │              │
│  │  线程名: MemoryIngestionWorker    │               │              │
│  │  ┌────────────────────────────┐  │               │              │
│  │  │ _consume_loop()            │  │               │              │
│  │  │ _flush_timer_loop()        │  │               │              │
│  │  │ _flush(scope_key)          │  │               │              │
│  │  │   └── _ingest_batch()      │  │               │              │
│  │  │       ├── create_episode() │  │               │              │
│  │  │       ├── _llm_extract()   │  │               │              │
│  │  │       ├── extract_and_     │  │               │              │
│  │  │       │  upsert_entities() │  │               │              │
│  │  │       ├── extract_and_     │  │               │              │
│  │  │       │  upsert_edges()    │  │               │              │
│  │  │       └── hiergraph        │  │               │              │
│  │  │          incremental_rebuild│  │               │              │
│  │  └────────────────────────────┘  │               │              │
│  └──────────────┬───────────────────┘               │              │
│                 │                                    │              │
│         ┌───────┴────────┐                           │              │
│         ▼                ▼                           │              │
│  ┌─────────────┐  ┌─────────────┐                    │              │
│  │  SQLAlchemy  │  │   Qdrant    │                    │              │
│  │  (图结构)    │  │  (向量索引)  │                    │              │
│  └─────────────┘  └─────────────┘                    │              │
│         │                │                            │              │
│         └───────┬────────┘                            │              │
│                 │  ←──────────────────────────────────┘              │
│                 ▼                                                     │
│  ┌──────────────────────────────────┐                                │
│  │     Dual-Route Retrieval          │                                │
│  │  ┌────────────┐ ┌──────────────┐ │                                │
│  │  │ System-1   │ │ System-2     │ │                                │
│  │  │ 向量相似度  │ │ 分层图遍历    │ │                                │
│  │  │ + RRF融合  │ │ + LLM选择    │ │                                │
│  │  └─────┬──────┘ └──────┬───────┘ │                                │
│  │        └───────┬───────┘          │                                │
│  │                ▼                  │                                │
│  │        合并去重 + Reranker         │                                │
│  │                │                  │                                │
│  │                ▼                  │                                │
│  │        MemoryContext              │                                │
│  │        .to_prompt_text()          │                                │
│  └──────────────────────────────────┘                                │
└─────────────────────────────────────────────────────────────────────┘
```

### 10.4 Scope Key 隔离体系

**文件位置**: [`gsuid_core/ai_core/memory/scope.py`](gsuid_core/ai_core/memory/scope.py)

所有记忆节点（Episode、Entity、Edge、Category）均携带 `scope_key` 字段，实现群组间严格隔离。

**ScopeType 枚举**：

| 类型 | 格式 | 说明 |
|------|------|------|
| `GROUP` | `group:{group_id}` | 群组级记忆，群内所有消息共享 |
| `USER_GLOBAL` | `user_global:{user_id}` | 用户跨群全局画像 |
| `USER_IN_GROUP` | `user_in_group:{user_id}@{group_id}` | 用户在特定群组内的局部档案（可选精细化） |
| `SELF` | `self:{bot_id}` | **（C6 新增）** Bot 自身的情景记忆与自我模型——"我说过/做过什么" |

> **C6（2026-05-19）**：新增 `SELF` scope。Bot 自身发言（`__assistant_*`）不再混入
> 群组事实图谱，改路由到 `self:{bot_id}` 做轻量摄入（仅 Episode、不抽取 Entity/Edge），
> 从根源杜绝"Bot 戏言污染群记忆"。

**示例**：
```python
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

make_scope_key(ScopeType.GROUP, "789012")
# → "ScopeType.GROUP:789012"

make_scope_key(ScopeType.USER_GLOBAL, "12345")
# → "ScopeType.USER_GLOBAL:12345"
```

**隔离规则**：
- 群 A 的记忆对群 B 完全不可见（SQL WHERE scope_key = ?）
- 用户全局画像可跨群查询（`enable_user_global_memory` 配置项控制）
- Qdrant 向量检索通过 `scope_key` payload filter 实现同等隔离

### 10.5 Observer 观察者管道

**文件位置**: [`gsuid_core/ai_core/memory/observer.py`](gsuid_core/ai_core/memory/observer.py)

Observer 是记忆系统的"被动感知层"，通过 `queue.Queue`（线程安全）在主线程和 IngestionWorker 线程之间传递观察记录。

> **设计变更**：原使用 `asyncio.Queue`（非线程安全），改为 `queue.Queue` 以支持
> IngestionWorker 在独立线程的事件循环中运行，避免 LLM 调用阻塞主事件循环。

**ObservationRecord 数据结构**：

```python
@dataclass
class ObservationRecord:
    raw_content: str      # 原始消息文本
    speaker_id: str       # 发言者 ID
    group_id: str         # 原始群组 ID
    scope_key: str        # 格式化后的 Scope Key
    timestamp: datetime   # 观察时间
    message_type: str     # "group_msg" | "private_msg"
    value_tier: str       # （C1 新增）记忆价值分级 "HIGH" / "LOW"
```

**C1 摄入质量门控（2026-05-19 新增）** —— 门控 **100% 由纯规则 / 正则实现，绝不调用 LLM**：

入队前的门控函数 `_gate()` 做两件事：① 过滤噪声；② 给每条记录打 `value_tier`。
`_should_observe()` 已被 `_gate()` + `_classify_value_tier()` 取代。

| 规则 | 说明 |
|------|------|
| 自身消息过滤 | `speaker_id == bot_self_id` 时不入队 |
| 黑名单群组 | `group_id in observer_blacklist` 时不入队 |
| 命令回显过滤 | 正则命中"请输入正确/功能名称"等框架报错回显 → 丢弃 |
| 注入特征过滤 | 正则命中"忘记所有指令""ignore previous instructions"等 → 丢弃 |
| 复读 / 刷屏过滤 | 与本 scope 最近 12 条完全相同 → 丢弃（保留首次出现） |
| 重要性分级 | 含姓名自述/称呼偏好/承诺/数字日期 → HIGH；情绪词兜底 → HIGH；纯寒暄且 < 10 字且无实体 → LOW；其余默认 HIGH |

> 不再因 `len < 5` 直接丢弃短消息——"我是张三""叫我老板"等短句改由重要性分级判定。
> `value_tier=LOW` 的记录由 IngestionWorker 只写 Episode、跳过 Entity/Edge 抽取。
> Bot 自身发言（`__assistant_*`）路由到 `SELF` scope（见 10.4），不过本门控。

**队列溢出策略**：队列满时丢弃最老的一条，保证新消息不丢失。

**调用方式**（共三个入队点，分属两条记忆路径 `memory_mode`）：

```python
# ① handler.py 消息入口 —— 被动感知：记录所有群友发言（含触发者本人）
#    门控：is_enable_memory and observer_enabled and "被动感知" in memory_mode
asyncio.create_task(
    observe(
        content=event.raw_text,
        speaker_id=str(event.user_id),
        group_id=str(event.group_id or event.user_id),
        bot_self_id=str(event.bot_self_id),
        observer_blacklist=memory_config.observer_blacklist,
        message_type="group_msg" if event.group_id else "private_msg",
    )
)

# ② handle_ai.py 入口 —— 主动会话：记录“触发者发言”
#    去重：仅在未开启“被动感知”时记录，否则 ① 已记过，避免同一条消息二次写入
if "主动会话" in memory_mode and "被动感知" not in memory_mode:
    await observe(
        content=event.raw_text,
        speaker_id=str(event.user_id),
        group_id=str(event.group_id or event.user_id),
        bot_self_id=str(event.bot_self_id),
        observer_blacklist=memory_config.observer_blacklist,
        message_type="group_msg" if event.group_id else "private_msg",
    )

# ③ bot.py 发送路径 —— 主动会话：记录“Bot 自身回复”
#    speaker_id 以 __assistant_ 开头 → observe() 内部路由到 SELF scope，仅写 Episode
if enable_ai and is_enable_memory and "主动会话" in memory_mode:
    observe(
        content=message_list_to_str(mr),
        speaker_id=f"__assistant_{bot_id}__",
        group_id=target_id if target_type == "group" else None,
        bot_self_id=bot_self_id,
        observer_blacklist=memory_config.observer_blacklist,
        message_type="group_msg" if target_type == "group" else "private_msg",
    )
```

### 10.6 Ingestion 摄入引擎

**文件位置**: [`gsuid_core/ai_core/memory/ingestion/`](gsuid_core/ai_core/memory/ingestion/)

#### 10.6.1 IngestionWorker

**文件位置**: [`gsuid_core/ai_core/memory/ingestion/worker.py`](gsuid_core/ai_core/memory/ingestion/worker.py)

单实例后台任务，从 `observation_queue` 消费消息，按 `scope_key` 分组缓冲，满足时间窗口或数量阈值时触发 flush。

**缓冲与 Flush 机制**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `batch_interval_seconds` | 1800 (30分钟) | 消息聚合窗口，超时强制 flush |
| `batch_max_size` | 30 | 单次最大聚合条数 |
| `llm_semaphore_limit` | 2 | 同时进行的 LLM 调用上限 |

**Flush 流程**：

```
_flush(scope_key)
    │
    ├── 1. 取出缓冲区所有 records
    ├── 2. 获取 LLM 信号量
    └── 3. _ingest_batch(session, records, scope_key)
            │
            ├── Step 1: 格式化对话文本
            ├── Step 2: AIMemEpisode.create_episode() 写入 Episode（含向量写入）
            ├── Step 3: _llm_extract() LLM 提取 Entity/Edge
            ├── Step 4: extract_and_upsert_entities() 两阶段去重写入
            ├── Step 5: extract_and_upsert_edges() 冲突检测写入
            ├── Step 7: user_global Scope 跨群属性写入
            └── Step 8: check_and_trigger_hierarchical_update()
```

#### 10.6.2 Entity 两阶段去重

**文件位置**: [`gsuid_core/ai_core/memory/ingestion/entity.py`](gsuid_core/ai_core/memory/ingestion/entity.py)

```
extract_and_upsert_entities()
    │
    ├── Phase 1: 精确名称匹配
    │   └── SELECT FROM mem_entities WHERE scope_key=? AND name=?
    │       ├── 命中 → 更新 summary/tag，关联 Episode
    │       └── 未命中 → Phase 2
    │
    └── Phase 2: 向量相似度匹配
        └── Qdrant search_entities(name, scope_key, top_k=3)
            ├── similarity >= 0.92 → 视为同一实体，合并
            └── similarity < 0.92 → 新建 Entity
```

**去重阈值**：`dedup_similarity_threshold = 0.92`

#### 10.6.3 Edge 冲突检测

**文件位置**: [`gsuid_core/ai_core/memory/ingestion/edge.py`](gsuid_core/ai_core/memory/ingestion/edge.py)

当新 Edge 与已有 Edge 语义冲突时（如"Alice 喜欢篮球" vs "Alice 不再打篮球"），通过 `invalid_at` 字段标记旧 Edge 失效：

```
extract_and_upsert_edges()
    │
    ├── 1. 查找 source/target Entity
    ├── 2. 向量搜索同源同目标的已有 Edge
    ├── 3. 冲突判断（similarity < edge_conflict_threshold）
    │   └── 冲突 → 旧 Edge.invalid_at = now
    └── 4. 写入新 Edge
```

**冲突阈值**：`edge_conflict_threshold = 0.88`（比 Entity 去重更宽松）

### 10.7 双路检索引擎

**文件位置**: [`gsuid_core/ai_core/memory/retrieval/`](gsuid_core/ai_core/memory/retrieval/)

#### 10.7.1 System-1：向量相似度检索

**文件位置**: [`gsuid_core/ai_core/memory/retrieval/system1.py`](gsuid_core/ai_core/memory/retrieval/system1.py)

对 Episode、Entity、Edge 三个 Qdrant Collection 分别进行向量搜索，使用 **RRF（Reciprocal Rank Fusion）** 合并排序。

```
system1_search(query, scope_keys, top_k)
    │
    ├── search_episodes(query, scope_keys)  → Episode 候选
    ├── search_entities(query, scope_keys)  → Entity 候选
    ├── search_edges(query, scope_keys)     → Edge 候选
    │
    └── RRF 融合排序
        score = Σ (1 / (k + rank_i))   # k=60 (标准 RRF 参数)
```

#### 10.7.2 System-2：分层图遍历

**文件位置**: [`gsuid_core/ai_core/memory/retrieval/system2.py`](gsuid_core/ai_core/memory/retrieval/system2.py)

从顶层 Category 开始 BFS 遍历，每层通过 LLM 判断哪些子节点与查询相关，逐层深入直到 Entity 叶子节点。

```
system2_global_selection(query, scope_key, session)
    │
    ├── 1. 获取顶层 Category (layer = max_layer)
    ├── 2. BFS 遍历
    │   └── 每层: LLM 选择相关子节点
    │       ├── 相关 → 继续深入
    │       └── 不相关 → 剪枝
    ├── 3. 收集所有相关 Entity
    └── 4. 加载关联 Edge
```

**成本控制**：System-2 需要多次 LLM 调用，可通过 `enable_system2=False` 关闭。

#### 10.7.3 双路合并与 Reranker

**文件位置**: [`gsuid_core/ai_core/memory/retrieval/dual_route.py`](gsuid_core/ai_core/memory/retrieval/dual_route.py)

```python
async def dual_route_retrieve(
    query: str,
    group_id: str,
    user_id: Optional[str] = None,
    session: Optional[AsyncSession] = None,
    top_k: int = 10,
    enable_system2: bool = True,
    enable_user_global: bool = False,
) -> MemoryContext:
    # 1. 并行执行双路
    s1_task = asyncio.create_task(system1_search(...))
    s2_task = asyncio.create_task(system2_global_selection(...))

    # 2. 合并去重
    merged_episodes = _merge_dedup(s1.episodes, s2.episodes)
    merged_entities = _merge_dedup(s1.entities, s2.entities)
    merged_edges = _merge_dedup(s1.edges, s2.edges)

    # 3. Reranker 重排序（复用 rag/reranker.py）
    reranker = get_reranker()
    if reranker:
        merged_edges = await rerank_results(query, merged_edges, ...)

    # 4. 返回 MemoryContext
    return MemoryContext(episodes=..., entities=..., edges=...)
```

**MemoryContext 输出**：

```python
@dataclass
class MemoryContext:
    episodes: list[dict]    # 相关对话片段
    entities: list[dict]    # 相关实体
    edges: list[dict]       # 相关事实（Edge）
    retrieval_meta: dict    # 检索元信息

    def to_prompt_text(self, max_chars=3000) -> str:
        """格式化为可注入 System Prompt 的记忆上下文文本"""
        # 输出格式：
        # 【已知事实】
        # • Alice 喜欢户外运动
        # • Bob 是程序员
        #
        # 【历史对话片段】
        # [2026-04-18] [Alice]: 今天天气真好...
```

### 10.8 分层语义图

**文件位置**: [`gsuid_core/ai_core/memory/ingestion/hiergraph.py`](gsuid_core/ai_core/memory/ingestion/hiergraph.py)

分层语义图（Hierarchical Graph）将大量 Entity 归纳为多层 Category，支持 System-2 的自顶向下遍历检索。

**构建流程**：

```
HierarchicalGraphBuilder.incremental_rebuild()
    │
    ├── 1. 触发判定（_check_should_rebuild）
    │   ├── Entity 增长 > hiergraph_rebuild_ratio
    │   └── 距上次重建 > hiergraph_rebuild_interval_seconds
    │
    ├── 2. [#4] 小 scope 跳过：总 Entity < MIN_ENTITIES_FOR_HIERGRAPH(30)
    │        → 仅更新 Meta 后返回（类目对小数据集无收益，召回靠 System-1 + edges）
    │
    ├── 3. 获取未分配 Entity（_get_unassigned_entities）
    │   ├── 入口过滤：仅 is_speaker 或至少有一条 edge 的实体（无 edge 噪声不进 LLM）
    │   └── [#3] 单轮上限：按 created_at 取最旧的至多 MAX_ENTITIES_PER_REBUILD(800) 个
    │
    ├── 4. Layer-1 归类
    │   ├── [#2] 向量预分配：与已归类近邻 summary_dense 余弦 ≥ VECTOR_ASSIGN_THRESHOLD(0.85)
    │   │        → 直接并入近邻所在 Category（_vector_pre_assign，零 LLM）
    │   └── 残余实体（speaker + 未命中）才走 LLM（_llm_categorize / _apply_entity_assignments）
    │
    ├── 5. Layer-2/3：逐层增量构建
    │   ├── [#1] 仅取"尚无父类目"的下层节点喂 LLM（_filter_unparented）——
    │   │        把"按存量收费"降为"按新增收费"，消除高频复发 token
    │   ├── 下层全部已归类 → 跳过 LLM，推进到上层继续向上
    │   └── 违反 node count reduction rule → 回滚本层并终止
    │
    ├── 6. 更新 Meta + 按需重算群组摘要缓存（_should_regen_group_summary 命中才发 LLM）
    │
    └── 7. [#3] backlog 续清：本轮达单轮上限 → 再调度一次 rebuild_task（backlog 单调收敛）
```

**配置项**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `min_children_per_category` | 3 | 每个 Category 至少包含的子节点数 |
| `max_layers` | 3 | 分层图最大层数 |
| `hiergraph_rebuild_ratio` | 2.50 | Entity 增长超过此比例时触发增量重建 |
| `hiergraph_rebuild_interval_seconds` | 172800 (48h) | 距上次重建超过此秒数时触发 |
| `MIN_ENTITIES_FOR_HIERGRAPH` | 30 | 低于此实体数的 scope 整体跳过分层图（hiergraph.py 常量） |
| `MAX_ENTITIES_PER_REBUILD` | 800 | 单轮重建最多归类的未分配实体数，超额续轮处理（hiergraph.py 常量） |
| `VECTOR_ASSIGN_THRESHOLD` | 0.85 | 向量预分配的余弦相似度阈值，达标则跳过 LLM 直接归类（hiergraph.py 常量） |

### 10.9 数据库模型

**文件位置**: [`gsuid_core/ai_core/memory/database/models.py`](gsuid_core/ai_core/memory/database/models.py)

记忆系统使用 `SQLModel`（非 `BaseIDModel`），与现有统计系统的 `BaseIDModel` 不同。模型直接继承 `SQLModel, table=True`，使用 `uuid.uuid4()` 作为主键。`AIMemHierarchicalGraphMeta` 定义在 `ingestion/hiergraph.py` 中（非 `database/models.py`）。

**模型总览**：

| 模型 | 表名 | 定义位置 | 说明 |
|------|------|----------|------|
| `AIMemEpisode` | `aimemepisode` | `database/models.py` | 原始对话片段（Base Graph 第一层） |
| `AIMemEntity` | `aimementity` | `database/models.py` | 实体节点（Base Graph 第二层） |
| `AIMemEdge` | `aimemedge` | `database/models.py` | 实体间关系边（Base Graph 第三层） |
| `AIMemCategory` | `aimemcategory` | `database/models.py` | 分层语义图节点 |
| `AIMemCategoryEdge` | `aimemcategoryedge` | `database/models.py` | Category ↔ Category 层次关联 |
| `AIMemHierarchicalGraphMeta` | `aimemhierarchicalgraphmeta` | `ingestion/hiergraph.py` | 分层图构建状态追踪 |

**关联表**：

| 表名 | 说明 |
|------|------|
| `mem_episode_entity_mentions` | Episode ↔ Entity 多对多 |
| `mem_category_entity_members` | Category ↔ Entity 多对多 |

**AIMemEpisode 关键字段**：

```python
class AIMemEpisode(SQLModel, table=True):
    id: str                 # uuid4 主键
    scope_key: str          # "ScopeType.GROUP:789012"
    content: str            # 聚合后的对话文本
    speaker_ids: list       # JSON: ["user_001", "user_002"]
    valid_at: datetime      # 最早消息时间
    created_at: datetime    # 创建时间
    qdrant_id: str          # Qdrant memory_episodes Collection point ID
    mentioned_entities: list[AIMemEntity]  # 多对多关联（via mem_episode_entity_mentions）
```

**AIMemEntity 关键字段**：

```python
class AIMemEntity(SQLModel, table=True):
    id: str                 # uuid4 主键
    scope_key: str          # "ScopeType.GROUP:789012"
    name: str               # 实体名称（同 scope_key 内唯一）
    summary: str            # 实体摘要
    tag: list               # JSON: ["Speaker", "Group Member"]
    is_speaker: bool        # 是否是群成员实体
    user_id: str | None     # Speaker 实体的原始 user_id
    created_at: datetime    # 创建时间
    updated_at: datetime    # 更新时间
    qdrant_id: str          # Qdrant memory_entities Collection point ID
    # 唯一约束: (scope_key, name)
    episodes: list[AIMemEpisode]          # 多对多关联
    outgoing_edges: list[AIMemEdge]       # 一对多（source）
    incoming_edges: list[AIMemEdge]       # 一对多（target）
```

**AIMemEdge 关键字段**：

```python
class AIMemEdge(SQLModel, table=True):
    id: str                 # uuid4 主键
    scope_key: str          # "ScopeType.GROUP:789012"
    fact: str               # 事实描述: "Alice 喜欢户外运动"
    source_entity_id: str   # FK → aimementity.id
    target_entity_id: str   # FK → aimementity.id
    valid_at: datetime      # 事实生效时间
    invalid_at: datetime | None  # 事实失效时间（冲突时设置）
    created_at: datetime    # 创建时间
    qdrant_id: str          # Qdrant memory_edges Collection point ID
    source_entity: Optional[AIMemEntity]  # 关联源实体
    target_entity: Optional[AIMemEntity]  # 关联目标实体
```

**AIMemCategory 关键字段**：

```python
class AIMemCategory(SQLModel, table=True):
    id: str                 # uuid4 主键
    scope_key: str          # "ScopeType.GROUP:789012"
    name: str               # 类目名称
    summary: str            # 类目摘要
    tag: list               # JSON: ["Sport", "Outdoor Activity"]
    layer: int              # 层级（1=最具体，max=最抽象）
    created_at: datetime    # 创建时间
    updated_at: datetime    # 更新时间
    # 唯一约束: (scope_key, layer, name)
    child_categories: list[AIMemCategory]   # 子类目（via AIMemCategoryEdge）
    parent_categories: list[AIMemCategory]  # 父类目（via AIMemCategoryEdge）
    member_entities: list[AIMemEntity]      # 直接包含的 Entity（via mem_category_entity_members）
```

### 10.10 向量存储

**文件位置**: [`gsuid_core/ai_core/memory/vector/`](gsuid_core/ai_core/memory/vector/)

复用现有 `rag/base.py` 的 Qdrant 客户端和嵌入模型提供方（`embedding_provider`），创建 3 个独立 Collection。

**Collection 定义**：

| Collection | 存储内容 | Payload |
|------------|----------|---------|
| `memory_episodes` | Episode 向量 | `scope_key`, `content` |
| `memory_entities` | Entity 向量 | `scope_key`, `name`, `summary` |
| `memory_edges` | Edge 向量 | `scope_key`, `fact` |

**向量维度**：复用 `rag/base.py` 的 `get_dimension()` 函数（随启用的嵌入模型动态变化，默认回退 512）。

**API 说明**：使用 `client.query_points()` 方法（非已弃用的 `client.search()`），与现有 `rag/knowledge.py` 和 `rag/tools.py` 保持一致。

**核心操作函数**：

| 函数 | 说明 |
|------|------|
| `upsert_episode_vector()` | 写入 Episode 向量 |
| `upsert_entity_vector()` | 写入 Entity 向量 |
| `upsert_edge_vector()` | 写入 Edge 向量 |
| `search_episodes()` | 向量搜索 Episode |
| `search_entities()` | 向量搜索 Entity |
| `search_edges()` | 向量搜索 Edge |

### 10.11 配置项

**文件位置**: [`gsuid_core/ai_core/memory/config.py`](gsuid_core/ai_core/memory/config.py)

全局单例 `memory_config = MemoryConfig()`，所有配置项均有合理默认值。

#### 观察者配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `observer_enabled` | bool | `True` | 是否启用消息观察者 |
| `observer_blacklist` | List[str] | `[]` | 黑名单群组 ID 列表 |

#### 摄入配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `ingestion_enabled` | bool | `True` | 是否启用摄入引擎 |
| `batch_interval_seconds` | int | `1800` | 消息聚合窗口（秒） |
| `batch_max_size` | int | `30` | 单次最大聚合条数 |
| `llm_semaphore_limit` | int | `2` | 同时进行的 LLM 调用上限 |

#### 检索配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_retrieval` | bool | `True` | 是否启用记忆检索 |
| `enable_system2` | bool | `True` | 是否启用 System-2（成本较高） |
| `enable_user_global_memory` | bool | `False` | 是否联合查询用户跨群画像 |
| `enable_heartbeat_memory` | bool | `True` | 是否在 Heartbeat 中注入群组摘要 |
| `retrieval_top_k` | int | `10` | 最终返回的 Episode 数量上限 |

#### 去重与冲突阈值

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `dedup_similarity_threshold` | float | `0.92` | Entity 去重余弦相似度阈值 |
| `edge_conflict_threshold` | float | `0.88` | Edge 语义冲突判断阈值 |

#### 分层图配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `min_children_per_category` | int | `3` | 每个 Category 最少子节点数 |
| `max_layers` | int | `5` | 分层图最大层数 |
| `hiergraph_rebuild_ratio` | float | `1.10` | Entity 增长触发增量重建的比例 |
| `hiergraph_rebuild_interval_seconds` | int | `86400` | 距上次重建触发增量重建的秒数 |

### 10.12 与现有模块的集成

#### 10.12.1 handler.py 集成

**文件位置**: [`gsuid_core/handler.py`](gsuid_core/handler.py:90)

在 `handle_event()` 的消息入口处添加 Memory Observer Hook，在历史记录写入之前：

```python
# handler.py - handle_event() 中（被动感知路径）
# 门控由 memory_mode 控制：仅在开启“被动感知”时入队，范围再由 memory_session 约束
if is_enable_memory and memory_config.observer_enabled and "被动感知" in memory_mode:
    # memory_session: "全部群聊" 记录全部；"按人格配置" 仅记录命中人格范围的 session
    if should_observe:
        from gsuid_core.ai_core.memory import observe
        asyncio.create_task(
            observe(
                content=event.raw_text,
                speaker_id=str(event.user_id),
                group_id=str(event.group_id or event.user_id),
                bot_self_id=str(event.bot_self_id),
                observer_blacklist=memory_config.observer_blacklist,
                message_type="group_msg" if event.group_id else "private_msg",
            )
        )
```

#### 10.12.2 handle_ai.py 集成

**文件位置**: [`gsuid_core/ai_core/handle_ai.py`](gsuid_core/ai_core/handle_ai.py:128)

**记忆检索（AI 回复前）**：在步骤 5 中，AI 生成回复前检索相关记忆并注入上下文：

```python
# handle_ai.py - 步骤 5: 记忆上下文
memory_context_text = ""
if memory_config.enable_retrieval:
    async with get_async_session() as mem_session:
        mem_ctx = await dual_route_retrieve(
            query=query,
            group_id=str(event.group_id or event.user_id),
            user_id=str(event.user_id),
            session=mem_session,
        )
    memory_context_text = mem_ctx.to_prompt_text(max_chars=2000)

# 合并记忆上下文到 full_context
if memory_context_text:
    full_context = f"{rag_context}\n【长期记忆】\n{memory_context_text}\n"
```

**记忆观察（主动会话路径）**：开启「主动会话」时，本轮交互的两侧都会入队，但分布在两处——

1. **触发者发言** —— 在 `handle_ai.py` 入口处入队。能进入 `handle_ai_chat()` 即代表 AI 实际参与了交互，按「主动会话」语义需记录触发者这条原话。**去重**：若同时开启「被动感知」，该消息已在 `handler.py` 消息入口被动入队过一次，此处通过 `"被动感知" not in memory_mode` 守卫跳过，避免二次写入。

```python
# handle_ai.py - handle_ai_chat() 入口
_memory_mode = memory_config.memory_mode
if (
    ai_config.get_config("enable_memory").data
    and "主动会话" in _memory_mode
    and "被动感知" not in _memory_mode
):
    await observe(
        content=event.raw_text,
        speaker_id=str(event.user_id),
        group_id=str(event.group_id or event.user_id),
        bot_self_id=str(event.bot_self_id),
        observer_blacklist=memory_config.observer_blacklist,
        message_type="group_msg" if event.group_id else "private_msg",
    )
```

2. **Bot 自身回复** —— 在 `bot.py` 的发送路径入队（见 §10.12.4）。`speaker_id` 以 `__assistant_` 开头，`observe()` 内部据此路由到 `SELF` scope（仅写 Episode、`value_tier=LOW`，不进群组事实图谱）。

#### 10.12.3 启动初始化

**文件位置**: [`gsuid_core/ai_core/memory/startup.py`](gsuid_core/ai_core/memory/startup.py)

```python
@on_core_start(priority=5)
async def init_memory_system():
    """初始化记忆系统"""
    # 检查AI总开关
    if not ai_config.get_config("enable").data:
        logger.info("🧠 [Memory] AI总开关已关闭，跳过记忆系统初始化")
        return

    # 0. 检查 RAG 是否已启用（前置条件）
    from gsuid_core.ai_core.rag.base import client, init_embedding_model
    if client is None:
        init_embedding_model()
        from gsuid_core.ai_core.rag.base import client
        if client is None:
            return  # RAG 未启用，跳过记忆系统初始化

    # 1. 确保 Qdrant Collection 存在
    await ensure_memory_collections()

    # 2. 启动 IngestionWorker（在独立线程中运行，避免 LLM 调用阻塞主事件循环）
    worker = IngestionWorker()
    worker.start_in_thread()  # 启动独立线程事件循环，而非 asyncio.create_task()
```

> **设计变更**：IngestionWorker 从 `asyncio.create_task(worker.start())` 改为
> `worker.start_in_thread()`，在独立线程的事件循环中运行。
> 这确保了 Memory 系统的 LLM 调用（Entity/Edge 提取）不会阻塞主事件循环，
> 避免 NoneBot2 WebSocket 心跳超时断连。
>
> **线程架构**：
> ```
> 主事件循环 (Main Event Loop)
> ├── WebSocket 消息接收
> ├── AI 对话处理
> └── 其他定时任务
>
> 独立线程事件循环 (MemoryIngestionWorker)
> ├── _consume_loop() - 从 queue.Queue 消费消息
> ├── _flush() - 批量 LLM 提取 + 数据库写入
> └── _flush_timer_loop() - 定时检查缓冲区
> ```

#### 10.12.4 bot.py 集成（主动会话 · Bot 自身回复）

**文件位置**: [`gsuid_core/bot.py`](gsuid_core/bot.py)（`_Bot.send()` 发送路径）

开启「主动会话」时，Bot 每次实际发出消息都会把回复内容入队观察。`speaker_id` 以
`__assistant_{bot_id}__` 构造，`observe()` 内部识别该前缀后路由到 `SELF` scope
（`self:{bot_self_id}`），仅写 Episode、`value_tier=LOW`，**不进群组事实图谱**——
从根源杜绝「Bot 戏言污染群记忆」（C6）。

```python
# bot.py - _Bot.send() 发送路径
if enable_ai and is_enable_memory and "主动会话" in memory_mode:
    asyncio.create_task(
        observe(
            content=message_list_to_str(mr),
            speaker_id=f"__assistant_{bot_id}__",
            group_id=target_id if target_type == "group" else None,
            bot_self_id=bot_self_id,
            observer_blacklist=memory_config.observer_blacklist,
            message_type="group_msg" if target_type == "group" else "private_msg",
        )
    )
```

> 与触发者发言（§10.12.2 路径 1）形成区分：触发者是真实用户、其发言进群组事实图谱
> 可被实体抽取；Bot 自身回复只作为轻量情景记忆存入 SELF scope。

### 10.13 记忆统计

记忆系统的运行统计集成在 AI Statistics 模块中，通过 `StatisticsManager` 的 `record_memory_*` 方法记录。

**统计指标**：

| 统计项 | 方法 | 记录位置 |
|--------|------|----------|
| 观察入队数 | `record_memory_observation()` | `observer.py` - `observe()` |
| 摄入完成数 | `record_memory_ingestion()` | `worker.py` - `_flush()` |
| 摄入失败数 | `record_memory_ingestion_error()` | `worker.py` - `_flush()` |
| 检索请求数 | `record_memory_retrieval()` | `handle_ai.py` - 记忆检索后 |
| 新建 Entity 数 | `record_memory_entity_created()` | `worker.py` - `_ingest_batch()` |
| 新建 Edge 数 | `record_memory_edge_created()` | `worker.py` - `_ingest_batch()` |
| 新建 Episode 数 | `record_memory_episode_created()` | `worker.py` - `_flush()` |

**数据库持久化**：以上 7 项统计字段已添加到 `AIDailyStatistics` 模型，随定时任务（每 30 分钟 + 零点重置）持久化到数据库。

**统计摘要输出**：`get_summary()` 和 `_daily_stats_to_dict()` 的 `memory` 区块：

```json
{
    "memory": {
        "observations": 150,
        "ingestions": 12,
        "ingestion_errors": 1,
        "retrievals": 45,
        "entities_created": 38,
        "edges_created": 25,
        "episodes_created": 12
    }
}
```

---

### 10.14 Meme 表情包模块

**文件位置**: [`gsuid_core/ai_core/meme/`](gsuid_core/ai_core/meme/)

让 AI 在群聊中具备「表情包意识」：自动采集群聊图片、智能打标、分类存储、智能发送。

> **详细设计文档**: 见 [MEME_MODULE.md](./MEME_MODULE.md)

**模块结构**:

```
meme/
├── config.py            # 配置项（StringConfig）
├── database_model.py    # AiMemeRecord SQLModel 表
├── filter.py            # 去重 + 质量过滤
├── library.py           # 文件 + DB + Qdrant 操作
├── observer.py          # 消息流监听（asyncio.create_task fire-and-forget）
├── selector.py          # 检索 + 决策
├── startup.py           # @on_core_start 钩子
└── tagger.py            # VLM 打标引擎
```

**AI 工具** (`buildin_tools/meme_tools.py`):

| 工具 | 说明 |
|------|------|
| `send_meme` | 根据情绪/场景智能选取并发送表情包 |
| `collect_meme` | 手动收集表情包入库 |
| `search_meme` | 搜索表情包库 |

**集成点**:
- `handle_ai.py` 中通过 `asyncio.create_task(observe_message_for_memes(event, ""))` 异步采集群聊图片
- `handle_ai.py` 中导入 `meme.startup` 和 `meme_tools` 以注册 `@on_core_start` 钩子和 `@ai_tools`

---

### 10.15 Image Understand 图片理解模块

**文件位置**: [`gsuid_core/ai_core/image_understand/`](gsuid_core/ai_core/image_understand/)

提供统一的图片理解接口，将图片内容转述为文本描述。当 LLM 模型不支持图片输入时，自动调用本模块将图片转述为文本后再发送给 LLM。

**模块结构**:

```
image_understand/
├── __init__.py       # 模块导出（understand_image）
└── understand.py     # 统一图片理解接口（MCP 驱动）
```

**核心函数**:

```python
from gsuid_core.ai_core.image_understand import understand_image

async def understand_image(
    image_url: str,       # 图片来源（HTTP URL 或 base64 DataURI）
    prompt: str | None = None,  # 对图片的提问，默认为通用描述
) -> str:                 # 图片内容的文本描述
```

**配置项** (`ai_config`):

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `image_understand_provider` | str | `"MCP"` | 图片理解服务提供方（目前仅支持 MCP） |

**工作流程**:

```
GsCoreAIAgent._prepare_user_message()
    │
    ├── 检查 model_support 配置
    │   ├── "image" in model_support → 保留 ImageUrl，直接传图给 LLM（回复路径不调 understand_image）
    │   └── "image" not in model_support → 调用 understand_image 转述
    │
    └── understand_image(image_url, task_level="high")
        ├── _resolve_native_image_model(task_level)：model_support 含 image 时
        │   └── 直接用大模型原生多模态（OpenAI / Anthropic 兼容请求）转述，无需 MCP
        └── 否则回退 MCP 转述模型
            ├── 读取 mcp_tools_config["image_understand_mcp_tool_id"]
            ├── call_mcp_tool(mcp_tool_id, arguments)
            └── 返回文本描述
```

> ⚠️ `understand_image` 不止被回复路径调用，记忆摄入（`memory/ingestion/multimodal.py`
> 的 `ImageUnderstandWorker`）、视频帧、表情包打标等后台路径也会调它。这些路径
> **不经过 `_prepare_user_message` 的能力分支**，因此当模型原生支持图片（`model_support`
> 含 `image`）时，必须由 `understand_image` 自身优先走大模型原生多模态，**而非要求
> 用户额外配置图片转述模型（MCP）**——否则未配 MCP 时会刷出"图片理解失败（已忽略）:
> Image Understand MCP 工具未配置"。

**与 GsCoreAIAgent 的集成**: [`_prepare_user_message()`](gsuid_core/ai_core/gs_agent.py:177) 方法在 `_execute_run` 中自动处理：
- 分离文本和图片内容
- 检查当前模型的 `model_support` 配置
- 模型支持图片时保留 `ImageUrl`
- 模型不支持图片时调用 `understand_image()` 将图片转述为文本

---

### 10.16 Web Search 统一搜索接口

**文件位置**: [`gsuid_core/ai_core/web_search/search.py`](gsuid_core/ai_core/web_search/search.py)

提供统一的 Web 搜索接口，根据用户配置自动选择搜索引擎（Tavily / Exa / MCP）。

**核心函数**:

```python
from gsuid_core.ai_core.web_search.search import web_search, web_search_with_context

async def web_search(query: str, max_results: int | None = None) -> list[dict]
async def web_search_with_context(query: str, max_results: int = 5) -> dict
```

**配置项** (`ai_config`):

| 配置项 | 类型 | 默认值 | 选项 | 说明 |
|--------|------|--------|------|------|
| `websearch_provider` | str | `"Tavily"` | `Tavily` / `Exa` / `MCP` | Web 搜索服务提供方 |

**提供方对比**:

| 提供方 | 说明 | 配置方式 |
|--------|------|----------|
| Tavily | Tavily AI 搜索（默认） | `TAVILY_API_KEY` 环境变量 |
| Exa | Exa 搜索引擎 | `EXA_API_KEY` 环境变量 |
| MCP | 通过 MCP 协议调用搜索工具 | `mcp_tools_config.websearch_mcp_tool_id` |

**MCP 搜索流程**:

```
web_search(query)
    │
    ├── provider == "MCP"
    │   └── _mcp_search(query)
    │       ├── 读取 mcp_tools_config["websearch_mcp_tool_id"]
    │       ├── call_mcp_tool(mcp_tool_id, {"query": query})
    │       └── _parse_mcp_search_result() 标准化结果格式
    │
    ├── provider == "Exa"
    │   └── exa_search(query)
    │
    └── 默认 Tavily
        └── tavily_search(query)
```

> **注意**: MiniMax 搜索已从独立实现迁移至 MCP 驱动，原 `minimax_search.py` 已删除。

---

## 11. 嵌入模型提供方抽象层

### 11.1 概述

**文件位置**: [`gsuid_core/ai_core/rag/embedding.py`](gsuid_core/ai_core/rag/embedding.py)

嵌入模型提供方抽象层将嵌入模型的调用统一为 `EmbeddingProvider` 接口，支持在本地 fastembed 模型和 OpenAI 兼容格式的远程 API 之间自由切换。通过 `ai_config` 中的 `embedding_provider` 配置项控制底层实现。

### 11.2 架构设计

```
┌─────────────────────────────────────────────────────────┐
│                    调用方（无需感知底层实现）                │
│  rag/tools.py · rag/knowledge.py · rag/image_rag.py     │
│  memory/vector/ops.py                                   │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│              EmbeddingProvider (ABC)                      │
│  embed_sync(texts) → list[list[float]]                   │
│  embed_single_sync(text) → list[float]                   │
│  embed(texts) → list[list[float]]  (async)               │
│  embed_single(text) → list[float]  (async)               │
│  dimension → int                                         │
└───────────┬─────────────────────────┬───────────────────┘
            │                         │
            ▼                         ▼
┌───────────────────────┐ ┌───────────────────────────────┐
│ LocalEmbeddingProvider│ │  OpenAIEmbeddingProvider       │
│ (fastembed + ONNX)    │ │  (httpx → /v1/embeddings)     │
│                       │ │                                │
│ model_name: str       │ │ base_url: str                  │
│ cache_dir: str        │ │ api_key: str                   │
│                       │ │ model_name: str                │
│ 同步: 线程池包装       │ │ 同步: httpx.Client             │
│ 异步: run_in_executor │ │ 异步: httpx.AsyncClient        │
└───────────────────────┘ └───────────────────────────────┘
```

### 11.3 配置项

**嵌入模型提供方选择**（`ai_config`）：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `embedding_provider` | str | `"local"` | 嵌入模型提供方，`"local"` 或 `"openai"` |

**本地嵌入配置**（`local_embedding_config`）：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `embedding_model_name` | str | `"BAAI/bge-small-zh-v1.5"` | 本地嵌入模型名称 |

**OpenAI 嵌入配置**（`openai_embedding_config`）：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `base_url` | str | `"https://api.openai.com/v1"` | API 基础 URL |
| `api_key` | list[str] | `["sk-"]` | API 密钥列表 |
| `embedding_model` | str | `"text-embedding-3-small"` | 嵌入模型名称 |

### 11.4 向后兼容

`rag/base.py` 中的 `embedding_model` 全局变量通过 `_EmbeddingModelWrapper` 包装 `EmbeddingProvider`，保持与原有 `fastembed.TextEmbedding` 相同的 `.embed([text])` 接口。现有调用方（`rag/tools.py`、`rag/knowledge.py`、`rag/image_rag.py`）无需任何修改。

新增 `embedding_provider` 全局变量暴露底层 `EmbeddingProvider` 实例，供 `memory/vector/ops.py` 等需要直接使用异步接口的模块使用。

### 11.5 WebConsole API

详见 [27. 嵌入模型配置 API](../gsuid_core/webconsole/docs/27-embedding-config.md)。

---

## 12. 完整流程图

### 12.1 消息处理总流程

```
┌──────────────────────────────────────────────────────────────────────┐
│                         用户发送消息                                   │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    handle_event(ws, msg, is_http)                     │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 1. 检查 IS_HANDDLE 全局开关                                      │  │
│  │ 2. 检查黑名单/屏蔽列表                                           │  │
│  │ 3. msg_process() 解析消息                                        │  │
│  │ 4. 记录用户消息到历史                                            │  │
│  │ 5. 主人用户自动订阅                                              │  │
│  │ 6. 用户/群组数据库记录                                          │  │
│  │ 7. 生成 session_id                                               │  │
│  │ 8. 重复消息检查                                                  │  │
│  │ 9. 命令前缀处理                                                  │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    触发器匹配检查 (SL.lst)                             │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ for sv in SL.lst:                                              │  │
│  │     for _type in SL.lst[sv].TL:                                │  │
│  │         for tr in SL.lst[sv].TL[_type]:                        │  │
│  │             if _check_command(trigger, priority, event):       │  │
│  │                 valid_event[trigger] = priority                │  │
│  └────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              │ 有匹配的触发器               │ 无匹配的触发器
              ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────────────────┐
│     执行命令触发器         │     │           AI 处理流程                │
│  ┌────────────────────┐  │     │  ┌─────────────────────────────┐   │
│  │ 1. 排序触发器       │  │     │  │ 1. enable_ai 全局开关检查   │   │
│  │ 2. 创建 Bot 实例   │  │     │  │ 2. 黑白名单检查             │   │
│  │ 3. 执行 trigger.func│  │     │  │ 3. 获取 session_id         │   │
│  │ 4. 任务入队         │  │     │  │ 4. 获取 persona_name       │   │
│  │ 5. 阻塞/非阻塞返回  │  │     │  │    get_persona_for_session │   │
│  └────────────────────┘  │     │  │ 5. 检查 ai_mode            │   │
└─────────────────────────┘     │  │    - "提及应答": 检查@/关键词│   │
                                │  │    - 其他模式...            │   │
                                │  │ 6. TaskContext 入队        │   │
                                │  │    handle_ai_chat()        │   │
                                │  └─────────────────────────────┘   │
                                └─────────────────────────────────────┘
```

### 12.2 AI 聊天处理流程 (handle_ai_chat)

```
┌──────────────────────────────────────────────────────────────────────┐
│                    handle_ai_chat(bot, event)                        │
└──────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────┐
│  1. enable_ai 检查（运行时动态读取）                                   │
│     └── if not ai_config.get_config("enable").data: return           │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  2. 并发控制 (D-8 修复)                                               │
│     └── async with _ai_semaphore:  # Semaphore(10)                   │
│         └── 最多允许 10 个并发 AI 调用，超出则等待队列                  │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  3. 双层长度防护 (D-10 修复)                                           │
│     ├── 第一层：if len > 60000: 硬截断 + 截断提示（防子Agent爆炸）     │
│     └── 第二层：if len > 15000:  调用 create_subagent 智能摘要          │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  4. 意图识别                                                          │
│     └── res = await classifier_service.predict_async(query)          │
│         ├── intent = "闲聊"                                          │
│         ├── intent = "工具"                                          │
│         └── intent = "问答"                                          │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  5. 获取 AI Session                                                   │
│     └── session = await get_ai_session(event)                        │
│         ├── 构建 session_id                                           │
│         ├── 检查 AISessionRegistry 中是否已存在                       │
│         ├── 不存在则创建新 Session                                     │
│         │   ├── get_persona_for_session()                            │
│         │   ├── build_persona_prompt()                                │
│         │   └── create_agent()                                        │
│         └── 返回 GsCoreAIAgent 实例                                   │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  6. 准备历史上下文 (D-11 修复：RAG 已移除为强制前置步骤)                  │
│     ├── format_history_for_agent() - 格式化近 30 条历史               │
│     └── rag_context = "【历史对话】\n{history_context}"              │
│                                                                      │
│  注意：RAG 知识库检索不再是前置强制步骤                                  │
│       主Agent通过 search_knowledge 工具按需决定是否检索               │
│       用户问"你好" → LLM 直接回复，不触发 RAG                          │
│       用户问"怎么配置" → LLM 主动调用 search_knowledge 工具            │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  7. 调用 Agent 生成回复                                                │
│     └── chat_result = await session.run(                             │
│             user_message=user_messages,                               │
│             bot=bot,                                                  │
│             ev=event,                                                 │
│             rag_context=rag_context,  # 含历史记录                    │
│         )                                                             │
│     Agent 内部按需调用 buildin 工具（含 search_knowledge）             │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  8. 发送回复                                                          │
│     └── await bot.send(chat_result)                                   │
└──────────────────────────────────────────────────────────────────────┘
```

### 12.3 Heartbeat 定时巡检流程

```
┌──────────────────────────────────────────────────────────────────────┐
│                    定时任务触发 (APScheduler)                          │
│                    每 inspect_interval 分钟执行一次                     │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  _inspect_all_sessions_for_persona(persona_name)                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 1. 获取该 persona 的 scope 和 target_groups                     │  │
│  │ 2. 获取所有活跃会话: history_manager.list_sessions()            │  │
│  │ 3. 遍历每个会话: for session_key in sessions                    │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  _should_inspect_session() - 过滤会话                                │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ if scope == "disabled": return False                           │  │
│  │ elif scope == "global": return True (所有会话)                 │  │
│  │ elif scope == "specific": return group_id in target_groups     │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  _inspect_session() - 处理单个会话                                    │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 1. 获取历史记录: history = _get_history(session_key)            │  │
│  │ 2. 防刷屏检查: _has_recent_ai_response(history)                 │  │
│  │    └── 如果 AI 最近 5 条消息内已发言，不继续                      │  │
│  │ 3. 获取 AI Session: ai_session = get_ai_session_by_id()       │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  LLM 决策阶段 (run_heartbeat 两阶段)                                  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ run_heartbeat(event, history, ai_session)                       │  │
│  │   ├── 阶段一：决策                                               │  │
│  │   │   ├── 1. 构建 DECISION_PROMPT_TEMPLATE                      │  │
│  │   │   ├── 2. 调用 LLM                                           │  │
│  │   │   ├── 3. _parse_decision_json() 解析响应                     │  │
│  │   │   └── 4. 返回 {should_speak, mood, context_hook}            │  │
│  │   └── 阶段二：生成发言（仅 should_speak=True）                   │  │
│  │       ├── 1. 构建 PROACTIVE_MESSAGE_PROMPT                      │  │
│  │       ├── 2. 调用 LLM                                           │  │
│  │       └── 3. _strip_message_quotes() 后处理                     │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │ should_speak = True           │ should_speak = False
                    ▼                               ▼
┌───────────────────────────────┐     ┌─────────────────────────────────┐
│  生成主动消息                  │     │  保持沉默                        │
│  ┌─────────────────────────┐  │     │  ├── mood: "不感兴趣"            │
│  │ PROACTIVE_MESSAGE_PROMPT│  │     │  ├── mood: "刚发过言"            │
│  │   ├── 注入 mood 上下文   │  │     │  └── mood: "不符合性格"          │
│  │   ├── 调用 LLM           │  │     └─────────────────────────────────┘
│  │   └── _strip_message_   │  │
│  │     quotes() 后处理      │  │
│  └─────────────────────────┘  │
└───────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  发送主动消息                                                         │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ _send_proactive_message(session_key, user_id, message, reason)  │  │
│  │   ├── 1. 获取 Bot 实例: _get_bot_for_session()                │  │
│  │   ├── 2. 发送消息: bot.target_send()                          │  │
│  │   └── 3. 记录到历史: metadata={proactive: True}                │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

### 12.4 配置更新与热重载流程

```
┌──────────────────────────────────────────────────────────────────────┐
│  PUT /api/persona/{persona_name}/config                              │
│  请求体: {"scope": "...", "ai_mode": [...], ...}                     │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  update_persona_config()                                             │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 1. 检查 persona 是否存在                                        │  │
│  │ 2. 逐项更新配置:                                                │  │
│  │    ├── scope -> set_scope()                                    │  │
│  │    ├── target_groups -> set_target_groups()                    │  │
│  │    ├── ai_mode -> set_ai_mode()                                │  │
│  │    │       └── if "定时巡检" in ai_mode:                       │  │
│  │    │               start_heartbeat_inspector()               │  │
│  │    ├── inspect_interval -> set_inspect_interval()             │  │
│  │    │       └── if 已启用定时巡检:                               │  │
│  │    │               inspector.stop_for_persona()               │  │
│  │    │               inspector.start_for_persona()               │  │
│  │    └── keywords -> set_keywords()                              │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  set_config() -> write_config()                                     │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 1. 更新内存: self.config[key].data = value                      │  │
│  │ 2. 持久化: json.dump() -> config.json                           │  │
│  │ 3. 返回: {"status": 0, "msg": "...", "data": {...}}            │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

### 12.5 消息触发 vs 定时巡检 对比

| 特性 | 提及应答模式 | 定时巡检模式 |
|------|-------------|-------------|
| **触发方式** | 用户消息 | 定时任务 |
| **触发条件** | @机器人 或 包含关键词 | 定时间隔 |
| **配置项** | `ai_mode` 包含 "提及应答" | `ai_mode` 包含 "定时巡检" |
| **Scope 检查** | `get_persona_for_session()` | `_should_inspect_session()` |
| **防刷屏** | 依赖 cooldown_tracker | 最近 5 条消息检查 |
| **消息来源** | 用户消息 | AI 主动生成 |
| **metadata 标记** | 无 | `proactive: True` |

---

## 13. 长任务编排与记忆生命周期（C5/C7/C11/C9）

> 2026-05-20 起落地；能力代理与长任务编排的当前最终语义见
> `docs/AGENT_CAPABILITY_AGENT_MERGED_20260521.md`。

### 13.1 Kanban 任务编排层 `ai_core/planning/`

把跨步骤、多代理协作的任务做成真正的数据库持久化任务树，取代历史上"假持久化
PersistentAgent"（agent_mesh）与"单代理跨天串行步骤长任务"（C5）两套老方案。

**两张持久化表 + 一张 Artifact 表**
- `AIAgentTask`（`aiagenttask`）：任务节点表——根任务 + 子任务**共表**，
  `node_kind="root|subtask"` 区分；字段含 `ordinal`(用户可见短序号) / `goal` /
  `status` / `parent_task_id` / `root_task_id` / `dependency_task_ids` /
  `agent_profile`(子任务由哪类能力代理推进) / `failure_reason` / `respawn_count`
  / `params_override` / `input_artifact_ids` / `output_artifact_id` /
  `failure_policy` / `workspace_policy` / `broadcast_targets` / `review_notes`。
- `AIAgentTaskLog`（`aiagenttasklog`）：任务事件流——`plan_created` /
  `step_started` / `step_done` / `step_failed` / `decision` / `approval` /
  `workspace_violation`，崩溃恢复 / 审计窗口的依据。
- `AIAgentArtifact`（`aiagentartifact`）：任务节点产出登记表——`payload_inline`
  ≤4KB / 超过走 `payload_path` 落盘；按 `root_task_id` 严格隔离跨树读取。

**约束 2：真实 ID 绝不暴露给 LLM**——LLM 工具参数无 `task_id` / `root_task_id`；
写原语作用于框架经 `runtime.py` 的 `contextvars` 绑定的 current_task；引用类
工具用自然语言句柄（`resolver.resolve_task_ref` 仅匹配根任务，解析"任务#3"/
"炒股那个"/"运行中的"）；子任务句柄形如 `"<root_ref>#sub<N>"`；artifact
则用显式 `res_xxx` 句柄。

**生命周期（事件驱动 · 无定时器）**：
1. **能力评估**：主人格先调 `evaluate_agent_mesh_capability`，由内部
   `capability_evaluator` 一次性无记忆代理判断现有画像能否覆盖。
2. **建树**：`covered=true` 时调 `register_kanban_task` 创建根任务 + N 子任务，
   立刻 `kick_root` 一次。
3. **并发派活**：`kanban_executor.execute_ready_tasks` 扫树拿可跑节点
   （依赖已满足、未被并发抢走），用 `asyncio.gather` 并发跑
   `run_capability_agent`。
4. **状态推进**：节点跑完 / 失败 → 子任务条件 SQL 落终态 +
   `refresh_root_status` 汇总根任务 → 递归 `_schedule_continuation` 最多 4 层
   处理"上游刚完成、下游立即就绪"的级联。
5. **追问溯源**：每个能力代理调 `artifact_put` 登记产出；没显式登记时执行器
   用 `raw_result` 兜底写一份 `output` artifact。
6. **失败处理**：默认 `notify_persona` 策略——子任务失败把 `failure_reason`
   用人格口吻通知主人格，主人格决定 `respawn_subtask` / `fail_task_tree`；
   重派达 3 次自动转 `waiting_approval`，由主人在 webconsole 或对话回复审批
   （`respond_subtask_approval`）。
7. **崩溃恢复**：启动期 `recover_zombie_subtasks` 复活心跳过期的 running
   子任务，再对所有 running / pending 根任务统一 `kick_root` 一次。

每轮对话由 `planning.context.build_task_context` 注入活跃**根任务**摘要
（不含子任务，避免顶层概览被污染）。

**没有定时器**：Kanban 纯事件驱动。需要"明天 6 点触发""每天复盘"等时间
触发条件，请用 `add_once_task` / `add_interval_task` 在那个时刻把主人格
唤醒，由主人格视情况调 `register_kanban_task` 或更新已有任务树。

**能力代理推进（执行/表达分离）**：调度器派活时绕过主人格会话——人格被设定为
"懒惰、回避分析"的话会让严肃执行抵制、空转，并出现人格漂移。
`run_capability_agent` 按子任务 `agent_profile` 唤醒 `ai_core/capability_agents/`
的**无人格能力代理**（不拒绝、不漂移），执行结果再经 `_persona_relay` 用人格
口吻转译后通知主人。框架内置 `research_agent` / `code_agent` / `aigc_creator` /
`data_analyst` / `memory_curator` / `scheduler_assistant` 6 个画像 + 内部
`capability_evaluator`，业务画像（如 `finance_agent`）由插件注册。

🆕 **追问溯源**：每个节点跑完后 `artifact_put` 或框架兜底落一份 `output`
artifact 到 `AIAgentArtifact` 表。主人格在被追问"为什么这样选"时调
`artifact_get_recent` 工具回溯任务树最近一份 artifact 原文，避免主人格自己
web_search 编造与原代理推理不一致的解释。决策树 3.6 强制走这条路径。

🆕 **Windows subprocess 兼容**：`core.py` 把事件循环切到
`WindowsSelectorEventLoopPolicy` 以规避 ProactorEventLoop 关闭 socket 时的
InvalidStateError，但 SelectorEventLoop **不支持** 子进程——`code_agent`
在 Windows 上跑 `execute_shell_command` / `execute_file` 必抛
`NotImplementedError`。修复方案：在两个工具内分平台分支，Windows 走"同步
`subprocess.run` + `asyncio.to_thread`"，POSIX 仍走原生
`asyncio.create_subprocess_exec`，timeout 转译为 `asyncio.TimeoutError` 保持
上层契约。

🆕 **code_agent 工具集扩充**：从 6 个文件 / 命令工具扩到 16 个，新增
`render_markdown_to_image` / `render_html_to_image` / `send_message_by_ai` /
`state_*` / `search_knowledge` / `web_search_tool` / `web_fetch_tool` /
`get_current_date`，并改写 `_CODE_PROMPT` 强调"端到端跑完拿到产物、把生成的图
发给主人"。`match_keywords` 由抽象词扩成具体动作词（"绘制 / PIL / 渲染 /
生成图 / 运行 Python"等），提高主人格 resolve 命中率。

🆕 **webconsole 管理**：
- `/api/ai/kanban/*`：Kanban 5 列看板 / 任务详情 / 暂停 / 恢复 / 终止 /
  审批 / 重派 / 评估触发，见 `webconsole/docs/35-kanban.md`。
- `/api/ai/artifacts/*` + `/api/ai/kanban/tasks/{id}/workspace/*`：Artifact Hub
  与 Workspace 文件管理。
- `/api/ai/capability-agents/*`：画像 CRUD（builtin / plugin / user 三态权限），
  用户画像落在 `data/ai_core/capability_agents/<id>.json`，启动自动挂回。
  见 `webconsole/docs/34-capability-agents.md`。

### 13.2 HITL 人工审批流

Kanban 子任务连续重派达 3 次（默认 `DEFAULT_RESPAWN_LIMIT`）后会自动转
`waiting_approval`，根任务状态汇总也会变 `waiting_approval`。主人有两条审批
通路：

1. **webconsole**：在 Kanban 看板 Blocked 列点击卡片走
   `POST /api/ai/kanban/subtasks/{id}/approve`。
2. **对话回复**：主人直接对 bot 说"同意 / 拒绝（附说明）"，主人格调
   `respond_subtask_approval(approved, note, subtask_ref="")` 把决定回传——
   后端等价于 webconsole 端点。批准 → 子任务退回 `pending` 进入下次调度；
   拒绝 → 子任务 `failed`，主人格再决定是否 `fail_task_tree` 整树终结。

### 13.3 记忆生命周期（C11）`ai_core/memory/lifecycle/`

`run_lifecycle_maintenance` 由 APScheduler 每周触发，纯规则无 LLM：
- **巩固**：`mention_count ≥ 3` 的高频 Edge `decay_score` 回升 1.0。
- **衰减**：14 天未被检索且非高频的 Edge `decay_score *= 0.85`。
- **遗忘**：`decay_score < 0.1` 的 Edge 物理删除（SQL + Qdrant）。
- **孤儿实体回收**（在遗忘 Edge 之后）：非 speaker、无任何 edge、`updated_at`
  超过 10 天的孤儿实体物理删除（SQL + Qdrant + 递减分层图计数，按 500 分块）。
  遗忘 Edge 是孤儿的主要来源，故紧随其后回收，防止实体只增不减膨胀分类成本。

`AIMemEdge` 新增 `decay_score` / `last_accessed` 列；检索命中后台刷新 `last_accessed`。
`ingestion/edge.py` 增否定极性矛盾检测：同 src/tgt 高相似但极性相反 → 旧 Edge 软删除 +
记录 `AIMemConflict`，不向 LLM 堆叠新旧矛盾。

### 13.4 多模态摄入（C9）`ai_core/memory/ingestion/multimodal.py`

`handler.py` 的 Observer Hook 检测到图片 → `submit_image_observation` 纯规则过滤
（URL 去重 + 按 scope 限流）后投入独立 `_multimodal_queue`（与文本 `observation_queue`
物理隔离）→ `ImageUnderstandWorker` 异步调 `understand_image` 转述 → 以 `[图片理解]`
前缀包装成观察记录推入主 `observe()` 管道。图片风暴不阻塞文本聊天。

### 13.5 可视化调试台（C10）`webconsole/agent_debug_api.py`

三面板后端 API（需鉴权）：记忆图谱浏览/软删 Edge、长任务看板/步骤改写/终止、
self_model 演化层查看/人工修正。

---

## 附录

### A. 相关文件路径

| 文件 | 说明 |
|------|------|
| [`gsuid_core/handler.py`](gsuid_core/handler.py) | 事件处理入口 |
| [`gsuid_core/ai_core/__init__.py`](gsuid_core/ai_core/__init__.py) | AI Core 初始化 |
| [`gsuid_core/ai_core/ai_router.py`](gsuid_core/ai_core/ai_router.py) | Session 路由 |
| [`gsuid_core/ai_core/handle_ai.py`](gsuid_core/ai_core/handle_ai.py) | AI 处理入口 |
| [`gsuid_core/ai_core/gs_agent.py`](gsuid_core/ai_core/gs_agent.py) | AI Agent 实现（含图片处理） |
| [`gsuid_core/ai_core/utils.py`](gsuid_core/ai_core/utils.py) | 工具函数（prepare_content_payload、send_chat_result、SILENCE_MARKERS 沉默标记常量） |
| [`gsuid_core/ai_core/persona/config.py`](gsuid_core/ai_core/persona/config.py) | Persona 配置 |
| [`gsuid_core/ai_core/heartbeat/inspector.py`](gsuid_core/ai_core/heartbeat/inspector.py) | 巡检器 |
| [`gsuid_core/ai_core/heartbeat/decision.py`](gsuid_core/ai_core/heartbeat/decision.py) | LLM 决策 |
| [`gsuid_core/ai_core/mcp/__init__.py`](gsuid_core/ai_core/mcp/__init__.py) | MCP 模块导出 |
| [`gsuid_core/ai_core/mcp/client.py`](gsuid_core/ai_core/mcp/client.py) | MCP 客户端 |
| [`gsuid_core/ai_core/mcp/config_manager.py`](gsuid_core/ai_core/mcp/config_manager.py) | MCP 配置管理器 |
| [`gsuid_core/ai_core/mcp/mcp_tool_caller.py`](gsuid_core/ai_core/mcp/mcp_tool_caller.py) | 通用 MCP 工具调用 |
| [`gsuid_core/ai_core/mcp/mcp_tools_config.py`](gsuid_core/ai_core/mcp/mcp_tools_config.py) | MCP 工具配置 |
| [`gsuid_core/ai_core/mcp/startup.py`](gsuid_core/ai_core/mcp/startup.py) | MCP 启动注册 |
| [`gsuid_core/ai_core/image_understand/__init__.py`](gsuid_core/ai_core/image_understand/__init__.py) | 图片理解模块导出 |
| [`gsuid_core/ai_core/image_understand/understand.py`](gsuid_core/ai_core/image_understand/understand.py) | 图片理解接口 |
| [`gsuid_core/ai_core/web_search/search.py`](gsuid_core/ai_core/web_search/search.py) | 统一搜索接口 |
| [`gsuid_core/ai_core/meme/startup.py`](gsuid_core/ai_core/meme/startup.py) | 表情包模块启动 |
| [`gsuid_core/ai_core/meme/observer.py`](gsuid_core/ai_core/meme/observer.py) | 表情包消息监听 |
| [`gsuid_core/ai_core/meme/library.py`](gsuid_core/ai_core/meme/library.py) | 表情包库操作 |
| [`gsuid_core/ai_core/buildin_tools/meme_tools.py`](gsuid_core/ai_core/buildin_tools/meme_tools.py) | 表情包 AI 工具 |
| [`gsuid_core/webconsole/persona_api.py`](gsuid_core/webconsole/persona_api.py) | Persona API |
| [`gsuid_core/webconsole/mcp_config_api.py`](gsuid_core/webconsole/mcp_config_api.py) | MCP 配置 API |
| [`gsuid_core/utils/plugins_config/gs_config.py`](gsuid_core/utils/plugins_config/gs_config.py) | 配置管理 |
| [`gsuid_core/ai_core/memory/__init__.py`](gsuid_core/ai_core/memory/__init__.py) | 记忆系统模块导出 |
| [`gsuid_core/ai_core/memory/config.py`](gsuid_core/ai_core/memory/config.py) | 记忆系统配置 |
| [`gsuid_core/ai_core/memory/observer.py`](gsuid_core/ai_core/memory/observer.py) | 观察者管道 |
| [`gsuid_core/ai_core/memory/scope.py`](gsuid_core/ai_core/memory/scope.py) | Scope Key 隔离 |
| [`gsuid_core/ai_core/memory/ingestion/worker.py`](gsuid_core/ai_core/memory/ingestion/worker.py) | 摄入引擎 Worker |
| [`gsuid_core/ai_core/memory/retrieval/dual_route.py`](gsuid_core/ai_core/memory/retrieval/dual_route.py) | 双路检索引擎 |
| [`gsuid_core/ai_core/memory/database/models.py`](gsuid_core/ai_core/memory/database/models.py) | 记忆系统数据模型 |
| [`gsuid_core/ai_core/memory/vector/ops.py`](gsuid_core/ai_core/memory/vector/ops.py) | 向量存储操作 |
| [`gsuid_core/ai_core/memory/lifecycle/consolidation_worker.py`](gsuid_core/ai_core/memory/lifecycle/consolidation_worker.py) | C11 记忆生命周期维护 Worker |
| [`gsuid_core/ai_core/memory/ingestion/multimodal.py`](gsuid_core/ai_core/memory/ingestion/multimodal.py) | C9 多模态摄入队列 + Worker |
| [`gsuid_core/ai_core/planning/models.py`](gsuid_core/ai_core/planning/models.py) | C5 长任务三表模型 |
| [`gsuid_core/ai_core/planning/manager.py`](gsuid_core/ai_core/planning/manager.py) | C5 框架内部编排函数 |
| [`gsuid_core/ai_core/planning/executor.py`](gsuid_core/ai_core/planning/executor.py) | C5 定时唤醒执行器 + 崩溃恢复 |
| [`gsuid_core/ai_core/planning/tools.py`](gsuid_core/ai_core/planning/tools.py) | C5/C7 暴露给 LLM 的长任务工具 |
| [`gsuid_core/ai_core/capability_agents/registry.py`](gsuid_core/ai_core/capability_agents/registry.py) | 能力代理画像注册表 + resolve_profile |
| [`gsuid_core/ai_core/capability_agents/profiles.py`](gsuid_core/ai_core/capability_agents/profiles.py) | 内置 research/code 能力代理画像 |
| [`gsuid_core/ai_core/capability_agents/runner.py`](gsuid_core/ai_core/capability_agents/runner.py) | 能力代理运行器（无人格 Plan-Solve 执行体） |
| [`gsuid_core/ai_core/multimodal/__init__.py`](gsuid_core/ai_core/multimodal/__init__.py) | 多模态模块导出 |
| [`gsuid_core/ai_core/multimodal/asr.py`](gsuid_core/ai_core/multimodal/asr.py) | 语音转文字 |
| [`gsuid_core/ai_core/multimodal/tts.py`](gsuid_core/ai_core/multimodal/tts.py) | 文字转语音 |
| [`gsuid_core/ai_core/multimodal/video.py`](gsuid_core/ai_core/multimodal/video.py) | 视频理解 |
| [`gsuid_core/ai_core/multimodal/document.py`](gsuid_core/ai_core/multimodal/document.py) | 文档提取 |
| [`gsuid_core/ai_core/persona/mood.py`](gsuid_core/ai_core/persona/mood.py) | 情绪状态机 |
| [`gsuid_core/ai_core/persona/group_context.py`](gsuid_core/ai_core/persona/group_context.py) | 群聊适应性 |
| [`gsuid_core/webconsole/agent_debug_api.py`](gsuid_core/webconsole/agent_debug_api.py) | C10 Agent 可视化调试台 API |

### B. 配置热重载矩阵

| 配置项 | 热重载 | 生效时机 | 备注 |
|--------|--------|----------|------|
| `enable` | ✅ | 下次消息处理 | AI 全局开关 |
| `ai_black_list` | ✅ | 下次消息处理 | |
| `ai_white_list` | ✅ | 下次消息处理 | |
| `scope` | ✅ | 下次会话匹配 | |
| `target_groups` | ✅ | 下次会话匹配 | |
| `ai_mode` | ✅ | 下次消息处理 | |
| `inspect_interval` | ⚠️ | 需重启巡检任务 | 会自动重启 |
| `keywords` | ✅ | 下次消息处理 | |
| `model_name` | ✅ | 下次创建 Session | `create_agent()` 每次动态获取 `get_openai_chat_model()` |

### C. Session ID 格式说明

```
Session ID 格式说明:

群聊:
  session_id = f"{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}"
  示例: "ws-onebot:onebot:bot_001:group:789012"

私聊:
  session_id = f"{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}"
  示例: "ws-onebot:onebot:bot_001:private:345678"

说明:
- 使用 {WS_BOT_ID}:{bot_id}: 前缀同时区分 WS 链接和平台
- 群聊使用 group: 前缀，私聊使用 private: 前缀
- 群聊共享同一个 session_id，实现上下文共享
- 用于在 HistoryManager 中唯一标识一个会话
```

### D. 已知问题汇总

| 问题 ID | 严重程度 | 影响模块 | 问题描述 | 状态 | 详见章节 |
|---------|----------|----------|----------|------|----------|
| D-1 | 🔴 致命 | AI Router | Session ID 绑定 user_id，导致群聊上下文割裂 | ✅ 已修复 | 5.6.1 |
| D-2 | 🔴 性能 | Heartbeat | 定时巡检可能引发并发雪崩和 Token 消耗 | ✅ 已修复 | 6.7.1 |
| D-3 | 🟡 设计 | Persona | Persona Prompt 修改后 Session 不更新 | ✅ 已修复 | 5.6.2 |
| D-4 | 🔴 安全 | Handler | 单条消息无长度保护，可能引发 Token 爆炸 | ✅ 已修复 | 2.3 |
| D-5 | 🔴 致命 | Heartbeat | _Bot 与 Bot 混淆导致 bot_self_id 缺失 | ✅ 已修复 | 6.7.2 |
| D-6 | 🟡 文档 | 文档 | 附录 C 仍显示旧格式 session_id 示例 | ✅ 已修复 | 附录 C |
| D-7 | 🔴 安全 | WebConsole | API 文件上传缺乏 MIME 类型检查 | ✅ 已修复 | 7.3 |
| D-8 | 🔴 性能 | Handler | 用户触发缺乏并发控制，可能引发 Rate Limit | ✅ 已修复 | 2.5 |
| D-9 | 🟡 设计 | Handler | 长文本粗暴截断导致语法破损，已改用 subagent 智能摘要替代 | ✅ 已修复 | 2.3 |
| D-10 | 🔴 安全 | Handler | 缺乏绝对长度上限，10万字文本导致子Agent Token爆炸 | ✅ 已修复 | 2.3 |
| D-11 | 🟡 性能 | handle_ai | RAG 强制前置检索，闲聊消息多1~2秒延迟+无意义Token消耗 | ✅ 已修复 | 2.6 |
| D-12 | 🔴 正确性 | worker.py | Edge去重key拼接f-string错误，三个相邻f-string未用括号包裹导致key只含source后的|字符，去重失效 | ✅ 已修复 | 10.6 |
| D-13 | 🔴 正确性 | worker.py/entity.py | entity计数虚高：increment_entity_count使用len(entity_name_to_id)包含已存在实体，导致HierGraph频繁重建 | ✅ 已修复 | 10.6 |
| D-14 | 🟡 正确性 | hiergraph.py | _apply_entity_assignments新建Category时existing_ids初始化缺失，可能导致重复插入 | ✅ 已修复 | 10.8 |
| D-15 | 🟡 性能 | models.py | Entity向量去重O(N)串行await，N=10时10次串行Qdrant查询 | ✅ 已修复 | 10.10 |
| D-16 | 🟡 性能 | dual_route.py | Reranker三路(episodes/entities/edges)串行调用，可改为asyncio.gather并行 | ✅ 已修复 | 10.7 |
| D-17 | 🟡 性能 | models.py | ORM Relationship lazy='selectin'导致N+1查询问题，应改为'noload'显式加载 | ✅ 已修复 | 10.9 |
| D-18 | 🟡 设计 | hiergraph.py | Layer-1 Speaker归类仅依赖LLM遵守指令，代码层缺乏硬性保障 | ✅ 已修复 | 10.8 |
| D-19 | 🟡 设计 | system1.py | System-1 One-hop邻居扩展未实现，与论文Section 2.3描述不符 | ✅ 已修复 | 10.7 |
| D-20 | 🟡 正确性 | gs_agent.py | 强制总结（UsageLimitExceeded fallback）偏离用户原始问题，AI自我总结而非直接回答 | ✅ 已修复 | 5.6.3 |
| D-21 | 🔴 安全 | 全局 | AI总开关关闭后，各模块启动钩子和定时任务仍可能执行AI逻辑 | ✅ 已修复 | 2.2/8.4.1 |

---

## 修订历史

| 日期 | 版本 | 修改内容 |
|------|------|----------|
| 2026-04-11 | v1.0 | 初始版本 |
| 2026-04-11 | v1.1 | 新增已知问题 D-1 (群聊上下文割裂)、D-2 (并发雪崩)、D-3 (Prompt 热重载) |
| 2026-04-11 | v1.2 | 新增 D-4 (输入截断)，修复章节编号问题 |
| 2026-04-11 | v1.3 | 新增 D-5 (文档旧格式)、D-7 (API文件上传类型检查) |
| 2026-04-11 | v1.4 | 新增第8节 AI Statistics 统计系统，包含完整的统计数据分类、数据库模型、每日持久化机制和前端API接口 |
| 2026-04-11 | v1.5 | 修复 AI Core 模块结构（补充遗漏模块）、修复 statistics 使用示例错误（get_statistics_manager -> statistics_manager）、更新 AI Router Session 创建流程以匹配实际代码 |
| 2026-04-11 | v1.6 | 移除费用计算相关代码（cost_usd/cost_cny）、AIHeartbeatMetrics 改为 should_not_speak_count、补充 AIGroupUserActivityStats 模型文档 |
| 2026-04-12 | v1.7 | 更新 AI Core 模块结构（新增 file_manager.py/self_info.py）、更新工具注册系统文档（category 分类）、新增 5.5 节工具注册系统与 Agent 架构、修正 5.6.1 节 Session ID 实际格式、更新 8.4.3 节定时任务实现（APScheduler cron）、新增 D-8/D-9 待改进问题（并发控制/长文本截断） |
| 2026-04-12 | v1.8 | 修复 D-8（用户触发并发控制，使用 `_ai_semaphore` 信号量限制）、D-9（长文本截断已实现但仍为粗暴截断，待进一步优化为智能截断） |
| 2026-04-12 | v1.9 | 完整修复 D-9：移除 handler.py 粗暴截断逻辑，改为在 handle_ai.py 中调用 create_subagent 智能摘要（>2000字符触发），新增"文本摘要专家"系统提示词；更新 8.2 流程图补充并发控制(步骤2)和长文本摘要(步骤5.5)；修正 D-4/D-9 问题表章节引用（2.4→2.3）；D-9 状态更新为已修复 |
| 2026-04-12 | v2.0 | 修复 D-10（双层长度防护：新增 ABSOLUTE_MAX_LENGTH=10000 硬截断层，防止子Agent Token爆炸）；修复 D-11（RAG 强制前置检索改为主Agent工具按需调用：移除 handle_ai.py 中强制 query_knowledge 逻辑，改由 LLM 自主调用 search_knowledge 工具，消除闲聊场景 1~2 秒无谓延迟）；更新 2.3 节（双层防护表格）、新增 2.6 节（RAG 按需调用对比说明）、更新 8.2 流程图（步骤3双层防护+步骤6历史上下文说明）、更新附录 D（D-10/D-11）|
| 2026-04-18 | v3.0 | 新增第10节 Memory 记忆系统：基于 Mnemis 双路检索的多群组/多用户 Agent 记忆系统，包含 Observer 观察者管道、Ingestion 摄入引擎（两阶段 Entity 去重 + Edge 冲突检测）、Dual-Route Retrieval 双路检索（System-1 向量相似度 + System-2 分层图遍历 + Reranker 重排序）、Hierarchical Graph 分层语义图、Scope Key 隔离体系、SQLAlchemy 图结构模型 + Qdrant 向量索引；更新 1.1 节目录结构（新增 memory/ 模块）；更新 9 节统计系统（新增 7 项记忆统计指标）；更新附录 A（新增记忆系统相关文件路径）；更新完整流程图章节编号（8→11） |
| 2026-04-19 | v3.1 | 全面核对 ai_core 代码与文档一致性，修正以下内容：1.1 模块结构（新增 dynamic_tool_discovery.py、dataclass_models.py、startup.py、scheduled_task/scheduler.py，移除 adapter.py、episode.py）；2.3 MAX_SUMMARY_LENGTH 4000→8000；3.1 AI 处理流程（8步：含记忆检索、send_chat_result、observe）；5.1 Session 创建（create_agent 移除 model_name、新增 create_by="Chat"，mtime 缓存，session_id 格式 bot:{bot_id}:group:{group_id}）；5.3 内存保护（DEFAULT_MAX_MESSAGES=40、MAX_AI_HISTORY_LENGTH=30、移除 MAX_HISTORY_CHARS、Agent 内部截断含 ToolCall/ToolReturn 配对保护）；5.5.2 @ai_tools 新增 check_func/**check_kwargs 参数和智能参数注入；5.5.7 buildin 工具新增 query_user_memory；5.5.10-5.5.11 动态工具发现和核心函数签名；6.4 巡检流程（_pre_check_session + _inspect_session_with_semaphore 两阶段）；6.5 决策 Prompt（mood/context_hook 替代 reason，_parse_decision_json，_strip_message_quotes）；6.7.1 INACTIVE_THRESHOLD_HOURS=1；6.7.2 _get_bot_for_session 三级查找（gss.active_bot）；7.2 模块结构（scheduler.py、startup.py）；7.4.2 独立工具函数替代 manage_scheduled_task；7.6-7.11 架构图和使用流程；8.1 统计模块结构（dataclass_models.py、startup.py）；8.3 数据库模型（BaseIDModel、AITokenUsageByType、api_529_count、memory 字段）；8.4 持久化机制（startup.py）；8.6 record_token_usage 新增 chat_type 参数；10.2 移除 episode.py；10.4 Scope Key 格式修正（ScopeType.GROUP:789012）；10.5 ObservationRecord 移除 ai_reply；10.6/10.11 batch_interval_seconds=1800、llm_semaphore_limit=2；10.9 数据库模型（SQLModel 非 BaseIDModel、AIMemHierarchicalGraphMeta 在 hiergraph.py）；10.12.3 启动初始化（无 create_all、IngestionWorker() 无参）；11.3 Heartbeat 流程图（mood/context_hook）；附录 B model_name 热重载 ✅；附录 C Session ID 格式 bot:{bot_id}:group:{group_id} |
| 2026-04-24 | v3.2 | Memory 系统 Bug 修复与性能优化：修复 B-01（Edge 去重 key 拼接 f-string 错误，worker.py）；修复 B-02（entity 计数虚高导致频繁重建 hiergraph，models.py/entity.py/worker.py）；修复 B-03（_apply_entity_assignments 新建 Category 初始化缺失，hiergraph.py）；优化 P-01（Entity 向量去重串行改并行，models.py）；优化 P-04（Reranker 三路并行化，dual_route.py）；优化 P-03（ORM Relationship lazy='selectin' 改为 'noload'，消除 N+1 查询，models.py）；修复 M-06（Speaker 强制 Layer-1 归类硬性保障，hiergraph.py）；新增 System-1 One-hop 邻居扩展（system1.py/ops.py）；新增已知问题 D-12~D-17 |
| 2026-05-05 | v4.0 | **MCP 重构 + Image Understand + Meme Module + Web Search 统一接口**：1.1 模块结构（新增 mcp/mcp_tool_caller.py、mcp/mcp_tools_config.py、image_understand/ 模块、meme/ 模块）；5.5.7 buildin 工具新增 web_fetch、send_meme/collect_meme/search_meme；5.5.12 MCP 工具集成全面更新（新增 register_as_ai_tools/tools 字段、MCP 工具 ID 格式、mcp_tools_config 配置、通用 call_mcp_tool 调用、MCP 预设配置、4 个新 API 端点）；8.0 MCP 配置 API 新增 tools/discover/import/presets 端点；新增 10.14 Meme 表情包模块（引用 MEME_MODULE.md）；新增 10.15 Image Understand 图片理解模块（MCP 驱动，GsCoreAIAgent._prepare_user_message 自动处理）；新增 10.16 Web Search 统一搜索接口（Tavily/Exa/MCP 三选一，MiniMax 搜索迁移至 MCP）；附录 A 新增 MCP/ImageUnderstand/Meme/WebSearch 相关文件路径 |
| 2026-05-11 | v4.1 | **修复 D-20（强制总结偏离用户问题）v3 到 v4 演进**：v3 实现保留 `_last_user_question`、`_extract_known_facts()`、message_history 置空、无工具 Agent；v4 在 v3 基础上进一步把 `_extract_known_facts` 替换为 `_extract_run_context`（按轮次保留工具返回+LLM 中间推理），去掉 fallback Agent 冗余的 `deps_type/deps` 参数，修正错误处理避免消息双发；更新 5.6.3 节 |
| 2026-05-14 | v4.2 | **AI 总开关控制全面修复**：1. `handle_ai.py` 中 `enable_ai` 改为函数内动态读取（`ai_config.get_config("enable").data`），确保 WebConsole 切换开关后无需重启即可生效；2. 所有 `@on_core_start` 钩子增加 `enable_ai` 检查：rag/startup.py、persona/startup.py、memory/startup.py、statistics/startup.py（heartbeat）、scheduled_task/startup.py、mcp/startup.py、mcp/server.py；3. `scheduled_task/executor.py` 执行前增加 `enable_ai` 检查；4. `heartbeat/inspector.py` 启动前增加 `enable_ai` 检查；5. 更新 2.2 节、8.4.1 节、10.12.3 节、12.2 节文档描述；6. 新增已知问题 D-21（AI 总开关控制不全面） |
| 2026-05-19 | v4.3 | **拟人化 Agent 升级 P0+P1+C8**（依据 `plans/agent_design_review.md`，详见归档文档 `docs/backups/AGENT_UPGRADE_P0P1C8_20260519.md`）：C1 记忆摄入纯规则质量门控（命令回显/注入/复读过滤 + HIGH/LOW 价值分级，`ObservationRecord` 新增 `value_tier`）；C1 Edge 跨发言者归并（`AIMemEdge` 新增 `mention_count` 列，旧库经 startup.py ALTER 补齐）；C6 新增 `ScopeType.SELF`，Bot 自身发言路由 `self:{bot_id}` 轻量摄入；C2 别名系统接通记忆链路（抽取提示词注入"已知别名+已存在实体"、`ai_alias` 加 `scope` 参数、`_ALIASES` 改分 scope 结构）；C3 自我认知（新增 `ai_core/self_cognition.py`，每轮动态注入 `self_cognition_context`、`@ai_tools` 加 `capability_domain`、主人实体打 `Master` 标签、新增 `update_self_note` 工具）；C4 记忆检索寒暄门控 + 主人记忆预算优先级；R1 `normalize.py` 降级为命令层 fallback；C8 新增 `heartbeat/dispatcher.py` 统一主动消息网关（防撞车 + 任务结果合并进 Heartbeat 语境）。更新 §10.4 / §10.5 |
| 2026-05-20 | v4.4 | **能力代理架构升级**（最终语义见 `AGENT_CAPABILITY_AGENT_MERGED_20260521.md`）：历史截断纳入 `RetryPromptPart`（`gs_agent.py` 截断函数 + 新增 `_drop_orphan_tool_results` 自愈兜底，根治"久聊必崩"400）；能力代理架构（新增 `ai_core/capability_agents/` 模块——执行/表达分离，长任务由无人格能力代理推进、结果经 `_persona_relay` 人格转译；`AIAgentTask` 新增 `agent_profile` 列；新增 `task_pause`/`task_resume` 工具；`register_long_task`/`create_subagent` 加 `agent_profile` 参数）；系统提示词改造（决策树补长任务分支 3.5、认知防火墙/极简原则改写、新增 `voice_anchor` 逐轮口吻锚点）；`register_long_task` 创建后立即执行第一步 + 单轮意图-行为一致性检测。更新 §13.1 |
| 2026-05-21 | v4.5 | **能力代理架构合并收尾**（详见 `AGENT_CAPABILITY_AGENT_MERGED_20260521.md`）：主人格追问溯源（`AIAgentTask` 新增 `last_artifact` 列、新增 `task_get_last_artifact` LLM 工具、决策树补 3.6 追问溯源分支、`last_artifact` 空串回退到 `step.result_summary`）；`_RESEARCH_PROMPT` 重写"工具优先级+诚实底线+结论双段"；`code_agent` 工具集扩到 16 个；Windows subprocess 兼容 SelectorEventLoop；`next_run_at` 持久化调度；新增 webconsole `long_tasks_api` / `capability_agents_api` 两套 REST API + 用户画像 JSON 持久化；二次审计联动收尾（`capability_agents/registry` 新增 `unregister_capability_agent` 公开 API、`_dto_to_profile` 改显式存在性检查、`heartbeat/dispatcher` 与 `inspector._target_key` 去 `dict.get`/`getattr`、`gs_agent._drop_orphan_tool_results` 拆双 isinstance 分支、`webconsole/capability_agents_api` 的 `_pick` 内联展开等） |
| 2026-05-27 | v4.6 | **沉默标记统一化**：`utils.py` 新增 `SILENCE_MARKERS` 模块级常量（`frozenset`，含 `<SILENCE>`/`[SILENCE]`/`SILENCE`/`<end_turn>`），`gs_agent.py`、`handle_ai.py`、`heartbeat/decision.py`、`send_chat_result`、`extract_json_from_text` 全部改为引用此常量，消除散落的硬编码列表；修复模型输出 `<end_turn>` 时被当作普通消息发送的 bug |
