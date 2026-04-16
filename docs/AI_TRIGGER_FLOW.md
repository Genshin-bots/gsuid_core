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
10. [完整流程图](#10-完整流程图)
11. [附录](#附录)
   - [D. 已知问题汇总](#d-已知问题汇总)

---

## 1. 系统概述

### 1.1 AI Core 模块结构

```
gsuid_core/ai_core/
├── __init__.py          # 核心初始化入口
├── ai_router.py         # Session 路由管理
├── adapter.py           # 共享适配器（LLM/嵌入配置复用）
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
│   ├── database_query.py    # 数据库查询
│   ├── favorability_manager.py  # 好感度管理
│   ├── file_manager.py      # 文件管理 (read/write/execute/diff/list)
│   ├── get_time.py          # 获取时间
│   ├── message_sender.py    # 消息发送
│   ├── rag_search.py        # RAG 检索 (knowledge/image)
│   ├── scheduler.py         # 预约定时任务
│   ├── self_info.py         # 获取自身 Persona 信息
│   ├── subagent.py          # 创建子Agent
│   └── web_search.py        # Web 搜索
├── scheduled_task/       # 定时任务系统
│   ├── __init__.py
│   ├── models.py          # ScheduledAITask 数据模型
│   └── executor.py        # 定时执行器
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
├── mem/                  # Agent 记忆层 (基于 memv)
│   ├── __init__.py
│   ├── memory.py        # memv 封装
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
├── skills/               # Skills 技能系统
│   ├── __init__.py
│   ├── operations.py
│   └── resource.py
├── statistics/           # AI 统计系统
│   ├── __init__.py
│   ├── manager.py       # 统计管理器
│   └── models.py        # 数据模型
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
MAX_SUMMARY_LENGTH = 4000    # 第二层：摘要阈值，超过则调用子Agent智能摘要

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
1. 意图识别
   └── classifier_service.predict_async(query)
       ├── "闲聊" - 闲聊模式
       ├── "工具" - 工具执行模式
       └── "问答" - 问答模式

2. 获取 AI Session
   └── session = await get_ai_session(event)

3. RAG 知识库检索
   ├── query_knowledge() - 检索知识库
   └── format_history_for_agent() - 格式化历史

4. 调用 Agent 生成回复
   └── chat_result = await session.run(
           user_message=user_messages,
           bot=bot,
           ev=event,
           rag_context=rag_context,
       )

5. 发送回复
   └── await bot.send(chat_result)
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

    # 创建 Agent
    model_name = openai_config.get_config("model_name").data
    session = create_agent(
        model_name=model_name,
        system_prompt=base_persona,
        persona_name=persona_name,
    )

    # 保存到 HistoryManager
    history_manager.set_ai_session(session_id, session)

    return session
```

**Session ID 格式**:
```
# 群聊时: 以 group: 为前缀
session_id = f"group:{group_id}"
示例: "group:789012"

# 私聊时: 以 private: 为前缀
session_id = f"private:{user_id}"
示例: "private:345678"
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
DEFAULT_MAX_MESSAGES = 40  # 每 Session 最多保留 60 条消息
MAX_AI_HISTORY_LENGTH = 30  # AI 对话历史最大长度

# 在 __init__ 中
self._histories[session_key] = deque(maxlen=self._max_messages)
```

**效果**: 每个 Session 的消息历史被限制在 `deque(maxlen=40)` 中，超过限制的旧消息自动被丢弃。

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
| 滑动窗口 | `deque(maxlen=60)` | 每 Session 最多 60 条消息 |
| AI 历史限制 | `MAX_AI_HISTORY_LENGTH=50` | AI 对话历史不超过 50 条 |
| **字符数截断** | `MAX_HISTORY_CHARS=6000` | 历史记录超过 6000 字符时从后往前丢弃 |
| 空闲清理 | `IDLE_THRESHOLD=86400` (1天) | 1天不活跃的 Session 自动清除 |
| 定时清理 | `cleanup_interval=3600` (1小时) | 每小时检查一次空闲 Session |

> **⚠️ 重要改进**：原版 `deque(maxlen=60)` 仅按消息条数截断，存在"隐形 Token 爆炸"风险。
> 如果在群聊中，5 个人连续发了 10 篇 5000 字的长文，虽然只有 50 条消息（未触发限制），
> 但合计 25 万字会瞬间突破主流 LLM 的 Token 上限或产生极其昂贵的费用。
>
> 现在 `format_history_for_agent()` 增加了**字符数滑动截断**机制：
> - 从后往前统计历史记录的总字符数
> - 超过 6000 字符阈值时，丢弃更早的历史
> - 避免长文本堆叠导致的 Token 爆炸问题

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
# bot.py - Bot.__init__()
self.uid = ev.user_id if ev.user_id else "0"
if ev.user_type != "direct":
    self.temp_gid = ev.group_id if ev.group_id else "0"
else:
    self.temp_gid = self.uid  # 私聊时 temp_gid 等于 uid

self.bid = ev.bot_id if ev.bot_id else "0"
self.session_id = f"{self.bid}{self.temp_gid}{self.uid}"
```

**关键区别**：
- 群聊时 `temp_gid = group_id`，但 Session ID 仍包含 `uid`
- 真正实现群聊共享上下文的是 AI Router 层面的处理，而非 Session ID 本身

**实际 Session 路由逻辑** (`ai_router.py`):
```python
# session_id 格式: "bot:{bot_id}:group:{group_id}" 或 "bot:{bot_id}:private:{user_id}"
# AI Router 根据 session_id 中的 "group" 或 "private" 前缀判断会话类型
```

修改后的架构：
- `Bot.session_id` 仍包含完整信息 `{bid}{temp_gid}{uid}`
- `get_persona_for_session()` 解析 session_id 提取 `group_id` 或 `user_id` 用于 Persona 匹配
- AI Session 的共享由 `history_manager._ai_sessions` 决定，按完整 session_id 存储

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
@ai_tools(category: str = "default")
async def my_tool(ctx: RunContext[ToolContext], ...) -> str:
    ...
```

**参数说明**：
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `category` | `str` | `"default"` | 工具分类名称，用于分组管理 |

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
| `query_user_memory` | 查询用户记忆 |

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

#### 5.5.10 核心函数

```python
def get_tools(tool_names: List[str]) -> ToolList:
    """根据名称列表获取工具对象"""
    ...

def get_main_agent_tools() -> ToolList:
    """获取主Agent专用工具（仅 buildin 分类）"""
    ...

def get_all_tools() -> Dict[str, ToolBase]:
    """获取所有工具（平铺结构）"""
    result = {}
    for category_tools in _TOOL_REGISTRY.values():
        result.update(category_tools)
    return result
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
    │   └── sessions = history_manager.list_sessions()
    │
    └── 3. 遍历每个会话
            │
            ├── _should_inspect_session() - 检查是否应该巡检
            │   ├── scope="disabled" -> 不巡检
            │   ├── scope="global" -> 巡检所有
            │   └── scope="specific" -> 只巡检 target_groups 中的群
            │
            └── _inspect_session() - 处理单个会话
                    │
                    ├── 1. 获取历史记录
                    │   └── history = _get_history(session_key)
                    │
                    ├── 2. 检查最近 AI 是否已发言 (防刷屏)
                    │   └── _has_recent_ai_response(history)
                    │
                    ├── 3. 获取 AI Session
                    │   └── ai_session = await get_ai_session_by_id()
                    │
                    ├── 4. LLM 决策是否发言
                    │   └── should_ai_speak(history, ai_session)
                    │       └── 返回 (bool, reason)
                    │
                    ├── 5. 如果决定发言，生成消息
                    │   └── generate_proactive_message(history, ai_session, reason)
                    │
                    └── 6. 发送消息并记录
                        └── _send_proactive_message(session_key, user_id, message)
```

### 6.5 LLM 决策 (`decision.py`)

**决策 Prompt** (`DECISION_PROMPT_TEMPLATE`):

```python
"""
你是一个 AI 聊天助手，请根据你的【性格与人设】以及【历史对话记录】，
判断你现在是否应该**主动**插话或开启新话题。

【你的性格与人设】
{persona_text}

【当前系统时间】
{current_time}

【决策指南】
1. 结合人设活跃度：高冷角色尽量少说话（非必要不开口），活泼角色可以主动活跃气氛。
2. 结合人设兴趣：如果大家在聊你非常感兴趣的事，你应该插话。
3. 察言观色：如果用户表现出困惑、求助，你应该主动提供帮助。
4. 观察时间线：对比消息时间与当前系统时间，如果距离最后一条消息已经过去很久（冷场），
   且符合你的性格，可以主动开启话题。
5. 避免刷屏：如果你刚刚已经发言过，或者当前话题已经自然结束大家准备离开，请不要发言。

【历史对话记录】
{history_context}

请综合思考后做出决策。必须以严格的 JSON 格式输出:
{"should_speak": true 或 false, "reason": "简要说明你做出该决策的思考过程"}
"""
```

**决策输出解析**:

```python
decision_data = json.loads(clean_response)
should_speak = bool(decision_data.get("should_speak", False))
reason = str(decision_data.get("reason", "未提供原因"))
```

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
INACTIVE_THRESHOLD_HOURS = 24  # 冷场阈值

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

    # 优先用 WS_BOT_ID 直接查找（WS 连接 ID 就是 gss.active_bot 的 key）
    if event.WS_BOT_ID and event.WS_BOT_ID in gss.active_bot:
        return gss.active_bot[event.WS_BOT_ID]

    # 兜底：遍历历史消息 metadata 找 bot_id
    ...
```

    # 兜底：返回第一个可用的 Bot 实例
    if BotClass.instances:
        return list(BotClass.instances.values())[0]
    return None
```

**修复要点**:
1. 使用 `Bot.instances` 而不是 `gss.active_bot`
2. 遍历查找匹配 `bot_id` 的 `Bot` 实例（`Bot.instances` 以 `session_id` 为 key）
3. `Bot` 类有 `bot_self_id` 属性

---

**改进后的巡检流程**:

```
定时任务触发
    │
    ▼
前置规则过滤
    ├── 最后消息来自 AI? → 直接跳过
    ├── 群已 24+ 小时不活跃? → 直接跳过
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
│   └── scheduler.py     # manage_scheduled_task / add_scheduled_task 工具
└── scheduled_task/
    ├── __init__.py      # 模块初始化
    ├── models.py        # 数据库模型 AIScheduledTask
    ├── executor.py      # execute_scheduled_task 执行器
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

#### 7.4.2 工具函数 - `manage_scheduled_task`

**文件位置**: [`gsuid_core/ai_core/buildin_tools/scheduler.py`](gsuid_core/ai_core/buildin_tools/scheduler.py)

主 Agent 调用的统一任务管理工具，支持增删改查启停。

```python
@ai_tools(category="buildin")
async def manage_scheduled_task(
    ctx: RunContext[ToolContext],
    action: Literal["add", "cancel", "modify", "list", "query", "pause", "resume"],
    task_id: Optional[str] = None,
    run_time: Optional[str] = None,
    interval_type: Optional[Literal["minutes", "hours", "days"]] = None,
    interval_value: Optional[int] = None,
    task_prompt: Optional[str] = None,
    max_executions: Optional[int] = None,
    enabled: Optional[bool] = None,
) -> str:
    """
    管理预约定时/循环任务（增删改查启停）

    支持一次性任务和循环任务两种类型。

    **安全限制**:
    - 单用户最多 20 个待执行任务
    - 循环任务最大执行次数为 10 次
    - 循环任务最小间隔为 5 分钟
    """
```

**action 参数说明**：

| action | 说明 | 必需参数 |
|--------|------|----------|
| `add` | 添加新任务 | task_prompt + (run_time 或 interval_type+interval_value) |
| `cancel` | 取消任务 | task_id |
| `modify` | 修改任务 | task_id |
| `list` | 列出所有任务 | - |
| `query` | 查询任务详情 | task_id |
| `pause` | 暂停任务（仅循环任务） | task_id |
| `resume` | 恢复任务 | task_id |

**使用示例**：

```python
# 添加一次性任务
await manage_scheduled_task(
    ctx,
    action="add",
    run_time="2024-05-15 06:30:00",
    task_prompt="查询英伟达(NVDA)的实时股价和最新新闻",
)

# 添加循环任务（每30分钟执行一次）
await manage_scheduled_task(
    ctx,
    action="add",
    interval_type="minutes",
    interval_value=30,
    task_prompt="帮我关注股市行情",
    max_executions=10,  # 最多执行10次
)

# 列出所有任务
await manage_scheduled_task(ctx, action="list")

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
│         识别意图 → 提取间隔和任务 → 调用 manage_scheduled_task    │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              buildin_tools/scheduler.py                           │
│                  manage_scheduled_task 工具                      │
│  1. 安全检查：用户任务数、最大次数、最小间隔                        │
│  2. 存入数据库 AIScheduledTask（包含循环任务字段）                │
│  3. 注册到 APScheduler (interval 触发器)                          │
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
   主 Agent 调用 `manage_scheduled_task(action="add", ...)`，系统：
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
from gsuid_core.ai_core.buildin_tools.scheduler import manage_scheduled_task
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
| `model_name` | 在 `ai_router.py` 模块加载时缓存 | 需重启服务 |
| Session system_prompt | Session 创建后不更新 | 需重启服务或创建新 Session |

---

## 8. AI Statistics 统计系统

### 8.1 概述

AI Statistics 模块负责收集、聚合和持久化 AI 模块的各类统计数据，支持前端展示面板的数据需求。

**文件位置**: [`gsuid_core/ai_core/statistics/`](gsuid_core/ai_core/statistics/)

```
statistics/
├── __init__.py     # 模块导出
├── models.py        # 数据库模型
└── manager.py      # 统计管理器
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
class AIRAGMissStatistics(BaseModel, table=True):
    """RAG 未命中统计表"""
    date: str                    # 统计日期 (YYYY-MM-DD)
    hit_count: int              # 命中次数
    miss_count: int             # 未命中次数
```

#### AIRAGDocumentStatistics - RAG 文档命中统计表

```python
class AIRAGDocumentStatistics(BaseModel, table=True):
    """RAG 文档命中统计表"""
    document_name: str          # 文档名称
    hit_count: int              # 命中次数
```

#### AIDailyStatistics - 每日 AI 统计数据表

```python
class AIDailyStatistics(BaseModel, table=True):
    """每日 AI 统计数据表（全局统计，无 bot_id）"""
    date: str                    # 统计日期 (YYYY-MM-DD)，主键
    total_input_tokens: int     # 总输入Token
    total_output_tokens: int     # 总输出Token
    avg_latency: float          # 平均延迟(秒)
    p95_latency: float          # P95延迟(秒)
    intent_chat_count: int      # 闲聊次数
    intent_tool_count: int      # 工具次数
    intent_qa_count: int        # 问答次数
    api_timeout_count: int       # API超时次数
    api_rate_limit_count: int   # RateLimit次数
    api_network_error_count: int # 网络错误次数
    api_usage_limit_count: int   # 使用限制次数
    api_agent_error_count: int  # Agent执行错误次数
    trigger_mention_count: int   # @触发次数
    trigger_keyword_count: int   # 关键词触发次数
    trigger_heartbeat_count: int # 主动巡检触发次数
    trigger_scheduled_count: int  # 定时任务触发次数
```

#### AITokenUsageByModel - 按模型分组的 Token 消耗

```python
class AITokenUsageByModel(BaseModel, table=True):
    """按模型分组的 Token 消耗统计（全局统计）"""
    date: str
    model_name: str              # 模型名称
    input_tokens: int
    output_tokens: int
```

#### AIHeartbeatMetrics - Heartbeat 巡检统计

```python
class AIHeartbeatMetrics(BaseModel, table=True):
    """Heartbeat 巡检详细指标（全局统计）"""
    date: str                    # 统计日期
    group_id: str                # 群组ID
    should_speak_count: int      # 应该发言次数
    should_not_speak_count: int  # 不应该发言次数
```

#### AIGroupUserActivityStats - 群组/用户活跃统计

```python
class AIGroupUserActivityStats(BaseModel, table=True):
    """群组/用户活跃统计（全局统计）"""
    date: str
    group_id: str
    user_id: str
    ai_interaction_count: int   # AI互动次数
    message_count: int          # 消息总数
```

### 8.4 每日数据持久化机制

#### 8.4.1 启动时

```python
@on_core_start
async def init_ai_core():
    statistics_manager = get_statistics_manager()
    await statistics_manager.start()  # 从数据库加载今日数据
```

#### 8.4.2 关闭时

```python
@on_core_shutdown
async def shutdown_ai_core():
    from gsuid_core.ai_core.statistics import statistics_manager
    await statistics_manager.stop()  # 持久化当前数据到数据库
```

#### 8.4.3 零点自动重置

统计系统使用 APScheduler 的 cron 定时任务实现每日零点重置：

```python
# ai_core/__init__.py
@scheduler.scheduled_job("cron", hour=0, minute=0)
async def _scheduled_ai_core_reset():
    """每日零点重置"""
    from gsuid_core.ai_core.statistics import statistics_manager
    await statistics_manager._persist_all_stats_to_db()
    statistics_manager._reset_daily_counters()
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"🧠 [Statistics] 每日重置完成，日期: {today}")

# ai_core/statistics/manager.py
@scheduler.scheduled_job("cron", minute="*/30")
async def _persist_loop():
    """每30分钟持久化一次统计数据"""
    await statistics_manager._persist_all_stats_to_db()
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

# 记录 Token 使用
statistics_manager.record_token_usage(
    model_name="gpt-4",
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

## 9. 完整流程图

### 8.1 消息处理总流程

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

### 8.2 AI 聊天处理流程 (handle_ai_chat)

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

### 8.3 Heartbeat 定时巡检流程

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
│  LLM 决策阶段 (隐形 Sub-Agent)                                        │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ should_ai_speak(history, ai_session)                            │  │
│  │   ├── 1. 格式化历史记录和当前时间                                │  │
│  │   ├── 2. 构建 DECISION_PROMPT_TEMPLATE                          │  │
│  │   ├── 3. 调用 LLM: session.run(prompt)                          │  │
│  │   ├── 4. 解析 JSON 响应: {should_speak, reason}                 │  │
│  │   └── 5. 返回 (bool, str)                                       │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │ should_speak = True           │ should_speak = False
                    ▼                               ▼
┌───────────────────────────────┐     ┌─────────────────────────────────┐
│  生成主动消息                  │     │  保持沉默                        │
│  ┌─────────────────────────┐  │     │  ├── reason: "话题已结束"        │
│  │ generate_proactive_     │  │     │  ├── reason: "AI刚发过言"       │
│  │ message(history,        │  │     │  └── reason: "不符合性格"       │
│  │ ai_session, reason)     │  │     └─────────────────────────────────┘
│  │   ├── 构建 PROACTIVE_    │  │
│  │     MESSAGE_PROMPT       │  │
│  │   ├── 调用 LLM           │  │
│  │   └── 返回消息文本        │  │
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

### 8.4 配置更新与热重载流程

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

### 8.5 消息触发 vs 定时巡检 对比

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
| `model_name` | ❌ | 需重启服务 | 模块加载时缓存 |

### C. Session ID 格式说明

```
Session ID 格式说明:

群聊:
  session_id = f"group:{group_id}"
  示例: "group:789012"

私聊:
  session_id = f"private:{user_id}"
  示例: "private:345678"

说明:
- 使用 group: / private: 前缀区分会话类型
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
