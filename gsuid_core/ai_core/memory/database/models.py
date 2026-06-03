"""记忆系统 SQLModel 数据模型 + 业务方法

存储模型：
- AIMemEpisode                : 原始对话片段（Base Graph 第一层）
- AIMemEntity                 : 提取出的实体节点（Base Graph 第二层）
- AIMemEdge                   : 实体间的关系（Base Graph 第三层）
- AIMemCategory               : 分层语义图节点（Hierarchical Graph）
- AIMemCategoryEdge           : Category ↔ Category 层次关联
- AIMemHierarchicalGraphMeta  : 分层图构建状态追踪
"""

import uuid
import asyncio
from typing import List, Optional
from datetime import datetime, timezone, timedelta

from sqlmodel import Field, SQLModel, Relationship, col, select
from sqlalchemy import Text, Index, Table, Column, String, ForeignKey, UniqueConstraint, or_, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import JSON

from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.vector.ops import upsert_episode_vector
from gsuid_core.utils.database.base_models import async_maker, with_session

# ─────────────────────────────────────────────
# 多对多关联表
# ─────────────────────────────────────────────
mem_episode_entity_mentions = Table(
    "mem_episode_entity_mentions",
    SQLModel.metadata,
    Column("episode_id", String(36), ForeignKey("aimemepisode.id"), primary_key=True),
    Column("entity_id", String(36), ForeignKey("aimementity.id"), primary_key=True),
)


mem_category_entity_members = Table(
    "mem_category_entity_members",
    SQLModel.metadata,
    Column("category_id", String(36), ForeignKey("aimemcategory.id"), primary_key=True),
    Column("entity_id", String(36), ForeignKey("aimementity.id"), primary_key=True),
    Index("ix_mem_cat_entity_entity_id", "entity_id"),
)


# ─────────────────────────────────────────────
# Episode：原始对话片段（Base Graph 第一层）
# ─────────────────────────────────────────────
class AIMemEpisode(SQLModel, table=True):
    """存储经聚合的对话片段，是所有记忆的原始素材。"""

    __table_args__ = (Index("ix_mem_episode_scope_valid_at", "scope_key", "valid_at"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    content: str = Field(sa_column=Column(Text, nullable=False))
    speaker_ids: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    valid_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    qdrant_id: str = Field(index=True, max_length=36)

    mentioned_entities: List["AIMemEntity"] = Relationship(
        back_populates="episodes",
        sa_relationship_kwargs={
            "secondary": mem_episode_entity_mentions,
            "back_populates": "episodes",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    @classmethod
    async def create_episode(
        cls,
        scope_key: str,
        content: str,
        speaker_ids: list[str],
        valid_at: datetime,
    ) -> "AIMemEpisode":
        """创建一条 Episode 记录并写入向量库。

        Args:
            scope_key: 作用域标识（如 "group:789012"）
            content: 聚合后的对话文本
            speaker_ids: 参与发言的 user_id 列表
            valid_at: 最早消息的时间戳

        Returns:
            新创建的 AIMemEpisode 对象
        """
        episode_id = str(uuid.uuid4())

        episode = AIMemEpisode(
            id=episode_id,
            scope_key=scope_key,
            content=content,
            speaker_ids=speaker_ids,
            valid_at=valid_at,
            created_at=datetime.now(timezone.utc),
            qdrant_id=episode_id,
        )
        async with async_maker() as session:
            session.add(episode)
            await session.commit()

        try:
            await upsert_episode_vector(
                episode_id=episode_id,
                content=content,
                scope_key=scope_key,
                valid_at_ts=valid_at.timestamp(),
                speaker_ids=speaker_ids,
            )
        except Exception as e:
            logger.warning(f"Episode vector upsert failed for {episode_id}: {e}")

        return episode


# ─────────────────────────────────────────────
# Entity：提取出的实体节点（Base Graph 第二层）
# ─────────────────────────────────────────────
class AIMemEntity(SQLModel, table=True):
    """从 Episode 中提取的实体，是知识图谱的核心节点。"""

    __table_args__ = (
        UniqueConstraint("scope_key", "name", name="uq_entity_scope_name"),
        Index("ix_mem_entity_scope_name", "scope_key", "name"),
        Index("ix_mem_entity_scope_speaker", "scope_key", "is_speaker"),
        Index("ix_mem_entity_scope_id", "scope_key", "id"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    name: str = Field(max_length=256)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    tag: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    is_speaker: bool = Field(default=False)
    user_id: Optional[str] = Field(default=None, index=True, max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    qdrant_id: str = Field(index=True, max_length=36)

    episodes: List["AIMemEpisode"] = Relationship(
        back_populates="mentioned_entities",
        sa_relationship_kwargs={
            "secondary": mem_episode_entity_mentions,
            "back_populates": "mentioned_entities",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )
    outgoing_edges: List["AIMemEdge"] = Relationship(
        back_populates="source_entity",
        sa_relationship_kwargs={
            "primaryjoin": "AIMemEntity.id == AIMemEdge.source_entity_id",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    incoming_edges: List["AIMemEdge"] = Relationship(
        back_populates="target_entity",
        sa_relationship_kwargs={
            "primaryjoin": "AIMemEntity.id == AIMemEdge.target_entity_id",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    # ── entity_dedup ──────────────────────────

    @classmethod
    @with_session
    async def find_existing(
        cls,
        session: AsyncSession,
        scope_key: str,
        name: str,
    ) -> Optional["AIMemEntity"]:
        """两阶段去重查找：精确匹配 → 混合检索（BM25+向量）"""
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.memory.vector.ops import _hybrid_search_entities

        # 阶段1：精确匹配
        result = await session.execute(select(cls).where(cls.scope_key == scope_key, cls.name == name))
        entity = result.scalar_one_or_none()
        if entity:
            return entity

        # 阶段2：混合检索（BM25+向量 RRF 融合），比纯向量更可靠
        similar = await _hybrid_search_entities(
            query=name,
            scope_keys=[scope_key],
            top_k=3,
        )
        threshold = memory_config.dedup_similarity_threshold
        if similar and similar[0]["score"] >= threshold:
            result = await session.execute(select(cls).where(cls.id == similar[0]["id"]))
            return result.scalar_one_or_none()

        return None

    @classmethod
    async def extract_and_upsert(
        cls,
        session: AsyncSession,
        scope_key: str,
        entities_data: list[dict],
        episode_id: str,
        speaker_ids: list[str],
    ) -> tuple[dict[str, str], list[dict], int]:
        """
        Returns:
            name_to_id,
            vector_payloads,
            new_entity_count  ← 新建实体数量（仅新建不包含已更新）
        """

        name_to_id: dict[str, str] = {}
        vector_payloads: list[dict] = []
        new_entity_count: int = 0

        for ed in entities_data:
            if "is_speaker" in ed or "Speaker" in (ed["tag"] if "tag" in ed else []):
                # 如果 LLM 写的是 user_444835641，统一改成 444835641
                name = ed["name"] if "name" in ed else ""
                if name.startswith("user_"):
                    ed["name"] = name[len("user_") :]

        # 再补充 speaker（此时 LLM 已有的会被精确匹配去重）
        """
        existing_speaker_names = {
            ed["name"] if "name" in ed else ""
            for ed in entities_data
            if "Speaker" in (ed["tag"] if "tag" in ed else [])
        }
        """
        existing_map: dict[str, AIMemEntity] = {}
        all_names = [
            (ed["name"] if "name" in ed else "").strip() for ed in entities_data if (ed["name"] if "name" in ed else "")
        ]
        if all_names:
            result = await session.execute(select(cls).where(cls.scope_key == scope_key, col(cls.name).in_(all_names)))
            existing_map = {e.name: e for e in result.scalars().all()}

        # ── 向量去重阶段：对 SQL 未命中的名称，用混合检索查找语义相似实体 ──
        # P-01 优化：改为 asyncio.gather 并行执行，避免串行 O(N) 查询延迟
        # Bug-05 修复：分两阶段执行，避免同一 AsyncSession 并发 SQL 查询
        # 阶段1：并行 Qdrant 相似度搜索（无 session 共享问题）
        # 阶段2：串行 SQL 查询确认（避免并发 session 问题）
        unmatched_names = [n for n in all_names if n not in existing_map]
        if unmatched_names:
            from gsuid_core.ai_core.memory.config import memory_config as _mc
            from gsuid_core.ai_core.memory.vector.ops import _hybrid_search_entities

            async def _hybrid_search_for_name(uname: str) -> tuple[str, list]:
                """并行查询单个名称的相似实体（仅 Qdrant 搜索）"""
                similar = await _hybrid_search_entities(
                    query=uname,
                    scope_keys=[scope_key],
                    top_k=3,
                )
                return uname, similar

            # 阶段1：并行执行所有 Qdrant 相似度搜索
            search_results = await asyncio.gather(*[_hybrid_search_for_name(n) for n in unmatched_names])

            # 阶段2：串行 SQL 查询确认（避免同一 session 并发执行）
            for uname, similar in search_results:
                if similar and similar[0]["score"] >= _mc.dedup_similarity_threshold:
                    sid = similar[0]["id"]
                    # 双重保障：SQL 查询增加 scope_key 条件，防止 Qdrant payload 不一致时跨 scope 匹配
                    r = await session.execute(select(cls).where(cls.id == sid, cls.scope_key == scope_key))
                    matched = r.scalar_one_or_none()
                    if matched:
                        existing_map[uname] = matched

        # Summary 增长上限，防止无限拼接导致 token 超限
        _MAX_SUMMARY_CHARS = 2000

        for entity_data in entities_data:
            name = (entity_data["name"] if "name" in entity_data else "").strip()
            if not name:
                continue

            existing = existing_map[name] if name in existing_map else None

            if existing:
                new_summary = entity_data["summary"] if "summary" in entity_data else ""
                if new_summary and new_summary not in existing.summary:
                    combined = f"{existing.summary}\n{new_summary}".strip()
                    if len(combined) > _MAX_SUMMARY_CHARS:
                        # Bug-06 修复：保留头部（早期上下文通常更重要），添加截断提示
                        combined = combined[:_MAX_SUMMARY_CHARS] + "\n[...早期记忆已截断...]"
                    existing.summary = combined

                existing_tags = existing.tag if isinstance(existing.tag, list) else []
                merged_tags = list(set(existing_tags) | set(entity_data["tag"] if "tag" in entity_data else []))

                existing.tag = merged_tags
                existing.updated_at = datetime.now(timezone.utc)
                session.add(existing)

                # 👉 收集 vector payload
                vector_payloads.append(
                    {
                        "entity_id": existing.id,
                        "name": existing.name,
                        "summary": existing.summary,
                        "scope_key": scope_key,
                        "is_speaker": existing.is_speaker,
                        "user_id": existing.user_id,
                        "tag": merged_tags,
                    }
                )

                name_to_id[name] = existing.id

            else:
                entity_id = str(uuid.uuid4())
                tag_list = entity_data["tag"] if "tag" in entity_data else []
                new_entity_count += 1

                new_entity = cls(
                    id=entity_id,
                    scope_key=scope_key,
                    name=name,
                    summary=entity_data["summary"] if "summary" in entity_data else "",
                    tag=tag_list,
                    is_speaker=entity_data["is_speaker"] if "is_speaker" in entity_data else False,
                    user_id=entity_data["user_id"] if "user_id" in entity_data else None,
                    qdrant_id=entity_id,
                )
                session.add(new_entity)

                vector_payloads.append(
                    {
                        "entity_id": entity_id,
                        "name": name,
                        "summary": entity_data["summary"] if "summary" in entity_data else "",
                        "scope_key": scope_key,
                        "is_speaker": entity_data["is_speaker"] if "is_speaker" in entity_data else False,
                        "user_id": entity_data["user_id"] if "user_id" in entity_data else None,
                        "tag": tag_list,
                    }
                )

                name_to_id[name] = entity_id

        # 写入 Episodic Edge（Episode ↔ Entity 关联），论文 Section 2.1
        if episode_id and name_to_id:
            # 查出已存在的关联，避免重复插入
            existing_mentions = await session.execute(
                select(mem_episode_entity_mentions.c.entity_id).where(
                    mem_episode_entity_mentions.c.episode_id == episode_id
                )
            )
            existing_entity_ids = {row[0] for row in existing_mentions.fetchall()}

            # name_to_id.values() 可能包含重复的 entity_id（不同 name 去重后指向同一 Entity），
            # 需要先 set() 去重，再与已有关联做差集
            new_relations = [
                {"episode_id": episode_id, "entity_id": entity_id}
                for entity_id in set(name_to_id.values())
                if entity_id not in existing_entity_ids
            ]
            if new_relations:
                await session.execute(insert(mem_episode_entity_mentions), new_relations)

        return name_to_id, vector_payloads, new_entity_count

    @classmethod
    @with_session
    async def get_frequent_names(
        cls,
        session: AsyncSession,
        scope_key: str,
        limit: int = 20,
    ) -> list[str]:
        """获取本 scope 最近活跃的非发言者实体名，作为跨批次消歧锚点（C2-b）。

        无独立的提及计数列，故以 updated_at 倒序近似"高频活跃"，
        并排除 Speaker 实体（其 name 为 user_id，对消歧无意义）。
        """
        result = await session.execute(
            select(cls.name)
            .where(cls.scope_key == scope_key, col(cls.is_speaker).is_(False))
            .order_by(col(cls.updated_at).desc())
            .limit(limit)
        )
        return [row[0] for row in result.all()]


# ─────────────────────────────────────────────
# Edge：实体间的关系（Base Graph 第三层）
# ─────────────────────────────────────────────
class AIMemEdge(SQLModel, table=True):
    """实体之间的有向关系边，存储一条可验证的事实。"""

    __table_args__ = (
        Index("ix_mem_edge_scope_valid", "scope_key", "valid_at", "invalid_at"),
        Index("ix_mem_edge_source", "source_entity_id"),
        Index("ix_mem_edge_target", "target_entity_id"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    fact: str = Field(sa_column=Column(Text, nullable=False))
    source_entity_id: str = Field(foreign_key="aimementity.id", max_length=36)
    target_entity_id: str = Field(foreign_key="aimementity.id", max_length=36)
    valid_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    invalid_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    qdrant_id: str = Field(index=True, max_length=36)
    # C1 跨发言者归并：同一 fact 被不同 source 重复陈述时，命中既有 Edge 只累加此计数，
    # 不再写入 N 条重复 Edge。旧库无此列，由 startup.py 的 ALTER 语句补齐（默认 1）。
    mention_count: int = Field(default=1)
    # C11 记忆生命周期：时效衰减分。检索排序按 reranker_score × decay_score 加权；
    # 长期未被检索的 Edge 由衰减 Worker 周期性下调，decay_score < 阈值则被遗忘。
    # 旧库无此列，由 startup.py 的 ALTER 语句补齐（默认 1.0）。
    decay_score: float = Field(default=1.0)
    # C11：最近一次被检索命中的时间，衰减判定的依据。旧库由 ALTER 补齐（默认 NULL）。
    last_accessed: Optional[datetime] = Field(default=None)

    source_entity: Optional["AIMemEntity"] = Relationship(
        back_populates="outgoing_edges",
        sa_relationship_kwargs={
            "foreign_keys": "[AIMemEdge.source_entity_id]",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    target_entity: Optional["AIMemEntity"] = Relationship(
        back_populates="incoming_edges",
        sa_relationship_kwargs={
            "foreign_keys": "[AIMemEdge.target_entity_id]",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    @classmethod
    @with_session
    async def get_for_entities(
        cls,
        session: AsyncSession,
        entity_ids: list[str],
        scope_key: str,
        limit: int = 30,
    ) -> list["AIMemEdge"]:
        """获取与指定 Entity 关联的有效 Edge"""
        result = await session.execute(
            select(cls)
            .where(
                cls.scope_key == scope_key,
                col(cls.invalid_at).is_(None),
                or_(
                    col(cls.source_entity_id).in_(entity_ids),
                    col(cls.target_entity_id).in_(entity_ids),
                ),
            )
            .order_by(col(cls.valid_at).desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    # ── C11 记忆生命周期 ───────────────────────────

    @classmethod
    @with_session
    async def get_confidence_inputs(cls, session: AsyncSession, edge_ids: list[str]) -> dict[str, tuple[int, float]]:
        """批量取边的置信度输入 {id: (mention_count, decay_score)}。

        检索期 dual_route 据此富集 Edge.weight（置信度=佐证×新鲜度）。decay_score 由衰减
        Worker 周期更新、且只落 DB（Qdrant 载荷会过期），故置信度一律以 DB 实时值为准。
        """
        if not edge_ids:
            return {}
        result = await session.execute(
            select(cls.id, cls.mention_count, cls.decay_score).where(col(cls.id).in_(edge_ids))
        )
        return {row[0]: (row[1], row[2]) for row in result.all()}

    @classmethod
    @with_session
    async def touch_accessed(cls, session: AsyncSession, edge_ids: list[str]) -> None:
        """把一批 Edge 标记为"刚被检索命中"，刷新 last_accessed（C11 Decay 依据）。"""
        if not edge_ids:
            return
        from sqlalchemy import update as _update

        await session.execute(
            _update(cls).where(col(cls.id).in_(edge_ids)).values(last_accessed=datetime.now(timezone.utc))
        )

    @classmethod
    @with_session
    async def apply_decay(
        cls,
        session: AsyncSession,
        stale_days: int = 14,
        decay_factor: float = 0.85,
        protect_mention_count: int = 3,
    ) -> int:
        """时效衰减（C11）：超过 stale_days 未被检索、且非高频提及的有效 Edge，
        ``decay_score *= decay_factor``。返回受影响行数。"""
        from sqlalchemy import update as _update

        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
        result = await session.execute(
            select(cls).where(
                col(cls.invalid_at).is_(None),
                col(cls.mention_count) < protect_mention_count,
                or_(col(cls.last_accessed).is_(None), col(cls.last_accessed) < cutoff),
            )
        )
        rows = list(result.scalars().all())
        for edge in rows:
            new_score = round((edge.decay_score or 1.0) * decay_factor, 4)
            await session.execute(_update(cls).where(col(cls.id) == edge.id).values(decay_score=new_score))
        return len(rows)

    @classmethod
    @with_session
    async def collect_forgotten(cls, session: AsyncSession, threshold: float = 0.1) -> list[str]:
        """收集 decay_score 已低于阈值、应被遗忘（物理删除）的 Edge ID 列表（C11）。"""
        result = await session.execute(select(cls.id, cls.qdrant_id).where(col(cls.decay_score) < threshold))
        return [row[1] or row[0] for row in result.all()]

    @classmethod
    @with_session
    async def purge_by_ids(cls, session: AsyncSession, edge_ids: list[str]) -> int:
        """按 ID 物理删除 Edge（C11 遗忘）。返回删除行数。"""
        if not edge_ids:
            return 0
        from sqlalchemy import delete as _delete

        await session.execute(_delete(cls).where(col(cls.qdrant_id).in_(edge_ids)))
        return len(edge_ids)


# ─────────────────────────────────────────────
# C11：记忆矛盾记录（Contradiction Engine）
# ─────────────────────────────────────────────
class AIMemConflict(SQLModel, table=True):
    """记录一对语义冲突的 Edge（同实体/关系但事实相反）。

    冲突检测不在普通回复中把新旧矛盾全量丢给 LLM——只保留一条框架生成的
    冲突摘要，并默认以最新有效记录为准（见 dual_route 后置拦截器）。
    """

    __table_args__ = (Index("ix_mem_conflict_scope_sig", "scope_key", "fact_signature"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    fact_signature: str = Field(default="", max_length=256)
    old_edge_id: str = Field(default="", max_length=36)
    new_edge_id: str = Field(default="", max_length=36)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    @with_session
    async def record(
        cls,
        session: AsyncSession,
        scope_key: str,
        fact_signature: str,
        old_edge_id: str,
        new_edge_id: str,
        summary: str,
    ) -> None:
        session.add(
            cls(
                scope_key=scope_key,
                fact_signature=fact_signature[:256],
                old_edge_id=old_edge_id,
                new_edge_id=new_edge_id,
                summary=summary[:2000],
            )
        )


# ─────────────────────────────────────────────
# Category ↔ Category 层次关联（链接模型）
# ─────────────────────────────────────────────
class AIMemCategoryEdge(SQLModel, table=True):
    """记录父 Category → 子 Category 的归属关系，支持多对多。"""

    parent_category_id: str = Field(foreign_key="aimemcategory.id", primary_key=True, max_length=36)
    child_category_id: str = Field(foreign_key="aimemcategory.id", primary_key=True, max_length=36)


# ─────────────────────────────────────────────
# Category：分层语义图节点（Hierarchical Graph）
# ─────────────────────────────────────────────
class AIMemCategory(SQLModel, table=True):
    """分层语义图的类目节点。Layer=1 最具体，Layer 越大越抽象。"""

    __table_args__ = (
        UniqueConstraint("scope_key", "layer", "name", name="uq_category_scope_layer_name"),
        Index("ix_mem_category_scope_layer", "scope_key", "layer"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    name: str = Field(max_length=256)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    tag: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    layer: int = Field(default=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    child_categories: List["AIMemCategory"] = Relationship(
        back_populates="parent_categories",
        link_model=AIMemCategoryEdge,
        sa_relationship_kwargs={
            "primaryjoin": "AIMemCategory.id == AIMemCategoryEdge.parent_category_id",
            "secondaryjoin": "AIMemCategory.id == AIMemCategoryEdge.child_category_id",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )
    parent_categories: List["AIMemCategory"] = Relationship(
        back_populates="child_categories",
        link_model=AIMemCategoryEdge,
        sa_relationship_kwargs={
            "primaryjoin": "AIMemCategory.id == AIMemCategoryEdge.child_category_id",
            "secondaryjoin": "AIMemCategory.id == AIMemCategoryEdge.parent_category_id",
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )
    member_entities: List["AIMemEntity"] = Relationship(
        sa_relationship_kwargs={
            "secondary": mem_category_entity_members,
            "lazy": "noload",  # P-03: 避免 selectin 自动加载导致 N+1 查询问题
        },
    )

    # ── hierarchical_graph ────────────────────

    @classmethod
    @with_session
    async def get_by_layer(
        cls,
        session: AsyncSession,
        scope_key: str,
        layer: int,
    ) -> list["AIMemCategory"]:
        result = await session.execute(select(cls).where(cls.scope_key == scope_key, cls.layer == layer))
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def get_children_of(
        cls,
        session: AsyncSession,
        parent_ids: list[str],
    ) -> list["AIMemCategory"]:
        result = await session.execute(
            select(cls)
            .join(AIMemCategoryEdge, col(AIMemCategoryEdge.child_category_id) == col(cls.id))
            .where(col(AIMemCategoryEdge.parent_category_id).in_(parent_ids))
            .distinct()
        )
        return list(result.scalars().all())
