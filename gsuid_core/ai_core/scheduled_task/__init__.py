"""
Scheduled Task 模块

提供定时 AI 任务能力，允许主 Agent 预约未来某个时间执行的复杂任务。
当时间到达时，系统会加载当时的 persona 和 session 来执行任务。

核心设计：
1. 任务持久化到数据库，确保重启不丢失
2. 使用 APScheduler 管理定时触发
3. 通过 get_ai_session 加载 persona 和 session 执行任务

使用流程：
1. 用户请求预约任务，如"明天早上6点帮我查一下英伟达的股价"
2. 主 Agent 调用 add_scheduled_task 工具（位于 buildin_tools/scheduler.py）
3. 系统将任务存入数据库，并注册到 APScheduler
4. 时间到达时，execute_scheduled_task 被触发
5. 使用 get_ai_session(event) 加载 persona 和 session 执行任务
6. 结果推送给用户

模块结构：
- models.py: 数据库模型 (AIScheduledTask)
- executor.py: 定时执行器
- README.md: 设计文档

注意：add_scheduled_task 工具位于 buildin_tools/scheduler.py
"""

# 导入启动模块以注册 on_core_start 和 on_core_shutdown 回调
from gsuid_core.ai_core.scheduled_task import startup
from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask
from gsuid_core.ai_core.scheduled_task.executor import (
    reload_pending_tasks,
    execute_scheduled_task,
)

__all__ = [
    "AIScheduledTask",
    "execute_scheduled_task",
    "reload_pending_tasks",
    "startup",
]
