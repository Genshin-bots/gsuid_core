# Scheduled Task 模块

## 概述

Scheduled Task 模块提供定时 AI 任务能力，允许主 Agent 预约未来某个时间执行的复杂任务。当时间到达时，系统会加载当时的 persona 和 session，使用与主 Agent 一致的语气执行任务。

## 设计理念

现代 AI 框架（如 AutoGen, LangChain）处理这类问题的标准做法是：
- **Scheduled Prompt（定时提示词）+ 唤醒 Sub-Agent（子智能体）**

你不必让 AI 写代码去查股票，而是让 AI 往 APScheduler 里存入一个"未来的指令"。当时间到了，系统把这个指令发给一个全新的子 Agent，让子 Agent 现场去查股票并发送。

## 架构设计

```
┌─────────────────────────────────────────────────────────────────┐
│                         用户请求                                  │
│   "明天早上6点30，帮我查一下英伟达的股价和最新新闻"                │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      主 Agent (LLM)                              │
│              识别意图 → 提取时间和任务 → 调用工具                   │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              buildin_tools/scheduler.py                           │
│                  add_scheduled_task 工具                          │
│  1. 存入数据库 AIScheduledTask（包含 persona_name, session_id）   │
│  2. 注册到 APScheduler                                           │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      数据库 (持久化)                              │
│              任务状态: pending / executed / failed               │
│              记录: persona_name, session_id                      │
└─────────────────────────────────────────────────────────────────┘

                          ...

┌─────────────────────────────────────────────────────────────────┐
│              时间到达 → APScheduler 触发                         │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              scheduled_task/executor.py                          │
│              execute_scheduled_task (执行器)                      │
│  1. 从数据库读取任务信息                                          │
│  2. 使用 get_ai_session(event) 加载 persona 和 session           │
│  3. 向 session 发送任务消息                                      │
│  4. 将结果推送给用户                                              │
└─────────────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. 数据库模型 - `AIScheduledTask`

位于 `scheduled_task/models.py`

```python
class AIScheduledTask(BaseBotIDModel, table=True):
    task_id: str             # 唯一ID
    user_id: str             # 谁制定的任务
    group_id: str            # 目标群（如果是私聊则为空）
    trigger_time: datetime   # 触发时间
    task_prompt: str         # 任务描述

    # Event 相关字段（用于发送消息）
    bot_self_id: str         # 机器人自身ID
    user_type: str           # 用户类型 (group/direct)
    WS_BOT_ID: Optional[str] # WS机器人ID

    # Persona 相关字段（用于执行时加载 persona）
    persona_name: Optional[str]  # Persona 名称
    session_id: str           # Session ID

    status: str              # pending / executed / failed
    created_at: datetime     # 创建时间
    executed_at: datetime    # 执行时间
    result: str              # 执行结果
    error_message: str       # 错误信息
```

### 2. 工具函数 - `add_scheduled_task`

位于 `buildin_tools/scheduler.py`

主 Agent 调用的工具，用于预约定时任务。

```python
@ai_tools(category="buildin")
async def add_scheduled_task(
    ctx: RunContext[ToolContext],
    run_time: str,         # 格式 "YYYY-MM-DD HH:MM:SS"
    task_prompt: str,      # 具体要执行的任务
) -> str:
    """
    当你需要为用户设定未来某个时间执行的复杂任务时调用。
    注意：task_prompt 必须非常详细，包含需要查询的实体和需要返回的格式。
    """
```

### 3. 执行器 - `execute_scheduled_task`

位于 `scheduled_task/executor.py`

被 APScheduler 触发时调用的统一执行器。使用 `get_ai_session(event)` 加载 persona 和 session。

```python
async def execute_scheduled_task(task_id: str):
    # 1. 从数据库读取任务信息
    task = await AIScheduledTask.select_rows(task_id=task_id)

    # 2. 构建 Event 对象
    ev = Event(
        bot_id=task.bot_id,
        user_id=task.user_id,
        ...
    )

    # 3. 使用 get_ai_session 加载 persona 和 session
    session = await get_ai_session(ev)

    # 4. 通过 session 执行任务
    result = await session.run(
        user_message=f"【定时任务执行】请完成以下任务...\n\n{task.task_prompt}",
        bot=bot_instance,
        ev=ev,
    )

    # 5. 将结果推送给用户
    await bot_instance.send(f"{result}")
```

## 使用流程

### 场景：用户预约查股票

**1. 用户输入（晚上10点）**
```
"明天早上 6 点半，帮我查一下英伟达（NVDA）的股价和最新新闻，然后推给我。"
```

**2. 主 Agent 思考**
- 意图识别发现这是一个未来任务
- 提取时间：`2024-05-15 06:30:00`
- 提炼提示词：`查询英伟达(NVDA)的实时股价和最新新闻并总结`

**3. 调用工具**
主 Agent 调用 `add_scheduled_task`（位于 buildin_tools/scheduler.py），系统：
- 将任务存入数据库（包含 persona_name, session_id）
- 往 APScheduler 注册了一个 date 触发器

**4. 主 Agent 回复用户**
```
"没问题，明天早上 6:30 我会准时向您汇报。"
```

**5. 时间流逝**
期间即使 Bot 重启，启动时也可以从数据库重新加载这些 pending 的任务到 APScheduler。

**6. 定时触发（第二天 6:30）**
APScheduler 触发 `execute_scheduled_task`

**7. 加载 Persona 和 Session**
- `execute_scheduled_task` 使用 `get_ai_session(ev)` 加载当时的 persona
- 保持与主 Agent 一致的语气和风格

**8. 执行任务**
- Sub-Agent 看到任务："查询英伟达(NVDA)的实时股价和最新新闻并总结"
- Sub-Agent 思考："我需要调用 web_search 工具"
- Sub-Agent 调用了现有的 web_search 搜索了雅虎财经，获取了数据
- Sub-Agent 生成总结文本

**9. 推送结果**
系统把 Sub-Agent 生成的这段话，主动发给用户

## 模块结构

```
gsuid_core/ai_core/
├── buildin_tools/
│   ├── __init__.py
│   ├── scheduler.py     # add_scheduled_task 工具 (新)
│   └── ...
└── scheduled_task/
    ├── __init__.py      # 模块初始化，导出主要接口
    ├── models.py        # 数据库模型 AIScheduledTask
    ├── executor.py      # execute_scheduled_task 执行器
    └── README.md        # 本文档
```

## 重启恢复

在系统启动时，调用 `reload_pending_tasks()` 可以重新加载所有待执行的任务：

```python
from gsuid_core.ai_core.scheduled_task import reload_pending_tasks

# 在启动流程中
await reload_pending_tasks()
```

此函数会：
1. 查询所有 `pending` 状态的任务
2. 对于已过期的任务，立即执行
3. 对于未过期的任务，重新注册到 APScheduler

## 注意事项

1. **task_prompt 必须详细**：包含需要查询的实体和需要返回的格式
2. **时间格式**：`YYYY-MM-DD HH:MM:SS`
3. **持久化**：任务存储在数据库，重启不丢失
4. **Persona 保持**：执行时会加载预约时的 persona，保持语气一致
5. **Session 复用**：通过 session_id 关联，可复用历史上下文
6. **结果推送**：执行完成后会自动推送给用户
