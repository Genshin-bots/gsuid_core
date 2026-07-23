"""Agent Mesh Kanban · 任务树 manager。

模块边界：本文件只负责"任务树状态机 + 依赖判定 + 状态汇总 + 失败 / 重派 /
审批 / 暂停恢复"。并发调度由 ``kanban_executor`` 调用 ``get_ready_child_tasks``
拿到可跑的子任务，再用 ``asyncio.gather`` 并发派活。
"""

import shutil
import asyncio
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path
from datetime import datetime, timedelta

from sqlmodel import col, select, update
from sqlalchemy import delete
from sqlalchemy.engine import CursorResult

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import async_maker

from .models import (
    AIAgentTask,
    AIAgentTaskLog,
    AIAgentArtifact,
)

# 子任务级别的 asyncio.Lock，防止同一子任务并发被多次派活
_TASK_NODE_LOCKS: Dict[str, asyncio.Lock] = {}
# 根任务级别的状态刷新锁，避免并发刷写脏读
_ROOT_REFRESH_LOCKS: Dict[str, asyncio.Lock] = {}

# 默认重派上限：达到该次数后强制 waiting_approval
DEFAULT_RESPAWN_LIMIT = 3
# 默认每棵任务树最多子任务数（防 LLM 拆爆）
DEFAULT_MAX_SUBTASKS = 20


def get_task_node_lock(task_id: str) -> asyncio.Lock:
    """取该子任务节点的执行锁；不存在则懒建。"""
    lock = _TASK_NODE_LOCKS.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _TASK_NODE_LOCKS[task_id] = lock
    return lock


def get_root_refresh_lock(root_task_id: str) -> asyncio.Lock:
    lock = _ROOT_REFRESH_LOCKS.get(root_task_id)
    if lock is None:
        lock = asyncio.Lock()
        _ROOT_REFRESH_LOCKS[root_task_id] = lock
    return lock


# 创建：任务树


async def create_kanban_tree(
    *,
    goal: str,
    owner_user_id: str,
    scope_key: str,
    bot_id: str,
    persona_name: Optional[str],
    bot_self_id: str = "",
    group_id: Optional[str] = None,
    user_type: str = "direct",
    WS_BOT_ID: Optional[str] = None,
    session_id: str = "",
    user_pm: int = 6,
    broadcast_targets: Optional[List[str]] = None,
    display_name: str = "",
    interval_seconds: int = 0,
    subtasks: Optional[List[Dict[str, Any]]] = None,
    recurring_trigger: Optional[str] = None,
    recurring_until: Optional[datetime] = None,
    root_agent_profile: str = "",
) -> Tuple[AIAgentTask, List[AIAgentTask]]:
    """创建一棵 Kanban 任务树。两种形态：

    1. **多步任务树**：``subtasks`` 非空 + ``root_agent_profile`` 为空——
       创建 1 个根任务（聚合节点，不执行）+ N 个子任务节点。
    2. **叶子根（单步自执行任务）**：``subtasks`` 为空 + ``root_agent_profile``
       非空——只创建 1 个根任务，该根任务自身带 ``agent_profile``，被
       ``kanban_executor`` 当作单一可执行节点派出。**用于
       `create_subagent(agent_profile=...)` 这种"一句话委派单个专业代理"**，
       避免冗余的"根 + 1 子任务"双节点结构（实测会话 e05e495b 主人投诉点）。

    Args:
        subtasks: 描述子任务的列表，每项形如::

            {
                "description": str,
                "agent_profile": str,
                "depends_on": [int],          # 引用本数组下标（0-based）
                "params_hint": dict,
            }

            为空时配合 ``root_agent_profile`` 走"叶子根"路径。
        root_agent_profile: 叶子根模式下根任务自身的 ``agent_profile``；与
            ``subtasks`` 不能同时非空。
        recurring_trigger: 周期触发规则字符串，留空创建一次性任务树。详见
            ``planning/recurring.py`` 的 ``parse_trigger_spec``。
        recurring_until: 周期模式失效时间；None 表示不过期。

    Returns:
        (root_task, list[subtask])。叶子根模式下 list[subtask] 为空。
        周期模式下 root 是**模板**，永不直接执行；每次到点由
        ``recurring._fire_template`` 克隆实例树执行。
    """
    subtasks = subtasks or []
    if subtasks and root_agent_profile:
        raise ValueError(t("create_kanban_tree: subtasks 与 root_agent_profile 不能同时非空"))
    if len(subtasks) > DEFAULT_MAX_SUBTASKS:
        raise ValueError(t("子任务数量超过上限 {DEFAULT_MAX_SUBTASKS}", DEFAULT_MAX_SUBTASKS=DEFAULT_MAX_SUBTASKS))

    is_template = bool(recurring_trigger)
    if is_template and root_agent_profile:
        raise ValueError(t("create_kanban_tree: 周期模板不支持叶子根（请显式建子任务）"))

    # 子任务级周期触发的依赖关系硬约束：禁止任何子任务 depends_on 周期子任务。
    # 周期子任务永不"完成"（armed 持续到 recurring_until 才 disarm），下游若依赖
    recurring_indexes = {i for i, spec in enumerate(subtasks) if (spec.get("recurring_trigger") or "").strip()}
    if recurring_indexes:
        for spec in subtasks:
            for dep in spec.get("depends_on") or []:
                if isinstance(dep, int) and dep in recurring_indexes:
                    raise ValueError(
                        t(
                            "create_kanban_tree: 子任务依赖周期子任务会死锁——周期子任务持续 armed，"
                            "下游永远等不到 completed。请用 not_before 给下游设定开始时间错开。"
                        )
                    )

    # 1) 根任务（模板状态/普通状态均为 pending；模板由 recurring_status 区分）
    #    叶子根模式下根任务自身带 agent_profile，被 kanban_executor 直接派出。
    root = await AIAgentTask.create_task(
        goal=goal,
        owner_user_id=owner_user_id,
        scope_key=scope_key,
        bot_id=bot_id,
        bot_self_id=bot_self_id,
        group_id=group_id,
        user_type=user_type,
        WS_BOT_ID=WS_BOT_ID,
        session_id=session_id,
        user_pm=user_pm,
        persona_name=persona_name,
        broadcast_targets=broadcast_targets or [],
        display_name=display_name,
        interval_seconds=interval_seconds,
        agent_profile=root_agent_profile,
        status="pending",
        node_kind="root",
        parent_task_id=None,
        recurring_trigger=recurring_trigger,
        recurring_until=recurring_until,
        recurring_status="armed" if is_template else "",
    )
    # 根任务 root_task_id = 自身 id
    await AIAgentTask.update_data_by_data(select_data={"id": root.id}, update_data={"root_task_id": root.id})
    root.root_task_id = root.id

    # 2) 子任务（先批量插入，再回填 dependency 的实际 id）
    child_ids: List[str] = []
    children: List[AIAgentTask] = []
    for spec in subtasks:
        not_before_val = spec.get("not_before")
        # 兼容 ISO 字符串 / datetime 两种入参
        if isinstance(not_before_val, str) and not_before_val:
            try:
                not_before_val = datetime.fromisoformat(not_before_val)
            except ValueError:
                not_before_val = None
        elif not isinstance(not_before_val, datetime):
            not_before_val = None

        # 子任务级周期触发参数
        sub_recurring_trigger = (spec.get("recurring_trigger") or "").strip() or None
        sub_recurring_until = spec.get("recurring_until")
        if isinstance(sub_recurring_until, str) and sub_recurring_until:
            try:
                sub_recurring_until = datetime.fromisoformat(sub_recurring_until)
            except ValueError:
                sub_recurring_until = None
        elif not isinstance(sub_recurring_until, datetime):
            sub_recurring_until = None

        child = await AIAgentTask.create_task(
            goal=str(spec.get("description") or "").strip() or goal,
            owner_user_id=owner_user_id,
            scope_key=scope_key,
            bot_id=bot_id,
            bot_self_id=bot_self_id,
            group_id=group_id,
            user_type=user_type,
            WS_BOT_ID=WS_BOT_ID,
            session_id=session_id,
            user_pm=user_pm,
            persona_name=persona_name,
            broadcast_targets=[],
            display_name=str(spec.get("description") or "")[:64] or display_name,
            interval_seconds=0,
            agent_profile=str(spec.get("agent_profile") or "").strip(),
            # 周期子任务模板自身不进 ready 队列；
            # 注册时直接挂上 APScheduler
            status="pending",
            node_kind="subtask",
            parent_task_id=root.id,
            root_task_id=root.id,
            params_override=spec.get("params_hint") or {},
            not_before=not_before_val,
            recurring_trigger=sub_recurring_trigger,
            recurring_until=sub_recurring_until,
            # 模板子任务的 recurring_status 入库时为空——首次依赖满足时由 executor
            # 转 "armed"；arm 失败时（trigger 解析错）转 "disarmed" 不阻塞下游。
            recurring_status="",
        )
        child_ids.append(child.id)
        children.append(child)

    # 3) 把 depends_on 的 0-based 下标转为兄弟子任务 id
    for child, spec in zip(children, subtasks):
        deps_idx = [int(x) for x in (spec.get("depends_on") or []) if isinstance(x, int)]
        dep_ids: List[str] = []
        for i in deps_idx:
            if 0 <= i < len(child_ids) and child_ids[i] != child.id:
                dep_ids.append(child_ids[i])
        if dep_ids:
            child.dependency_task_ids = dep_ids
            await AIAgentTask.update_data_by_data(
                select_data={"id": child.id},
                update_data={"dependency_task_ids": dep_ids},
            )

    await AIAgentTaskLog.add_log(
        root.id,
        "plan_created",
        f"Kanban 任务树创建：1 根 + {len(children)} 子任务 | 目标: {goal[:200]}",
    )
    logger.info(
        t(
            "📋 [Kanban] 创建任务树 root=#{p0} {p1}（{p2} 子任务）",
            p0=root.ordinal,
            p1=root.display_name,
            p2=len(children),
        )
    )
    return root, children


# 周期触发：模板克隆


async def clone_tree_for_fire(
    template_root: AIAgentTask,
) -> Tuple[AIAgentTask, List[AIAgentTask]]:
    """从一棵模板任务树克隆出"本次开火"的实例树。

    模板根（``recurring_trigger`` 非空）自身永远不参与执行；它只是个样板，
    每次 ``fire_recurring`` 到点时调用本函数复制出一棵全新的"实例树"（新 id /
    新 ordinal / pending 状态），实例树跑完即完结，不影响模板。

    实例树根的 ``template_root_id`` 反向指向模板，便于 webconsole 把"近 N 次
    开火实例"挂到模板下展示。
    """
    template_children = await _query_children(template_root.id)

    instance_root = await AIAgentTask.create_task(
        goal=template_root.goal,
        owner_user_id=template_root.owner_user_id,
        scope_key=template_root.scope_key,
        bot_id=template_root.bot_id,
        bot_self_id=template_root.bot_self_id,
        group_id=template_root.group_id,
        user_type=template_root.user_type,
        WS_BOT_ID=template_root.WS_BOT_ID,
        session_id=template_root.session_id,
        user_pm=template_root.user_pm,
        persona_name=template_root.persona_name,
        broadcast_targets=list(template_root.broadcast_targets or []),
        display_name=f"{template_root.display_name} #{template_root.fire_count + 1}"[:64],
        interval_seconds=0,
        agent_profile="",
        status="pending",
        node_kind="root",
        parent_task_id=None,
        # 关键：实例根反向指回模板
        template_root_id=template_root.id,
        # 实例本身不是周期任务
        recurring_trigger=None,
        recurring_status="",
    )
    await AIAgentTask.update_data_by_data(
        select_data={"id": instance_root.id},
        update_data={"root_task_id": instance_root.id},
    )
    instance_root.root_task_id = instance_root.id

    # 模板子任务的旧 id → 新实例子任务的 id 映射，用于重映射 dependency_task_ids
    id_map: Dict[str, str] = {}
    instance_children: List[AIAgentTask] = []
    for tpl_child in template_children:
        # 注意：模板上的 not_before 是绝对时间，克隆到实例时通常已经过期；
        # 周期模板自身的 cron 表达式已经决定了"什么时候开火"，子任务级再叠 not_before
        new_child = await AIAgentTask.create_task(
            goal=tpl_child.goal,
            owner_user_id=tpl_child.owner_user_id,
            scope_key=tpl_child.scope_key,
            bot_id=tpl_child.bot_id,
            bot_self_id=tpl_child.bot_self_id,
            group_id=tpl_child.group_id,
            user_type=tpl_child.user_type,
            WS_BOT_ID=tpl_child.WS_BOT_ID,
            session_id=tpl_child.session_id,
            user_pm=tpl_child.user_pm,
            persona_name=tpl_child.persona_name,
            broadcast_targets=[],
            display_name=tpl_child.display_name,
            interval_seconds=0,
            agent_profile=tpl_child.agent_profile,
            status="pending",
            node_kind="subtask",
            parent_task_id=instance_root.id,
            root_task_id=instance_root.id,
            params_override=dict(tpl_child.params_override or {}),
        )
        id_map[tpl_child.id] = new_child.id
        instance_children.append(new_child)

    # 把 dependency_task_ids 从模板子任务 id 重映射为实例子任务 id
    for tpl_child, new_child in zip(template_children, instance_children):
        old_deps = tpl_child.dependency_task_ids if isinstance(tpl_child.dependency_task_ids, list) else []
        new_deps = [id_map[d] for d in old_deps if d in id_map]
        if new_deps:
            new_child.dependency_task_ids = new_deps
            await AIAgentTask.update_data_by_data(
                select_data={"id": new_child.id},
                update_data={"dependency_task_ids": new_deps},
            )

    # 模板的 fire_count +1
    await AIAgentTask.update_data_by_data(
        select_data={"id": template_root.id},
        update_data={"fire_count": template_root.fire_count + 1},
    )
    await AIAgentTaskLog.add_log(
        template_root.id,
        "decision",
        f"周期触发 #{template_root.fire_count + 1} 开火 → 克隆实例 root={instance_root.id}",
    )
    await AIAgentTaskLog.add_log(
        instance_root.id,
        "plan_created",
        f"周期任务实例创建（模板 root={template_root.id} 第 {template_root.fire_count + 1} 次开火）",
    )
    logger.info(
        t(
            "📋 [Kanban] 周期触发: 模板 root={p0} 第 {p1} 次开火，克隆实例 root={p2}",
            p0=template_root.id,
            p1=template_root.fire_count + 1,
            p2=instance_root.id,
        )
    )
    return instance_root, instance_children


async def list_armed_templates() -> List[AIAgentTask]:
    """列出所有已挂上 APScheduler 的模板根任务（启动期恢复用）。"""
    async with async_maker() as session:
        stmt = (
            select(AIAgentTask)
            .where(col(AIAgentTask.node_kind) == "root")
            .where(col(AIAgentTask.recurring_trigger).is_not(None))
            .where(col(AIAgentTask.recurring_status) == "armed")
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def list_armed_subtask_templates() -> List[AIAgentTask]:
    """列出所有已 armed 的周期子任务模板（启动期恢复用）。"""
    async with async_maker() as session:
        stmt = (
            select(AIAgentTask)
            .where(col(AIAgentTask.node_kind) == "subtask")
            .where(col(AIAgentTask.recurring_trigger).is_not(None))
            .where(col(AIAgentTask.recurring_status) == "armed")
            .where(col(AIAgentTask.template_subtask_id).is_(None))
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def clone_subtask_for_fire(template: AIAgentTask) -> Optional[AIAgentTask]:
    """周期子任务模板到点开火：在同一棵任务树下克隆出一个"执行实例子任务"。

    与根任务级 ``clone_tree_for_fire`` 的差别：

    - 不克隆整棵树，只新建一个子任务行；
    - 执行实例的 ``template_subtask_id`` 反向指向模板自身；
    - 实例的 ``dependency_task_ids`` 故意留空——模板的依赖已经在首次 arm 时
      被框架确认满足，每次 fire 都是独立任务；
    - 实例自己**不**带 recurring_trigger / recurring_status，跑完即完结；
    - 实例保留模板的 ``params_override`` / ``agent_profile`` / ``goal``，让代理拿到
      跟首次相同的指令。

    并发安全：fire 流程仍走 APScheduler 单线程回调，模板 ``fire_count`` 累加
    用条件 UPDATE 保证乐观一致。

    Returns:
        新建的执行实例子任务行；模板 disarmed / cancelled / 过期时返回 None。
    """
    # 状态合法性校验：模板不应被 disarm / 整树 cancel 后还触发
    fresh = await AIAgentTask.get_by_id(template.id)
    if fresh is None:
        return None
    if fresh.recurring_status != "armed":
        return None
    if fresh.recurring_until is not None and fresh.recurring_until < datetime.now():
        # 模板已过期，自动 disarm
        await disarm_subtask_template(fresh.id)
        return None
    # 整树是否仍活跃
    root_task = await AIAgentTask.get_by_id(fresh.root_task_id) if fresh.root_task_id else None
    if root_task is None or root_task.status in ("failed", "cancelled", "completed"):
        # 根任务已终结，被动 disarm 不再 fire
        await disarm_subtask_template(fresh.id)
        return None

    new_child = await AIAgentTask.create_task(
        goal=fresh.goal,
        owner_user_id=fresh.owner_user_id,
        scope_key=fresh.scope_key,
        bot_id=fresh.bot_id,
        bot_self_id=fresh.bot_self_id,
        group_id=fresh.group_id,
        user_type=fresh.user_type,
        WS_BOT_ID=fresh.WS_BOT_ID,
        session_id=fresh.session_id,
        user_pm=fresh.user_pm,
        persona_name=fresh.persona_name,
        broadcast_targets=[],
        display_name=f"{fresh.display_name} #{fresh.fire_count + 1}"[:64],
        interval_seconds=0,
        agent_profile=fresh.agent_profile,
        status="pending",
        node_kind="subtask",
        parent_task_id=fresh.root_task_id,
        root_task_id=fresh.root_task_id,
        params_override=dict(fresh.params_override or {}),
        # 实例不挂依赖——每次 fire 独立。下游需要"等周期收尾"的场景请用 not_before
        # 给汇总子任务设定时间锚点。
        dependency_task_ids=[],
        # 关键：反向指针，便于 webconsole 把"近 N 次开火"挂到模板下展示
        template_subtask_id=fresh.id,
        recurring_trigger=None,
        recurring_status="",
    )

    # 模板 fire_count +1（条件 UPDATE）
    await AIAgentTask.update_data_by_data(
        select_data={"id": fresh.id},
        update_data={"fire_count": fresh.fire_count + 1},
    )
    await AIAgentTaskLog.add_log(
        fresh.id,
        "decision",
        f"周期子任务触发 #{fresh.fire_count + 1} 开火 → 执行实例 subtask={new_child.id}",
    )
    await AIAgentTaskLog.add_log(
        new_child.id,
        "plan_created",
        f"周期子任务实例创建（模板 subtask={fresh.id} 第 {fresh.fire_count + 1} 次开火）",
    )
    logger.info(
        t(
            "📋 [Kanban] 周期子任务: 模板 subtask={p0} 第 {p1} 次开火 → 实例 subtask={p2} root={p3}",
            p0=fresh.id,
            p1=fresh.fire_count + 1,
            p2=new_child.id,
            p3=fresh.root_task_id,
        )
    )
    return new_child


async def arm_recurring_subtask(template: AIAgentTask, trigger_spec: str) -> Tuple[bool, str]:
    """把一个周期子任务模板挂到 APScheduler 并写库 armed 状态。

    - 解析 trigger_spec 失败 → 把模板直接 disarmed（避免阻塞下游），返回错误描述；
    - APScheduler add_job 失败 → 同样 disarmed；
    - 成功 → 模板 status 保持 pending（"不直接执行"语义），``recurring_status='armed'``。

    Returns:
        (success, message)。message 在失败时含详细原因。
    """
    from .recurring import schedule_subtask_template

    end_date_iso = template.recurring_until.isoformat() if template.recurring_until else None
    ok = schedule_subtask_template(
        template.id,
        template.root_task_id,
        trigger_spec,
        end_date=end_date_iso,
    )
    if not ok:
        await AIAgentTask.update_data_by_data(
            select_data={"id": template.id},
            update_data={"recurring_status": "disarmed"},
        )
        await AIAgentTaskLog.add_log(
            template.id,
            "step_failed",
            f"周期子任务 arm 失败：trigger_spec={trigger_spec!r}，已转 disarmed",
        )
        return False, f"周期子任务 arm 失败：trigger_spec={trigger_spec!r}"
    await AIAgentTask.update_data_by_data(
        select_data={"id": template.id},
        update_data={"recurring_status": "armed"},
    )
    await AIAgentTaskLog.add_log(
        template.id,
        "decision",
        f"周期子任务 armed: trigger={trigger_spec}",
    )
    logger.info(
        t("📋 [Kanban] 周期子任务 armed subtask={p0} trigger={trigger_spec}", p0=template.id, trigger_spec=trigger_spec)
    )
    return True, "armed"


async def disarm_subtask_template(subtask_id: str) -> bool:
    """主人手动停止 / recurring_until 到期：把周期子任务模板 disarm，摘除 APScheduler。

    模板自身保留为 ``status="pending"``（避免 completed 把 fire_count 之类历史抹掉），
    ``recurring_status="disarmed"`` 后下次 ``has_active_recurring_subtask`` 返回 False，
    根任务汇总状态才能正常推进到 completed。
    """
    task = await AIAgentTask.get_by_id(subtask_id)
    if task is None or not task.recurring_trigger:
        return False
    await AIAgentTask.update_data_by_data(
        select_data={"id": subtask_id},
        update_data={"recurring_status": "disarmed"},
    )
    await AIAgentTaskLog.add_log(subtask_id, "decision", "周期子任务已 disarm")
    try:
        from .recurring import unschedule_subtask_template

        unschedule_subtask_template(subtask_id)
    except Exception:
        pass
    return True


async def disarm_template(template_root_id: str) -> bool:
    """主人手动停止一个周期模板（不删除模板，仅 disarm 不再触发）。"""
    task = await AIAgentTask.get_by_id(template_root_id)
    if task is None or not task.recurring_trigger:
        return False
    await AIAgentTask.update_data_by_data(
        select_data={"id": template_root_id},
        update_data={"recurring_status": "disarmed"},
    )
    await AIAgentTaskLog.add_log(template_root_id, "decision", "周期模板已 disarm")
    return True


# 查询：任务树


async def _query_children(root_task_id: str) -> List[AIAgentTask]:
    """取一棵任务树下的全部子任务节点。

    模块级函数不能直接用 ``@with_session``——该装饰器对调用方签名硬编码了
    ``self`` 占位（见 ``utils/database/base_models.py::with_session``），
    只支持 classmethod / 实例方法。这里直接用 ``async_maker()`` 管 session。
    """
    async with async_maker() as session:
        stmt = (
            select(AIAgentTask)
            .where(col(AIAgentTask.root_task_id) == root_task_id)
            .where(col(AIAgentTask.node_kind) == "subtask")
            .order_by(col(AIAgentTask.created_at).asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_task_tree(
    root_task_id: str,
) -> Tuple[Optional[AIAgentTask], List[AIAgentTask]]:
    """读取整棵任务树：根任务 + 全部子任务。"""
    root = await AIAgentTask.get_by_id(root_task_id)
    if root is None or root.node_kind != "root":
        return None, []
    children = await _query_children(root_task_id)
    return root, children


def is_recurring_subtask_template(task: AIAgentTask) -> bool:
    """判定一个子任务是否为"周期子任务模板"（非克隆出来的执行实例）。

    判据：``node_kind=="subtask"`` + ``recurring_trigger`` 非空 + ``template_subtask_id``
    为空（执行实例的 template_subtask_id 反向指向模板自身）。
    """
    return task.node_kind == "subtask" and bool(task.recurring_trigger) and not task.template_subtask_id


def has_active_recurring_subtask(children: List[AIAgentTask]) -> bool:
    """任务树内是否还有 ``armed`` 周期子任务模板——根任务汇总状态用。

    armed 模板表示"持续工作中，未来还要 fire"，等同于 running 的延伸；
    所有 armed 子任务全部 disarmed/cancelled 之前根任务不允许 completed。
    """
    return any(c.recurring_status == "armed" for c in children if is_recurring_subtask_template(c))


def deps_satisfied_for(task: AIAgentTask, children: List[AIAgentTask]) -> bool:
    """判断某子任务的依赖是否已全部满足（completed/skipped）。

    依赖被周期模板满足的判定：模板**armed** 之后即视为"持续生产中"，等同于
    completed——下游可以并发跑。这是子任务级 recurring 的关键语义：
    主流程「init → recurring → final」中 final 必须用 not_before 错开时间，
    不能 depends_on recurring（``create_kanban_tree`` 已硬拦该错误编排）。
    """
    if not task.dependency_task_ids:
        return True
    accepted_ids = {
        c.id
        for c in children
        if c.status in ("completed", "skipped") or (is_recurring_subtask_template(c) and c.recurring_status == "armed")
    }
    return all(d in accepted_ids for d in task.dependency_task_ids)


def get_ready_child_tasks(
    children: List[AIAgentTask],
    *,
    root_status: str = "running",
    now: Optional[datetime] = None,
) -> List[AIAgentTask]:
    """从子任务列表里挑出"可进入 progress 列"的节点。

    条件：
    1. status == pending；
    2. dependency_task_ids 全部为 completed / skipped（或依赖项是已 armed 的周期模板，
       见 ``deps_satisfied_for``）；
    3. 根任务不在 paused / failed / cancelled / waiting_approval；
    4. ``not_before`` 字段为空或 ≤ ``now``——支持"等上班 / 等下班"等延后语义；
    5. 当前子任务的 task_node_lock 未被占（执行器层自行加锁防并发）。
    6. **排除周期子任务模板本身**（``recurring_trigger`` 非空且 ``template_subtask_id``
       为空——模板不直接派活，由调度器单独走 arm 路径挂 APScheduler）。
    """
    if root_status in ("paused", "failed", "cancelled", "waiting_approval"):
        return []
    now = now or datetime.now()
    ready: List[AIAgentTask] = []
    for c in children:
        if c.status != "pending":
            continue
        if is_recurring_subtask_template(c):
            # 周期子任务模板：依赖未满足时仍在 pending；依赖刚满足时也不进 ready
            # 由 ``kanban_executor._maybe_arm_recurring_subtasks`` 转 armed 挂 APScheduler。
            continue
        if not deps_satisfied_for(c, children):
            continue
        if c.not_before is not None and c.not_before > now:
            continue
        ready.append(c)
    return ready


def get_pending_recurring_templates_ready_to_arm(
    children: List[AIAgentTask],
    *,
    root_status: str = "running",
) -> List[AIAgentTask]:
    """挑出"依赖已满足、可以 arm 的周期子任务模板"。

    判据：``recurring_trigger`` 非空 + ``template_subtask_id`` 为空 + 状态仍是
    ``pending`` 且 ``recurring_status`` 为空（首次 arm） + 依赖全部 completed。
    """
    if root_status in ("paused", "failed", "cancelled", "waiting_approval"):
        return []
    out: List[AIAgentTask] = []
    for c in children:
        if c.status != "pending" or not is_recurring_subtask_template(c):
            continue
        if c.recurring_status:
            continue
        if not deps_satisfied_for(c, children):
            continue
        out.append(c)
    return out


def next_not_before(children: List[AIAgentTask]) -> Optional[datetime]:
    """返回所有 pending 子任务里最早一次未到的 ``not_before``——供调度器决定
    "再过多久要不要醒一次"。全无延后子任务时返回 None。
    """
    now = datetime.now()
    upcoming = [
        c.not_before for c in children if c.status == "pending" and c.not_before is not None and c.not_before > now
    ]
    if not upcoming:
        return None
    return min(upcoming)


def compute_kanban_column(task: AIAgentTask, deps_satisfied: bool = True) -> str:
    """Kanban 列映射。"""
    if task.status in ("completed", "skipped"):
        return "Done"
    if task.status in ("failed", "cancelled"):
        return "failed"
    if task.status in ("paused", "waiting_approval"):
        return "Blocked"
    if task.status == "running":
        return "progress"
    # pending：依赖未满足 -> target，依赖满足 -> progress（即将调度）
    if task.status == "pending":
        return "progress" if deps_satisfied else "target"
    return "target"


# 状态机：mark_subtask_running / completed / failed


async def mark_subtask_running(task: AIAgentTask) -> bool:
    """把子任务从 pending 转 running；用条件 SQL 防止并发派活。

    Returns:
        True  -> 转移成功（当前执行体已"赢得"该子任务）；
        False -> 已被其它派活占用，本次跳过。
    """
    now = datetime.now()
    async with async_maker() as session:
        stmt = (
            update(AIAgentTask)
            .where(col(AIAgentTask.id) == task.id)
            .where(col(AIAgentTask.status) == "pending")
            .values(status="running", last_heartbeat_at=now, updated_at=now)
        )
        result = await session.execute(stmt)
        await session.commit()
        return (result.rowcount if isinstance(result, CursorResult) else 0) > 0


async def mark_subtask_completed(task: AIAgentTask, output_artifact_id: str = "") -> None:
    now = datetime.now()
    update_data = {"status": "completed", "updated_at": now, "last_heartbeat_at": now}
    if output_artifact_id:
        update_data["output_artifact_id"] = output_artifact_id
    await AIAgentTask.update_data_by_data(select_data={"id": task.id}, update_data=update_data)
    await AIAgentTaskLog.add_log(task.id, "step_done", f"子任务完成 | output_artifact={output_artifact_id or '-'}")
    _drop_not_before_job(task.id)


async def mark_subtask_failed(task: AIAgentTask, reason: str) -> None:
    now = datetime.now()
    await AIAgentTask.update_data_by_data(
        select_data={"id": task.id},
        update_data={
            "status": "failed",
            "failure_reason": reason[:4000],
            "updated_at": now,
        },
    )
    await AIAgentTaskLog.add_log(task.id, "step_failed", f"子任务失败：{reason[:400]}")
    _drop_not_before_job(task.id)


def _drop_not_before_job(subtask_id: str) -> None:
    """子任务进入终态 / 被重派时，把 not_before APScheduler job 摘掉——避免日后
    无效唤醒重复 kick。导入晚绑定避免与 recurring 模块循环依赖。"""
    try:
        from .recurring import unschedule_not_before_wakeup

        unschedule_not_before_wakeup(subtask_id)
    except Exception:
        pass


def _drop_subtask_recurring_job(subtask_id: str) -> None:
    """周期子任务模板终结（disarm / cancel / fail_task_tree）时摘除其 APScheduler job。

    与 ``_drop_not_before_job`` 同样的延迟导入策略避免循环依赖。
    """
    try:
        from .recurring import unschedule_subtask_template

        unschedule_subtask_template(subtask_id)
    except Exception:
        pass


# 根任务状态汇总


def is_leaf_root(root: AIAgentTask, children_count: int) -> bool:
    """根任务是否为"叶子根"——自身带 agent_profile 且没有子任务，被
    ``kanban_executor`` 直接派出执行。

    判定：``node_kind == "root"`` + ``agent_profile != ""`` + ``children_count == 0``。
    """
    return root.node_kind == "root" and bool(root.agent_profile) and children_count == 0


async def refresh_root_status(root_task_id: str) -> Optional[str]:
    """根据子任务状态汇总刷新根任务 status；返回新状态。

    汇总规则：
    - 全部 completed / skipped → completed
    - 任一 waiting_approval → waiting_approval
    - 任一 paused → paused
    - 任一 pending / running → running
    - 根任务被人工 failed / cancelled 时不被覆盖
    - **叶子根**（``agent_profile`` 非空、无子任务）→ 状态由 ``_run_one_task_node``
      直接维护（pending → running → completed / failed），本函数不动它。
    """
    async with get_root_refresh_lock(root_task_id):
        root, children = await get_task_tree(root_task_id)
        if root is None:
            return None
        # 人工终结状态保留，不被汇总覆盖
        if root.status in ("failed", "cancelled"):
            return root.status
        # 叶子根的 status 由执行器直接维护，不在此处汇总
        if is_leaf_root(root, len(children)):
            return root.status

        statuses = [c.status for c in children]
        has_armed_recurring = has_active_recurring_subtask(children)
        new_status: str
        if not statuses:
            new_status = root.status
        elif has_armed_recurring and any(s in ("pending", "running", "waiting_approval", "paused") for s in statuses):
            # 周期子任务模板存在且还有正在跑/等待的兄弟节点 → 保持 running
            # 注意：周期模板自身 status 永远是 pending，不计入"全 completed"判断。
            new_status = "running"
        elif all(s in ("completed", "skipped") for s in statuses):
            if has_armed_recurring:
                # 非周期子任务全完成 + 周期模板仍 armed → 任务持续运行中
                new_status = "running"
            else:
                new_status = "completed"
        elif any(s == "waiting_approval" for s in statuses):
            new_status = "waiting_approval"
        elif any(s == "paused" for s in statuses):
            new_status = "paused"
        elif any(s in ("pending", "running") for s in statuses):
            new_status = "running"
        elif all(s == "failed" for s in statuses):
            # 全部子任务失败但主人格未明确终结时，仍保留为 running 让主人格决定
            # 是否 fail_task_tree——这条路径主要用于"全失败 + 长时间不处理"的告警源
            new_status = "running"
        else:
            new_status = root.status

        if new_status != root.status:
            now = datetime.now()
            await AIAgentTask.update_data_by_data(
                select_data={"id": root.id},
                update_data={"status": new_status, "updated_at": now},
            )
            await AIAgentTaskLog.add_log(root.id, "decision", f"根任务状态汇总：{root.status} -> {new_status}")
        return new_status


# 任务级状态操作：暂停 / 恢复 / 终止（webconsole 与主人格句柄都走这里）


async def pause_task(task_id: str) -> bool:
    """暂停一个任务（保留进度，可后续 ``resume_task`` 继续）。

    仅暂停 pending / running 状态的任务；返回是否成功暂停。
    """
    task = await AIAgentTask.get_by_id(task_id)
    if not task or task.status not in ("pending", "running"):
        return False
    now = datetime.now()
    await AIAgentTask.update_data_by_data(
        select_data={"id": task_id},
        update_data={"status": "paused", "updated_at": now},
    )
    await AIAgentTaskLog.add_log(task_id, "decision", "任务已被主人暂停")
    logger.info(t("📋 [Kanban] 任务 {task_id} 已暂停", task_id=task_id))
    return True


async def resume_task(task_id: str) -> bool:
    """恢复一个先前暂停的任务（非 waiting_approval 才能恢复）。"""
    task = await AIAgentTask.get_by_id(task_id)
    if not task or task.status != "paused":
        return False
    now = datetime.now()
    await AIAgentTask.update_data_by_data(
        select_data={"id": task_id},
        update_data={"status": "running", "updated_at": now},
    )
    await AIAgentTaskLog.add_log(task_id, "decision", "任务已被主人恢复")
    logger.info(t("📋 [Kanban] 任务 {task_id} 已恢复", task_id=task_id))
    return True


async def abort_task(task_id: str, reason: str) -> None:
    """终止单个任务（不级联）；未完成子任务的级联终结请走 ``fail_task_tree``。"""
    now = datetime.now()
    await AIAgentTask.update_data_by_data(
        select_data={"id": task_id},
        update_data={"status": "cancelled", "updated_at": now},
    )
    await AIAgentTaskLog.add_log(task_id, "decision", f"任务终止：{reason}")
    _drop_not_before_job(task_id)
    _drop_subtask_recurring_job(task_id)
    logger.info(t("📋 [Kanban] 任务 {task_id} 已终止：{reason}", task_id=task_id, reason=reason))


# 失败处理：重派 / 审批 / 整树失败


async def respawn_child_task(
    task: AIAgentTask,
    *,
    new_description: Optional[str] = None,
    new_params: Optional[Dict[str, Any]] = None,
    new_agent_profile: Optional[str] = None,
    respawn_limit: int = DEFAULT_RESPAWN_LIMIT,
) -> Tuple[bool, str]:
    """复活一个 failed 子任务，把状态重置为 pending，自增 respawn_count。

    Returns:
        (success, message)。达上限时不重派，转 waiting_approval。
    """
    if task.status not in ("failed", "waiting_approval"):
        return False, f"子任务当前状态 {task.status} 不可重派（仅 failed / waiting_approval）"

    if task.respawn_count >= respawn_limit:
        # 走统一入口挂审批：开中心票据（幂等），否则对话侧 respond_approval 找不到该请求
        await request_subtask_approval(
            task,
            f"重派次数达上限 {respawn_limit}，需主人裁决是否继续重试。"
            f"最近失败原因：{(task.failure_reason or '（无记录）')[:300]}",
        )
        return False, f"重派次数达上限 {respawn_limit}，已转待审批"

    now = datetime.now()
    update_data: Dict[str, Any] = {
        "status": "pending",
        "respawn_count": task.respawn_count + 1,
        "failure_reason": None,
        "updated_at": now,
    }
    if new_description:
        update_data["goal"] = new_description[:2000]
        update_data["display_name"] = new_description[:64]
    if new_params is not None:
        update_data["params_override"] = new_params
    if new_agent_profile:
        update_data["agent_profile"] = new_agent_profile

    await AIAgentTask.update_data_by_data(select_data={"id": task.id}, update_data=update_data)
    await AIAgentTaskLog.add_log(
        task.id,
        "decision",
        f"子任务被主人格重派 #{task.respawn_count + 1}: {new_description or '（原描述）'}",
    )
    # 重派之后旧的 not_before（如有）已经无效，摘掉避免到点又触发
    _drop_not_before_job(task.id)
    return True, f"已重派子任务（第 {task.respawn_count + 1} 次）"


async def request_subtask_approval(task: AIAgentTask, approval_prompt: str) -> None:
    """把子任务挂为 waiting_approval，并向统一审批中心提交一条 master 级请求。

    ``waiting_approval`` 状态从此是**派生视图**（看板 Blocked 列渲染用）；
    审批账本 / 裁决入口 / 过期统一在 ``ai_core.approval``。
    """
    now = datetime.now()
    await AIAgentTask.update_data_by_data(
        select_data={"id": task.id},
        update_data={
            "status": "waiting_approval",
            "failure_reason": approval_prompt[:4000],
            "updated_at": now,
        },
    )
    await AIAgentTaskLog.add_log(task.id, "approval", f"请求审批：{approval_prompt[:400]}")
    from gsuid_core.ai_core import approval as approval_center

    # 同一子任务重复挂起（重派再达上限等）不重复开票，复用现有 pending
    existing = await approval_center.AIApprovalRequest.list_pending(category="kanban_subtask", ref_key=task.id)
    if not existing:
        await approval_center.submit(
            category="kanban_subtask",
            title=f"任务#{task.ordinal}｜{task.display_name}：{approval_prompt[:400]}",
            audience="master",
            ref_key=task.id,
            operator_user_id=task.owner_user_id,
            origin_session_id=task.session_id or "",
            payload={"task_id": task.id, "root_task_id": task.root_task_id or ""},
        )
    if task.root_task_id:
        await refresh_root_status(task.root_task_id)


async def approve_subtask(task: AIAgentTask, approved: bool, note: str = "") -> Tuple[bool, str]:
    """主人格 / 主人对一个 waiting_approval 子任务的审批结果落库。"""
    if task.status != "waiting_approval":
        return False, f"子任务状态 {task.status} 不在待审批"
    now = datetime.now()
    if approved:
        await AIAgentTask.update_data_by_data(
            select_data={"id": task.id},
            update_data={"status": "pending", "updated_at": now},
        )
        await AIAgentTaskLog.add_log(task.id, "approval", f"主人批准：{note}")
        return True, "已批准，子任务进入 pending 等待调度"
    await AIAgentTask.update_data_by_data(
        select_data={"id": task.id},
        update_data={
            "status": "failed",
            "failure_reason": f"主人拒绝：{note}"[:4000],
            "updated_at": now,
        },
    )
    await AIAgentTaskLog.add_log(task.id, "approval", f"主人拒绝：{note}")
    return True, "已拒绝，子任务标记 failed"


async def fail_task_tree(root_task_id: str, reason: str) -> bool:
    """明确终结整棵任务树：根任务 failed + 未完成子任务级联 failed。"""
    root, children = await get_task_tree(root_task_id)
    if root is None:
        return False
    now = datetime.now()
    await AIAgentTask.update_data_by_data(
        select_data={"id": root.id},
        update_data={
            "status": "failed",
            "failure_reason": reason[:4000],
            "updated_at": now,
        },
    )
    cascaded = 0
    for c in children:
        if c.status in ("completed", "skipped", "failed", "cancelled"):
            # 即使子任务已"完结"，如果是 armed 周期模板，也要摘除 APScheduler
            if is_recurring_subtask_template(c) and c.recurring_status == "armed":
                await AIAgentTask.update_data_by_data(
                    select_data={"id": c.id},
                    update_data={"recurring_status": "disarmed"},
                )
                _drop_subtask_recurring_job(c.id)
            continue
        await AIAgentTask.update_data_by_data(
            select_data={"id": c.id},
            update_data={
                "status": "failed",
                "failure_reason": f"根任务被终结：{reason}"[:4000],
                "updated_at": now,
            },
        )
        cascaded += 1
        _drop_not_before_job(c.id)
        _drop_subtask_recurring_job(c.id)
    # 兜底：把任何残留的 armed 周期子任务模板都 disarm
    for c in children:
        if is_recurring_subtask_template(c) and c.recurring_status == "armed":
            await AIAgentTask.update_data_by_data(
                select_data={"id": c.id},
                update_data={"recurring_status": "disarmed"},
            )
            _drop_subtask_recurring_job(c.id)
    await AIAgentTaskLog.add_log(root.id, "decision", f"整棵任务树被终结：{reason} | 级联失败 {cascaded} 子任务")
    logger.info(
        t("📋 [Kanban] 整树失败 root={root_task_id} cascaded={cascaded}", root_task_id=root_task_id, cascaded=cascaded)
    )
    return True


async def hard_delete_task_tree(
    task_id: str,
    *,
    delete_files: bool = True,
    include_instances: bool = False,
) -> Tuple[bool, str, Dict[str, Any]]:
    """硬删除指定任务所在的整棵 Kanban 树及关联内容。

    与 ``fail_task_tree`` 的软终结不同，本函数会直接删除数据库行：
    ``AIAgentTask`` / ``AIAgentTaskLog`` / ``AIAgentArtifact``。当
    ``delete_files=True`` 时，还会删除 ``data/ai_core/artifacts/<root_id>/``
    下的 workspace / artifact payload 文件。

    如果传入子任务 id，会解析到它所属的根任务，并删除整棵树，避免留下断裂的
    依赖边。周期模板会先从 APScheduler 摘除；``include_instances=True`` 时，
    会同时删除该模板已克隆出的历史实例树。
    """
    task = await AIAgentTask.get_by_id(task_id)
    if task is None:
        return False, "任务不存在", {}

    root_id = task.id if task.node_kind == "root" else task.root_task_id
    if not root_id:
        return False, "任务缺少 root_task_id，无法安全硬删除", {}

    root = task if task.id == root_id and task.node_kind == "root" else await AIAgentTask.get_by_id(root_id)
    if root is None:
        return False, "根任务不存在，无法安全硬删除", {}

    roots: List[AIAgentTask] = [root]
    if include_instances:
        async with async_maker() as session:
            result = await session.execute(
                select(AIAgentTask)
                .where(col(AIAgentTask.node_kind) == "root")
                .where(col(AIAgentTask.template_root_id) == root.id)
            )
            roots.extend(list(result.scalars().all()))

    root_ids = []
    seen_root_ids = set()
    for r in roots:
        if r.id not in seen_root_ids:
            root_ids.append(r.id)
            seen_root_ids.add(r.id)

    stats: Dict[str, Any] = {
        "requested_task_id": task_id,
        "root_task_id": root.id,
        "root_task_ids": root_ids,
        "tasks_deleted": 0,
        "logs_deleted": 0,
        "artifacts_deleted": 0,
        "files_deleted": 0,
        "dirs_deleted": 0,
        "unscheduled_jobs": 0,
    }

    try:
        from .recurring import unschedule_template

        for r in roots:
            if r.recurring_trigger or r.recurring_status:
                if unschedule_template(r.id):
                    stats["unscheduled_jobs"] += 1
    except Exception as e:
        logger.warning(t("📋 [Kanban] 硬删除前摘除周期 job 失败 task={task_id}: {e}", task_id=task_id, e=e))

    # 删除前把所有子任务的 not_before + 周期子任务 APScheduler job 也摘掉
    try:
        from .recurring import unschedule_subtask_template, unschedule_not_before_wakeup

        async with async_maker() as session:
            sub_result = await session.execute(
                select(AIAgentTask.id)
                .where(col(AIAgentTask.node_kind) == "subtask")
                .where(col(AIAgentTask.root_task_id).in_(root_ids))
            )
            for sub_id in sub_result.scalars().all():
                unschedule_not_before_wakeup(sub_id)
                unschedule_subtask_template(sub_id)
    except Exception as e:
        logger.warning(
            t(
                "📋 [Kanban] 硬删除前摘除 not_before/subtask-recurring job 失败 task={task_id}: {e}",
                task_id=task_id,
                e=e,
            )
        )

    all_task_ids: List[str] = []
    artifact_paths: List[str] = []
    async with async_maker() as session:
        task_result = await session.execute(
            select(AIAgentTask.id).where(
                (col(AIAgentTask.id).in_(root_ids)) | (col(AIAgentTask.root_task_id).in_(root_ids))
            )
        )
        all_task_ids = list(task_result.scalars().all())
        if not all_task_ids:
            return False, "未找到需要删除的任务节点", stats

        artifact_result = await session.execute(
            select(AIAgentArtifact).where(col(AIAgentArtifact.root_task_id).in_(root_ids))
        )
        artifacts = list(artifact_result.scalars().all())
        artifact_paths = [a.payload_path for a in artifacts if a.payload_path]
        stats["artifacts_deleted"] = len(artifacts)

        log_count_result = await session.execute(
            select(AIAgentTaskLog.id).where(col(AIAgentTaskLog.task_id).in_(all_task_ids))
        )
        log_ids = list(log_count_result.scalars().all())
        stats["logs_deleted"] = len(log_ids)
        stats["tasks_deleted"] = len(all_task_ids)

        await session.execute(delete(AIAgentArtifact).where(col(AIAgentArtifact.root_task_id).in_(root_ids)))
        await session.execute(delete(AIAgentTaskLog).where(col(AIAgentTaskLog.task_id).in_(all_task_ids)))
        await session.execute(delete(AIAgentTask).where(col(AIAgentTask.id).in_(all_task_ids)))
        await session.commit()

    if delete_files:
        try:
            from .workspace import ARTIFACT_ROOT

            artifact_root = ARTIFACT_ROOT.resolve()
            for raw_path in artifact_paths:
                try:
                    p = Path(raw_path).resolve()
                    if p.exists() and p.is_file() and str(p).startswith(str(artifact_root)):
                        p.unlink()
                        stats["files_deleted"] += 1
                except OSError:
                    pass
            for rid in root_ids:
                try:
                    root_dir = (ARTIFACT_ROOT / rid).resolve()
                    if root_dir.exists() and root_dir.is_dir() and str(root_dir).startswith(str(artifact_root)):
                        shutil.rmtree(root_dir, ignore_errors=True)
                        stats["dirs_deleted"] += 1
                except OSError:
                    pass
        except Exception as e:
            logger.warning(t("📋 [Kanban] 硬删除任务文件失败 task={task_id}: {e}", task_id=task_id, e=e))

    for tid in all_task_ids:
        _TASK_NODE_LOCKS.pop(tid, None)
    for rid in root_ids:
        _ROOT_REFRESH_LOCKS.pop(rid, None)

    logger.info(
        t("📋 [Kanban] 硬删除任务树 root=%s requested=%s tasks=%s logs=%s artifacts=%s dirs=%s"),
        root.id,
        task_id,
        stats["tasks_deleted"],
        stats["logs_deleted"],
        stats["artifacts_deleted"],
        stats["dirs_deleted"],
    )
    return True, "ok", stats


async def bulk_delete_task_trees(
    *,
    scope_key: Optional[str] = None,
    bot_id: Optional[str] = None,
    group_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    status: Optional[str] = None,
    delete_files: bool = True,
    include_instances: bool = False,
) -> Tuple[int, int, Dict[str, Any]]:
    """批量删除符合条件的 Kanban 任务树。

    先通过 ``list_root_tasks`` 筛选出目标根任务，再逐棵调用
    ``hard_delete_task_tree`` 进行硬删除。返回汇总统计。

    Returns:
        (deleted_count, failed_count, stats_dict)
    """
    roots = await list_root_tasks(
        scope_key=scope_key,
        bot_id=bot_id,
        group_id=group_id,
        owner_user_id=owner_user_id,
        status=status,
    )
    deleted = 0
    failed = 0
    total_stats: Dict[str, Any] = {
        "tasks_deleted": 0,
        "logs_deleted": 0,
        "artifacts_deleted": 0,
        "files_deleted": 0,
        "dirs_deleted": 0,
        "unscheduled_jobs": 0,
        "root_ids": [],
        "failed_root_ids": [],
    }

    for root in roots:
        ok, _msg, stats = await hard_delete_task_tree(
            root.id,
            delete_files=delete_files,
            include_instances=include_instances,
        )
        if ok:
            deleted += 1
            total_stats["tasks_deleted"] += stats.get("tasks_deleted", 0)
            total_stats["logs_deleted"] += stats.get("logs_deleted", 0)
            total_stats["artifacts_deleted"] += stats.get("artifacts_deleted", 0)
            total_stats["files_deleted"] += stats.get("files_deleted", 0)
            total_stats["dirs_deleted"] += stats.get("dirs_deleted", 0)
            total_stats["unscheduled_jobs"] += stats.get("unscheduled_jobs", 0)
            total_stats["root_ids"].append(root.id)
        else:
            failed += 1
            total_stats["failed_root_ids"].append(root.id)

    logger.info(
        t("📋 [Kanban] 批量删除完成 matched=%s deleted=%s failed=%s tasks=%s logs=%s artifacts=%s dirs=%s"),
        len(roots),
        deleted,
        failed,
        total_stats["tasks_deleted"],
        total_stats["logs_deleted"],
        total_stats["artifacts_deleted"],
        total_stats["dirs_deleted"],
    )
    return deleted, failed, total_stats


# 崩溃恢复


async def recover_zombie_subtasks(stale_minutes: int = 15) -> int:
    """启动时把因进程崩溃滞留在 ``running`` 的子任务**和叶子根**复活为 ``pending``。

    依据 ``last_heartbeat_at`` 判定僵尸——超过 ``stale_minutes`` 视为无心跳。
    重置 + 写日志后由 startup 末尾对 ``running`` / ``pending`` 根任务统一发一次
    ``kick_root``，重新走调度。叶子根（``node_kind=="root"`` + ``agent_profile``
    非空）和普通子任务一样可能崩溃在 running 状态，需要同样的复活路径。

    返回处理的任务数量（子任务 + 叶子根合计）。

    注意：周期子任务模板 (recurring_status='armed') 自身永远停留在 pending，不会
    进入 running——因此它们不会被本函数误判为僵尸；执行实例子任务（fire 出来的）
    跟普通子任务一样跟着复活。
    """
    cutoff = datetime.now() - timedelta(minutes=stale_minutes)
    async with async_maker() as session:
        stmt = (
            select(AIAgentTask)
            .where(col(AIAgentTask.status) == "running")
            .where((col(AIAgentTask.last_heartbeat_at).is_(None)) | (col(AIAgentTask.last_heartbeat_at) < cutoff))
            .where(
                (col(AIAgentTask.node_kind) == "subtask")
                | ((col(AIAgentTask.node_kind) == "root") & (col(AIAgentTask.agent_profile) != ""))
            )
        )
        result = await session.execute(stmt)
        zombies = list(result.scalars().all())
        if not zombies:
            return 0
        now = datetime.now()
        for z in zombies:
            await session.execute(
                update(AIAgentTask)
                .where(col(AIAgentTask.id) == z.id)
                .where(col(AIAgentTask.status) == "running")
                .values(status="pending", updated_at=now)
            )
        await session.commit()
    # 日志走 AIAgentTaskLog.add_log（自带 @with_session classmethod），出 session 外写
    for z in zombies:
        kind_label = "叶子根" if z.node_kind == "root" else "子任务"
        await AIAgentTaskLog.add_log(
            z.id,
            "decision",
            f"崩溃恢复：running {kind_label}已重置为 pending（心跳超时）",
        )
    logger.info(t("📋 [Kanban] 崩溃恢复：复活 {p0} 个僵尸任务", p0=len(zombies)))
    return len(zombies)


# 查询辅助：列出全部根任务（看板视图）


async def list_root_tasks(
    *,
    scope_key: Optional[str] = None,
    bot_id: Optional[str] = None,
    group_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    status: Optional[str] = None,
) -> List[AIAgentTask]:
    stmt = select(AIAgentTask).where(col(AIAgentTask.node_kind) == "root")
    if scope_key:
        stmt = stmt.where(col(AIAgentTask.scope_key) == scope_key)
    if bot_id:
        stmt = stmt.where(col(AIAgentTask.bot_id) == bot_id)
    if group_id:
        stmt = stmt.where(col(AIAgentTask.group_id) == group_id)
    if owner_user_id:
        stmt = stmt.where(col(AIAgentTask.owner_user_id) == owner_user_id)
    if status:
        stmt = stmt.where(col(AIAgentTask.status) == status)
    stmt = stmt.order_by(col(AIAgentTask.updated_at).desc())
    async with async_maker() as session:
        result = await session.execute(stmt)
        return list(result.scalars().all())
