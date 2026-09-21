"""Phase 7：用近邻余弦近似「可预测性」，已知内容从 HIGH 降为 LOW。"""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.memory.lifecycle.sleep_extract import cosine_dense


async def should_downgrade_high(scope_key: str, content: str) -> bool:
    """最近 K 条 Episode 向量里有一条足够像，就不必再抽实体/边。"""
    if not scope_key or not (content or "").strip():
        return False
    if memory_config.eval_mode:
        return False
    from gsuid_core.ai_core.memory.vector.ops import embed_query, retrieve_episode_dense_vectors
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    try:
        recent = await AIMemEpisode.recent_ids(scope_key, limit=8)
        if not recent:
            return False
        vecs = await retrieve_episode_dense_vectors(recent)
        if not vecs:
            return False
        qv = await embed_query(content)
        if not qv:
            return False
        best = 0.0
        for vec in vecs.values():
            sim = cosine_dense(qv, vec)
            if sim > best:
                best = sim
        return best >= memory_config.predict_calibrate_threshold
    except (TimeoutError, OSError, RuntimeError, SQLAlchemyError, ConnectionError):
        return False
