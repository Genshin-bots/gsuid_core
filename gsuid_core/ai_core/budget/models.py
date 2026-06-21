"""AI 预算限制数据模型。

三张表（均 `BaseIDModel`，受 AI 总开关控制创建）：
- `AIBudgetRule`：预算规则（按 global/group/member/user 维度设 5h/天/周上限）。
- `AIBudgetWhitelist`：白名单（命中即整体豁免，永不拦截）。
- `AIBudgetUsageRecord`：用量流水的持久化后备（真值源是 `manager` 的内存账本，本表只
  供定时落库与重启回载；闸门 / 看板一律读内存，不查本表）。

维度与 Session 语义对齐（见 `gsuid_core/models.py::Event.session_id`）：群聊全群共享一个
Session，故 `group` 维度=全群共享额度，`member` 维度=群内某人，`user` 维度=私聊某人。
"""

from typing import Any, Dict, List, Optional

from sqlmodel import Field, col, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import BaseIDModel, with_session

# 维度合法值
SCOPE_TYPES = ("global", "group", "member", "user")
# 窗口合法值
WINDOW_KEYS = ("short", "day", "week")


class AIBudgetRule(BaseIDModel, table=True):
    """一条预算规则。多条匹配规则可同时生效，任一窗口超限即拦截。"""

    __table_args__ = {"extend_existing": True}

    name: str = Field(default="", title="规则名称")
    # global=兜底全局总额; group=该群全员共享; member=群内某人; user=该用户私聊
    scope_type: str = Field(default="global", title="作用维度")
    # group/member 填群号, user 填用户号, global 留空
    scope_id: str = Field(default="", title="作用对象ID")
    # 仅 member 维度: 群内用户号
    member_id: str = Field(default="", title="群内成员ID")
    # ""=不限平台, 否则仅该平台(event.bot_id)
    bot_id: str = Field(default="", title="平台")
    enabled: bool = Field(default=True, title="是否启用")
    priority: int = Field(default=0, title="优先级")
    # rolling=滚动窗口(最近N); fixed=固定窗口(对齐零点/周一/epoch块)
    period_mode: str = Field(default="rolling", title="窗口模式")
    short_window_hours: int = Field(default=5, title="短窗口小时数")
    limit_short: int = Field(default=0, title="短窗口Token上限")
    limit_day: int = Field(default=0, title="天Token上限")
    limit_week: int = Field(default=0, title="周Token上限")
    note: str = Field(default="", title="备注")
    created_at: int = Field(default=0, title="创建时间戳")
    updated_at: int = Field(default=0, title="更新时间戳")

    @classmethod
    @with_session
    async def create(cls, session: AsyncSession, **data: Any) -> int:
        """插入一条规则并返回新行 id。"""
        obj = cls(**data)
        session.add(obj)
        await session.flush()
        return int(obj.id)

    @classmethod
    @with_session
    async def get_all_rules(cls, session: AsyncSession) -> List["AIBudgetRule"]:
        """获取全部规则（按优先级倒序、id 正序，便于稳定展示）。"""
        stmt = select(cls).order_by(col(cls.priority).desc(), col(cls.id).asc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def get_rule(cls, session: AsyncSession, rule_id: int) -> Optional["AIBudgetRule"]:
        """按 id 获取单条规则。"""
        stmt = select(cls).where(col(cls.id) == rule_id)
        return (await session.execute(stmt)).scalars().first()

    def limit_for(self, window: str) -> int:
        """返回指定窗口的 Token 上限（0=该档不限）。"""
        if window == "short":
            return self.limit_short
        if window == "day":
            return self.limit_day
        if window == "week":
            return self.limit_week
        return 0


class AIBudgetWhitelist(BaseIDModel, table=True):
    """白名单条目。命中即整体豁免（永不被预算拦截）。"""

    __table_args__ = {"extend_existing": True}

    user_id: str = Field(default="", title="用户ID")
    # ""=全局(含私聊)豁免; 否则仅在该群内豁免
    group_id: str = Field(default="", title="群号")
    bot_id: str = Field(default="", title="平台")
    enabled: bool = Field(default=True, title="是否启用")
    note: str = Field(default="", title="备注")
    created_at: int = Field(default=0, title="创建时间戳")

    @classmethod
    @with_session
    async def create(cls, session: AsyncSession, **data: Any) -> int:
        """插入一条白名单并返回新行 id。"""
        obj = cls(**data)
        session.add(obj)
        await session.flush()
        return int(obj.id)

    @classmethod
    @with_session
    async def get_all_entries(cls, session: AsyncSession) -> List["AIBudgetWhitelist"]:
        """获取全部白名单条目。"""
        stmt = select(cls).order_by(col(cls.id).asc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def get_entry(cls, session: AsyncSession, entry_id: int) -> Optional["AIBudgetWhitelist"]:
        """按 id 获取单条白名单。"""
        stmt = select(cls).where(col(cls.id) == entry_id)
        return (await session.execute(stmt)).scalars().first()


class AIBudgetUsageRecord(BaseIDModel, table=True):
    """用量流水的持久化后备表（内存账本定时整批落库的承载，重启时回载入内存）。"""

    __table_args__ = {"extend_existing": True}

    bot_id: str = Field(default="", index=True, title="平台")
    group_id: str = Field(default="", index=True, title="群号")
    user_id: str = Field(default="", index=True, title="用户ID")
    session_id: str = Field(default="", title="会话ID")
    input_tokens: int = Field(default=0, title="输入Token")
    output_tokens: int = Field(default=0, title="输出Token")
    cache_read_tokens: int = Field(default=0, title="缓存读取Token")
    cache_write_tokens: int = Field(default=0, title="缓存写入Token")
    # 按全局 count_mode 预计算的计费量, 求和直接用它(避免按规则重算无法走索引聚合)
    total_tokens: int = Field(default=0, title="计费Token")
    # 写入时该用户是否豁免(主人/白名单), 供 count_exempt_usage 决定是否计入额度
    exempt: bool = Field(default=False, title="是否豁免用量")
    created_at: int = Field(default=0, index=True, title="创建时间戳")

    @classmethod
    @with_session
    async def bulk_add(cls, session: AsyncSession, rows: List[Dict[str, Any]]) -> bool:
        """批量写入若干用量流水（一个事务一次性 add_all）。成功返回 True。

        内存是用量真值源（见 `manager.BudgetManager._usage`），本表仅作持久化后备：由
        `budget_manager.flush` 定时把内存增量整批落库，替代过去「每次 run 一笔单行写库」——
        后者在记忆摄入写风暴下反复抢不到 SQLite 写锁、重试耗尽后被 with_session 静默丢弃，
        导致用量严重漏记。整批写库把写频次降一两个数量级，避开锁竞争。

        返回 True 让调用方据此置 persisted：`with_session` 在重试耗尽失败时返回 None（不抛），
        故调用方只在拿到 True 时才标记已落库，否则保留待下次 flush 重试，绝不漏记。
        """
        if not rows:
            return True
        session.add_all([cls(**r) for r in rows])
        return True

    @classmethod
    @with_session
    async def get_records_since(cls, session: AsyncSession, since_ts: int) -> List["AIBudgetUsageRecord"]:
        """取 since_ts 起的全部流水。仅启动时一次性载入内存，此后读写都走内存。"""
        stmt = select(cls).where(col(cls.created_at) >= since_ts).order_by(col(cls.created_at).asc())
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_session
    async def delete_scope_usage(
        cls,
        session: AsyncSession,
        since_ts: int,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        bot_id: Optional[str] = None,
    ) -> int:
        """删除某 scope 在窗口内的流水（管理员手动放行）。返回删除行数。"""
        stmt = delete(cls).where(col(cls.created_at) >= since_ts)
        if group_id is not None:
            stmt = stmt.where(col(cls.group_id) == group_id)
        if user_id is not None:
            stmt = stmt.where(col(cls.user_id) == user_id)
        if bot_id:
            stmt = stmt.where(col(cls.bot_id) == bot_id)
        result = await session.execute(stmt)
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    @with_session
    async def prune(cls, session: AsyncSession, before_ts: int) -> int:
        """删除早于 before_ts 的旧流水（账本最长只需保留周窗）。返回删除行数。"""
        result = await session.execute(delete(cls).where(col(cls.created_at) < before_ts))
        deleted = result.rowcount if isinstance(result, CursorResult) else 0
        if deleted:
            logger.info(f"💰 [Budget] 清理过期用量流水 {deleted} 条")
        return deleted
