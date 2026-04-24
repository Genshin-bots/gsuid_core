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
11. [完整流程图](#11-完整流程图)
12. [附录](#附录)
   - [D. 已知问题汇总](#d-已知问题汇总)

---

## 1. 系统概述

### 1.1 AI Core 模块结构

```
gsuid_core/ai_core/
├── __init__.py          # 核心初始化入口
├── ai_router.py         # Session 路由管理
├── check_func.py        # 检查函数
├── gs_agent.py          # AI Agent 实现
├── handle_ai.py         # AI 聊天处理入口
├── models.py            # 数据模型
├── normalize.py         # 查询规范化 (已移至子模块)
├── register.py          # 工具注册
├── resource.py          # 资源管理
├── utils.py             # 工具函数
├── configs/             # 配置文件
│   ├── __init__.py
│   ├── ai_config.py     # AI 全局配置
│   └── models.py        # 配置数据模型
├── buildin_tools/       # 内建 AI 工具
│   ├── __init__.py
│   ├── command_executor.py  # 执行系统命令
│   ├── database_query.py    # 数据库查询（好感度/记忆）
│   ├── dynamic_tool_discovery.py  # 动态工具发现
│   ├── favorability_manager.py  # 好感度管理
│   ├── file_manager.py      # 文件管理 (read/write/execute/diff/list)
│   ├── get_time.py          # 获取时间
│   ├── message_sender.py    # 消息发送
│   ├── rag_search.py        # RAG 检索 (knowledge/image)
│   ├── scheduler.py         # 预约定时任务（独立工具函数）
│   ├── self_info.py         # 获取自身 Persona 信息
│   ├── subagent.py          # 创建子Agent
│   └── web_search.py        # Web 搜索
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
├── system_prompt/        # System Prompt 管理
│   ├── __init__.py
│   ├── defaults.py
│   ├── models.py
│   ├── search.py
│   ├── storage.py
│   └── vector_store.py
└── web_search/           # Web 搜索
    ├── __init__.py
    └── search.py
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

### 2.2 AI 触发条件 (handler.py: 298-357)

```python
# 检查顺序
1. enable_ai 全局开关检查
   └── ai_config.get_config("enable").data

2. 黑白名单检查
   ├── user_in_black_list = event.user_id in ai_black_list
   ├── group_in_black_list = event.group_id in ai_black_list
   ├── user_in_white_list = event.user_id in ai_white_list
   └── group_in_white_list = event.group_id in ai_white_list

3. Persona 配置检查
   ├── session_id = f"{bid}%%%{temp_gid}%%%{uid}"
   └── persona_name = persona_config_manager.get_persona_for_session(session_id)

4. AI Mode 检查
   ├── "提及应答" in ai_mode: 检查 @机器人 或 关键词
   └── 其他模式...

5. 任务入队
   └── ws.queue.put_nowait(TaskContext(coro=handle_ai_chat(...)))
```

---

### 2.3 双层长度防护机制（D-9、D-10 修复）

**问题**: 原代码对超大文本缺乏硬上限保护。恶意用户发送 10 万字文本时，系统会把原始文本直接塞给子Agent摘要，导致 OpenAI 单次输入超限或消耗数万 Token。

**修复方案**: 在 `handle_ai_chat()` 中引入**双层长度防护**：

```python
# handle_ai.py
ABSOLUTE_MAX_LENGTH = 14000  # 第一层：绝对上限，超过直接硬截断
MAX_SUMMARY_LENGTH = 8000    # 第二层：摘要阈值，超过则调用子Agent智能摘要

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
| 第一层 | `> 14000` 字符 | 硬截断至 14000 字符 + 截断提示 | 防止子Agent Token爆炸、API超限 |
| 第二层 | `> 8000` 字符 | 调用子Agent智能摘要 | 压缩长文本，保留关键信息 |
| 无需处理 | `≤ 8000` 字符 | 直接传递给主Agent | 正常短消息处理 |

> **说明**：第二层阈值从 2000 调整为 8000，因为现代 LLM 上下文窗口动辄 128K（约 10 万汉字），2000 字符对 LLM 来说毫无压力。对于代码、报错日志等长文本，摘要会丢失细节，应尽量避免自动摘要。

**新增 System Prompt** (`system_prompt/defaults.py`):
- ID: `default-text-summarizer`
- Title: 文本摘要专家
- Tags: 摘要、总结、压缩、文本处理、长文本

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
   ├── > 14000 字符: 硬截断（防子Agent Token爆炸）
   └── > 8000 字符: 调用 create_subagent 智能摘要

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

    # 检查是否已存在 AI session
    session = history_manager.get_ai_session(session_id)
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

    # 保存到 HistoryManager
    history_manager.set_ai_session(session_id, session)
    history_manager.update_session_access(event)

    return session
```

**Session ID 格式**:
```
# 群聊时: 以 bot: 为前缀，包含 bot_id
session_id = f"bot:{bot_id}:group:{group_id}"
示例: "bot:onebot:group:789012"

# 私聊时: 以 bot: 为前缀，包含 bot_id
session_id = f"bot:{bot_id}:private:{user_id}"
示例: "bot:onebot:private:345678"
```

### 5.2 Session 存储

Session 存储在 `HistoryManager` 中 (`history/manager.py`):

```python
class HistoryManager:
    def __init__(self):
        self._ai_sessions: Dict[str, GsCoreAIAgent] = {}

    def get_ai_session(self, session_id: str) -> Optional[GsCoreAIAgent]:
        return self._ai_sessions.get(session_id)

    def set_ai_session(self, session_id: str, session: GsCoreAIAgent):
        self._ai_sessions[session_id] = session
```

### 5.3 内存保护机制 (滑动窗口 + 自动清理)

HistoryManager 包含完善的内存保护机制，**不存在 OOM 风险**：

#### 5.3.1 滑动窗口机制

```python
# 每个 Session 使用 deque 限制消息数量
DEFAULT_MAX_MESSAGES = 40  # 每 Session 最多保留 40 条消息
MAX_AI_HISTORY_LENGTH = 30  # AI 对话历史最大长度

# 在 __init__ 中
self._histories[storage_event] = deque(maxlen=self._max_messages)
```

**效果**: 每个 Session 的消息历史被限制在 `deque(maxlen=40)` 中，超过限制的旧消息自动被丢弃。

> **注意**：群聊场景下，`storage_event` 的 `user_id` 被设为空字符串，确保同一群聊的所有用户消息共享同一个 deque。

#### 5.3.2 空闲 Session 清理

```python
IDLE_THRESHOLD = 86400  # 空闲阈值（秒），默认 1 天
CLEANUP_INTERVAL = 3600  # 清理检查间隔（秒），默认 1 小时

# 启动清理循环
async def start_cleanup_loop(self):
    self._cleanup_task = asyncio.create_task(self._cleanup_loop())

# 清理逻辑
async def cleanup_idle_sessions(self, idle_threshold: int = None):
    # 清理超过阈值未活跃的 AI Session
    if current_time - last_access > idle_threshold:
        self.remove_ai_session(session_id)
```

**效果**: 超过 1 天未活跃的 Session 自动从内存中清除。

#### 5.3.3 内存保护总结

| 机制 | 配置 | 效果 |
|------|------|------|
| 滑动窗口 | `deque(maxlen=40)` | 每 Session 最多 40 条消息 |
| AI 历史限制 | `MAX_AI_HISTORY_LENGTH=30` | AI 对话历史不超过 30 条 |
| Agent 内部截断 | `max_history=50` | `GsCoreAIAgent.history` 超过 50 条时安全截断（含 ToolCall/ToolReturn 配对保护） |
| 空闲清理 | `IDLE_THRESHOLD=86400` (1天) | 1天不活跃的 Session 自动清除 |
| 定时清理 | `CLEANUP_INTERVAL=3600` (1小时) | 每小时检查一次空闲 Session |

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
# 群聊: bot:{bot_id}:group:{group_id}
# 私聊: bot:{bot_id}:private:{user_id}
```

```python
# models.py - Event.session_id 属性
# session_id 由 Event 的属性自动生成
# 群聊: f"bot:{bot_id}:group:{group_id}"
# 私聊: f"bot:{bot_id}:private:{user_id}"
```

**关键区别**：
- Session ID 不再包含 `user_id`（群聊场景），群内所有用户共享同一个 Session
- 私聊时使用 `user_id` 作为标识，确保一对一对话的独立性

**实际 Session 路由逻辑** (`ai_router.py`):
```python
# session_id 格式: "bot:{bot_id}:group:{group_id}" 或 "bot:{bot_id}:private:{user_id}"
# AI Router 使用 event.session_id 获取或创建 Session
# HistoryManager 以 Event 为 key 存储历史记录（群聊时 user_id 置空以保证一致性）
```

修改后的架构：
- `Event.session_id` 格式为 `bot:{bot_id}:group:{group_id}` 或 `bot:{bot_id}:private:{user_id}`
- `get_persona_for_session()` 解析 session_id 提取 `group_id` 或 `user_id` 用于 Persona 匹配
- `HistoryManager` 以 `Event` 对象为 key 存储历史记录，群聊时 `user_id` 置空确保同一群聊共享 deque
- AI Session 的共享由 `history_manager._ai_sessions` 决定，按 `session_id` 字符串存储

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

系统采用**渐进式加载**机制，工具按用途和重要性分为四个层级：

| 分类 | 说明 | 加载方式 | 示例 |
|------|------|----------|------|
| `self` | 仅为自身服务的能力 | 主Agent专属，始终加载 | 好感度管理、发送消息、创建子Agent |
| `buildin` | 默认内置工具 | 主Agent始终加载 | 知识库检索、Web搜索、查询记忆 |
| `common` | 通常工具 | 按需加载，用户明确需要时 | 定时任务管理、获取自身信息 |
| `default` | 子Agent工具 | 由子Agent使用 | 文件操作、日期获取、系统命令 |

**加载优先级**: `self` > `buildin` > `common`

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
│  │ common 工具 (按需加载)                                │   │
│  │ - 定时任务管理 (add/list/query/modify/cancel...)    │   │
│  │ - 获取自身Persona信息                                │   │
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

**主Agent (Main Agent)**
- 使用 `get_main_agent_tools()` 获取基础工具集
- 加载 `self` 和 `buildin` 分类的所有工具（始终加载）
- 通过 `search_tools(non_category=["self", "buildin"])` 按需加载 `common` 分类工具
- **不会调用 `default` 分类的工具**

**子Agent (Sub Agent)**
- 由 `create_subagent()` 创建
- 使用 `search_tools(non_category="self")` 搜索工具
- 加载 `buildin`、`common`、`default` 分类的工具
- **不会调用 `self` 分类的工具**（如 `query_user_favorability`、`send_message_by_ai` 等）

这种设计确保了工具调用的安全性：
- `self` 工具仅限主Agent使用，防止子Agent直接操作用户数据
- `default` 工具（如文件操作、系统命令）仅通过子Agent使用

#### 5.5.6 Self 工具 (`category="self"`)

主Agent专属工具，用于自身能力调用，始终加载。

| 工具 | 说明 |
|------|------|
| `query_user_favorability` | 查询用户好感度 |
| `update_user_favorability` | 更新用户好感度（增量） |
| `send_message_by_ai` | 发送消息给用户 |
| `create_subagent` | 创建子Agent |

#### 5.5.7 主Agent内置工具 (`category="buildin"`)

主Agent默认加载的核心工具，直接调用。

| 工具 | 说明 |
|------|------|
| `search_knowledge` | 检索知识库内容 |
| `web_search` | Web搜索 (Tavily API) |
| `query_user_memory` | 查询用户记忆条数 |

#### 5.5.8 通常工具 (`category="common"`)

当用户明确需要相关功能时按需加载。

| 工具 | 说明 |
|------|------|
| `get_self_persona_info` | 获取自身Persona信息 |
| `add_once_task` | 添加一次性定时任务 |
| `add_interval_task` | 添加循环任务 |
| `list_scheduled_tasks` | 列出所有定时任务 |
| `query_scheduled_task` | 查询任务详情 |
| `modify_scheduled_task` | 修改任务 |
| `cancel_scheduled_task` | 取消任务 |
| `pause_scheduled_task` | 暂停任务 |
| `resume_scheduled_task` | 恢复任务 |

#### 5.5.9 子Agent工具 (`category="default"`)

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

#### 5.5.10 动态工具发现

**文件位置**: [`gsuid_core/ai_core/buildin_tools/dynamic_tool_discovery.py`](gsuid_core/ai_core/buildin_tools/dynamic_tool_discovery.py)

系统提供动态工具发现能力，允许 AI 根据任务需求搜索可能用到的新工具。

| 工具 | 说明 | 状态 |
|------|------|------|
| `discover_tools` | 根据任务描述搜索相关工具 | 已定义但未注册（装饰器被注释） |
| `list_available_tools` | 列出当前系统中所有可用的AI工具 | 已定义但未注册（装饰器被注释） |

> **注意**：这两个工具函数已实现但 `@ai_tools` 装饰器被注释掉，暂未作为 AI 工具注册。主Agent 通过 `gs_agent.py` 中的 `search_tools()` 函数实现类似的动态工具发现能力。

#### 5.5.11 核心函数

```python
def get_main_agent_tools() -> ToolList:
    """获取主Agent专用工具（self + buildin 分类）"""
    all_tools_cag = get_registered_tools()
    all_tools = {}
    for cat in ["self", "buildin"]:
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

每个 action 对应一个独立的 AI 工具函数，均使用 `@ai_tools(category="common")` 注册。

**工具列表**：

| 工具函数 | 说明 | 必需参数 |
|----------|------|----------|
| `add_once_task` | 添加一次性定时任务 | run_time, task_prompt |
| `add_interval_task` | 添加循环任务 | interval_value, task_prompt, interval_type, max_executions |
| `list_scheduled_tasks` | 列出当前用户的所有定时任务 | - |
| `query_scheduled_task` | 查询指定任务的详细信息 | task_id |
| `modify_scheduled_task` | 修改定时任务 | task_id, task_prompt?, max_executions? |
| `cancel_scheduled_task` | 取消定时任务 | task_id |
| `pause_scheduled_task` | 暂停循环任务 | task_id |
| `resume_scheduled_task` | 恢复已暂停的循环任务 | task_id |

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

### 7.1 Persona API 端点

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
    # 启动 HistoryManager 的清理任务
    history_manager = get_history_manager()
    await history_manager.start_cleanup_loop()

    start_heartbeat_inspector()

    statistics_manager._today = datetime.now().strftime("%Y-%m-%d")
    await statistics_manager._load_today_data_from_db()  # 从数据库加载今日数据
```

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
- **单进程 asyncio.Queue**：避免进程间通信的复杂性。

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
├── observer.py           # 观察者管道（asyncio.Queue + 过滤逻辑）
├── startup.py            # @on_core_start 初始化入口
├── database/             # 图结构存储（SQLAlchemy，独立 MemBase）
│   ├── __init__.py       # _MemorySessionFactory + get_async_session
│   └── models.py         # 6 个模型 + 2 个关联表
├── ingestion/            # 摄入引擎（后台消费 + LLM 提取）
│   ├── __init__.py
│   ├── worker.py         # IngestionWorker（单实例后台任务）+ _ingest_batch()
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
│  │     asyncio.Queue (maxsize=10000) │               │              │
│  │     _observation_queue            │               │              │
│  └──────────────┬───────────────────┘               │              │
│                 │                                    │              │
│                 ▼                                    │              │
│  ┌──────────────────────────────────┐               │              │
│  │     IngestionWorker (单实例)       │               │              │
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
| `GROUP` | `ScopeType.GROUP:{group_id}` | 群组级记忆，群内所有消息共享 |
| `USER_GLOBAL` | `ScopeType.USER_GLOBAL:{user_id}` | 用户跨群全局画像 |
| `USER_IN_GROUP` | `ScopeType.USER_IN_GROUP:{user_id}@{group_id}` | 用户在特定群组内的局部档案（可选精细化） |

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

Observer 是记忆系统的"被动感知层"，通过 `asyncio.Queue` 在单进程内传递观察记录。

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
```

**过滤规则** (`_should_observe()`)：

| 规则 | 说明 |
|------|------|
| 自身消息过滤 | `speaker_id == bot_self_id` 时不入队 |
| 黑名单群组 | `group_id in observer_blacklist` 时不入队 |
| 过短内容 | `< 5` 字符的纯表情/单字回复不入队 |
| 纯图片/文件 | 无文字内容不入队 |

**队列溢出策略**：队列满时丢弃最老的一条，保证新消息不丢失。

**调用方式**：
```python
# handler.py 中（消息入口）
asyncio.create_task(
    observe(
        content=event.raw_text,
        speaker_id=str(event.user_id),
        group_id=str(event.group_id or event.user_id),
        bot_self_id=str(ws.bot_id),
        observer_blacklist=memory_config.observer_blacklist,
        message_type="group_msg" if event.group_id else "private_msg",
    )
)

# handle_ai.py 中（AI 回复后）
asyncio.create_task(
    observe(
        content=chat_result,
        speaker_id=f"bot_{bot.bot_id}",
        group_id=str(event.group_id or event.user_id),
        bot_self_id=str(bot.bot_id),
        observer_blacklist=_mc.observer_blacklist,
        message_type="ai_reply",
    )
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
    ├── 1. 检查是否需要重建
    │   ├── Entity 增长 > hiergraph_rebuild_ratio (1.10)
    │   └── 距上次重建 > hiergraph_rebuild_interval_seconds (86400)
    │
    ├── 2. 获取未分配 Category 的 Entity
    │   └── _get_unassigned_entities()
    │
    ├── 3. LLM 分类
    │   └── _llm_categorize(entities, parent_category=None)
    │       └── 返回 {category_name: [entity_names]}
    │
    ├── 4. 应用分类结果
    │   ├── _apply_assignments() → 创建 Category + 关联 Entity
    │   └── 递归对子 Category 继续分类（直到 min_children_per_category 或 max_layers）
    │
    ├── 5. 更新 Meta
    │   └── _update_meta()
    │
    └── 6. 更新群组摘要缓存
        └── _update_group_summary_cache()
            └── 用顶层 Category 的 name + summary 生成群组整体摘要
```

**配置项**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `min_children_per_category` | 3 | 每个 Category 至少包含的子节点数 |
| `max_layers` | 5 | 分层图最大层数 |
| `hiergraph_rebuild_ratio` | 1.10 | Entity 增长超过此比例时触发增量重建 |
| `hiergraph_rebuild_interval_seconds` | 86400 (24h) | 距上次重建超过此秒数时触发 |

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

复用现有 `rag/base.py` 的 Qdrant 客户端和 Embedding 模型，创建 3 个独立 Collection。

**Collection 定义**：

| Collection | 存储内容 | Payload |
|------------|----------|---------|
| `memory_episodes` | Episode 向量 | `scope_key`, `content` |
| `memory_entities` | Entity 向量 | `scope_key`, `name`, `summary` |
| `memory_edges` | Edge 向量 | `scope_key`, `fact` |

**向量维度**：复用 `rag/base.py` 的 `DIMENSION` 常量。

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
# handler.py - handle_event() 中
if event.raw_text and event.raw_text.strip():
    try:
        from gsuid_core.ai_core.memory.config import memory_config
        if memory_config.observer_enabled:
            from gsuid_core.ai_core.memory import observe
            asyncio.create_task(
                observe(
                    content=event.raw_text,
                    speaker_id=str(event.user_id),
                    group_id=str(event.group_id or event.user_id),
                    bot_self_id=str(ws.bot_id),
                    observer_blacklist=memory_config.observer_blacklist,
                    message_type="group_msg" if event.group_id else "private_msg",
                )
            )
    except Exception:
        pass  # Observer 失败不应影响主流程
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

**记忆观察（AI 回复后）**：AI 回复发送后，将回复内容入队观察：

```python
# handle_ai.py - 步骤 8 后
if chat_result and _mc.observer_enabled:
    asyncio.create_task(
        observe(
            content=chat_result,
            speaker_id=f"bot_{bot.bot_id}",
            group_id=str(event.group_id or event.user_id),
            bot_self_id=str(bot.bot_id),
            observer_blacklist=_mc.observer_blacklist,
            message_type="ai_reply",
        )
    )
```

#### 10.12.3 启动初始化

**文件位置**: [`gsuid_core/ai_core/memory/startup.py`](gsuid_core/ai_core/memory/startup.py)

```python
@on_core_start(priority=5)
async def init_memory_system():
    """初始化记忆系统"""
    # 0. 检查 RAG 是否已启用（前置条件）
    from gsuid_core.ai_core.rag.base import client, init_embedding_model
    if client is None:
        init_embedding_model()
        from gsuid_core.ai_core.rag.base import client
        if client is None:
            return  # RAG 未启用，跳过记忆系统初始化

    # 1. 确保 Qdrant Collection 存在
    await ensure_memory_collections()

    # 2. 启动 IngestionWorker（无参构造，内部使用 async_maker）
    worker = IngestionWorker()
    asyncio.create_task(worker.start())
```

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

## 11. 完整流程图

### 11.1 消息处理总流程

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

### 11.2 AI 聊天处理流程 (handle_ai_chat)

```
┌──────────────────────────────────────────────────────────────────────┐
│                    handle_ai_chat(bot, event)                        │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  1. enable_ai 检查                                                    │
│     └── if not enable_ai: return                                     │
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
│     ├── 第一层：if len > 14000: 硬截断 + 截断提示（防子Agent爆炸）     │
│     └── 第二层：if len > 8000:  调用 create_subagent 智能摘要          │
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
│         ├── 检查 HistoryManager 中是否已存在                          │
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

### 11.3 Heartbeat 定时巡检流程

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

### 11.4 配置更新与热重载流程

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

### 11.5 消息触发 vs 定时巡检 对比

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

## 附录

### A. 相关文件路径

| 文件 | 说明 |
|------|------|
| [`gsuid_core/handler.py`](gsuid_core/handler.py) | 事件处理入口 |
| [`gsuid_core/ai_core/__init__.py`](gsuid_core/ai_core/__init__.py) | AI Core 初始化 |
| [`gsuid_core/ai_core/ai_router.py`](gsuid_core/ai_core/ai_router.py) | Session 路由 |
| [`gsuid_core/ai_core/handle_ai.py`](gsuid_core/ai_core/handle_ai.py) | AI 处理入口 |
| [`gsuid_core/ai_core/persona/config.py`](gsuid_core/ai_core/persona/config.py) | Persona 配置 |
| [`gsuid_core/ai_core/heartbeat/inspector.py`](gsuid_core/ai_core/heartbeat/inspector.py) | 巡检器 |
| [`gsuid_core/ai_core/heartbeat/decision.py`](gsuid_core/ai_core/heartbeat/decision.py) | LLM 决策 |
| [`gsuid_core/webconsole/persona_api.py`](gsuid_core/webconsole/persona_api.py) | Persona API |
| [`gsuid_core/utils/plugins_config/gs_config.py`](gsuid_core/utils/plugins_config/gs_config.py) | 配置管理 |
| [`gsuid_core/ai_core/memory/__init__.py`](gsuid_core/ai_core/memory/__init__.py) | 记忆系统模块导出 |
| [`gsuid_core/ai_core/memory/config.py`](gsuid_core/ai_core/memory/config.py) | 记忆系统配置 |
| [`gsuid_core/ai_core/memory/observer.py`](gsuid_core/ai_core/memory/observer.py) | 观察者管道 |
| [`gsuid_core/ai_core/memory/scope.py`](gsuid_core/ai_core/memory/scope.py) | Scope Key 隔离 |
| [`gsuid_core/ai_core/memory/ingestion/worker.py`](gsuid_core/ai_core/memory/ingestion/worker.py) | 摄入引擎 Worker |
| [`gsuid_core/ai_core/memory/retrieval/dual_route.py`](gsuid_core/ai_core/memory/retrieval/dual_route.py) | 双路检索引擎 |
| [`gsuid_core/ai_core/memory/database/models.py`](gsuid_core/ai_core/memory/database/models.py) | 记忆系统数据模型 |
| [`gsuid_core/ai_core/memory/vector/ops.py`](gsuid_core/ai_core/memory/vector/ops.py) | 向量存储操作 |

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
  session_id = f"bot:{bot_id}:group:{group_id}"
  示例: "bot:onebot:group:789012"

私聊:
  session_id = f"bot:{bot_id}:private:{user_id}"
  示例: "bot:onebot:private:345678"

说明:
- 使用 bot:{bot_id}: 前缀区分不同 Bot 实例
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
