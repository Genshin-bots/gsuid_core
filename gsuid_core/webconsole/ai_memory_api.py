"""
AI Memory APIs
提供记忆系统（Mnemis 双路检索）相关的 RESTful APIs

包括记忆检索、Episode/Entity/Edge 浏览、分层语义图查看、配置管理、统计信息等。
"""

from typing import Dict, List, Optional

from fastapi import Depends
from pydantic import Field, BaseModel
from sqlmodel import col, func, delete, select
from sqlalchemy import or_

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemEpisode,
    AIMemCategory,
    AIMemCategoryEdge,
    mem_category_entity_members,
    mem_episode_entity_mentions,
)
from gsuid_core.ai_core.memory.ingestion.hiergraph import AIMemHierarchicalGraphMeta

# ─────────────────────────────────────────────
# Pydantic 请求模型
# ─────────────────────────────────────────────


class MemorySearchRequest(BaseModel):
    """记忆检索请求"""

    query: str = Field(..., min_length=1, max_length=2000, description="查询文本")
    group_id: Optional[str] = Field(None, min_length=1, max_length=128, description="群组 ID")
    user_id: str = Field(..., max_length=64, description="用户 ID（可选，用于联合用户全局记忆）")
    top_k: int = Field(default=10, ge=1, le=50, description="返回结果数量上限")
    enable_system2: bool = Field(default=True, description="是否启用 System-2 分层图遍历")
    enable_user_global: bool = Field(default=False, description="是否联合查询用户跨群画像")


class MemoryConfigUpdateRequest(BaseModel):
    """记忆系统配置更新请求"""

    observer_enabled: Optional[bool] = None
    observer_blacklist: Optional[List[str]] = None
    ingestion_enabled: Optional[bool] = None
    batch_interval_seconds: Optional[int] = Field(default=None, ge=60, le=86400)
    batch_max_size: Optional[int] = Field(default=None, ge=5, le=100)
    llm_semaphore_limit: Optional[int] = Field(default=None, ge=1, le=10)
    enable_retrieval: Optional[bool] = None
    enable_system2: Optional[bool] = None
    enable_user_global_memory: Optional[bool] = None
    enable_heartbeat_memory: Optional[bool] = None
    retrieval_top_k: Optional[int] = Field(default=None, ge=1, le=50)
    dedup_similarity_threshold: Optional[float] = Field(default=None, ge=0.5, le=1.0)
    edge_conflict_threshold: Optional[float] = Field(default=None, ge=0.5, le=1.0)
    min_children_per_category: Optional[int] = Field(default=None, ge=2, le=20)
    max_layers: Optional[int] = Field(default=None, ge=1, le=10)
    hiergraph_rebuild_ratio: Optional[float] = Field(default=None, ge=1.0, le=3.0)
    hiergraph_rebuild_interval_seconds: Optional[int] = Field(default=None, ge=3600, le=604800)


# ─────────────────────────────────────────────
# 1. 记忆检索 API
# ─────────────────────────────────────────────


@app.post("/api/ai/memory/search")
async def search_memory(
    req: MemorySearchRequest,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    记忆双路检索

    并行执行 System-1（向量相似度）和 System-2（分层图遍历），
    合并去重后经 Reranker 重排序，返回最终的 MemoryContext。

    Args:
        req: 检索请求参数

    Returns:
        status: 0成功，1失败
        data: 包含 episodes、entities、edges、retrieval_meta 的检索结果
    """
    try:
        from gsuid_core.ai_core.memory.retrieval.dual_route import dual_route_retrieve

        if not memory_config.enable_retrieval:
            return {
                "status": 1,
                "msg": "记忆检索功能已禁用，请在配置中启用 enable_retrieval",
                "data": None,
            }

        mem_ctx = await dual_route_retrieve(
            query=req.query,
            group_id=req.group_id,
            user_id=req.user_id,
            top_k=req.top_k,
            enable_system2=req.enable_system2,
            enable_user_global=req.enable_user_global,
        )

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "episodes": mem_ctx.episodes,
                "entities": mem_ctx.entities,
                "edges": mem_ctx.edges,
                "retrieval_meta": mem_ctx.retrieval_meta,
                "prompt_text": mem_ctx.to_prompt_text(),
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"记忆检索失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 2. Episode 浏览 API
# ─────────────────────────────────────────────


@app.get("/api/ai/memory/episodes")
async def list_episodes(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    all_scopes: bool = False,
    page: int = 1,
    page_size: int = 20,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取 Episode（对话片段）列表

    支持按 group_id 或 scope_key 过滤，分页返回。
    设置 all_scopes=true 可返回所有范围的 Episode。

    Args:
        group_id: 群组 ID（可选，优先级低于 scope_key）
        scope_key: 完整的 Scope Key（可选，优先级高于 group_id）
        all_scopes: 是否返回所有范围的 Episode（默认 False）
        page: 页码，从 1 开始
        page_size: 每页数量

    Returns:
        status: 0成功，1失败
        data: Episode 列表及分页信息
    """
    try:
        actual_scope_key = scope_key
        if not all_scopes and actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)

        async with async_maker() as session:
            query = select(AIMemEpisode)
            count_query = select(func.count()).select_from(AIMemEpisode)

            if actual_scope_key:
                query = query.where(AIMemEpisode.scope_key == actual_scope_key)
                count_query = count_query.where(AIMemEpisode.scope_key == actual_scope_key)

            total_result = await session.execute(count_query)
            total = total_result.scalar() or 0

            query = query.order_by(col(AIMemEpisode.valid_at).desc())
            query = query.offset((page - 1) * page_size).limit(page_size)

            result = await session.execute(query)
            episodes = result.scalars().all()

            items = []
            for ep in episodes:
                items.append(
                    {
                        "id": ep.id,
                        "scope_key": ep.scope_key,
                        "content": ep.content,
                        "speaker_ids": ep.speaker_ids,
                        "valid_at": str(ep.valid_at) if ep.valid_at else None,
                        "created_at": str(ep.created_at) if ep.created_at else None,
                        "qdrant_id": ep.qdrant_id,
                    }
                )

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取 Episode 列表失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/memory/episodes/{episode_id}")
async def get_episode_detail(
    episode_id: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取单个 Episode 详情

    包含关联的 Entity 列表。

    Args:
        episode_id: Episode ID

    Returns:
        status: 0成功，1失败
        data: Episode 详情
    """
    try:
        async with async_maker() as session:
            result = await session.execute(select(AIMemEpisode).where(AIMemEpisode.id == episode_id))
            episode = result.scalar_one_or_none()

            if episode is None:
                return {"status": 1, "msg": f"未找到 Episode: {episode_id}", "data": None}

            mentioned_entities = []
            if episode.mentioned_entities:
                for entity in episode.mentioned_entities:
                    mentioned_entities.append(
                        {
                            "id": entity.id,
                            "name": entity.name,
                            "summary": entity.summary,
                            "tag": entity.tag,
                            "is_speaker": entity.is_speaker,
                        }
                    )

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "id": episode.id,
                    "scope_key": episode.scope_key,
                    "content": episode.content,
                    "speaker_ids": episode.speaker_ids,
                    "valid_at": str(episode.valid_at) if episode.valid_at else None,
                    "created_at": str(episode.created_at) if episode.created_at else None,
                    "qdrant_id": episode.qdrant_id,
                    "mentioned_entities": mentioned_entities,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取 Episode 详情失败: {str(e)}",
            "data": None,
        }


@app.delete("/api/ai/memory/episodes/{episode_id}")
async def delete_episode(
    episode_id: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    删除单个 Episode

    同时删除数据库记录和 Qdrant 中的向量。

    Args:
        episode_id: Episode ID

    Returns:
        status: 0成功，1失败
    """
    try:
        async with async_maker() as session:
            result = await session.execute(select(AIMemEpisode).where(AIMemEpisode.id == episode_id))
            episode = result.scalar_one_or_none()

            if episode is None:
                return {"status": 1, "msg": f"未找到 Episode: {episode_id}", "data": None}

            # 删除 Qdrant 向量
            try:
                from gsuid_core.ai_core.rag.base import client
                from gsuid_core.ai_core.memory.vector.collections import MEMORY_EPISODES_COLLECTION

                if client is not None:
                    await client.delete(
                        collection_name=MEMORY_EPISODES_COLLECTION,
                        points_selector=[episode.qdrant_id],
                    )
            except Exception:
                pass

            await session.delete(episode)
            await session.commit()

            return {"status": 0, "msg": "ok", "data": None}
    except Exception as e:
        return {
            "status": 1,
            "msg": f"删除 Episode 失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 3. Entity 浏览 API
# ─────────────────────────────────────────────


@app.get("/api/ai/memory/entities")
async def list_entities(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    all_scopes: bool = False,
    is_speaker: Optional[bool] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取 Entity（实体节点）列表

    支持按群组、是否为说话者、名称搜索过滤，分页返回。
    设置 all_scopes=true 可返回所有范围的 Entity。

    Args:
        group_id: 群组 ID（可选）
        scope_key: 完整的 Scope Key（可选，优先级高于 group_id）
        all_scopes: 是否返回所有范围的 Entity（默认 False）
        is_speaker: 是否为说话者（可选）
        search: 名称搜索关键词（可选）
        page: 页码
        page_size: 每页数量

    Returns:
        status: 0成功，1失败
        data: Entity 列表及分页信息
    """
    try:
        actual_scope_key = scope_key
        if not all_scopes and actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)

        async with async_maker() as session:
            query = select(AIMemEntity)
            count_query = select(func.count()).select_from(AIMemEntity)

            if actual_scope_key:
                query = query.where(AIMemEntity.scope_key == actual_scope_key)
                count_query = count_query.where(AIMemEntity.scope_key == actual_scope_key)

            if is_speaker is not None:
                query = query.where(AIMemEntity.is_speaker == is_speaker)
                count_query = count_query.where(AIMemEntity.is_speaker == is_speaker)

            if search:
                query = query.where(col(AIMemEntity.name).contains(search))
                count_query = count_query.where(col(AIMemEntity.name).contains(search))

            total_result = await session.execute(count_query)
            total = total_result.scalar() or 0

            query = query.order_by(col(AIMemEntity.updated_at).desc())
            query = query.offset((page - 1) * page_size).limit(page_size)

            result = await session.execute(query)
            entities = result.scalars().all()

            items = []
            for entity in entities:
                items.append(
                    {
                        "id": entity.id,
                        "scope_key": entity.scope_key,
                        "name": entity.name,
                        "summary": entity.summary,
                        "tag": entity.tag,
                        "is_speaker": entity.is_speaker,
                        "user_id": entity.user_id,
                        "created_at": str(entity.created_at) if entity.created_at else None,
                        "updated_at": str(entity.updated_at) if entity.updated_at else None,
                    }
                )

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取 Entity 列表失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/memory/entities/{entity_id}")
async def get_entity_detail(
    entity_id: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取单个 Entity 详情

    包含关联的 Episode、Edge 信息。

    Args:
        entity_id: Entity ID

    Returns:
        status: 0成功，1失败
        data: Entity 详情
    """
    try:
        async with async_maker() as session:
            result = await session.execute(select(AIMemEntity).where(AIMemEntity.id == entity_id))
            entity = result.scalar_one_or_none()

            if entity is None:
                return {"status": 1, "msg": f"未找到 Entity: {entity_id}", "data": None}

            episodes = []
            if entity.episodes:
                for ep in entity.episodes[:20]:
                    episodes.append(
                        {
                            "id": ep.id,
                            "content": ep.content[:200],
                            "valid_at": str(ep.valid_at) if ep.valid_at else None,
                        }
                    )

            edges = []
            if entity.outgoing_edges:
                for edge in entity.outgoing_edges[:20]:
                    edges.append(
                        {
                            "id": edge.id,
                            "fact": edge.fact,
                            "target_entity_id": edge.target_entity_id,
                            "valid_at": str(edge.valid_at) if edge.valid_at else None,
                            "direction": "outgoing",
                        }
                    )
            if entity.incoming_edges:
                for edge in entity.incoming_edges[:20]:
                    edges.append(
                        {
                            "id": edge.id,
                            "fact": edge.fact,
                            "source_entity_id": edge.source_entity_id,
                            "valid_at": str(edge.valid_at) if edge.valid_at else None,
                            "direction": "incoming",
                        }
                    )

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "id": entity.id,
                    "scope_key": entity.scope_key,
                    "name": entity.name,
                    "summary": entity.summary,
                    "tag": entity.tag,
                    "is_speaker": entity.is_speaker,
                    "user_id": entity.user_id,
                    "created_at": str(entity.created_at) if entity.created_at else None,
                    "updated_at": str(entity.updated_at) if entity.updated_at else None,
                    "qdrant_id": entity.qdrant_id,
                    "episodes": episodes,
                    "edges": edges,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取 Entity 详情失败: {str(e)}",
            "data": None,
        }


@app.delete("/api/ai/memory/entities/{entity_id}")
async def delete_entity(
    entity_id: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    删除单个 Entity

    同时删除数据库记录和 Qdrant 中的向量。

    Args:
        entity_id: Entity ID

    Returns:
        status: 0成功，1失败
    """
    try:
        async with async_maker() as session:
            result = await session.execute(select(AIMemEntity).where(AIMemEntity.id == entity_id))
            entity = result.scalar_one_or_none()

            if entity is None:
                return {"status": 1, "msg": f"未找到 Entity: {entity_id}", "data": None}

            # 删除 Qdrant 向量
            try:
                from gsuid_core.ai_core.rag.base import client
                from gsuid_core.ai_core.memory.vector.collections import MEMORY_ENTITIES_COLLECTION

                if client is not None:
                    await client.delete(
                        collection_name=MEMORY_ENTITIES_COLLECTION,
                        points_selector=[entity.qdrant_id],
                    )
            except Exception:
                pass

            await session.delete(entity)
            await session.commit()

            return {"status": 0, "msg": "ok", "data": None}
    except Exception as e:
        return {
            "status": 1,
            "msg": f"删除 Entity 失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 4. Edge 浏览 API
# ─────────────────────────────────────────────


@app.get("/api/ai/memory/edges")
async def list_edges(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    all_scopes: bool = False,
    entity_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取 Edge（实体关系）列表

    支持按群组、关联 Entity 过滤，分页返回。
    设置 all_scopes=true 可返回所有范围的 Edge。

    Args:
        group_id: 群组 ID（可选）
        scope_key: 完整的 Scope Key（可选，优先级高于 group_id）
        all_scopes: 是否返回所有范围的 Edge（默认 False）
        entity_id: 关联的 Entity ID（可选，返回该 Entity 的出边和入边）
        page: 页码
        page_size: 每页数量

    Returns:
        status: 0成功，1失败
        data: Edge 列表及分页信息
    """
    try:
        actual_scope_key = scope_key
        if not all_scopes and actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)

        async with async_maker() as session:
            query = select(AIMemEdge)
            count_query = select(func.count()).select_from(AIMemEdge)

            if actual_scope_key:
                query = query.where(AIMemEdge.scope_key == actual_scope_key)
                count_query = count_query.where(AIMemEdge.scope_key == actual_scope_key)

            if entity_id:
                query = query.where(
                    or_(
                        col(AIMemEdge.source_entity_id) == entity_id,
                        col(AIMemEdge.target_entity_id) == entity_id,
                    )
                )
                count_query = count_query.where(
                    or_(
                        col(AIMemEdge.source_entity_id) == entity_id,
                        col(AIMemEdge.target_entity_id) == entity_id,
                    )
                )

            total_result = await session.execute(count_query)
            total = total_result.scalar() or 0

            query = query.order_by(col(AIMemEdge.valid_at).desc())
            query = query.offset((page - 1) * page_size).limit(page_size)

            result = await session.execute(query)
            edges = result.scalars().all()

            items = []
            for edge in edges:
                items.append(
                    {
                        "id": edge.id,
                        "scope_key": edge.scope_key,
                        "fact": edge.fact,
                        "source_entity_id": edge.source_entity_id,
                        "target_entity_id": edge.target_entity_id,
                        "valid_at": str(edge.valid_at) if edge.valid_at else None,
                        "invalid_at": str(edge.invalid_at) if edge.invalid_at else None,
                        "created_at": str(edge.created_at) if edge.created_at else None,
                    }
                )

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取 Edge 列表失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/memory/edges/{edge_id}")
async def get_edge_detail(
    edge_id: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取单个 Edge 详情

    包含源 Entity 和目标 Entity 信息。

    Args:
        edge_id: Edge ID

    Returns:
        status: 0成功，1失败
        data: Edge 详情
    """
    try:
        async with async_maker() as session:
            result = await session.execute(select(AIMemEdge).where(AIMemEdge.id == edge_id))
            edge = result.scalar_one_or_none()

            if edge is None:
                return {"status": 1, "msg": f"未找到 Edge: {edge_id}", "data": None}

            source_entity = None
            if edge.source_entity:
                source_entity = {
                    "id": edge.source_entity.id,
                    "name": edge.source_entity.name,
                    "summary": edge.source_entity.summary,
                }

            target_entity = None
            if edge.target_entity:
                target_entity = {
                    "id": edge.target_entity.id,
                    "name": edge.target_entity.name,
                    "summary": edge.target_entity.summary,
                }

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "id": edge.id,
                    "scope_key": edge.scope_key,
                    "fact": edge.fact,
                    "source_entity_id": edge.source_entity_id,
                    "target_entity_id": edge.target_entity_id,
                    "valid_at": str(edge.valid_at) if edge.valid_at else None,
                    "invalid_at": str(edge.invalid_at) if edge.invalid_at else None,
                    "created_at": str(edge.created_at) if edge.created_at else None,
                    "qdrant_id": edge.qdrant_id,
                    "source_entity": source_entity,
                    "target_entity": target_entity,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取 Edge 详情失败: {str(e)}",
            "data": None,
        }


@app.delete("/api/ai/memory/edges/{edge_id}")
async def delete_edge(
    edge_id: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    删除单个 Edge

    同时删除数据库记录和 Qdrant 中的向量。

    Args:
        edge_id: Edge ID

    Returns:
        status: 0成功，1失败
    """
    try:
        async with async_maker() as session:
            result = await session.execute(select(AIMemEdge).where(AIMemEdge.id == edge_id))
            edge = result.scalar_one_or_none()

            if edge is None:
                return {"status": 1, "msg": f"未找到 Edge: {edge_id}", "data": None}

            # 删除 Qdrant 向量
            try:
                from gsuid_core.ai_core.rag.base import client
                from gsuid_core.ai_core.memory.vector.collections import MEMORY_EDGES_COLLECTION

                if client is not None:
                    await client.delete(
                        collection_name=MEMORY_EDGES_COLLECTION,
                        points_selector=[edge.qdrant_id],
                    )
            except Exception:
                pass

            await session.delete(edge)
            await session.commit()

            return {"status": 0, "msg": "ok", "data": None}
    except Exception as e:
        return {
            "status": 1,
            "msg": f"删除 Edge 失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 5. 分层语义图（Category）浏览 API
# ─────────────────────────────────────────────


@app.get("/api/ai/memory/categories")
async def list_categories(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    all_scopes: bool = False,
    layer: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取 Category（分层语义图节点）列表

    支持按群组、层级过滤，分页返回。
    设置 all_scopes=true 可返回所有范围的 Category。

    Args:
        group_id: 群组 ID（可选）
        scope_key: 完整的 Scope Key（可选，优先级高于 group_id）
        all_scopes: 是否返回所有范围的 Category（默认 False）
        layer: 层级编号（可选，1=最具体，越大越抽象）
        page: 页码
        page_size: 每页数量

    Returns:
        status: 0成功，1失败
        data: Category 列表及分页信息
    """
    try:
        actual_scope_key = scope_key
        if not all_scopes and actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)

        async with async_maker() as session:
            # 1. 显式定义子查询：计算子类目数量
            child_count_subq = (
                select(func.count(col(AIMemCategoryEdge.child_category_id)))
                .where(AIMemCategoryEdge.parent_category_id == AIMemCategory.id)
                .scalar_subquery()
                .label("child_count")
            )

            # 2. 显式定义子查询：计算关联实体数量
            member_count_subq = (
                select(func.count(mem_category_entity_members.c.entity_id))
                .where(mem_category_entity_members.c.category_id == AIMemCategory.id)
                .scalar_subquery()
                .label("member_count")
            )

            # 3. 构造主查询：同时选取模型和两个子查询结果
            query = select(AIMemCategory, child_count_subq, member_count_subq)
            count_query = select(func.count()).select_from(AIMemCategory)

            # 注意：actual_scope_key 为 None 且 all_scopes=True 时，不加过滤条件，查所有
            if actual_scope_key:
                query = query.where(AIMemCategory.scope_key == actual_scope_key)
                count_query = count_query.where(AIMemCategory.scope_key == actual_scope_key)

            if layer is not None:
                query = query.where(AIMemCategory.layer == layer)
                count_query = count_query.where(AIMemCategory.layer == layer)

            # 获取总数
            total_result = await session.execute(count_query)
            total = total_result.scalar() or 0

            # 分页与排序
            query = query.order_by(col(AIMemCategory.layer).asc(), col(AIMemCategory.name).asc())
            query = query.offset((page - 1) * page_size).limit(page_size)

            # 执行查询
            result = await session.execute(query)
            # 注意：此时 result 里的每一行是一个元组: (AIMemCategory实例, child_count, member_count)
            rows = result.all()

            items = []
            for cat, child_count, member_count in rows:
                items.append(
                    {
                        "id": cat.id,
                        "scope_key": cat.scope_key,
                        "name": cat.name,
                        "summary": cat.summary,
                        "tag": cat.tag,
                        "layer": cat.layer,
                        "child_categories_count": child_count,  # 直接使用 SQL 计算出的结果
                        "member_entities_count": member_count,  # 直接使用 SQL 计算出的结果
                        "created_at": str(cat.created_at) if cat.created_at else None,
                        "updated_at": str(cat.updated_at) if cat.updated_at else None,
                    }
                )

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取 Category 列表失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/memory/categories/{category_id}")
async def get_category_detail(
    category_id: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取单个 Category 详情

    包含子 Category 列表和成员 Entity 列表。

    Args:
        category_id: Category ID

    Returns:
        status: 0成功，1失败
        data: Category 详情
    """
    try:
        async with async_maker() as session:
            result = await session.execute(select(AIMemCategory).where(AIMemCategory.id == category_id))
            category = result.scalar_one_or_none()

            if category is None:
                return {"status": 1, "msg": f"未找到 Category: {category_id}", "data": None}

            child_categories = []
            if category.child_categories:
                for child in category.child_categories:
                    child_categories.append(
                        {
                            "id": child.id,
                            "name": child.name,
                            "layer": child.layer,
                        }
                    )

            parent_categories = []
            if category.parent_categories:
                for parent in category.parent_categories:
                    parent_categories.append(
                        {
                            "id": parent.id,
                            "name": parent.name,
                            "layer": parent.layer,
                        }
                    )

            member_entities = []
            if category.member_entities:
                for entity in category.member_entities:
                    member_entities.append(
                        {
                            "id": entity.id,
                            "name": entity.name,
                            "summary": entity.summary,
                            "is_speaker": entity.is_speaker,
                        }
                    )

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "id": category.id,
                    "scope_key": category.scope_key,
                    "name": category.name,
                    "summary": category.summary,
                    "tag": category.tag,
                    "layer": category.layer,
                    "created_at": str(category.created_at) if category.created_at else None,
                    "updated_at": str(category.updated_at) if category.updated_at else None,
                    "parent_categories": parent_categories,
                    "child_categories": child_categories,
                    "member_entities": member_entities,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取 Category 详情失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/memory/hiergraph/status")
async def get_hiergraph_status(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取分层语义图构建状态

    返回指定 scope 的分层图元数据，包括最大层级、上次重建时间等。

    Args:
        group_id: 群组 ID（可选）
        scope_key: 完整的 Scope Key（可选，优先级高于 group_id）

    Returns:
        status: 0成功，1失败
        data: 分层图状态信息
    """
    try:
        actual_scope_key = scope_key
        if actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)

        if actual_scope_key is None:
            return {"status": 1, "msg": "请提供 group_id 或 scope_key", "data": None}

        async with async_maker() as session:
            result = await session.execute(
                select(AIMemHierarchicalGraphMeta).where(AIMemHierarchicalGraphMeta.scope_key == actual_scope_key)
            )
            meta = result.scalar_one_or_none()

            if meta is None:
                return {
                    "status": 0,
                    "msg": "ok",
                    "data": {
                        "scope_key": actual_scope_key,
                        "initialized": False,
                        "max_layer": 0,
                        "last_rebuild_at": None,
                        "entity_count_at_last_rebuild": 0,
                        "current_entity_count": 0,
                    },
                }

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "scope_key": meta.scope_key,
                    "initialized": meta.max_layer > 0,
                    "max_layer": meta.max_layer,
                    "last_rebuild_at": str(meta.last_rebuild_at) if meta.last_rebuild_at else None,
                    "entity_count_at_last_rebuild": meta.entity_count_at_last_rebuild,
                    "current_entity_count": meta.current_entity_count,
                    "group_summary_cache": meta.group_summary_cache,
                    "group_summary_updated_at": (
                        str(meta.group_summary_updated_at) if meta.group_summary_updated_at else None
                    ),
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取分层图状态失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 6. 记忆统计 API
# ─────────────────────────────────────────────


@app.get("/api/ai/memory/stats")
async def get_memory_stats(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取记忆系统统计数据

    返回指定 scope 或全局的各类记忆节点数量统计。

    Args:
        group_id: 群组 ID（可选）
        scope_key: 完整的 Scope Key（可选，优先级高于 group_id）

    Returns:
        status: 0成功，1失败
        data: 记忆统计数据
    """
    try:
        actual_scope_key = scope_key
        if actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)

        async with async_maker() as session:
            # Episode 统计
            ep_query = select(func.count()).select_from(AIMemEpisode)
            if actual_scope_key:
                ep_query = ep_query.where(AIMemEpisode.scope_key == actual_scope_key)
            episode_count = (await session.execute(ep_query)).scalar() or 0

            # Entity 统计
            ent_query = select(func.count()).select_from(AIMemEntity)
            if actual_scope_key:
                ent_query = ent_query.where(AIMemEntity.scope_key == actual_scope_key)
            entity_count = (await session.execute(ent_query)).scalar() or 0

            # Speaker Entity 统计
            speaker_query = (
                select(func.count()).select_from(AIMemEntity).where(col(AIMemEntity.is_speaker) == True)  # noqa: E712
            )
            if actual_scope_key:
                speaker_query = speaker_query.where(AIMemEntity.scope_key == actual_scope_key)
            speaker_count = (await session.execute(speaker_query)).scalar() or 0

            # Edge 统计
            edge_query = select(func.count()).select_from(AIMemEdge)
            if actual_scope_key:
                edge_query = edge_query.where(AIMemEdge.scope_key == actual_scope_key)
            edge_count = (await session.execute(edge_query)).scalar() or 0

            # 有效 Edge 统计（invalid_at 为空）
            active_edge_query = select(func.count()).select_from(AIMemEdge).where(col(AIMemEdge.invalid_at).is_(None))
            if actual_scope_key:
                active_edge_query = active_edge_query.where(AIMemEdge.scope_key == actual_scope_key)
            active_edge_count = (await session.execute(active_edge_query)).scalar() or 0

            # Category 统计
            cat_query = select(func.count()).select_from(AIMemCategory)
            if actual_scope_key:
                cat_query = cat_query.where(AIMemCategory.scope_key == actual_scope_key)
            category_count = (await session.execute(cat_query)).scalar() or 0

            # Scope 列表（所有有记忆数据的 scope_key）
            scope_query = select(AIMemEpisode.scope_key).distinct()
            scope_result = await session.execute(scope_query)
            scope_keys = [row[0] for row in scope_result.fetchall()]

            # 观察队列状态
            from gsuid_core.ai_core.memory.observer import get_observation_queue

            queue = get_observation_queue()
            queue_size = queue.qsize()

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "scope_key": actual_scope_key,
                    "episode_count": episode_count,
                    "entity_count": entity_count,
                    "speaker_entity_count": speaker_count,
                    "edge_count": edge_count,
                    "active_edge_count": active_edge_count,
                    "category_count": category_count,
                    "observation_queue_size": queue_size,
                    "scope_keys": scope_keys,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取记忆统计失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 7. 配置管理 API
# ─────────────────────────────────────────────


@app.get("/api/ai/memory/config")
async def get_memory_config(
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取记忆系统当前配置

    Returns:
        status: 0成功，1失败
        data: 记忆系统配置项
    """
    try:
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "observer_enabled": memory_config.observer_enabled,
                "observer_blacklist": memory_config.observer_blacklist,
                "ingestion_enabled": memory_config.ingestion_enabled,
                "batch_interval_seconds": memory_config.batch_interval_seconds,
                "batch_max_size": memory_config.batch_max_size,
                "llm_semaphore_limit": memory_config.llm_semaphore_limit,
                "enable_retrieval": memory_config.enable_retrieval,
                "enable_system2": memory_config.enable_system2,
                "enable_user_global_memory": memory_config.enable_user_global_memory,
                "enable_heartbeat_memory": memory_config.enable_heartbeat_memory,
                "retrieval_top_k": memory_config.retrieval_top_k,
                "dedup_similarity_threshold": memory_config.dedup_similarity_threshold,
                "edge_conflict_threshold": memory_config.edge_conflict_threshold,
                "min_children_per_category": memory_config.min_children_per_category,
                "max_layers": memory_config.max_layers,
                "hiergraph_rebuild_ratio": memory_config.hiergraph_rebuild_ratio,
                "hiergraph_rebuild_interval_seconds": memory_config.hiergraph_rebuild_interval_seconds,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取记忆配置失败: {str(e)}",
            "data": None,
        }


@app.put("/api/ai/memory/config")
async def update_memory_config(
    req: MemoryConfigUpdateRequest,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    更新记忆系统配置

    仅更新请求中提供的字段，未提供的字段保持不变。
    配置立即生效，但不会持久化（重启后恢复默认值）。

    Args:
        req: 配置更新请求

    Returns:
        status: 0成功，1失败
        data: 更新后的完整配置
    """
    try:
        update_data = req.model_dump(exclude_none=True)

        for key, value in update_data.items():
            if hasattr(memory_config, key):
                setattr(memory_config, key, value)

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "observer_enabled": memory_config.observer_enabled,
                "observer_blacklist": memory_config.observer_blacklist,
                "ingestion_enabled": memory_config.ingestion_enabled,
                "batch_interval_seconds": memory_config.batch_interval_seconds,
                "batch_max_size": memory_config.batch_max_size,
                "llm_semaphore_limit": memory_config.llm_semaphore_limit,
                "enable_retrieval": memory_config.enable_retrieval,
                "enable_system2": memory_config.enable_system2,
                "enable_user_global_memory": memory_config.enable_user_global_memory,
                "enable_heartbeat_memory": memory_config.enable_heartbeat_memory,
                "retrieval_top_k": memory_config.retrieval_top_k,
                "dedup_similarity_threshold": memory_config.dedup_similarity_threshold,
                "edge_conflict_threshold": memory_config.edge_conflict_threshold,
                "min_children_per_category": memory_config.min_children_per_category,
                "max_layers": memory_config.max_layers,
                "hiergraph_rebuild_ratio": memory_config.hiergraph_rebuild_ratio,
                "hiergraph_rebuild_interval_seconds": memory_config.hiergraph_rebuild_interval_seconds,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"更新记忆配置失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 8. Scope 浏览 API
# ─────────────────────────────────────────────


@app.get("/api/ai/memory/scopes")
async def list_scopes(
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取所有有记忆数据的 Scope Key 列表

    返回所有存在记忆数据的 scope_key，附带各类节点的数量统计。

    Returns:
        status: 0成功，1失败
        data: Scope 列表
    """
    try:
        async with async_maker() as session:
            # 获取所有不同的 scope_key 及其 Episode 数量
            ep_result = await session.execute(
                select(AIMemEpisode.scope_key, func.count().label("episode_count")).group_by(AIMemEpisode.scope_key)
            )
            ep_stats = {row[0]: row[1] for row in ep_result.fetchall()}

            # Entity 统计
            ent_result = await session.execute(
                select(AIMemEntity.scope_key, func.count().label("entity_count")).group_by(AIMemEntity.scope_key)
            )
            ent_stats = {row[0]: row[1] for row in ent_result.fetchall()}

            # Edge 统计
            edge_result = await session.execute(
                select(AIMemEdge.scope_key, func.count().label("edge_count")).group_by(AIMemEdge.scope_key)
            )
            edge_stats = {row[0]: row[1] for row in edge_result.fetchall()}

            # Category 统计
            cat_result = await session.execute(
                select(AIMemCategory.scope_key, func.count().label("category_count")).group_by(AIMemCategory.scope_key)
            )
            cat_stats = {row[0]: row[1] for row in cat_result.fetchall()}

            # 合并所有 scope_key
            all_scope_keys = (
                set(ep_stats.keys()) | set(ent_stats.keys()) | set(edge_stats.keys()) | set(cat_stats.keys())
            )

            scopes = []
            for sk in sorted(all_scope_keys):
                scope_type = ""
                scope_id = ""
                if ":" in sk:
                    parts = sk.split(":", 1)
                    scope_type = parts[0]
                    scope_id = parts[1] if len(parts) > 1 else ""

                scopes.append(
                    {
                        "scope_key": sk,
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "episode_count": ep_stats.get(sk, 0),
                        "entity_count": ent_stats.get(sk, 0),
                        "edge_count": edge_stats.get(sk, 0),
                        "category_count": cat_stats.get(sk, 0),
                    }
                )

            return {
                "status": 0,
                "msg": "ok",
                "data": scopes,
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取 Scope 列表失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 9. 批量删除 API
# ─────────────────────────────────────────────


@app.delete("/api/ai/memory/scopes/{scope_key}")
async def delete_scope_memory(
    scope_key: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    删除指定 Scope 下的所有记忆数据

    包括 Episode、Entity、Edge、Category 及其 Qdrant 向量。
    此操作不可逆，请谨慎使用。

    Args:
        scope_key: 完整的 Scope Key

    Returns:
        status: 0成功，1失败
        data: 删除的记录数量统计
    """
    try:
        async with async_maker() as session:
            # 统计待删除数量
            ep_count = (
                await session.execute(
                    select(func.count()).select_from(AIMemEpisode).where(AIMemEpisode.scope_key == scope_key)
                )
            ).scalar() or 0
            ent_count = (
                await session.execute(
                    select(func.count()).select_from(AIMemEntity).where(AIMemEntity.scope_key == scope_key)
                )
            ).scalar() or 0
            edge_count = (
                await session.execute(
                    select(func.count()).select_from(AIMemEdge).where(AIMemEdge.scope_key == scope_key)
                )
            ).scalar() or 0
            cat_count = (
                await session.execute(
                    select(func.count()).select_from(AIMemCategory).where(AIMemCategory.scope_key == scope_key)
                )
            ).scalar() or 0

            # 删除 Qdrant 向量（按 scope_key 过滤）
            try:
                from qdrant_client.models import Filter, MatchValue, FieldCondition

                from gsuid_core.ai_core.rag.base import client
                from gsuid_core.ai_core.memory.vector.collections import (
                    MEMORY_EDGES_COLLECTION,
                    MEMORY_ENTITIES_COLLECTION,
                    MEMORY_EPISODES_COLLECTION,
                )

                if client is not None:
                    for collection in [
                        MEMORY_EPISODES_COLLECTION,
                        MEMORY_ENTITIES_COLLECTION,
                        MEMORY_EDGES_COLLECTION,
                    ]:
                        try:
                            await client.delete(
                                collection_name=collection,
                                points_selector=Filter(
                                    must=[FieldCondition(key="scope_key", match=MatchValue(value=scope_key))]
                                ),
                            )
                        except Exception:
                            pass
            except Exception:
                pass

            # 删除数据库记录（按依赖顺序）
            # 1. 获取该 scope 下的 category IDs
            cat_ids_result = await session.execute(select(AIMemCategory.id).where(AIMemCategory.scope_key == scope_key))
            cat_ids = [row[0] for row in cat_ids_result.fetchall()]

            if cat_ids:
                # 删除 CategoryEdge 关联
                await session.execute(
                    delete(AIMemCategoryEdge).where(col(AIMemCategoryEdge.parent_category_id).in_(cat_ids))
                )
                await session.execute(
                    delete(AIMemCategoryEdge).where(col(AIMemCategoryEdge.child_category_id).in_(cat_ids))
                )
                # 删除 Category-Entity 关联
                await session.execute(
                    mem_category_entity_members.delete().where(mem_category_entity_members.c.category_id.in_(cat_ids))
                )

            # 2. 获取该 scope 下的 entity IDs
            ent_ids_result = await session.execute(select(AIMemEntity.id).where(AIMemEntity.scope_key == scope_key))
            ent_ids = [row[0] for row in ent_ids_result.fetchall()]

            if ent_ids:
                # 删除 Episode-Entity 关联
                await session.execute(
                    mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.entity_id.in_(ent_ids))
                )

            # 3. 获取该 scope 下的 episode IDs
            ep_ids_result = await session.execute(select(AIMemEpisode.id).where(AIMemEpisode.scope_key == scope_key))
            ep_ids = [row[0] for row in ep_ids_result.fetchall()]

            if ep_ids:
                await session.execute(
                    mem_episode_entity_mentions.delete().where(mem_episode_entity_mentions.c.episode_id.in_(ep_ids))
                )

            # 4. 删除主表记录
            await session.execute(delete(AIMemEdge).where(col(AIMemEdge.scope_key == scope_key)))
            await session.execute(delete(AIMemEpisode).where(col(AIMemEpisode.scope_key == scope_key)))
            await session.execute(delete(AIMemEntity).where(col(AIMemEntity.scope_key == scope_key)))
            await session.execute(delete(AIMemCategory).where(col(AIMemCategory.scope_key == scope_key)))

            # 5. 删除分层图元数据
            await session.execute(
                delete(AIMemHierarchicalGraphMeta).where(col(AIMemHierarchicalGraphMeta.scope_key == scope_key))
            )

            await session.commit()

            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "scope_key": scope_key,
                    "deleted_episodes": ep_count,
                    "deleted_entities": ent_count,
                    "deleted_edges": edge_count,
                    "deleted_categories": cat_count,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"删除 Scope 记忆失败: {str(e)}",
            "data": None,
        }
