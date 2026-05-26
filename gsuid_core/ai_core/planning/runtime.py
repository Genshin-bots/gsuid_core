"""长任务运行时上下文绑定（C5 + v2 Kanban）

约束 2：真实 task_id / step_id 绝不作为 LLM 工具参数。框架在定时唤醒执行某个
任务时，把"当前是哪个 Task 的哪个 Step"绑定到一个 ``contextvars.ContextVar``；
``tools.py`` 的写原语据此作用于框架已绑定的 current_task，LLM 无需也无法传 UUID。

v2 · Agent Mesh Kanban 在此基础上扩展了 Artifact Workspace 绑定：

- ``root_task_id`` / ``task_id``：当前正在执行的任务节点位置；
- ``artifact_workspace``：本任务节点的唯一可写目录，由调度器在 ``_run_one_task_node``
  之前创建并绑定；
- ``allowed_write_roots``：允许写入的根目录列表（用于 file_manager / command_executor
  的路径守卫）；
- ``agent_profile``：当前执行体的画像，登记 artifact 时回填 ``from_profile``。

绑定生命周期仅限单次 ``session.run`` / ``_run_one_task_node``——纯进程内存，
无持久化、无向后兼容问题。
"""

import contextvars
from typing import List, Optional
from pathlib import Path
from dataclasses import field, dataclass


@dataclass
class PlanRunContext:
    """单次定时唤醒执行时框架绑定的当前任务上下文（v1 + v2 合一）。"""

    task_id: str
    step_id: Optional[str] = None
    # v2 · Kanban 任务树位置（v1 / 退化树时 root_task_id == task_id）
    root_task_id: str = ""
    # v2 · Artifact Workspace 绝对路径；调度器在 _run_one_task_node 前创建
    artifact_workspace: Optional[Path] = None
    # v2 · 允许写入的根目录白名单（防越界）；默认仅含 artifact_workspace
    allowed_write_roots: List[Path] = field(default_factory=list)
    # v2 · 当前执行体画像（artifact_put / workspace_file 登记时回填）
    agent_profile: str = ""


_current_plan: contextvars.ContextVar[Optional[PlanRunContext]] = contextvars.ContextVar(
    "_current_plan_run_context", default=None
)


def bind_plan_context(ctx: Optional[PlanRunContext]) -> contextvars.Token:
    """绑定当前任务上下文，返回用于复原的 token。"""
    return _current_plan.set(ctx)


def reset_plan_context(token: contextvars.Token) -> None:
    """复原到绑定前的状态。"""
    _current_plan.reset(token)


def get_plan_context() -> Optional[PlanRunContext]:
    """读取当前绑定的任务上下文（无绑定时返回 None）。"""
    return _current_plan.get()
