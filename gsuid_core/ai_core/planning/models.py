"""Agent Mesh Kanban · 数据模型

两张表共同承载一棵"可持久化、可并发推进"的多代理任务树：

- ``AIAgentTask``     : 任务节点表——根任务 + N 子任务节点，靠 ``node_kind``
  区分；状态 / 归属 / 依赖关系 / 失败原因 / 工作区策略全部在此。
- ``AIAgentTaskLog``  : 任务事件日志表——崩溃恢复 / 审批轨迹 / 越界告警的
  幂等依据；前端审计窗口也走这里。
- ``AIAgentArtifact`` : 任务节点产出登记表——上游 ``artifact_put`` 写 inline /
  落盘，下游被调度时由 executor 注入提示词。

UUID 主键，不复用 BaseIDModel 的自增 int 主键，因此通过下方 ``_PlanCRUD``
混入提供通用增改方法。
"""

import uuid
from typing import List, Optional
from datetime import datetime

from sqlmodel import Field, SQLModel, col, select, update
from sqlalchemy import Text, Column
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import JSON

from gsuid_core.utils.database.base_models import with_session

# 任务状态：``waiting_approval`` 用于把"审批挂起"与"主人手动暂停"区分开，
# 避免同一个 paused 同时表示两种语义
TASK_STATUSES = (
    "pending",
    "running",
    "paused",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
)
# 任务节点类型：根任务 / 子任务
NODE_KINDS = ("root", "subtask")
# 失败处理策略：默认通知主人格定夺，不自动级联整树失败
FAILURE_POLICIES = ("notify_persona", "auto_abort")
# 工作区策略：默认仅允许操作 Artifact Workspace 内文件
WORKSPACE_POLICIES = ("artifact_only", "unrestricted")
# 看板列与任务状态映射
KANBAN_COLUMNS = ("target", "progress", "Done", "Blocked", "failed")


def _uuid() -> str:
    return str(uuid.uuid4())


class _PlanCRUD:
    """Kanban 表共用的通用增改方法（UUID 主键，故不复用 BaseIDModel）。"""

    @classmethod
    @with_session
    async def update_data_by_data(
        cls,
        session: AsyncSession,
        select_data: dict,
        update_data: dict,
    ) -> int:
        """按 select_data 条件更新 update_data 字段。"""
        stmt = update(cls)  # type: ignore[arg-type]
        for k, v in select_data.items():
            stmt = stmt.where(col(getattr(cls, k)) == v)
        await session.execute(stmt.values(**update_data))
        return 0

    @classmethod
    @with_session
    async def batch_insert_data(cls, session: AsyncSession, rows: list) -> None:
        """批量插入若干 ORM 行。"""
        if rows:
            session.add_all(rows)


class AIAgentTask(_PlanCRUD, SQLModel, table=True):
    """Kanban 任务节点表。表名自动推导为 ``aiagenttask``。

    根任务（``node_kind="root"``）与子任务（``node_kind="subtask"``）共表，
    通过 ``parent_task_id`` / ``root_task_id`` 建立树形结构；子任务靠
    ``dependency_task_ids`` 建立兄弟依赖边。
    """

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=36)
    # 用户可见的短序号（按 owner 递增，非主键，可变）——给 LLM / 用户的句柄
    ordinal: int = Field(default=0, index=True)
    goal: str = Field(sa_column=Column(Text, nullable=False))
    display_name: str = Field(default="", max_length=128)
    task_alias: Optional[str] = Field(default=None, max_length=128)
    status: str = Field(default="pending", index=True, max_length=16)

    # 归属与作用域
    scope_key: str = Field(default="", index=True, max_length=128)
    owner_user_id: str = Field(default="", index=True, max_length=64)
    persona_name: Optional[str] = Field(default=None, max_length=64)
    # 由哪类能力代理推进本节点（capability_agents 的 profile_id）。
    # 根任务不分配画像（只负责状态聚合）；子任务必须分配。
    agent_profile: str = Field(default="", max_length=64)

    # 重建 Event 所需的上下文
    bot_id: str = Field(default="", max_length=64)
    bot_self_id: str = Field(default="", max_length=64)
    group_id: Optional[str] = Field(default=None, max_length=64)
    user_type: str = Field(default="direct", max_length=16)
    WS_BOT_ID: Optional[str] = Field(default=None, max_length=64)
    session_id: str = Field(default="", max_length=256)
    # 派活时的用户权限等级（与 Event.user_pm 对齐，越小权限越高，0=主人）。
    # 必须随任务持久化：Kanban 执行体由 _build_event 重建 Event 后，pm 门控工具
    # （check_pm，如 plugin_dev 全家）要靠它判定主人身份；不存就退回默认 6（非管理员），
    # 导致主人派出的子代理被自家工具拒绝（实测：pm=0 主人无法让代理写插件）。
    user_pm: int = Field(default=6)

    # 授权播报白名单（群号 / 用户号列表）；空表示仅回送 owner
    broadcast_targets: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # 复盘累积区（任务树终结后由调用方追加）
    review_notes: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))

    # 根任务的兜底心跳间隔（秒）；默认 0 = 不挂任何定时器。
    # 当前 Kanban 调度入口完全由 ``kick_root`` 驱动；该字段仅保留给 webconsole
    # 展示与未来潜在的定时心跳扩展使用。
    interval_seconds: int = Field(default=0)

    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    # 心跳时间，崩溃恢复判定僵尸任务的依据（``recover_zombie_subtasks``）
    last_heartbeat_at: Optional[datetime] = Field(default=None)

    # ── Kanban 任务树字段 ────────────────────────────
    # 根任务：parent_task_id=None / root_task_id=自身 id / node_kind="root"
    # 子任务：parent_task_id=根 id / root_task_id=根 id / node_kind="subtask"

    parent_task_id: Optional[str] = Field(default=None, index=True, max_length=36)
    root_task_id: str = Field(default="", index=True, max_length=36)
    node_kind: str = Field(default="root", index=True, max_length=16)
    # 当前子任务依赖的兄弟子任务 id 列表；为空表示无前置依赖
    dependency_task_ids: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # 最近一次失败原因（自然语言），给主人格做 respawn / fail / 审批决策的依据
    failure_reason: Optional[str] = Field(default=None, sa_column=Column(Text))
    # 已被主人格重派次数；达到上限（默认 3）后强制走 waiting_approval
    respawn_count: int = Field(default=0)
    # 主人格 / 审批后修正的参数；调度时拼进任务文本
    params_override: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    # 上游产出的 artifact id 列表；调度时由 executor 注入到任务提示词
    input_artifact_ids: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # 本节点最终输出的主 artifact id；为空表示尚未交付
    output_artifact_id: Optional[str] = Field(default=None, max_length=36)
    # 失败策略：notify_persona（默认） 或 auto_abort
    failure_policy: str = Field(default="notify_persona", max_length=32)
    # 工作区策略：artifact_only（默认） 或 unrestricted
    workspace_policy: str = Field(default="artifact_only", max_length=32)
    # 子任务级别"最早可执行时间"——绝对时间。``get_ready_child_tasks`` 会把
    # ``not_before > now`` 的子任务过滤掉，调度时不派活；到点后下一次 ``kick_root``
    # 会自然把它们拉起。诞生原因：实测会话 b8cf57ca 主人要"虚拟盘开盘时段每整点
    # 看盘"，单子任务被立刻派出（凌晨 4 点跑了一次），完全无视市场时间。新增字段
    # 让主人格在 ``register_kanban_task`` 时直接传 ``not_before`` 实现"等开盘""等
    # 主人下班"等延后语义，与周期模板（``recurring_trigger`` 多次开火）正交。
    not_before: Optional[datetime] = Field(default=None)

    # ── 周期触发字段 ─────────────────────────────────────────
    # recurring_trigger 形如：
    #   "interval:<seconds>"          —— 每隔 N 秒触发一次
    #   "cron:<minute> <hour> <dom> <mon> <dow>"  —— 标准 5 段 cron
    # 为空表示一次性任务（默认行为）。
    #
    # 既支持**根任务级**周期（整棵树克隆——保留用于"任意时刻独立跑一棵新树"的
    # 老用法），也支持**子任务级**周期（一棵树内某个子任务到点克隆出一个执行实例
    # 子任务）。新设计推崇子任务级周期：让"持久化状态 + 周期更新 + 最终汇总"
    # 形态的任务用**一棵树**表达（init / recurring / final 子任务），告别旧版
    # 必须拆"三棵树"的笨拙折中。
    recurring_trigger: Optional[str] = Field(default=None, max_length=128)
    # 周期任务的失效时间；为空表示永远生效，直到主人手动 disarm。
    recurring_until: Optional[datetime] = Field(default=None)
    # template_root_id：指向"模板根任务"。模板自身值=None；克隆出的实例值=模板根 id。
    # 模板根的 status 永远停留在 pending，从不真正执行——它只是"开火时被克隆"的样板。
    # 仅根任务级周期使用；子任务级周期走 template_subtask_id。
    template_root_id: Optional[str] = Field(default=None, max_length=36, index=True)
    # template_subtask_id：与 template_root_id 对称——指向"模板子任务"。
    # 模板子任务 (recurring_trigger 非空 + node_kind="subtask") 自身值=None；
    # 克隆出的执行实例子任务值=模板子任务 id。每次开火，框架在同一棵任务树下
    # 新建一个执行实例子任务行（dependency_task_ids 留空，立即 ready），
    # 模板子任务自身保持 armed 不变，``fire_count`` 累加。
    template_subtask_id: Optional[str] = Field(default=None, max_length=36, index=True)
    # 周期状态：armed=已挂上 APScheduler；disarmed=主人手动停止/recurring_until 到期；
    # 空=非周期任务。模板子任务长期处于 armed，执行实例子任务此字段保持空。
    recurring_status: str = Field(default="", max_length=16)
    # 模板被开火的次数（每次克隆 +1，用于做"最多触发 N 次"上限）
    fire_count: int = Field(default=0)

    @classmethod
    @with_session
    async def create_task(
        cls,
        session: AsyncSession,
        goal: str,
        owner_user_id: str,
        scope_key: str,
        bot_id: str,
        **kwargs,
    ) -> "AIAgentTask":
        """创建任务节点，并自动分配 owner 维度的短序号 ``ordinal``。"""
        result = await session.execute(
            select(AIAgentTask.ordinal)
            .where(col(AIAgentTask.owner_user_id) == owner_user_id)
            .order_by(col(AIAgentTask.ordinal).desc())
            .limit(1)
        )
        last_ordinal = result.scalar_one_or_none() or 0
        task = cls(
            goal=goal,
            owner_user_id=owner_user_id,
            scope_key=scope_key,
            bot_id=bot_id,
            ordinal=last_ordinal + 1,
            display_name=kwargs.pop("display_name", "") or goal[:32],
            **kwargs,
        )
        session.add(task)
        return task

    @classmethod
    @with_session
    async def get_by_id(cls, session: AsyncSession, task_id: str) -> Optional["AIAgentTask"]:
        result = await session.execute(select(cls).where(col(cls.id) == task_id))
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def list_for_owner(
        cls,
        session: AsyncSession,
        owner_user_id: str,
        only_active: bool = False,
        root_only: bool = True,
    ) -> List["AIAgentTask"]:
        """按 owner 倒序列出任务。

        Args:
            only_active: 仅含未结束状态（pending / running / paused / waiting_approval）。
            root_only: 仅含根任务（默认）——避免 Kanban 子任务被当成顶层任务展示。
        """
        stmt = select(cls).where(col(cls.owner_user_id) == owner_user_id)
        if root_only:
            stmt = stmt.where(col(cls.node_kind) == "root")
        if only_active:
            stmt = stmt.where(col(cls.status).in_(("pending", "running", "paused", "waiting_approval")))
        stmt = stmt.order_by(col(cls.updated_at).desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def list_by_status(cls, session: AsyncSession, *statuses: str) -> List["AIAgentTask"]:
        result = await session.execute(select(cls).where(col(cls.status).in_(statuses)))
        return list(result.scalars().all())


class AIAgentTaskLog(_PlanCRUD, SQLModel, table=True):
    """任务事件日志表。表名自动推导为 ``aiagenttasklog``。

    event_type 取值：plan_created / step_started / step_done / step_failed /
    decision / broadcast / review / approval / workspace_violation。
    """

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=36)
    task_id: str = Field(index=True, max_length=36)
    step_id: Optional[str] = Field(default=None, max_length=36)
    timestamp: datetime = Field(default_factory=datetime.now)
    event_type: str = Field(default="decision", max_length=24)
    content: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))

    @classmethod
    @with_session
    async def add_log(
        cls,
        session: AsyncSession,
        task_id: str,
        event_type: str,
        content: str,
        step_id: Optional[str] = None,
    ) -> None:
        session.add(cls(task_id=task_id, event_type=event_type, content=content[:4000], step_id=step_id))

    @classmethod
    @with_session
    async def get_for_task(
        cls,
        session: AsyncSession,
        task_id: str,
        limit: int = 100,
    ) -> List["AIAgentTaskLog"]:
        result = await session.execute(
            select(cls).where(col(cls.task_id) == task_id).order_by(col(cls.timestamp).asc()).limit(limit)
        )
        return list(result.scalars().all())


# Artifact 类型；workspace_file 由命令执行后扫描 workspace 变更登记
ARTIFACT_KINDS = ("output", "workspace_file", "log", "report", "patch")


class AIAgentArtifact(_PlanCRUD, SQLModel, table=True):
    """Kanban 任务节点产出登记表（表名 aiagentartifact）。

    任务节点之间通过 Artifact Hub 传递结构化产出：上游 ``artifact_put`` 留下
    ``res_id``，下游被调度时由 executor 收集 ``dependency_task_ids`` 对应的
    artifact 注入到提示词。

    访问边界：默认禁止跨 ``root_task_id`` 读取——保证同一任务树的产出不会泄漏
    给其他用户 / 其他任务树。
    """

    id: str = Field(
        default_factory=lambda: f"res_{uuid.uuid4().hex[:12]}",
        primary_key=True,
        max_length=64,
    )
    root_task_id: str = Field(index=True, max_length=36)
    task_id: str = Field(index=True, max_length=36)  # 产生它的任务节点
    parent_task_id: Optional[str] = Field(default=None, index=True, max_length=36)
    from_profile: str = Field(default="", max_length=64)
    artifact_kind: str = Field(default="output", max_length=32)
    mime: str = Field(default="application/json", max_length=64)
    summary: str = Field(default="", max_length=512)
    # 小工件（≤4KB 文本）直接 inline；超过的走 payload_path 落盘
    payload_inline: Optional[str] = Field(default=None, sa_column=Column(Text))
    payload_path: str = Field(default="", max_length=512)
    size_bytes: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = Field(default=None, index=True)

    @classmethod
    @with_session
    async def get_by_id(cls, session: AsyncSession, res_id: str) -> Optional["AIAgentArtifact"]:
        result = await session.execute(select(cls).where(col(cls.id) == res_id))
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def list_for_root(cls, session: AsyncSession, root_task_id: str) -> List["AIAgentArtifact"]:
        result = await session.execute(
            select(cls).where(col(cls.root_task_id) == root_task_id).order_by(col(cls.created_at).asc())
        )
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def list_for_task(cls, session: AsyncSession, task_id: str) -> List["AIAgentArtifact"]:
        result = await session.execute(
            select(cls).where(col(cls.task_id) == task_id).order_by(col(cls.created_at).asc())
        )
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def list_recent_for_root(
        cls,
        session: AsyncSession,
        root_task_id: str,
        limit: int = 1,
    ) -> List["AIAgentArtifact"]:
        """按 created_at 倒序拉最近 N 条 artifact——给主人格"追问溯源"用。"""
        result = await session.execute(
            select(cls).where(col(cls.root_task_id) == root_task_id).order_by(col(cls.created_at).desc()).limit(limit)
        )
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def delete_expired(
        cls,
        session: AsyncSession,
        now: Optional[datetime] = None,
    ) -> int:
        """删除所有已过期（``expires_at < now``）的 artifact 行 + 它们的落盘文件。

        由 ``planning/startup.init_planning`` 注册的每日 APScheduler job 触发，
        默认 TTL 30 天（``workspace.DEFAULT_TTL_DAYS``）。``expires_at IS NULL``
        视为"永久保留"，不删。

        Returns:
            实际删除的 artifact 行数（不含静默忽略的落盘清理错误）。
        """
        from pathlib import Path

        cut = now or datetime.now()
        stmt = select(cls).where(col(cls.expires_at).is_not(None)).where(col(cls.expires_at) < cut)
        result = await session.execute(stmt)
        expired = list(result.scalars().all())
        if not expired:
            return 0

        # 先清理落盘 payload；删失败时记日志但不阻塞行删除——arifact 在 webconsole
        # 上看到行被删了但磁盘还留着，比"DB 删一半"风险更可控。
        # 新存储模型下 payload_path 都指向 workspace 内文件，多个 artifact 行可能
        # 共享同一文件（例如同一 PNG 同时被自动登记为 workspace_file 和被
        # `artifact_put(file_path=)` 登记为 output）——按路径去重，避免 missing_ok
        # 的二次 unlink 把日志刷脏。
        from gsuid_core.logger import logger as _logger

        seen_paths: set = set()
        for art in expired:
            if not art.payload_path:
                continue
            if art.payload_path in seen_paths:
                continue
            seen_paths.add(art.payload_path)
            payload_file = Path(art.payload_path)
            if payload_file.exists():
                payload_file.unlink(missing_ok=True)
            # 仅清空目录壳（workspace 内还有其它代理产物 / 未过期 artifact 时不删）
            parent = payload_file.parent
            try:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

        # 批量 DELETE
        from sqlmodel import delete as sql_delete

        del_stmt = sql_delete(cls).where(col(cls.expires_at).is_not(None)).where(col(cls.expires_at) < cut)
        await session.execute(del_stmt)
        _logger.info(f"📦 [Kanban] Artifact TTL 清理: 删除 {len(expired)} 条过期记录")
        return len(expired)
