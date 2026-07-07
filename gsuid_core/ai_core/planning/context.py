"""Kanban 任务上下文注入

每轮对话由 handle_ai 调用，把"当前用户的活跃根任务摘要"作为动态上下文注入，
让用户无需知道任何 ID 就能追问进度（"那个炒股任务怎么样了"），主人格也不会
对自己正在跑的任务树"失明"。注入内容只含用户可见短序号，绝不含 UUID。

每个活跃根任务下附**子任务粒度摘要**：让主人格区分"任务已派出去等运行"与
"任务还没动 / 等依赖 / 全部完成 / 个别失败"，避免在子任务还在跑时盲目
fail_task_tree 重建（参见 docs/AI_AGENT_ARCHITECTURE.md §3.5 持久化产物交付判据）。
"""

from gsuid_core.logger import logger

from . import kanban as kanban_manager
from .models import AIAgentTask

# 每轮最多注入的根任务条数
_MAX_INJECT = 5

# 任务可能出现的状态 → 中文。status 字段值域受 TASK_STATUSES 约束
# （见 models.py），越界即按原文显示。
_STATUS_CN: dict[str, str] = {
    "pending": "待启动",
    "running": "运行中",
    "paused": "已暂停",
    "waiting_approval": "等待审批",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def _status_cn(s: str) -> str:
    return _STATUS_CN[s] if s in _STATUS_CN else s


async def build_task_context(user_id: str) -> str:
    """构造当前用户活跃根任务的注入文本块（含每个根下子任务状态摘要）。"""
    try:
        tasks = await AIAgentTask.list_for_owner(str(user_id), only_active=True, root_only=True)
    except Exception as e:
        logger.debug(f"📋 [Kanban] 任务上下文注入失败: {e}")
        return ""
    if not tasks:
        return ""

    lines = ["【你正在为对方推进的 Kanban 任务（可被追问，无需 ID）】"]
    for t in tasks[:_MAX_INJECT]:
        upd = t.updated_at.strftime("%m-%d %H:%M") if t.updated_at else ""
        status_cn = _status_cn(t.status)
        # 周期模板：显式标注"模板态"——主人格看到这种不要 fail 重建
        is_template = bool(t.recurring_trigger)
        kind_label = "周期模板（等 cron 触发）" if is_template else "一次性任务"
        lines.append(f"任务#{t.ordinal}｜{t.display_name}｜{status_cn}｜{kind_label}｜更新于{upd}")

        # 子任务摘要——只取活跃 + 最近完成 + 最近失败各几条，避免太长
        try:
            _, children = await kanban_manager.get_task_tree(t.id)
        except Exception as e:
            logger.debug(f"📋 [Kanban] 拉子任务摘要失败 root={t.id}: {e}")
            children = []
        if not children:
            continue
        # 按 ordinal 排序，给出每个子任务一行
        bucket: dict[str, int] = {}
        for ch in children:
            bucket[ch.status] = bucket.get(ch.status, 0) + 1
        if bucket:
            buckets_text = "、".join(f"{_status_cn(k)}×{v}" for k, v in bucket.items())
            lines.append(f"  └ 子任务 {len(children)} 个：{buckets_text}")
        # 列出"running / waiting_approval / failed"个别——主人格最需要知道这几类
        salient = [ch for ch in children if ch.status in ("running", "waiting_approval", "failed")][:4]
        for ch in salient:
            agent = ch.agent_profile or "-"
            short = (ch.display_name or ch.goal or "")[:30]
            lines.append(f"     · #{ch.ordinal} [{agent}] {short}｜{_status_cn(ch.status)}")
    return "\n".join(lines)


async def has_actionable_task(user_id: str) -> bool:
    """判断用户是否有需要主人格即时介入的 Kanban 任务。

    与 ``build_task_context`` 的"只要活跃就注入"不同，本函数只关心
    ``running`` / ``waiting_approval`` 状态——这两种状态意味着主人格
    随时可能需要 fail_task_tree / respond_approval。
    ``pending`` / ``paused``（如十几天后的周期模板）不触发，避免闲聊
    时无谓挂载 15 个 planning 工具。
    """
    try:
        tasks = await AIAgentTask.list_for_owner(str(user_id), only_active=True, root_only=True)
    except Exception:
        return False
    actionable_statuses = {"running", "waiting_approval"}
    return any(t.status in actionable_statuses for t in tasks)
