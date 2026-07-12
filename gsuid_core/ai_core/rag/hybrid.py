"""稠密 + 稀疏（BM25）混合检索的统一入口。

把 ``knowledge`` / ``memory`` / ``meme`` 三处几乎逐字重复的检索机制收敛为单一函数
``hybrid_query``：

- Dense + Sparse 的 Qdrant 原生 **RRF 融合**；
- 稀疏向量不可用时**自动降级纯 dense**（命名向量用 ``dense_using``）；
- 集合向量结构/维度异常（启动迁移未完成）→ **降级空结果**，待重嵌后自动恢复；
- 本地 Qdrant 索引崩溃（``IndexError``）→ 记 critical 并降级空结果。

**职责边界**：调用方负责查询文本的 dense/sparse 嵌入、``Filter`` 构造、阈值语义、
rerank、统计与 ``payload`` → 领域对象映射；本函数只负责"把已算好的查询向量按 RRF
打到 Qdrant 并稳健降级"，返回原始 ``ScoredPoint`` 列表。

阈值语义：``dense_score_threshold`` 只作用于 **dense 分支**（纯 dense 查询时作为
``score_threshold``；混合时下到 dense 那条 ``Prefetch``）。RRF 融合分是名次分而非余弦，
不能再用余弦阈值硬筛；sparse 词项重合即强相关，无需余弦门。
"""

from typing import List, Union, Optional

from qdrant_client.models import (
    Filter,
    Fusion,
    Prefetch,
    FusionQuery,
    SparseVector,
)
from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.i18n import t
from gsuid_core.logger import logger


async def hybrid_query(
    collection_name: str,
    query_dense: List[float],
    query_sparse: Optional[SparseVector],
    *,
    limit: int,
    dense_using: str = "dense",
    sparse_using: str = "sparse",
    query_filter: Optional[Filter] = None,
    dense_score_threshold: Optional[float] = None,
    prefetch_multiplier: int = 2,
    extra_prefetch: Optional[List[Prefetch]] = None,
    with_payload: Union[bool, List[str]] = True,
    with_vectors: Union[bool, List[str]] = False,
) -> List[ScoredPoint]:
    """Dense + Sparse(BM25) 的 Qdrant 原生 RRF 混合检索，带统一降级。

    Args:
        collection_name: 目标集合。
        query_dense: 查询稠密向量（调用方已嵌入）。
        query_sparse: 查询 BM25 稀疏向量；``None`` 时降级纯 dense。
        limit: 返回数量。
        dense_using: dense 命名向量名（知识库 ``"dense"``、记忆实体 ``"summary_dense"``、
            表情 ``MEME_DENSE_VECTOR`` 等）。
        sparse_using: sparse 命名向量名，默认 ``"sparse"``。
        query_filter: 服务端下推过滤；dense 与 sparse 两条 ``Prefetch`` 共用同一过滤。
        dense_score_threshold: 仅作用于 dense 分支的余弦门（见模块文档）；``None`` 不设门。
        prefetch_multiplier: 每条 ``Prefetch`` 的取回倍数（默认 2，即 ``limit*2``）。
        extra_prefetch: 额外的 ``Prefetch`` 分支（如素材库的 ``clip_image`` 多向量视觉召回），
            会一并参与 RRF 融合。提供它时即使 ``query_sparse`` 为 ``None`` 也走融合路径。
        with_payload / with_vectors: 透传给 Qdrant。

    Returns:
        原始 ``ScoredPoint`` 列表；client 未就绪或降级时返回 ``[]``。
    """
    from gsuid_core.ai_core.rag.base import client
    from gsuid_core.ai_core.rag.collection_migration import is_vector_structure_error

    if client is None:
        return []

    # 纯 dense：无稀疏向量且无额外召回分支时，单路 dense 查询（命名向量需指定 using）。
    use_pure_dense = query_sparse is None and not extra_prefetch

    try:
        if use_pure_dense:
            response = await client.query_points(
                collection_name=collection_name,
                query=query_dense,
                using=dense_using,
                query_filter=query_filter,
                limit=limit,
                score_threshold=dense_score_threshold,
                with_payload=with_payload,
                with_vectors=with_vectors,
            )
        else:
            prefetch: List[Prefetch] = [
                Prefetch(
                    query=query_dense,
                    using=dense_using,
                    filter=query_filter,
                    score_threshold=dense_score_threshold,
                    limit=limit * prefetch_multiplier,
                )
            ]
            if query_sparse is not None:
                prefetch.append(
                    Prefetch(
                        query=query_sparse,
                        using=sparse_using,
                        filter=query_filter,
                        limit=limit * prefetch_multiplier,
                    )
                )
            if extra_prefetch:
                prefetch.extend(extra_prefetch)
            response = await client.query_points(
                collection_name=collection_name,
                prefetch=prefetch,
                query=FusionQuery(fusion=Fusion.RRF),
                limit=limit,
                with_payload=with_payload,
                with_vectors=with_vectors,
            )
    except IndexError as e:
        # 本地 Qdrant 索引长度不同步会抛 IndexError，是存储级损坏，非业务错误。
        logger.critical(
            t(
                "🧠 [Hybrid] 集合 {collection_name} 本地索引崩溃: {e}。建议删除本地存储目录并重启。",
                collection_name=collection_name,
                e=e,
            )
        )
        return []
    except Exception as e:
        # 启动迁移尚未完成时，旧无名集合上的命名向量查询会抛结构错误：降级空结果，待重嵌后恢复。
        # 仅对"向量结构/维度异常"降级，其余异常照常抛出（不做无差别兜底）。
        if is_vector_structure_error(str(e)):
            logger.warning(
                t(
                    "🧠 [Hybrid] 集合 {collection_name} 向量结构/维度异常（疑似迁移未完成），本次检索降级为空: {e}",
                    collection_name=collection_name,
                    e=e,
                )
            )
            return []
        raise

    return response.points
