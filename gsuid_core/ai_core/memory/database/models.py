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
from typing import List, Optional
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, Relationship, col, select
from sqlalchemy import Text, Index, Table, Column, String, ForeignKey, UniqueConstraint, or_
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
    valid_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    qdrant_id: str = Field(index=True, max_length=36)

    mentioned_entities: List["AIMemEntity"] = Relationship(
        back_populates="episodes",
        sa_relationship_kwargs={
            "secondary": mem_episode_entity_mentions,
            "back_populates": "episodes",
            "lazy": "selectin",
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
            created_at=datetime.utcnow(),
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
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True, max_length=36)
    scope_key: str = Field(index=True, max_length=128)
    name: str = Field(max_length=256)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    tag: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    is_speaker: bool = Field(default=False)
    user_id: Optional[str] = Field(default=None, index=True, max_length=64)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    qdrant_id: str = Field(index=True, max_length=36)

    episodes: List["AIMemEpisode"] = Relationship(
        back_populates="mentioned_entities",
        sa_relationship_kwargs={
            "secondary": mem_episode_entity_mentions,
            "back_populates": "mentioned_entities",
            "lazy": "selectin",
        },
    )
    outgoing_edges: List["AIMemEdge"] = Relationship(
        back_populates="source_entity",
        sa_relationship_kwargs={
            "primaryjoin": "AIMemEntity.id == AIMemEdge.source_entity_id",
            "lazy": "selectin",
        },
    )

    incoming_edges: List["AIMemEdge"] = Relationship(
        back_populates="target_entity",
        sa_relationship_kwargs={
            "primaryjoin": "AIMemEntity.id == AIMemEdge.target_entity_id",
            "lazy": "selectin",
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
        """两阶段去重查找：精确匹配 → 向量相似度"""
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.memory.vector.ops import search_entities

        # 阶段1：精确匹配
        result = await session.execute(select(cls).where(cls.scope_key == scope_key, cls.name == name))
        entity = result.scalar_one_or_none()
        if entity:
            return entity

        # 阶段2：向量相似度搜索
        similar = await search_entities(query=name, scope_keys=[scope_key], top_k=3)
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
    ) -> tuple[dict[str, str], list[dict]]:
        """
        Returns:
            name_to_id,
            vector_payloads  ← 新增
        """

        name_to_id: dict[str, str] = {}
        vector_payloads: list[dict] = []

        # 补 speaker
        for uid in speaker_ids:
            entities_data.append(
                {
                    "name": uid,
                    "summary": f"用户 {uid}",
                    "tag": ["Speaker"],
                    "is_speaker": True,
                    "user_id": uid,
                }
            )

        for entity_data in entities_data:
            name = entity_data.get("name", "").strip()
            if not name:
                continue

            result = await session.execute(select(cls).where(cls.scope_key == scope_key, cls.name == name))
            existing = result.scalar_one_or_none()

            if existing:
                new_summary = entity_data.get("summary", "")
                if new_summary and new_summary not in existing.summary:
                    existing.summary = f"{existing.summary}\n{new_summary}".strip()

                existing_tags = existing.tag if isinstance(existing.tag, list) else []
                merged_tags = list(set(existing_tags) | set(entity_data.get("tag", [])))

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
                tag_list = entity_data.get("tag", [])

                new_entity = cls(
                    id=entity_id,
                    scope_key=scope_key,
                    name=name,
                    summary=entity_data.get("summary", ""),
                    tag=tag_list,
                    is_speaker=entity_data.get("is_speaker", False),
                    user_id=entity_data.get("user_id"),
                    qdrant_id=entity_id,
                )
                session.add(new_entity)

                vector_payloads.append(
                    {
                        "entity_id": entity_id,
                        "name": name,
                        "summary": entity_data.get("summary", ""),
                        "scope_key": scope_key,
                        "is_speaker": entity_data.get("is_speaker", False),
                        "user_id": entity_data.get("user_id"),
                        "tag": tag_list,
                    }
                )

                name_to_id[name] = entity_id

        return name_to_id, vector_payloads


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
    valid_at: datetime = Field(default_factory=datetime.utcnow)
    invalid_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    qdrant_id: str = Field(index=True, max_length=36)

    source_entity: Optional["AIMemEntity"] = Relationship(
        back_populates="outgoing_edges",
        sa_relationship_kwargs={"foreign_keys": "[AIMemEdge.source_entity_id]"},
    )

    target_entity: Optional["AIMemEntity"] = Relationship(
        back_populates="incoming_edges",
        sa_relationship_kwargs={"foreign_keys": "[AIMemEdge.target_entity_id]"},
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
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    child_categories: List["AIMemCategory"] = Relationship(
        back_populates="parent_categories",
        link_model=AIMemCategoryEdge,
        sa_relationship_kwargs={
            "primaryjoin": "AIMemCategory.id == AIMemCategoryEdge.parent_category_id",
            "secondaryjoin": "AIMemCategory.id == AIMemCategoryEdge.child_category_id",
            "lazy": "selectin",
        },
    )
    parent_categories: List["AIMemCategory"] = Relationship(
        back_populates="child_categories",
        link_model=AIMemCategoryEdge,
        sa_relationship_kwargs={
            "primaryjoin": "AIMemCategory.id == AIMemCategoryEdge.child_category_id",
            "secondaryjoin": "AIMemCategory.id == AIMemCategoryEdge.parent_category_id",
            "lazy": "selectin",
        },
    )
    member_entities: List["AIMemEntity"] = Relationship(
        sa_relationship_kwargs={
            "secondary": mem_category_entity_members,
            "lazy": "selectin",
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
