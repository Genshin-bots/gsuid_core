"""纯 Python CJK 二元语法 BM25 稀疏向量工具。

复用 ``tool_lexical.tokenize``（中文单字 + 相邻二元语法 + 拉丁/数字词）与标准 BM25 词频公式
生成 Qdrant ``SparseVector``，配合服务端 ``modifier="idf"`` 实现混合检索。
不依赖 ``fastembed`` / ``onnxruntime`` / ``jieba``，写作与查询共用同一路径，零模型常驻内存。
"""

from __future__ import annotations

import hashlib
from collections import Counter

from gsuid_core.ai_core.rag.qmodels import SparseVector
from gsuid_core.ai_core.rag.tool_lexical import tokenize

_BM25_K1: float = 1.2
_BM25_B: float = 0.75
_BM25_AVG_LEN: float = 256.0
_INDEX_MASK: int = 0x7FFFFFFF


def _token_index(token: str) -> int:
    """将词元确定性映射到 31 位非负整数槽位，跨进程/跨平台一致。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "little") & _INDEX_MASK


def _embed_one(text: str) -> SparseVector:
    tokens = tokenize(text)
    if not tokens:
        return SparseVector(indices=[], values=[])

    counts = Counter(tokens)
    doc_len = float(len(tokens))
    denom_base = _BM25_K1 * (1.0 - _BM25_B + _BM25_B * doc_len / _BM25_AVG_LEN)
    tf_map: dict[int, float] = {}
    for token, raw_freq in counts.items():
        idx = _token_index(token)
        freq = float(raw_freq)
        tf = freq * (_BM25_K1 + 1.0) / (freq + denom_base)
        # 极低概率哈希碰撞时累加词频得分，保持索引唯一有序。
        tf_map[idx] = tf_map[idx] + tf if idx in tf_map else tf

    ordered = sorted(tf_map.items())
    return SparseVector(
        indices=[idx for idx, _ in ordered],
        values=[val for _, val in ordered],
    )


def sparse_embed_batch(texts: list[str]) -> list[SparseVector | None]:
    """同步批量生成 BM25 稀疏向量；AI 总开关关闭时返回等长 None。"""
    if not texts:
        return []
    from gsuid_core.ai_core.rag.base import is_enable_ai

    if not is_enable_ai():
        return [None] * len(texts)
    return [_embed_one(text) for text in texts]


async def sparse_embed_batch_async(texts: list[str]) -> list[SparseVector | None]:
    """异步批量生成 BM25 稀疏向量（纯内存轻量分词，直接返回不额外养线程池）。"""
    return sparse_embed_batch(texts)


async def sparse_embed_single(text: str) -> SparseVector | None:
    """异步生成单条 BM25 稀疏向量。"""
    results = sparse_embed_batch([text])
    return results[0] if results else None
