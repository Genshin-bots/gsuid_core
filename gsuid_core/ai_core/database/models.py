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
from sqlalchemy import Text, Column
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import BaseModel, with_session


def _clamp_favor(value: int) -> int:
    """把好感度钳制到 ai_config 的 favor_floor / favor_ceil（§F.3-1），防越界无限涨跌。

    两项已在 AI_CONFIG 模板注册，get_config 未命中时自动补默认值，无需兜底。
    """
    from gsuid_core.ai_core.configs.ai_config import ai_config

    floor: int = ai_config.get_config("favor_floor").data
    ceil: int = ai_config.get_config("favor_ceil").data
    return max(floor, min(ceil, value))


class UserFavorability(BaseModel, table=True):
    """
    用户好感度表

    存储用户与 AI 角色之间的人际关系数据。
    好感度范围通常为 -100 到 100+，影响角色的对话风格和行为。

    Attributes:
        user_id: 用户ID
        bot_id: 机器人ID
        user_name: 用户名
        favorability: 好感度值 (范围约 -100 到 100+)
        interaction_count: 交互次数
        last_interaction_time: 最后交互时间戳
        memory_count: 记忆条数
        tags: 用户标签，JSON格式存储
    """

    __table_args__ = {"extend_existing": True}

    user_name: Optional[str] = Field(default="", title="用户名")
    favorability: int = Field(default=0, title="好感度")
    interaction_count: int = Field(default=0, title="交互次数")
    last_interaction_time: int = Field(default=0, title="最后交互时间戳")
    memory_count: int = Field(default=0, title="记忆条数")
    tags: Optional[str] = Field(default="[]", title="用户标签")

    @property
    def relationship_level(self) -> str:
        """
        根据好感度返回关系等级描述

        Returns:
            关系等级字符串
        """
        if self.favorability < -50:
            return "厌恶"
        elif self.favorability < -10:
            return "冷淡"
        elif self.favorability < 10:
            return "陌生"
        elif self.favorability < 50:
            return "认识"
        elif self.favorability < 80:
            return "熟人"
        elif self.favorability < 100:
            return "朋友"
        else:
            return "挚友"

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
            logger.info(i18n_t("🧠 [UserFavorability] 创建用户好感度记录: {user_id}", user_id=user_id))
            return 1
        except Exception as e:
            logger.exception(i18n_t("🧠 [UserFavorability] 创建用户好感度记录失败: {e}", e=e))
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
    async def update_favorability(
        cls,
        user_id: str,
        bot_id: str,
        delta: int,
        user_name: str = "",
    ) -> bool:
        """
        更新用户好感度（增量）

        Args:
            user_id: 用户ID
            bot_id: 机器人ID
            delta: 好感度变化值（可为负数）
            user_name: 用户名（可选）

        Returns:
            是否更新成功
        """
        try:
            # 确保记录存在
            record = await cls.get_or_create_user_favorability(user_id, bot_id, user_name)

            # clamp 到配置上下限：防越界无限涨/跌（曾出现主人好感度 107 越过设计上限 100）
            new_value = _clamp_favor(record.favorability + delta)
            stmt = (
                update(cls)
                .where(
                    and_(
                        cls.user_id == user_id,
                        cls.bot_id == bot_id,
                    )
                )
                .values(
                    favorability=new_value,
                    interaction_count=record.interaction_count + 1,
                    last_interaction_time=int(time.time()),
                )
            )

            from gsuid_core.utils.database.base_models import async_maker

            async with async_maker() as session:
                await session.execute(stmt)
                await session.commit()

            # debug 级：被动累积每条消息都会调用，info 级会刷屏；
            # 显式工具路径（favorability_manager）另有自己的 info 结果日志。
            logger.debug(
                i18n_t(
                    "🧠 [UserFavorability] 更新用户 {user_id} 好感度: {p0} -> {new_value}",
                    user_id=user_id,
                    p0=record.favorability,
                    new_value=new_value,
                )
            )
            return True
        except Exception as e:
            logger.exception(i18n_t("🧠 [UserFavorability] 更新好感度失败: {e}", e=e))
            return False

    @classmethod
    async def set_favorability(
        cls,
        user_id: str,
        bot_id: str,
        value: int,
        user_name: str = "",
    ) -> bool:
        """
        设置用户好感度（绝对值）

        Args:
            user_id: 用户ID
            bot_id: 机器人ID
            value: 好感度目标值
            user_name: 用户名（可选）

        Returns:
            是否设置成功
        """
        try:
            # 确保记录存在
            record = await cls.get_or_create_user_favorability(user_id, bot_id, user_name)

            clamped = _clamp_favor(value)
            stmt = (
                update(cls)
                .where(
                    and_(
                        cls.user_id == user_id,
                        cls.bot_id == bot_id,
                    )
                )
                .values(
                    favorability=clamped,
                    interaction_count=record.interaction_count + 1,
                    last_interaction_time=int(time.time()),
                )
            )

            from gsuid_core.utils.database.base_models import async_maker

            async with async_maker() as session:
                await session.execute(stmt)
                await session.commit()

            logger.info(
                i18n_t("🧠 [UserFavorability] 设置用户 {user_id} 好感度: {clamped}", user_id=user_id, clamped=clamped)
            )
            return True
        except Exception as e:
            logger.exception(i18n_t("🧠 [UserFavorability] 设置好感度失败: {e}", e=e))
            return False

    @classmethod
    @with_session
    async def update_interaction(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
    ) -> bool:
        """
        更新用户交互次数

        Args:
            session: 数据库会话
            user_id: 用户ID
            bot_id: 机器人ID

        Returns:
            是否更新成功
        """
        try:
            stmt = select(cls).where(and_(cls.user_id == user_id, cls.bot_id == bot_id))
            result = await session.execute(stmt)
            record = result.scalars().first()

            if not record:
                return False

            stmt = (
                update(cls)
                .where(
                    and_(
                        cls.user_id == user_id,
                        cls.bot_id == bot_id,
                    )
                )
                .values(
                    interaction_count=record.interaction_count + 1,
                    last_interaction_time=int(time.time()),
                )
            )
            await session.execute(stmt)
            await session.commit()

            return True
        except Exception as e:
            logger.exception(i18n_t("🧠 [UserFavorability] 更新交互次数失败: {e}", e=e))
            return False

    @classmethod
    @with_session
    async def update_memory_count(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        count: int,
    ) -> bool:
        """
        更新用户记忆条数

        Args:
            session: 数据库会话
            user_id: 用户ID
            bot_id: 机器人ID
            count: 记忆条数

        Returns:
            是否更新成功
        """
        try:
            stmt = (
                update(cls)
                .where(
                    and_(
                        cls.user_id == user_id,
                        cls.bot_id == bot_id,
                    )
                )
                .values(memory_count=count)
            )
            await session.execute(stmt)
            await session.commit()

            return True
        except Exception as e:
            logger.exception(i18n_t("🧠 [UserFavorability] 更新记忆条数失败: {e}", e=e))
            return False

    @classmethod
    async def increment_memory_count(
        cls,
        user_id: str,
        bot_id: str,
    ) -> bool:
        """
        增加用户记忆条数

        Args:
            user_id: 用户ID
            bot_id: 机器人ID

        Returns:
            是否更新成功
        """
        try:
            record = await cls.get_user_favorability(user_id, bot_id)
            if not record:
                return False

            return await cls.update_memory_count(user_id, bot_id, record.memory_count + 1)
        except Exception as e:
            logger.exception(i18n_t("🧠 [UserFavorability] 增加记忆条数失败: {e}", e=e))
            return False

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
            logger.exception(i18n_t("🧠 [UserFavorability] 获取所有用户好感度失败: {e}", e=e))
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
            logger.exception(i18n_t("🧠 [UserFavorability] 获取高好感度用户失败: {e}", e=e))
            return []

    @classmethod
    @with_session
    async def decay_all_toward_neutral(cls, session: AsyncSession, step: int) -> int:
        """让所有用户好感度向中性(0)回归一个步长（§F.3-3，每日 job 调用）。

        正值降 step、负值升 step、跨 0 直接归 0；``step<=0`` 不衰减。使亲密需要持续正向
        互动维持，一次性刷分会随时间回落。返回受影响行数（近似，两条 UPDATE 之和）。
        """
        if step <= 0:
            return 0
        # 用 case 表达式（SQLite / PostgreSQL 均可移植；func.max/min 的 2 参标量形态在 PG 上是聚合，会报错）：
        # 正值 favor>step → favor-step，否则(0<favor≤step)归 0；负值对称。跨 0 直接落 0。
        dec = (
            update(cls)
            .where(col(cls.favorability) > 0)
            .values(favorability=case((col(cls.favorability) > step, col(cls.favorability) - step), else_=0))
        )
        inc = (
            update(cls)
            .where(col(cls.favorability) < 0)
            .values(favorability=case((col(cls.favorability) < -step, col(cls.favorability) + step), else_=0))
        )
        r1 = await session.execute(dec)
        r2 = await session.execute(inc)
        n1 = r1.rowcount if isinstance(r1, CursorResult) else 0
        n2 = r2.rowcount if isinstance(r2, CursorResult) else 0
        return n1 + n2


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
            logger.warning(i18n_t("🧠 [Knowledge] AIKnowledgeChunk 建表检查失败（将沿用既有表）: {e}", e=e))
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
