"""Edge 时效写入模块

处理 LLM 提取的 Edge，检测语义冲突后将旧 Edge 标记为过期，
写入新 Edge 到数据库和向量库。
"""

import re
import uuid
import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel import col, select
from sqlalchemy.exc import OperationalError

from gsuid_core.i18n import t
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.memory.vector.ops import search_edges, upsert_edge_vectors_batch
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemConflict

logger = logging.getLogger(__name__)

# §6 残句拦截：句末裸言说/认知动词=缺宾语的悬空谓语（"用户X提到"），摄入/注入两侧共用。
# 动名兼类词（讨论/回复/评价…）须带体标记"了/过"才判悬空，防误杀名词用法（评审修复 F13）。
_DANGLING_FACT_RE = re.compile(
    r"(?:(?:提到|提及|被提及|表示|认为|觉得|提出|指出|强调|透露|说道|谈到|聊到|说起|问到|询问|说)[了过]?"
    r"|(?:讨论|谈论|回复|回应|回答|评价|分享|补充|吐槽|感叹)[了过])"
    r"[。.!！?？…]?$"
)

# C11 矛盾检测：否定极性标记词。两条同 src/tgt 的高相似 fact 若极性相反，
# 视为"语义矛盾"而非"重复陈述"——按时效以新事实为准，旧事实软删除并记录冲突。
_NEGATION_MARKERS = ("不", "没", "无", "非", "别", "讨厌", "拒绝", "反对", "停止")

# 英文否定：词边界匹配，避免 note/nothing→"not"、"knows"→"no" 类误命中。
# （BEAM 教训：仅中文标记时英文语料 "never/not/n't" 全漏检，C11 矛盾引擎从未触发。）
_NEGATION_RE_EN = re.compile(
    r"\b(never|not|no|none|without|refuse[sd]?|den(?:y|ies|ied)|stopped)\b|n't\b",
    re.IGNORECASE,
)


def _fact_polarity(fact: str) -> bool:
    """粗判 fact 的否定极性：含奇数个否定标记 → True（否定句）。"""
    hits = sum(fact.count(m) for m in _NEGATION_MARKERS)
    hits += len(_NEGATION_RE_EN.findall(fact))
    return hits % 2 == 1


def _norm_fact(fact: str) -> str:
    """fact 归一化签名：去空白 + 小写，用于 eval 模式下零向量的精确重复判定。"""
    return fact.strip().lower().replace(" ", "")


async def _eval_find_mergeable_edges(
    scope_key: str,
    valid_edges: list[tuple[dict, str, str, str]],
) -> list[str]:
    """eval_mode 专用：一次 SQL 预取本批 (src,tgt) 对应的既有有效边，在内存里判定每条新边
    可归并到的既有边 id（精确重复→归并；同 (src,tgt) 极性相反→矛盾），无则空串。零向量检索。
    """
    if not valid_edges:
        return []
    src_ids = {sid for _, sid, _, _ in valid_edges}
    tgt_ids = {tid for _, _, tid, _ in valid_edges}
    by_pair: dict[tuple[str, str], list[AIMemEdge]] = {}
    async with async_maker() as session:
        result = await session.execute(
            select(AIMemEdge).where(
                AIMemEdge.scope_key == scope_key,
                col(AIMemEdge.invalid_at).is_(None),
                col(AIMemEdge.source_entity_id).in_(src_ids),
                col(AIMemEdge.target_entity_id).in_(tgt_ids),
            )
        )
        for e in result.scalars().all():
            by_pair.setdefault((e.source_entity_id, e.target_entity_id), []).append(e)

    out: list[str] = []
    for _, sid, tid, fact in valid_edges:
        cands = by_pair.get((sid, tid), [])
        mid = ""
        if cands:
            new_norm = _norm_fact(fact)
            # ① 精确重复 fact → 归并（mention++）
            for c in cands:
                if _norm_fact(c.fact) == new_norm:
                    mid = c.id
                    break
            # ② 同 (src,tgt) 极性相反 → 矛盾（下游会失效旧边 + 记 Conflict）
            if not mid:
                new_pol = _fact_polarity(fact)
                for c in cands:
                    if _fact_polarity(c.fact) != new_pol:
                        mid = c.id
                        break
        out.append(mid)
    return out


async def extract_and_upsert_edges(
    scope_key: str,
    edges_data: list[dict],
    entity_name_to_id: dict[str, str],
    valid_at: "datetime | None" = None,
):
    """处理 LLM 提取的 Edge，检测冲突后写入。

    所有 Edge 在同一个 session 中处理，避免每条 Edge 独立打开/关闭连接。
    冲突检测的向量搜索在 session 外并行执行，结果在 session 内统一写入。

    Args:
        scope_key: 作用域标识
        edges_data: LLM 提取的 Edge 数据列表
        entity_name_to_id: {entity_name: entity_id} 映射
        valid_at: 事实的"陈述时间"。回放/评测语料带真实对话时间戳时必须传入，
            否则边的 valid_at 落成抽取时刻，"取最新值"类时序推理会被抽取顺序污染。
            缺省仍用当前时间（线上实时摄入语义不变）。
    """
    now = valid_at or datetime.now(timezone.utc)
    threshold = memory_config.edge_conflict_threshold

    # 预处理：过滤有效 Edge 并收集 fact 列表
    valid_edges: list[tuple[dict, str, str, str]] = []  # (edge_data, source_id, target_id, fact)
    for edge_data in edges_data:
        source_name = edge_data["source"] if "source" in edge_data else ""
        target_name = edge_data["target"] if "target" in edge_data else ""
        fact = edge_data["fact"].strip() if "fact" in edge_data and edge_data["fact"] else ""
        if not fact:
            continue
        # §6 残句拦截（摄入侧）：悬空谓语结尾的 fact（"用户X提到"）零信息量，
        # 不入库——与注入侧同判据，源头止血。
        if _DANGLING_FACT_RE.search(fact):
            logger.debug(t("🧠 [Memory] 摄入拦截残句 fact: {fact}", fact=fact))
            continue
        source_id = entity_name_to_id[source_name] if source_name in entity_name_to_id else None
        target_id = entity_name_to_id[target_name] if target_name in entity_name_to_id else None
        if not source_id or not target_id:
            continue
        valid_edges.append((edge_data, source_id, target_id, fact))

    if not valid_edges:
        return

    # C1 跨发言者归并：并行检索语义等价的既有 Edge（session 外执行，避免长时间持连接）。
    # 同一 fact（相似度≥阈值）被不同 source 重复陈述时，归并到既有 Edge 并累加
    # mention_count，而不再写入 N 条重复 Edge + 软删除。
    async def _find_mergeable_edge(fact: str, source_id: str, target_id: str) -> str:
        """返回可归并到的既有有效 Edge ID（同 src/tgt 且语义≥阈值），无则返回空串。"""
        try:
            similar_edges = await search_edges(fact, [scope_key], top_k=3)
        except Exception:
            return ""
        for sim_edge in similar_edges:
            if (
                sim_edge["score"] >= threshold
                and sim_edge["source_id"] == source_id
                and sim_edge["target_id"] == target_id
                and sim_edge["invalid_at_ts"] is None
            ):
                return sim_edge["id"]
        return ""

    if memory_config.eval_mode:
        # §14 大规模回灌优化：eval_mode 下用一次"按 (src,tgt) 预取既有有效边"的 SQL 替代
        # 每条边一次向量检索（search_edges = embed+Qdrant，窗口化并发下是主要耗时来源之一）。
        # 归并判定：①新 fact 与既有 fact 归一化后完全相同 → 归并(mention++)；②同 (src,tgt)
        # 但极性相反 → 矛盾(失效旧边+记 Conflict)；③其余（同 (src,tgt) 不同 fact，如版本更新）
        # → 不归并，两条都保留为有效边（检索期由"取最新值"提示择新，优于旧逻辑把版本更新误并）。
        # 不做语义近似归并（省去向量检索），代价仅是近义重复事实多留几条，对 BEAM 探针无碍。
        merge_results = await _eval_find_mergeable_edges(scope_key, valid_edges)
    else:
        merge_results = await asyncio.gather(
            *[_find_mergeable_edge(fact, sid, tid) for _, sid, tid, fact in valid_edges]
        )

    # 统一在一个 session 中写入所有 Edge。
    # OperationalError（"database is locked"）重试：SQLite 单写者 + 大库 WAL 检查点在高并发
    # 回灌下偶发写锁超时；重试而非放弃，杜绝丢窗口边（§14）。每次重试重置累积态避免重复。
    edges_vector_data: list[dict] = []
    merged_count = 0
    from gsuid_core.ai_core.memory.ingestion.eval_write_lock import eval_write_guard

    for _attempt in range(6):
        edges_vector_data = []
        merged_count = 0
        try:
            # eval_mode 下与 entity 写共用进程内写锁，串行化 SQLite 写事务、消除并发忙等。
            async with eval_write_guard(), async_maker() as session:
                for i, (edge_data, source_id, target_id, fact) in enumerate(valid_edges):
                    merge_into = merge_results[i]
                    if merge_into:
                        result = await session.execute(select(AIMemEdge).where(col(AIMemEdge.id) == merge_into))
                        old_edge = result.scalar_one_or_none()
                        if old_edge is not None:
                            if _fact_polarity(old_edge.fact) != _fact_polarity(fact):
                                # C11 语义矛盾：同 src/tgt 高相似但极性相反 → 以新事实为准，
                                # 旧事实软删除 + 记录 AIMemConflict（不在普通回复中堆叠新旧矛盾）。
                                old_edge.invalid_at = now
                                await AIMemConflict.record(
                                    scope_key=scope_key,
                                    fact_signature=f"{source_id}|{target_id}",
                                    old_edge_id=old_edge.id,
                                    new_edge_id="",
                                    summary=f"[事实更新] 旧:{old_edge.fact[:120]} → 新:{fact[:120]}",
                                )
                            else:
                                # 命中既有等价 Edge：累加提及次数并刷新有效期，不写重复 Edge
                                old_edge.mention_count = (old_edge.mention_count or 1) + 1
                                old_edge.valid_at = now
                                merged_count += 1
                                continue

                    # 创建新 Edge
                    edge_id = str(uuid.uuid4())
                    new_edge = AIMemEdge(
                        id=edge_id,
                        scope_key=scope_key,
                        fact=fact,
                        source_entity_id=source_id,
                        target_entity_id=target_id,
                        valid_at=now,
                        qdrant_id=edge_id,
                        mention_count=1,
                    )
                    session.add(new_edge)

                    # 收集向量写入数据（session 外批量执行）
                    edges_vector_data.append(
                        {
                            "edge_id": edge_id,
                            "fact": fact,
                            "scope_key": scope_key,
                            "valid_at_ts": now.timestamp(),
                            "invalid_at_ts": None,
                            "source_entity_id": source_id,
                            "target_entity_id": target_id,
                        }
                    )

                await session.commit()
            break
        except OperationalError:
            if _attempt < 5:
                await asyncio.sleep(0.1 * (_attempt + 1))
                continue
            logger.warning(
                t(
                    "🧠 [Memory] scope={scope_key} Edge 写入重试 6 次仍失败（database locked），跳过本窗口边",
                    scope_key=scope_key,
                )
            )
            return

    if merged_count:
        logger.info(
            t(
                "🧠 [Memory] scope={scope_key} Edge 归并 {merged_count} 条重复事实",
                scope_key=scope_key,
                merged_count=merged_count,
            )
        )

    # 批量写入所有 Qdrant 向量（无锁并发计算 + 单次批量加锁写入）
    if edges_vector_data:
        try:
            await upsert_edge_vectors_batch(edges_vector_data)
        except Exception as e:
            logger.warning(f"Edge vector batch upsert failed: {e}")
