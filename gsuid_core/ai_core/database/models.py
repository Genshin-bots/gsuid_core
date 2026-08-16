"""
AI Core 数据库模型模块

定义 AI Agent 相关的数据模型，包括用户好感度等。
复用 gsuid_core 的数据库基础设施。
"""

import json
import time
from typing import Any, Set, Dict, List, Optional
from collections.abc import Sequence

from sqlmodel import Field, SQLModel, col, and_, case, delete, select, update
from sqlalchemy import Text, Column, UniqueConstraint
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import BaseModel, with_session


def _clamp_favor(value: int) -> int:
    """把分数钳到 favor_floor / favor_ceil。两项已在配置模板注册。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    floor: int = ai_config.get_config("favor_floor").data
    ceil: int = ai_config.get_config("favor_ceil").data
    return max(floor, min(ceil, value))


class UserFavorability(BaseModel, table=True):
    """用户关系温度表（每用户 × 每 bot，**不含 group_id**——关系是对人的，不是对房间的）。

    唯一写入口是 ``relationship.engine.settle_turn``（管理覆盖走 ``apply_admin_set``）。

    ``UniqueConstraint(user_id, bot_id)``：物理主键是自增 id，历史上没有唯一约束，
    并发首次互动能插出两行同 (user_id,bot_id)，症状是「好感度偶尔回退」。
    引入 daily_* 日预算后重复行会让配额翻倍失效，所以必须补约束 + 一次去重。

    ``tags`` / ``memory_count`` 为 **deprecated**：代码已停写停读，为兼容多后端不删列。
    """

    __table_args__ = (
        UniqueConstraint("user_id", "bot_id", name="ux_userfavorability_user_bot"),
        {"extend_existing": True},
    )

    user_name: Optional[str] = Field(default="", title="用户名")
    favorability: int = Field(default=0, title="好感度")
    interaction_count: int = Field(default=0, title="有效互动次数")
    last_interaction_time: int = Field(default=0, title="最后交互时间戳")
    memory_count: int = Field(default=0, title="记忆条数(deprecated)")
    tags: Optional[str] = Field(default="[]", title="用户标签(deprecated)")
    # ── 关系引擎：可解释性 + 预算 ──
    last_delta: int = Field(default=0, title="上次变更量")
    last_reason: str = Field(default="", max_length=32, title="上次变更原因码")
    last_eval_at: int = Field(default=0, title="上次结算时间戳")
    daily_gain: int = Field(default=0, title="当日已加")
    daily_loss: int = Field(default=0, title="当日已扣(绝对值)")
    daily_ymd: str = Field(default="", max_length=10, title="当日日期(YYYY-MM-DD)")
    last_positive_interact_at: int = Field(default=0, title="最近一次正向互动时间戳")

    @property
    def relationship_level(self) -> str:
        """关系等级中文名。**转调 ``relationship.zones``**，不再自划档。

        历史上这里、``self_cognition``、人设卡、README 各有一套区间，同一分数四种翻译。
        """
        from gsuid_core.ai_core.relationship.zones import level_name_of

        return level_name_of(self.favorability)

    @classmethod
    @with_session
    async def get_user_favorability(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
    ) -> Optional["UserFavorability"]:
        """
        获取用户好感度信息

        Args:
            session: 数据库会话
            user_id: 用户ID
            bot_id: 机器人ID

        Returns:
            UserFavorability 对象，如果不存在则返回 None
        """
        stmt = select(cls).where(and_(cls.user_id == user_id, cls.bot_id == bot_id))
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def create_user_favorability(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        user_name: str = "",
    ) -> int:
        """
        创建用户好感度记录

        Args:
            session: 数据库会话
            user_id: 用户ID
            bot_id: 机器人ID
            user_name: 用户名

        Returns:
            插入成功的行数
        """
        try:
            await session.merge(
                cls(
                    user_id=user_id,
                    bot_id=bot_id,
                    user_name=user_name or user_id,
                    favorability=0,
                    interaction_count=0,
                    last_interaction_time=int(time.time()),
                )
            )
            await session.commit()
            logger.info(i18n_t("log.ai.userfavorability_created_user_favorability_creat", user_id=user_id))
            return 1
        except Exception as e:
            logger.exception(i18n_t("log.ai.userfavorability_create_user_favorability_fail", e=e))
            return 0

    @classmethod
    async def get_or_create_user_favorability(
        cls,
        user_id: str,
        bot_id: str,
        user_name: str = "",
    ) -> "UserFavorability":
        """
        获取或创建用户好感度记录

        Args:
            user_id: 用户ID
            bot_id: 机器人ID
            user_name: 用户名

        Returns:
            UserFavorability 对象
        """
        existing = await cls.get_user_favorability(user_id, bot_id)
        if existing:
            return existing

        # 创建新记录
        await cls.create_user_favorability(user_id, bot_id, user_name)
        result = await cls.get_user_favorability(user_id, bot_id)
        if result is None:
            raise ValueError(f"Failed to create user favorability record for {user_id}")
        return result

    @classmethod
    @with_session
    async def get_scores_for(
        cls,
        session: AsyncSession,
        user_ids: List[str],
        bot_id: str,
    ) -> Dict[str, int]:
        """批量取一组用户的分数（``user_id -> favorability``）。

        装配层每轮要判「本 scope 内谁是高好感」，逐个查会打出 N 次往返。
        缺记录的用户不出现在返回值里（调用方按「未打分」处理）。
        """
        if not user_ids:
            return {}
        stmt = select(cls).where(and_(col(cls.user_id).in_(list(set(user_ids))), cls.bot_id == bot_id))
        rows = (await session.execute(stmt)).scalars().all()
        # 同 (user_id,bot_id) 若存在历史重复行，取分数绝对值最大的那条（与去重迁移同口径）
        best: Dict[str, int] = {}
        for row in rows:
            uid = str(row.user_id)
            if uid not in best or abs(row.favorability) > abs(best[uid]):
                best[uid] = row.favorability
        return best

    @classmethod
    @with_session
    async def get_all_user_favorability(
        cls,
        session: AsyncSession,
        bot_id: str,
    ) -> List["UserFavorability"]:
        """
        获取所有用户的好感度信息

        Args:
            session: 数据库会话
            bot_id: 机器人ID

        Returns:
            用户好感度列表
        """
        try:
            stmt = select(cls).where(cls.bot_id == bot_id)
            result = await session.execute(stmt)
            records = result.scalars().all()
            return list(records)
        except Exception as e:
            logger.exception(i18n_t("log.ai.userfavorability_get_user_favorability", e=e))
            return []

    @classmethod
    @with_session
    async def get_top_favorability_users(
        cls,
        session: AsyncSession,
        bot_id: str,
        limit: int = 10,
    ) -> List["UserFavorability"]:
        """
        获取好感度最高的用户列表

        Args:
            session: 数据库会话
            bot_id: 机器人ID
            limit: 返回数量限制

        Returns:
            好感度最高的用户列表
        """
        try:
            stmt = select(cls).where(cls.bot_id == bot_id).order_by(col(cls.favorability).desc()).limit(limit)
            result = await session.execute(stmt)
            records = result.scalars().all()
            return list(records)
        except Exception as e:
            logger.exception(i18n_t("log.ai.userfavorability_get_high_favorability_fail", e=e))
            return []

    @classmethod
    @with_session
    async def decay_idle_toward_neutral(
        cls,
        session: AsyncSession,
        step: int,
        idle_before_ts: int,
    ) -> int:
        """让**闲置**用户的关系温度向中性(0)回归一个步长（每日 job 调用）。

        语义变更：旧实现每天打全表，活跃用户被「每轮 +1」立刻补回，等于只惩罚
        「聊完就走」的人，对天天水群的人没写。现在只衰减
        ``last_positive_interact_at < idle_before_ts`` 的行——活跃用户不衰减，
        也不会因水群而升档。

        正值降 step、负值升 step、跨 0 直接归 0；``step<=0`` 不衰减。
        返回受影响行数（近似，两条 UPDATE 之和）。
        """
        if step <= 0:
            return 0
        idle = col(cls.last_positive_interact_at) < idle_before_ts
        # 用 case 表达式（SQLite / PostgreSQL 均可移植；func.max/min 的 2 参标量形态在 PG 上是聚合，会报错）：
        # 正值 favor>step → favor-step，否则(0<favor≤step)归 0；负值对称。跨 0 直接落 0。
        dec = (
            update(cls)
            .where(and_(col(cls.favorability) > 0, idle))
            .values(
                favorability=case((col(cls.favorability) > step, col(cls.favorability) - step), else_=0),
                last_reason="decay.idle",
                last_delta=-step,
            )
        )
        inc = (
            update(cls)
            .where(and_(col(cls.favorability) < 0, idle))
            .values(
                favorability=case((col(cls.favorability) < -step, col(cls.favorability) + step), else_=0),
                last_reason="decay.idle",
                last_delta=step,
            )
        )
        r1 = await session.execute(dec)
        r2 = await session.execute(inc)
        n1 = r1.rowcount if isinstance(r1, CursorResult) else 0
        n2 = r2.rowcount if isinstance(r2, CursorResult) else 0
        return n1 + n2

    @classmethod
    @with_session
    async def dedupe_user_bot_rows(cls, session: AsyncSession) -> int:
        """合并同 ``(user_id, bot_id)`` 的历史重复行，返回删除的行数。

        保留策略：分数**绝对值最大**的那行（保住已积累的关系强度），
        其余删除。唯一约束创建前必须先跑，否则建索引会失败。
        """
        rows = (await session.execute(select(cls))).scalars().all()
        keep: Dict[tuple[str, str], "UserFavorability"] = {}
        victims: List[int] = []
        for row in rows:
            key = (str(row.user_id), str(row.bot_id))
            if key not in keep:
                keep[key] = row
                continue
            best = keep[key]
            loser, winner = (best, row) if abs(row.favorability) > abs(best.favorability) else (row, best)
            keep[key] = winner
            if loser.id is not None:
                victims.append(loser.id)
        if not victims:
            return 0
        await session.execute(delete(cls).where(col(cls.id).in_(victims)))
        await session.commit()
        return len(victims)

    @classmethod
    @with_session
    async def apply_settlement(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        *,
        new_score: int,
        delta: int,
        reason: str,
        now_ts: int,
        daily_ymd: str,
        daily_gain: int,
        daily_loss: int,
        bump_interaction: bool,
        refresh_positive: bool,
        user_name: str = "",
    ) -> None:
        """把一次结算结果落库（引擎的唯一写路径，一次 UPDATE）。

        ``interaction_count`` 只在**有效互动**时 +1：管理侧 set 不再冒充互动，
        否则「衰减 / 管理设定」和「聊了一句」在计数上分不清。
        """
        record = await cls.get_or_create_user_favorability(user_id, bot_id, user_name)
        values: Dict[str, Any] = {
            "favorability": new_score,
            "last_delta": delta,
            "last_reason": reason,
            "last_eval_at": now_ts,
            "daily_ymd": daily_ymd,
            "daily_gain": daily_gain,
            "daily_loss": daily_loss,
        }
        if bump_interaction:
            values["interaction_count"] = record.interaction_count + 1
            values["last_interaction_time"] = now_ts
        if refresh_positive:
            values["last_positive_interact_at"] = now_ts
        stmt = update(cls).where(and_(col(cls.user_id) == user_id, col(cls.bot_id) == bot_id)).values(**values)
        await session.execute(stmt)
        await session.commit()


# 进程级建表标记：与全局 create_all 的启动时序解耦（RAG 初始化在后台线程，
# 可能早于/晚于 create_all，故首次读写前自建表，参考 state_store._ensure_table）。
_knowledge_table_ensured = False


class AIKnowledgeChunk(SQLModel, table=True):
    """手动知识库的 **SQL 真值源**（分片粒度，1 行 = 1 个 Qdrant point）。

    背景：控制台手动知识历史上**只存在于 Qdrant**，无磁盘/SQL 真值源——换嵌入模型、
    本地向量库目录损坏或迁移中断都可能永久丢数据；列表分页又因 Qdrant local 不支持
    offset 而退化为 O(n) 全量 scroll。本表把手动知识的结构化内容沉到 SQL：

    - **持久性**：向量库丢失后可从本表全量重嵌（见 ``rag/knowledge.reconcile_manual_knowledge``）。
    - **分页**：列表/检索走 SQL 原生 offset/limit（治 P5）。
    - **文档维度**：``doc_id`` 把一篇长文切出的多个分片聚合，支持整篇删除/导出（治 P3）。

    插件知识（``source="plugin"``）的真值源是插件代码 + ``_ENTITIES``，不入本表。
    """

    __table_args__ = {"extend_existing": True}

    # 逻辑 ID：文档分片为 ``{doc_id}#{chunk_index}``，单条手动知识为 uuid4。
    # 与 Qdrant payload["id"] 一致；Qdrant point id = get_point_id(逻辑ID)（UUID5）。
    id: str = Field(primary_key=True, max_length=160, title="逻辑ID")
    doc_id: str = Field(default="", index=True, max_length=128, title="文档ID")
    chunk_index: int = Field(default=0, title="分片序号")
    title: str = Field(default="", max_length=512, title="标题")
    content: str = Field(sa_column=Column(Text, nullable=False), title="正文")
    tags: str = Field(default="[]", title="标签(JSON 字符串)")
    source: str = Field(default="manual", index=True, max_length=32, title="来源")
    plugin: str = Field(default="manual", max_length=64, title="所属插件/分组")
    qdrant_id: str = Field(default="", index=True, max_length=64, title="向量点ID")
    content_hash: str = Field(default="", max_length=64, title="内容哈希")
    created_at: int = Field(default_factory=lambda: int(time.time()), title="创建时间戳")
    updated_at: int = Field(default_factory=lambda: int(time.time()), title="更新时间戳")

    # ───────── 序列化助手 ─────────
    def tags_list(self) -> List[str]:
        try:
            v = json.loads(self.tags)
            return [str(t) for t in v] if isinstance(v, list) else []
        except Exception:
            return []

    def to_dict(self) -> Dict[str, Any]:
        """API / 导出输出形状（兼容旧手动知识字段，附带 doc_id/chunk_index 扩展）。"""
        return {
            "id": self.id,
            "doc_id": self.doc_id,
            "chunk_index": self.chunk_index,
            "plugin": self.plugin,
            "title": self.title,
            "content": self.content,
            "tags": self.tags_list(),
            "source": self.source,
        }

    # ───────── 建表（与启动时序解耦） ─────────
    @classmethod
    async def ensure_table(cls) -> None:
        global _knowledge_table_ensured
        if _knowledge_table_ensured:
            return
        try:
            from gsuid_core.utils.database.base_models import engine

            async with engine.begin() as conn:
                # sqlmodel.pyi 把 ``__tablename__`` 标为 InstrumentedAttribute,
                # 不被 metadata.tables[str] 接受。SQLModel 自动以小写类名为表名,
                # 这里显式硬编码, 跳过 stub 噪音, 与 LLM.md §3.1.1 命名前缀一致。
                await conn.run_sync(
                    cls.metadata.create_all,
                    tables=[cls.metadata.tables["aichunk"]],
                    checkfirst=True,
                )
            _knowledge_table_ensured = True
        except Exception as e:
            logger.warning(i18n_t("log.ai.kb_aiknowledgechunk_table_check_fail", e=e))
            _knowledge_table_ensured = True

    # ───────── CRUD ─────────
    @classmethod
    async def upsert_many(cls, rows: List["AIKnowledgeChunk"]) -> int:
        """按主键幂等 upsert（merge）一批分片，返回写入行数。"""
        if not rows:
            return 0
        await cls.ensure_table()
        from gsuid_core.utils.database.base_models import async_maker

        async with async_maker() as session:
            for row in rows:
                await session.merge(row)
            await session.commit()
        return len(rows)

    @classmethod
    async def get_by_id(cls, entity_id: str) -> Optional["AIKnowledgeChunk"]:
        await cls.ensure_table()
        from gsuid_core.utils.database.base_models import async_maker

        async with async_maker() as session:
            result = await session.execute(select(cls).where(cls.id == entity_id))
            return result.scalars().first()

    @classmethod
    async def list_page(
        cls,
        source: str = "manual",
        doc_id: Optional[str] = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[Sequence["AIKnowledgeChunk"], int]:
        """SQL 原生分页（治 P5 的 O(n) scroll）。``source="all"`` 不限来源。"""
        await cls.ensure_table()
        from sqlalchemy import func

        from gsuid_core.utils.database.base_models import async_maker

        conds: List[Any] = []
        if source and source != "all":
            conds.append(cls.source == source)
        if doc_id:
            conds.append(cls.doc_id == doc_id)

        async with async_maker() as session:
            count_stmt = select(func.count()).select_from(cls)
            list_stmt = select(cls)
            for c in conds:
                count_stmt = count_stmt.where(c)
                list_stmt = list_stmt.where(c)
            total = (await session.execute(count_stmt)).scalar() or 0
            list_stmt = list_stmt.order_by(col(cls.doc_id), col(cls.chunk_index)).offset(offset).limit(limit)
            rows = list((await session.execute(list_stmt)).scalars().all())
            return rows, int(total)

    @classmethod
    async def iter_all(cls, source: str = "manual") -> List["AIKnowledgeChunk"]:
        """取全部行（导出/对账用）。``source="all"`` 不限来源。"""
        await cls.ensure_table()
        from gsuid_core.utils.database.base_models import async_maker

        async with async_maker() as session:
            stmt = select(cls)
            if source and source != "all":
                stmt = stmt.where(cls.source == source)
            return list((await session.execute(stmt)).scalars().all())

    @classmethod
    async def id_set(cls, source: str = "manual") -> Set[str]:
        """取全部逻辑 ID 集合（对账：判断 Qdrant 里哪些点尚未沉到 SQL）。"""
        await cls.ensure_table()
        from gsuid_core.utils.database.base_models import async_maker

        async with async_maker() as session:
            stmt = select(cls.id)
            if source and source != "all":
                stmt = stmt.where(cls.source == source)
            return {row[0] for row in (await session.execute(stmt)).all()}

    @classmethod
    async def delete_ids(cls, ids: List[str]) -> int:
        if not ids:
            return 0
        await cls.ensure_table()
        from gsuid_core.utils.database.base_models import async_maker

        async with async_maker() as session:
            await session.execute(delete(cls).where(col(cls.id).in_(ids)))
            await session.commit()
        return len(ids)

    @classmethod
    async def delete_doc(cls, doc_id: str) -> List[str]:
        """删除整篇文档的全部分片，返回被删分片的 qdrant_id 列表（供清理向量）。"""
        await cls.ensure_table()
        from gsuid_core.utils.database.base_models import async_maker

        async with async_maker() as session:
            rows = (await session.execute(select(cls).where(cls.doc_id == doc_id))).scalars().all()
            qids = [r.qdrant_id for r in rows if r.qdrant_id]
            if rows:
                # LLM.md §3.5.1: 比较表达式一律用 col() 包裹列
                # (delete 是 SQLAlchemy 原生, where() 严格只收 ColumnElement[bool])。
                await session.execute(delete(cls).where(col(cls.doc_id) == doc_id))
                await session.commit()
            return qids
