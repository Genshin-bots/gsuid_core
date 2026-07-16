"""Kanban 任务上下文注入

每轮对话由 handle_ai 调用，把"当前用户的活跃根任务摘要"作为动态上下文注入，
让用户无需知道任何 ID 就能追问进度（"那个炒股任务怎么样了"），主人格也不会
对自己正在跑的任务树"失明"。注入内容只含用户可见短序号，绝不含 UUID。

每个活跃根任务下附**子任务粒度摘要**：让主人格区分"任务已派出去等运行"与
"任务还没动 / 等依赖 / 全部完成 / 个别失败"，避免在子任务还在跑时盲目
fail_task_tree 重建（参见 docs/AI_AGENT_ARCHITECTURE.md §3.5 持久化产物交付判据）。
"""

from typing import Optional

from gsuid_core.i18n import t as i18n_t
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


async def build_task_context(user_id: str, current_group_id: Optional[str] = None) -> str:
    """构造当前用户活跃根任务的注入文本块（含每个根下子任务状态摘要）。

    §24 跨群脱敏：任务按 user 维度归属，但 A 群的任务详情（群号/任务名）注入
    B 群上下文后可能被模型复述外泄。传入 ``current_group_id`` 时，非本群任务
    只汇总为一行脱敏计数，不展开细节。
    """
    try:
        tasks = await AIAgentTask.list_for_owner(str(user_id), only_active=True, root_only=True)
    except Exception as e:
        logger.debug(i18n_t("📋 [Kanban] 任务上下文注入失败: {e}", e=e))
        return ""
    if not tasks:
        return ""

    other_scope_count = 0
    if current_group_id is not None:
        visible: list = []
        for t in tasks:
            # 私聊任务（group_id 空）对群同样是"其他会话"：详情不进群上下文（评审修复 F12）
            if str(t.group_id or "") != str(current_group_id):
                other_scope_count += 1
            else:
                visible.append(t)
        tasks = visible

    lines = ["【你正在为对方推进的 Kanban 任务（可被追问，无需 ID）】"]
    if other_scope_count:
        lines.append(f"（另有 {other_scope_count} 个任务在其他会话推进中——细节不属于本群，被问到也只说这一句）")
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
            logger.debug(i18n_t("📋 [Kanban] 拉子任务摘要失败 root={p0}: {e}", p0=t.id, e=e))
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


async def has_actionable_task(user_id: str, current_group_id: Optional[str] = None) -> bool:
    """判断用户是否有需要主人格即时介入的 Kanban 任务。

    与 ``build_task_context`` 的"只要活跃就注入"不同，本函数只关心
    ``running`` / ``waiting_approval`` 状态——这两种状态意味着主人格
    随时可能需要 fail_task_tree / respond_approval。
    ``pending`` / ``paused``（如十几天后的周期模板）不触发，避免闲聊
    时无谓挂载 15 个 planning 工具。

    传入 ``current_group_id`` 时只统计当前群的任务：他群/私聊任务不在本群
    触发 kanban 工具族挂载，防经工具通道旁路 §24 脱敏（评审修复 E15）。
    """
    try:
        tasks = await AIAgentTask.list_for_owner(str(user_id), only_active=True, root_only=True)
    except Exception:
        return False
    if current_group_id is not None:
        tasks = [t for t in tasks if str(t.group_id or "") == str(current_group_id)]
    actionable_statuses = {"running", "waiting_approval"}
    return any(t.status in actionable_statuses for t in tasks)
