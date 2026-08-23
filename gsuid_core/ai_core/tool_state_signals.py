"""L2 状态驱动工具池：按"用户已存在的持久实体"把对应能力族补进保底工具池。

背景
----
工具默认靠"单条消息的语义召回"加载，但这对**跨轮次/有时间差的追问**无能为力：
用户先说"明晚提醒我吃西湖醋鱼"，一小时后再说"帮我改成后天吧"——后一句本身没有任何
调度语义，向量检索召不回 ``modify_scheduled_task`` / ``cancel_scheduled_task``。

本模块改为按"持久状态"判定：只要用户名下确实存在某类持久实体，就把对应能力族常驻进
工具列表，使变更/读取工具在任意后续轮次都触手可及，无论那一轮用户说了什么。现有映射：

- 活跃(running/waiting_approval) Kanban 任务 → ``长期任务编排`` + ``产物`` 族；
- 未完成(pending/paused) 定时任务            → ``定时任务`` 族；
- 名下存在 ``record:*`` 结构化集合            → ``结构化记录`` 族。

这也是 planning 工具退出"无条件保底池"（见 rag.tools.GUARANTEED_TOOL_CATEGORIES）后的
精确召回主路径：A-1 要求"随时可调"的 artifact_get_recent / record_* 在真正相关的状态下
必然在列，而无关轮次不再全量常驻。

调用方（``gs_agent``）已对本模块整体加了 I/O 兜底，单次状态检测失败不会影响主聊天流程；
因此这里不再重复包裹 try-except，让 DB 异常如实抛给调用方统一处理。

扩展
----
新增"状态信号 → 能力族"映射时，只需在 :func:`get_state_driven_families` 里追加一条判定即可。
"""

from typing import Set, List, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.register import get_tools_by_capability_domain
from gsuid_core.ai_core.rag.tools import ToolList

# 有持久实体才该进池的族：核内种子不闭合同族（避免闲聊/旁观提前挂上变更工具）。
STATE_DRIVEN_FAMILY_DOMAINS: frozenset[str] = frozenset({"长期任务编排", "产物", "定时任务", "结构化记录"})


async def user_has_active_schedules(user_id: str) -> bool:
    """用户是否存在未完成(pending/paused)的定时任务。"""
    from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

    rows = await AIScheduledTask.select_rows(user_id=user_id)
    for row in rows or []:
        task = row if isinstance(row, AIScheduledTask) else AIScheduledTask(**row)
        if task.status in ("pending", "paused"):
            return True
    return False


async def _user_has_record_collections(ev: Event) -> bool:
    """当前对话作用域下是否已存在 record:* 结构化集合。

    record_* 工具按 ev 推断作用域落库（群聊→``group:<gid>``，私聊→``user:<uid>``，
    见 record_tools._resolve_scope），这里按同一作用域查 key 前缀，判断"有没有
    可被后续轮次追问读取的集合"。
    """
    from gsuid_core.ai_core.state_store.store import state_list_keys

    scope = f"group:{ev.group_id}" if ev.group_id else f"user:{ev.user_id}"
    keys = await state_list_keys(scope, prefix="record:")
    return bool(keys)


async def _user_has_recent_completed_kanban(ev: Event, within_hours: float = 6.0) -> bool:
    """近 within_hours 内本群/本会话是否有 completed 根任务。"""
    from datetime import datetime, timedelta

    from gsuid_core.ai_core.planning.models import AIAgentTask

    rows = await AIAgentTask.list_for_owner(str(ev.user_id), only_active=False, root_only=True)
    if not rows:
        return False
    cutoff = datetime.now() - timedelta(hours=within_hours)
    gid = str(ev.group_id) if ev.group_id else ""
    sid = str(ev.session_id) if ev.session_id else ""
    for task in rows[:20]:
        if gid and str(task.group_id or "") != gid:
            continue
        if (not gid) and sid and str(task.session_id or "") and str(task.session_id) != sid:
            continue
        if task.status != "completed":
            continue
        updated = task.updated_at
        if updated is not None and updated >= cutoff:
            return True
    return False


async def get_state_driven_families(ev: Optional[Event], has_active_task: bool = False) -> List[str]:
    """返回应按"持久状态"补进保底池的能力族名列表。

    判定依据是"用户名下确实存在的持久实体"，而非当前这句话的语义：
    - 有活跃(running/waiting_approval) Kanban 任务 → 长期任务编排 + 产物 两族
      （主人格随时可能 fail_task_tree / respond_approval / 追问产物原文，
      即 A-1 要求"随时可调"的 artifact_get_recent）；
    - 有近 6h 内刚完成的根任务 → 至少挂 产物 族（追问溯源）；
    - 有未完成(pending/paused)的定时任务 → 定时任务 族（改时间 / 取消 / 暂停…）；
    - 名下存在 record:* 结构化集合 → 结构化记录 族（随时可被追问读取 / 汇总）。

    Args:
        ev: 当前事件，用于确定用户身份与作用域。
        has_active_task: 是否存在需即时介入的 Kanban 任务（由 handle_ai 预先算好透传，
            避免在此重复查库；语义同 ``planning.context.has_actionable_task``）。
    """
    if ev is None or not ev.user_id:
        return []

    domains: List[str] = []

    # 活跃 Kanban 任务：把任务编排族 + 产物族一起带出（A-1 兜底产物追问溯源）
    if has_active_task:
        domains.append("长期任务编排")
        domains.append("产物")
    elif await _user_has_recent_completed_kanban(ev):
        # 刚完成：编排工具可卸，但产物追问必须仍可用
        domains.append("产物")

    # 定时任务：用户有未完成的定时任务 → 带出整个"定时任务"族（含 modify/cancel/pause...）
    if await user_has_active_schedules(ev.user_id):
        domains.append("定时任务")

    # 结构化集合：名下已有 record:* 集合 → 带出 record 族，供任意后续轮次读取/汇总
    if await _user_has_record_collections(ev):
        domains.append("结构化记录")

    return domains


async def get_state_driven_family_tools(
    ev: Optional[Event],
    exclude_names: Set[str],
    has_active_task: bool = False,
    intent: Optional[str] = None,
) -> ToolList:
    """把状态驱动命中的能力族展开为工具列表（去重 ``exclude_names``）。

    Args:
        ev: 当前事件，用于确定用户身份。
        exclude_names: 已在保底池中的工具名，避免重复加载。
        has_active_task: 是否存在需即时介入的 Kanban 任务（透传给族判定）。
        intent: 保留兼容；**不再**因闲聊裁剪——分类器会误判，有实体就必须整族可用。
    """
    del intent  # 兼容旧调用方；装配不按意图砍状态族
    domains = await get_state_driven_families(ev, has_active_task=has_active_task)
    if not domains:
        return []

    seen = set(exclude_names)
    out: ToolList = []
    for dom in domains:
        for tb in get_tools_by_capability_domain(dom):
            if tb.name in seen:
                continue
            seen.add(tb.name)
            out.append(tb.tool)
    if out:
        logger.debug(t("log.ai.toolstate_state_driven_supplementary_create", domains=domains, p0=len(out)))
    return out
