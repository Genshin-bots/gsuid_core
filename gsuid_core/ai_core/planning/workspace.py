"""v2 · Agent Mesh Kanban · Artifact Workspace 路径守卫与登记。

设计稿见 docs/AGENT_MESH_COLLABORATION_PROPOSAL_20260521.md §5。

核心原则：**任何能力代理 / 命令工具的文件操作只允许发生在它本任务节点的
Artifact Workspace 内**。仅靠提示词不够，必须工具层强制：

1. ``ensure_workspace`` ：调度器在 ``_run_one_task_node`` 前创建并绑定。
2. ``resolve_safe_path``：file_manager / command_executor 把所有相对路径解析到
   workspace 下；绝对路径必须落在 ``allowed_write_roots`` 内。
3. ``scan_workspace_changes``：命令执行后扫描新增 / 修改文件，登记为
   ``workspace_file`` 类型的 artifact。
4. ``record_violation``：越界写入直接拒绝并写 ``workspace_violation`` 日志，
   同一子任务多次越界由调度器升级为 fail。
"""

import os
from typing import List, Tuple, Optional
from pathlib import Path
from datetime import datetime, timedelta

from sqlmodel import col, func, select

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import AI_CORE_PATH, get_res_path
from gsuid_core.utils.database.base_models import async_maker

from .models import AIAgentTask, AIAgentTaskLog, AIAgentArtifact
from .runtime import PlanRunContext, get_plan_context

# data/ai_core/artifacts/{root_task_id}/{task_id}/workspace/...
ARTIFACT_ROOT: Path = get_res_path(AI_CORE_PATH / "artifacts")

# 单 inline 工件上限（设计稿 §5.2：≤4KB 文本用 inline）
INLINE_PAYLOAD_LIMIT = 4 * 1024
# Artifact 默认 TTL
DEFAULT_TTL_DAYS = 30
# 单子任务允许的连续越界次数；超过后调度器升级为 fail
MAX_WORKSPACE_VIOLATIONS = 3


def task_workspace_root(root_task_id: str, task_id: str) -> Path:
    """返回某任务节点的 Artifact Workspace 绝对路径（不创建）。

    路径形如 ``data/ai_core/artifacts/<root>/<task>/workspace/``——本任务的
    **所有中间代码 + 真实产物文件 + 落盘 artifact** 都住在这里，方便用户在
    webconsole 直接查看，也方便主人格用 `artifact_get` / `send_message_by_ai`
    取产物。
    """
    return ARTIFACT_ROOT / root_task_id / task_id / "workspace"


def ensure_workspace(root_task_id: str, task_id: str, agent_profile: str = "") -> Path:
    """创建 / 复用某任务节点的 Artifact Workspace；返回绝对路径。

    设计上**不再**按 ``agent_profile`` 分子目录——同一任务下中间代码与最终产物
    曾经被 ``workspace/<profile>/`` 与 ``{artifact_id}/payload.<ext>`` 分散在两个
    目录，导致 webconsole 只能看见最终产物却看不到中间代码（实测会话
    ``e05e495b`` 主人投诉点之一）。现在统一打平到 ``workspace/`` 一层：

    - 中间产物（代码 / 临时文件）写在 ``workspace/...``；
    - ``artifact_put(file_path=...)`` 不再 copy 文件——直接登记 workspace 内的
      原文件路径；
    - ``artifact_put(payload=...)`` 大文本溢出时落盘到 ``workspace/_artifact_<id>.<ext>``。

    保留 ``agent_profile`` 入参以维持调用方签名兼容，但目前忽略它。
    """
    _ = agent_profile  # 兼容旧调用方
    ws = task_workspace_root(root_task_id, task_id)
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _is_inside(path: Path, root: Path) -> bool:
    """判断 path 是否在 root 子树内（resolve 后比较绝对路径）。

    使用字符串前缀比较前先以 ``os.sep`` 结尾，防止
    ``/data/ai_core/artifacts/abc`` 误匹配 ``/data/ai_core/artifacts/abcd``。
    """
    try:
        rp = path.resolve()
        rr = root.resolve()
    except OSError:
        return False
    rp_s = str(rp)
    rr_s = str(rr).rstrip(os.sep) + os.sep
    return rp_s == str(rr).rstrip(os.sep) or rp_s.startswith(rr_s)


def resolve_safe_path(
    requested: str,
    fallback_root: Path,
    plan_ctx: Optional[PlanRunContext] = None,
) -> Tuple[Optional[Path], str]:
    """解析一个工具请求路径，强制落在允许的可写根目录内。

    解析顺序：
    1. 如果当前在 Kanban 任务执行上下文（plan_ctx.artifact_workspace 非空）：
       - 相对路径解析到 workspace 下；
       - 绝对路径必须位于 ``allowed_write_roots`` 任一根之内（含 workspace）。
    2. 否则（v1 旧路径 / 兼容场景）：退回 ``fallback_root`` 沙盒（与历史行为一致）。
    3. 任何 ``..`` 跳出 / 符号链接跳出 / 越界绝对路径直接拒绝并返回原因。

    Returns:
        (解析后的绝对路径, 错误信息)。成功时第二项为空串。
    """
    if not requested:
        return None, "路径不能为空"

    # 1) Kanban 任务上下文：以 workspace 为根
    if plan_ctx is None:
        plan_ctx = get_plan_context()
    workspace = plan_ctx.artifact_workspace if plan_ctx else None
    allowed_roots: List[Path] = list(plan_ctx.allowed_write_roots) if plan_ctx else []

    try:
        norm = os.path.normpath(requested)
        candidate = Path(norm)

        if workspace is not None:
            # workspace 始终算允许写入根
            roots = list(allowed_roots)
            if workspace not in roots:
                roots.append(workspace)
            base = candidate if candidate.is_absolute() else workspace / candidate
            full = base.resolve()
            for r in roots:
                if _is_inside(full, r):
                    return full, ""
            return None, ("越界路径：仅允许写入任务的 Artifact Workspace 或显式授权根目录")

        # 2) 兼容旧路径：FILE_PATH 沙盒
        base = candidate if candidate.is_absolute() else fallback_root / candidate
        full = base.resolve()
        if _is_inside(full, fallback_root):
            return full, ""
        return None, "越界路径：超出沙盒目录"
    except OSError as e:
        return None, f"路径解析失败：{e}"


async def record_violation(
    task_id: str,
    detail: str,
    *,
    root_task_id: str = "",
) -> None:
    """登记一次工作区越界事件（``workspace_violation`` 日志）。

    设计稿 §5.4.6：同一子任务累计越界达 ``MAX_WORKSPACE_VIOLATIONS`` 次时，
    框架直接把该子任务升级为 ``failed`` 并把原因交给主人格，避免代理无限重试。
    """
    await AIAgentTaskLog.add_log(
        task_id,
        "workspace_violation",
        f"工作区越界拒绝：{detail}"[:4000],
    )
    logger.warning(
        t(
            "📋 [Kanban] 工作区越界 task={task_id} root={root_task_id}: {p0}",
            task_id=task_id,
            root_task_id=root_task_id,
            p0=detail[:200],
        )
    )
    # 统计同一子任务的越界次数，达上限直接升级 fail
    async with async_maker() as session:
        stmt = (
            select(func.count())
            .select_from(AIAgentTaskLog)
            .where(col(AIAgentTaskLog.task_id) == task_id)
            .where(col(AIAgentTaskLog.event_type) == "workspace_violation")
        )
        row = await session.execute(stmt)
        count = int(row.scalar_one() or 0)
    if count < MAX_WORKSPACE_VIOLATIONS:
        return
    # 升级 fail
    task = await AIAgentTask.get_by_id(task_id)
    if task is None or task.status not in ("pending", "running"):
        return
    from . import kanban  # 避免循环导入

    reason = f"工作区越界累计 {count} 次，达上限 {MAX_WORKSPACE_VIOLATIONS}，最近一次：{detail[:200]}"
    await kanban.mark_subtask_failed(task, reason)
    if task.root_task_id:
        await kanban.refresh_root_status(task.root_task_id)


def scan_workspace_changes(
    workspace: Path,
    before_snapshot: dict,
) -> List[Tuple[Path, int]]:
    """对比命令执行前后的 workspace 快照，返回新增 / 修改文件列表。

    Args:
        workspace: 工作区根目录
        before_snapshot: 由 ``snapshot_workspace`` 在执行前拍下的快照
            ``{relative_path_str: mtime}``

    Returns:
        变更文件列表 ``[(absolute_path, size_bytes)]``。
    """
    changes: List[Tuple[Path, int]] = []
    if not workspace.exists():
        return changes
    for p in workspace.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = str(p.relative_to(workspace))
            mtime = p.stat().st_mtime
            size = p.stat().st_size
        except OSError:
            continue
        prev = before_snapshot.get(rel)
        if prev is None or prev < mtime:
            changes.append((p, size))
    return changes


def snapshot_workspace(workspace: Path) -> dict:
    """拍一份 workspace 内文件 mtime 快照，供 ``scan_workspace_changes`` 对比。"""
    snap: dict = {}
    if not workspace.exists():
        return snap
    for p in workspace.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = str(p.relative_to(workspace))
            snap[rel] = p.stat().st_mtime
        except OSError:
            continue
    return snap


async def register_workspace_artifacts(
    root_task_id: str,
    task_id: str,
    workspace: Path,
    changes: List[Tuple[Path, int]],
    agent_profile: str = "",
    parent_task_id: Optional[str] = None,
) -> List[AIAgentArtifact]:
    """把命令执行后扫描到的 workspace 文件登记为 ``workspace_file`` artifact。"""
    rows: List[AIAgentArtifact] = []
    now = datetime.now()
    for path, size in changes:
        try:
            rel = str(path.relative_to(workspace))
        except OSError:
            continue
        art = AIAgentArtifact(
            root_task_id=root_task_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            from_profile=agent_profile,
            artifact_kind="workspace_file",
            mime="application/octet-stream",
            summary=f"workspace 写入: {rel}"[:512],
            payload_path=str(path),
            size_bytes=size,
            expires_at=now + timedelta(days=DEFAULT_TTL_DAYS),
        )
        rows.append(art)
    if rows:
        await AIAgentArtifact.batch_insert_data(rows)
    return rows


# 常见文件后缀 → MIME 速查（供 file_path 登记路径自动推断）。覆盖图片 / 文档 /
# 数据等代理最常落盘的产物，未命中时退回 "application/octet-stream"——
# send_message_by_ai 看到不是 image/* 也能按字节发，不会卡住。
_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".xml": "application/xml",
    ".zip": "application/zip",
}


async def put_artifact(
    payload: str = "",
    summary: str = "",
    mime: str = "",
    *,
    artifact_kind: str = "output",
    plan_ctx: Optional[PlanRunContext] = None,
    file_path: Optional[Path] = None,
) -> Optional[AIAgentArtifact]:
    """在当前任务上下文里登记一个 artifact。

    三种登记模式（必须三选一，不要混用）：

    1. **登记真实文件** —— 传 ``file_path``（``Path`` 或字符串）。文件会被
       拷贝到 ``data/ai_core/artifacts/{root}/{task}/{art_id}/payload.<ext>``，
       artifact 行的 ``payload_path`` 指向新副本（不动原文件，便于命令前后扫描
       仍能登记 workspace_file）。``mime`` 不传时按后缀自动推断；``summary`` 不
       传时退回 ``"file: <basename>"``。这是 PNG / PDF / CSV / 二进制等"真产物"
       的唯一正确登记入口——别再用 ``payload="{file: 'x.png', size: 17842}"``
       这种"元数据冒充文件"的写法。
    2. **登记 inline 文本** —— 传 ``payload``（≤ 4KB 文本，``mime`` 默认 ``text/plain``）。
       存进 ``payload_inline``，主人格 ``artifact_get`` 时直读。
    3. **登记落盘大文本** —— 同 (2) 但 payload 超 4KB，会自动落盘成
       ``payload.<ext>``，``mime`` 决定扩展名（``json`` → ``.json`` / ``html`` →
       ``.html`` / ``markdown`` → ``.md`` / 其它 → ``.txt``）。

    返回新建的 AIAgentArtifact 行；不在任务上下文中时返回 None。
    """
    if plan_ctx is None:
        plan_ctx = get_plan_context()
    if plan_ctx is None or not plan_ctx.root_task_id:
        return None

    workspace = plan_ctx.artifact_workspace
    if workspace is None:
        # 没有 workspace 上下文也允许 inline 文本（兜底）；真实文件无处可放则失败。
        workspace = task_workspace_root(plan_ctx.root_task_id, plan_ctx.task_id)
        workspace.mkdir(parents=True, exist_ok=True)

    # ── 模式 1：登记真实文件（不再 copy；直接登记 workspace 内的原文件路径）──
    if file_path is not None:
        src = Path(file_path) if not isinstance(file_path, Path) else file_path
        if not src.is_absolute():
            src = (workspace / src).resolve()
        if not src.exists() or not src.is_file():
            return None
        # 安全闸刀：登记的文件必须落在 workspace 内（避免登记越界路径，绕过沙盒）
        if not _is_inside(src, workspace):
            logger.warning(t("📋 [Kanban] artifact_put 拒绝登记越界文件: {src} 不在 workspace 内", src=src))
            return None
        suffix = src.suffix.lower()
        resolved_mime = mime or _EXT_TO_MIME.get(suffix, "application/octet-stream")
        art = AIAgentArtifact(
            root_task_id=plan_ctx.root_task_id,
            task_id=plan_ctx.task_id,
            from_profile=plan_ctx.agent_profile,
            artifact_kind=artifact_kind,
            mime=resolved_mime,
            summary=(summary or f"file: {src.name}")[:512],
            payload_path=str(src),
            size_bytes=src.stat().st_size,
            expires_at=datetime.now() + timedelta(days=DEFAULT_TTL_DAYS),
        )
        await AIAgentArtifact.batch_insert_data([art])
        return art

    # ── 模式 2 / 3：inline 文本 / 大文本落盘 ──
    resolved_mime = mime or "text/plain"
    art = AIAgentArtifact(
        root_task_id=plan_ctx.root_task_id,
        task_id=plan_ctx.task_id,
        from_profile=plan_ctx.agent_profile,
        artifact_kind=artifact_kind,
        mime=resolved_mime,
        summary=summary[:512],
        expires_at=datetime.now() + timedelta(days=DEFAULT_TTL_DAYS),
    )
    if len(payload) <= INLINE_PAYLOAD_LIMIT:
        art.payload_inline = payload
        art.size_bytes = len(payload.encode("utf-8"))
    else:
        # 大文本溢出落盘 —— 直接写到 workspace 里，与代理写的中间产物共存
        ext = "txt"
        if "json" in resolved_mime:
            ext = "json"
        elif "html" in resolved_mime:
            ext = "html"
        elif "markdown" in resolved_mime or resolved_mime == "text/markdown":
            ext = "md"
        target = workspace / f"_artifact_{art.id}.{ext}"
        target.write_text(payload, encoding="utf-8")
        art.payload_path = str(target)
        art.size_bytes = target.stat().st_size
    await AIAgentArtifact.batch_insert_data([art])
    return art
