# 九、Scheduled Task 定时任务

定时任务系统支持一次性任务和循环任务。**注意：与插件侧 APScheduler（[§6 of `gscore-plugin-development`](../gscore-plugin-development/references/06-scheduler-and-subscribe.md)）是两套独立系统**——本章节是 **AI Core 的定时任务工具**，由 LLM 通过 `add_once_task` / `add_interval_task` 等工具创建；插件侧的 APScheduler 是插件自己用 Python 调度器跑业务，与本章节无关。

## 9.1 模块导入

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

## 9.2 工具签名速查

完整工具签名（按 category 分组）见 [§7.1.1 定时任务管理工具](./07-builtin-tools.md)：

- **Self 保底池（创建入口，口语化触发）**：
  - `add_once_task` — [§7.1](./07-builtin-tools.md)
  - `add_interval_task` — [§7.1](./07-builtin-tools.md)
- **Common 向量池（管理类）**：
  - `list_scheduled_tasks` / `query_scheduled_task` / `modify_scheduled_task` /
  - `cancel_scheduled_task` / `pause_scheduled_task` / `resume_scheduled_task` —
    [§7.1.1](./07-builtin-tools.md)

## 9.3 数据模型

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

### 字段状态机

- `task_type="once"`：`trigger_time` 必填，触发后状态从 `pending` 转到 `executed` / `failed`
- `task_type="interval"`：`interval_seconds` 必填，按 `interval_seconds` 重复触发，达到 `max_executions` 自动结束
- 状态迁移：`pending` ⇄ `paused` → `executed` / `failed` / `cancelled`
- `next_run_time`：调度器下次拉起的判定字段，循环任务每完成一次会重算
