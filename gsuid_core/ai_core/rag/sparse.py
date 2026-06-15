"""稀疏向量（BM25）嵌入共享工具

把"jieba 中文预分词 + fastembed BM25 稀疏嵌入"收敛为可复用的公共函数，供需要
"稀疏 + 稠密"混合检索的模块（表情包等）共用，保证**写入与查询两侧使用同一分词**、
token 一致。

设计沿用 rag/knowledge.py 的成熟实现：
- BM25 模型来自 ``rag.base._get_sparse_model``（全局懒加载、线程安全）；
- jieba 预分词解决 fastembed 的 BM25 SimpleTokenizer 只按非 ``\\w`` 切分、会把连续中文
  整句切成"一个巨型 token"导致与库内词条永不匹配的问题；
- 同步 BM25 计算移入单线程执行器：ONNX Runtime 自带多线程，多 Python 线程会过度订阅
  反而更慢，故 max_workers=1。
"""

import asyncio
import logging
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor

from qdrant_client.models import SparseVector

from gsuid_core.logger import logger

# BM25 稀疏嵌入专用单线程执行器
_SPARSE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sparse_embed")

# jieba 中文预分词状态：None=未尝试 / True=可用 / False=不可用（避免每次调用重复 import 与告警）
_jieba_state: Optional[bool] = None


def _ensure_jieba() -> bool:
    """惰性初始化 jieba（首次调用建词典，可能数百 ms），并抑制其首次构建的 info 噪声。"""
    global _jieba_state
    if _jieba_state is not None:
        return _jieba_state
    try:
        import jieba

        jieba.setLogLevel(logging.WARNING)
        _jieba_state = True
    except Exception as e:
        logger.warning(f"🧠 [Sparse] jieba 不可用，BM25 退化为不分词（中文匹配受限）: {e}")
        _jieba_state = False
    return _jieba_state


def jieba_segment(text: str) -> str:
    """jieba 中文预分词：切词后以空格连接，喂给 BM25 即可按词匹配；不可用/失败时原样返回。"""
    if not text or not _ensure_jieba():
        return text
    try:
        import jieba

        tokens = [t for t in jieba.lcut(text) if t and not t.isspace()]
        return " ".join(tokens) if tokens else text
    except Exception:
        return text


def sparse_embed_batch(texts: List[str]) -> List[Optional[SparseVector]]:
    """同步批量生成 BM25 稀疏向量；模型不可用/失败时返回等长 None（调用方自动降级纯 dense）。"""
    from gsuid_core.ai_core.rag.base import _get_sparse_model

    model = _get_sparse_model()
    if model is None:
        return [None] * len(texts)
    try:
        seg_texts = [jieba_segment(t) for t in texts]
        results = list(model.embed(seg_texts))
        return [SparseVector(indices=r.indices.tolist(), values=r.values.tolist()) for r in results]
    except Exception as e:
        logger.warning(f"🧠 [Sparse] BM25 稀疏嵌入失败，本批降级纯 dense: {e}")
        return [None] * len(texts)


async def sparse_embed_batch_async(texts: List[str]) -> List[Optional[SparseVector]]:
    """异步包装：把同步 BM25 计算移入单线程执行器，避免阻塞事件循环。"""
    if not texts:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_SPARSE_EXECUTOR, sparse_embed_batch, texts)


async def sparse_embed_single(text: str) -> Optional[SparseVector]:
    """异步生成单条 BM25 稀疏向量；不可用时返回 None。"""
    results = await sparse_embed_batch_async([text])
    return results[0] if results else None
