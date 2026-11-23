"""在途委派的唯一读模型：``dlg_`` 句柄 + 状态/产物投影。

不新建表——``AIAgentTask`` 本身就是委派记录（id / status / agent_profile /
session_id / 产物挂 ``AIAgentArtifact``）。本模块只做两件事：

1. 给模型一个**能被 inspect 工具消费**的单一标识符 ``dlg_<root_task_id>``。
   旧版 ``create_subagent`` 超时只印 ``root.id[:8]``，而 ``list_persisted_outputs``
   是 SQL 等值查询 → 模型拿到的 id 永远查不到东西（生产已复现，见 INV-5）。
2. 把「轮询」与「等待」收敛到同一入口，使同步内联等待退化为默认参数而非行为分叉。
"""

from __future__ import annotations

import re
import asyncio
from typing import Literal
from dataclasses import dataclass

DELEGATION_HANDLE_PREFIX = "dlg_"
_ORDINAL_HANDLE_RE = re.compile(r"^(?:(?:事项|任务|task)\s*#?|#)\s*(\d+)$", re.IGNORECASE)

DelegationStatus = Literal["running", "paused", "done", "failed", "cancelled", "waiting_approval"]

# AIAgentTask.status → 对模型暴露的状态语义
_STATUS_MAP: dict[str, DelegationStatus] = {
    "pending": "running",
    "running": "running",
    "paused": "paused",
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
    hid = (handle or "").strip()
    return hid.startswith(DELEGATION_HANDLE_PREFIX) or _ORDINAL_HANDLE_RE.match(hid) is not None


def _parse_ordinal_handle(handle: str) -> int | None:
    hid = (handle or "").strip()
    if hid.isdigit():
        return int(hid)
    m = _ORDINAL_HANDLE_RE.match(hid)
    if m is not None:
        return int(m.group(1))
    return None


async def load_delegation(
    handle: str,
    *,
    owner_user_id: str = "",
    session_id: str = "",
) -> Delegation | None:
    """读取委派状态与产物；支持 ``dlg_<uuid>``、裸 UUID 及同会话 ``#ordinal``。"""
    from gsuid_core.ai_core.planning.models import AIAgentTask, AIAgentArtifact

    root_id = root_task_id_of(handle)
    if not root_id:
        return None
    task = await AIAgentTask.get_by_id(root_id)
    if task is None:
        ordinal = _parse_ordinal_handle(root_id)
        if ordinal is not None:
            task = await AIAgentTask.get_root_by_ordinal(
                ordinal,
                owner_user_id=owner_user_id,
                session_id=session_id,
            )
    if task is None:
        return None
    mapped = _STATUS_MAP[task.status] if task.status in _STATUS_MAP else "running"
    handles: tuple[str, ...] = ()
    images: tuple[str, ...] = ()
    if mapped in _TERMINAL:
        from gsuid_core.ai_core.planning.kanban_executor import select_delivery_artifacts

        raw_arts = await AIAgentArtifact.list_for_root(task.root_task_id or task.id)
        arts, _primary = select_delivery_artifacts(task, raw_arts)
        handles = tuple(a.id for a in arts)
        images = tuple(a.id for a in arts if bool(a.payload_path) and (a.mime or "").startswith("image/"))
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


async def await_delegation(
    handle: str,
    *,
    wait_sec: float = 0.0,
    owner_user_id: str = "",
    session_id: str = "",
) -> Delegation | None:
    """轮询或等待到终态。

    ``wait_sec <= 0`` 即单次读取（用户追问进度用）；``> 0`` 则轮询至终态或超时。
    这样「内联同步等 5s」只是本函数的一个默认参数，不再是独立代码路径。
    """
    current = await load_delegation(handle, owner_user_id=owner_user_id, session_id=session_id)
    if current is None or wait_sec <= 0 or current.is_terminal or current.status == "paused":
        return current
    waited = 0.0
    while waited < wait_sec:
        await asyncio.sleep(_POLL_INTERVAL_SEC)
        waited += _POLL_INTERVAL_SEC
        current = await load_delegation(handle, owner_user_id=owner_user_id, session_id=session_id)
        if current is None or current.is_terminal or current.status == "paused":
            return current
    return current


_STATUS_LABEL: dict[DelegationStatus, str] = {
    "running": "⏳ 仍在后台执行",
    "paused": "⏸️ 已暂停",
    "done": "✅ 已完成",
    "failed": "❌ 失败",
    "cancelled": "🚫 已取消",
    "waiting_approval": "⏸️ 等待审批",
}


# 追问用 read_handle(dlg_) 回看事实包；120 字只剩抬头。
_GOAL_EXCERPT = 800


def format_delegation(d: Delegation) -> str:
    """给模型看的状态卡（句柄只进工具参数，禁写入台词）。"""
    lines = [
        f"委派 {d.id} | {_STATUS_LABEL[d.status]} | 节点={d.profile or '未指定'} | 任务#{d.ordinal}",
        f"任务: {d.goal[:_GOAL_EXCERPT]}",
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
    if d.is_terminal and not d.artifacts and not d.failure_reason:
        lines.append("（暂无产物登记）")
    if d.status == "paused":
        lines.append("当前已暂停：可用 resume_my_kanban_tree 恢复，或用 respawn_subtask 修改描述重派。")
    elif not d.is_terminal:
        lines.append(
            "尚未完成：中间草稿未定稿，完成后框架会自动回灌交付包；请简短安抚或回应一句，勿重复委派或提前声称已完成。"
        )
    return "\n".join(lines)
