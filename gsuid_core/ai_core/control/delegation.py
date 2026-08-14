"""在途委派的唯一读模型：``dlg_`` 句柄 + 状态/产物投影。

不新建表——``AIAgentTask`` 本身就是委派记录（id / status / agent_profile /
session_id / 产物挂 ``AIAgentArtifact``）。本模块只做两件事：

1. 给模型一个**能被 inspect 工具消费**的单一标识符 ``dlg_<root_task_id>``。
   旧版 ``create_subagent`` 超时只印 ``root.id[:8]``，而 ``list_persisted_outputs``
   是 SQL 等值查询 → 模型拿到的 id 永远查不到东西（生产已复现，见 INV-5）。
2. 把「轮询」与「等待」收敛到同一入口，使同步内联等待退化为默认参数而非行为分叉。
"""

from __future__ import annotations

import asyncio
from typing import Literal, Sequence
from dataclasses import dataclass

DELEGATION_HANDLE_PREFIX = "dlg_"

DelegationStatus = Literal["running", "done", "failed", "cancelled", "waiting_approval"]

# AIAgentTask.status → 对模型暴露的终态语义（pending/paused 对模型都是「还在跑」）
_STATUS_MAP: dict[str, DelegationStatus] = {
    "pending": "running",
    "running": "running",
    "paused": "running",
    "completed": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "waiting_approval": "waiting_approval",
}

_TERMINAL: frozenset[DelegationStatus] = frozenset({"done", "failed", "cancelled", "waiting_approval"})

_POLL_INTERVAL_SEC = 0.5


@dataclass(frozen=True)
class Delegation:
    """一次委派的读模型投影。"""

    id: str
    root_task_id: str
    ordinal: int
    profile: str
    goal: str
    status: DelegationStatus
    artifacts: tuple[str, ...] = ()
    image_artifacts: tuple[str, ...] = ()
    failure_reason: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL


def delegation_handle(root_task_id: str) -> str:
    """``root_task_id`` → 模型可见句柄。"""
    rid = (root_task_id or "").strip()
    if not rid:
        return ""
    if rid.startswith(DELEGATION_HANDLE_PREFIX):
        return rid
    return f"{DELEGATION_HANDLE_PREFIX}{rid}"


def root_task_id_of(handle: str) -> str:
    """句柄 → ``root_task_id``（裸 uuid 原样返回，容忍模型漏写前缀）。"""
    hid = (handle or "").strip()
    if hid.startswith(DELEGATION_HANDLE_PREFIX):
        return hid[len(DELEGATION_HANDLE_PREFIX) :]
    return hid


def is_delegation_handle(handle: str) -> bool:
    return (handle or "").strip().startswith(DELEGATION_HANDLE_PREFIX)


async def load_delegation(handle: str) -> Delegation | None:
    """读取委派状态与产物；找不到返回 None。"""
    from gsuid_core.ai_core.planning.models import AIAgentTask, AIAgentArtifact

    root_id = root_task_id_of(handle)
    if not root_id:
        return None
    task = await AIAgentTask.get_by_id(root_id)
    if task is None:
        return None
    # 按 root 取：产物登记在**执行节点**上（多节点树是 child），list_for_task(root)
    # 只能命中叶子根模式，树模式会永远返回「暂无产物登记」。
    arts = await AIAgentArtifact.list_for_root(task.root_task_id or task.id)
    handles = tuple(a.id for a in arts)
    images = tuple(a.id for a in arts if bool(a.payload_path) and (a.mime or "").startswith("image/"))
    mapped = _STATUS_MAP[task.status] if task.status in _STATUS_MAP else "running"
    return Delegation(
        id=delegation_handle(task.id),
        root_task_id=task.id,
        ordinal=task.ordinal,
        profile=task.agent_profile,
        goal=task.goal,
        status=mapped,
        artifacts=handles,
        image_artifacts=images,
        failure_reason=task.failure_reason or "",
    )


async def await_delegation(handle: str, *, wait_sec: float = 0.0) -> Delegation | None:
    """轮询或等待到终态。

    ``wait_sec <= 0`` 即单次读取（用户追问进度用）；``> 0`` 则轮询至终态或超时。
    这样「内联同步等 5s」只是本函数的一个默认参数，不再是独立代码路径。
    """
    current = await load_delegation(handle)
    if current is None or wait_sec <= 0 or current.is_terminal:
        return current
    waited = 0.0
    while waited < wait_sec:
        await asyncio.sleep(_POLL_INTERVAL_SEC)
        waited += _POLL_INTERVAL_SEC
        current = await load_delegation(handle)
        if current is None or current.is_terminal:
            return current
    return current


_STATUS_LABEL: dict[DelegationStatus, str] = {
    "running": "⏳ 仍在后台执行",
    "done": "✅ 已完成",
    "failed": "❌ 失败",
    "cancelled": "🚫 已取消",
    "waiting_approval": "⏸️ 等待审批",
}


def format_delegation(d: Delegation) -> str:
    """给模型看的状态卡（句柄只进工具参数，禁写入台词）。"""
    lines = [
        f"委派 {d.id} | {_STATUS_LABEL[d.status]} | 节点={d.profile or '未指定'} | 任务#{d.ordinal}",
        f"任务: {d.goal[:120]}",
    ]
    if d.failure_reason:
        lines.append(f"失败原因: {d.failure_reason[:300]}")
    if d.image_artifacts:
        lines.append(
            "图片产物: "
            + ", ".join(d.image_artifacts[:5])
            + "（send_message_by_ai(image_id=) 直发；句柄只进参数，勿写进台词）"
        )
    text_arts = [a for a in d.artifacts if a not in d.image_artifacts]
    if text_arts:
        lines.append("文本产物: " + ", ".join(text_arts[:5]) + "（artifact_get 取原文）")
    if not d.artifacts:
        lines.append("（暂无产物登记）")
    if not d.is_terminal:
        lines.append("尚未完成：完成后框架会自动回灌交付包，不要重复委派同一任务。")
    return "\n".join(lines)


def format_delegations(items: Sequence[Delegation]) -> str:
    if not items:
        return "ℹ️ 当前没有在途委派。"
    return "\n\n".join(format_delegation(d) for d in items)
