"""
AI Memory APIs
提供记忆系统（Mnemis 双路检索）相关的 RESTful APIs

包括记忆检索、Episode/Entity/Edge 浏览、分层语义图查看、配置管理、统计信息等。
"""

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Literal, Optional
from datetime import datetime, timezone

from fastapi import Depends
from pydantic import Field, BaseModel
from sqlmodel import col, func, select
from sqlalchemy import or_, and_
from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory import (
    parse_iso_or_unix_timestamp,
)
from gsuid_core.ai_core.text_chunk import chunk_text as _chunk_text
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.fts import ensure_fts_table
from gsuid_core.webconsole._local_test_gate import (
    LOCAL_TEST_MODE,
    require_local_test,
    require_auth_or_local_test,
)
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemThread,
    AIMemEpisode,
    AIMemPreference,
)
from gsuid_core.ai_core.memory.database.clear_ops import (
    clear_group_memories,
    clear_user_global_memories,
    clear_memories_for_scope_async,
)

from ._api_tags import AI_MEMORY

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.observer import ObservationRecord

# WebConsole 端点捕获的"合法运行时/DB 故障"：这些应转成 status=1 返回给前端
# 而**不**包括编程错误（KeyError/AttributeError/TypeError/NameError 等）——后者应
_RUNTIME_ERRORS = (SQLAlchemyError, asyncio.TimeoutError, OSError, ValueError)

_VIRTUAL_L1: tuple[tuple[str, str, str], ...] = (
    ("virt:L1:attribute", "人物与实体属性", "attribute"),
    ("virt:L1:relation", "跨实体关系网", "relation"),
    ("virt:L1:milestone", "关键事件里程碑", "milestone"),
    ("virt:L1:conflict", "待核实传闻与冲突", "conflict"),
)

# Pydantic 请求模型


class MemorySearchRequest(BaseModel):
    """记忆检索请求"""

    query: str = Field(..., min_length=1, max_length=2000, description="查询文本")
    group_id: Optional[str] = Field(None, min_length=1, max_length=128, description="群组 ID")
    user_id: str = Field(..., max_length=64, description="用户 ID（可选，用于联合用户全局记忆）")
    top_k: int = Field(default=10, ge=1, le=50, description="返回结果数量上限")
    enable_system2: bool = Field(default=False, description="兼容字段（schema_v2 已无图遍历）")
    enable_user_global: bool = Field(default=False, description="是否联合查询用户跨群画像")
    memory_intent: str = Field(
        default="point_latest",
        description="none / point_latest / version_diff / timeline_order / broad_summary",
    )


class BatchObserveTurn(BaseModel):
    """批量摄入单条 turn"""

    role: Literal["user", "assistant"] = Field(..., description="发言者角色")
    content: str = Field(..., min_length=1, description="消息原文")
    timestamp: Optional[str] = Field(
        default=None,
        max_length=64,
        description="可选时间戳（ISO8601 / Unix 数字），用于回填历史对话的时序",
    )


class BatchObserveRequest(BaseModel):
    """批量摄入请求（评测 / 回灌场景）。

    与 ``/api/chat_with_history`` 行为上的差异：
    - 不创建 Agent、不调用 LLM 生成回答，纯摄入；
    - 接受 turn 级 ``timestamp`` 用于回填事件时间；
    - 单次可投递大量 turn（受 ``_observation_queue`` maxsize=10000 约束，建议 < 5k/批）；
    - 末尾可选 ``flush`` + ``trigger_rebuild``，二者均**同步等待**完成后才返回。

    注意：
    - 仅支持 ``scope_type=user_global | group``；``user_in_group`` 暂未对接。
    - ``role=user`` 与 ``role=assistant`` 的 turn **同样**摄入到 ``scope_type`` 对应的
      user_global/group scope 并参与实体/边抽取（回放语料里的 assistant 是对话内容，
      不是本机 Bot 戏言，不走 C6 的 SELF 轻量路由——否则半数事实被跳过抽取、探针召回不到）。
    - 端点默认 404，需开启 ``GSUID_LOCAL_TEST_MODE=1`` 才能调用（与 chat_with_history 对齐）；
      若设了 ``GSUID_LOCAL_TEST_TOKEN``，还需带 ``X-Local-Test-Token`` 请求头。
    """

    user_id: str = Field(..., min_length=1, max_length=64, description="用户 ID（user_global 场景的主键）")
    scope_type: str = Field(
        default="user_global",
        description="scope 类型：user_global / group",
    )
    group_id: Optional[str] = Field(default=None, max_length=64, description="scope_type=group 时必填")
    bot_self_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Bot 自 ID；为空时默认 'ai'；用于 SELF scope 与黑名单匹配",
    )
    observer_blacklist: Optional[List[str]] = Field(default=None, description="群组黑名单")
    turns: List[BatchObserveTurn] = Field(..., min_length=1, description="待摄入 turn 列表")
    flush: bool = Field(default=True, description="末尾是否调用 worker.flush_all() 同步等待摄入完成")
    trigger_rebuild: bool = Field(default=False, description="flush 后是否同步等待 hiergraph 重建完成")

    # ── §14 全量图谱构建：Episode 粒度 / 抽取批次粒度解耦 ──────────
    write_episodes: bool = Field(
        default=True,
        description=(
            "是否写入 granular Episode（每 turn 一条小 Episode + 嵌入）。"
            "图谱评测对已摄入 Episode 的 scope 只补抽取时可置 False，避免重复嵌入、"
            "也规避 §5 的高并发重嵌入丢向量风险。"
        ),
    )
    extract: bool = Field(
        default=False,
        description=(
            "是否对本批 turn 做窗口化实体/边抽取（§14.1）。开启后按连续 turn 拼窗口、"
            "每窗口一次 LLM 抽取，复用 worker._extract_and_upsert_from_episode 下游写实体/边；"
            "**不**复活 observer 队列 + 80-turn 聚合路径。建议配 eval_mode=true，抽取后统一 rebuild。"
        ),
    )
    extract_window_chars: int = Field(
        default=12000,
        ge=1000,
        le=14000,
        description="单个抽取窗口的字符上限（<_llm_extract 的 14000 单次阈值，保证一窗口一次 LLM 调用）",
    )
    extract_window_turns: int = Field(
        default=20,
        ge=1,
        le=200,
        description="单个抽取窗口的最大 turn 数（与字符上限取先到者）",
    )
    extract_window_timeout: float = Field(
        default=300.0,
        ge=30.0,
        le=1800.0,
        description="单窗口抽取宽松超时（秒）。超时仅记录并跳过该窗口，不丢整 plan、不取消父任务（§14.2）",
    )
    extract_concurrency: int = Field(
        default=0,
        ge=0,
        le=16,
        description="窗口抽取并发上限；0 表示用 memory_config.llm_semaphore_limit",
    )
    extract_max_windows: int = Field(
        default=0,
        ge=0,
        description="仅抽取前 N 个窗口（0=不限）；子集快速验证用，避免一上来就跑全量",
    )


class MemoryClearRequest(BaseModel):
    """清空记忆请求"""

    scope_key: Optional[str] = Field(None, max_length=128, description="精确匹配的 Scope Key，如 group:789012")
    scope_pattern: Optional[str] = Field(
        None,
        max_length=128,
        description="前缀匹配的 Scope Key，如 group:789012 会匹配 group:789012 及 group:789012@...",
    )
    dry_run: bool = Field(default=False, description="是否仅统计数量而不实际删除")


class MemoryConfigUpdateRequest(BaseModel):
    """记忆系统配置更新请求"""

    observer_enabled: Optional[bool] = None
    observer_blacklist: Optional[List[str]] = None
    ingestion_enabled: Optional[bool] = None
    batch_interval_seconds: Optional[int] = Field(default=None, ge=60, le=86400)
    batch_max_size: Optional[int] = Field(default=None, ge=5, le=100)
    llm_semaphore_limit: Optional[int] = Field(default=None, ge=1, le=10)
    idle_flush_seconds: Optional[int] = Field(
        default=None,
        ge=0,
        le=86400,
        description="对话静默多久后 flush 落库；0=关闭静默落库",
    )
    enable_retrieval: Optional[bool] = None
    enable_user_global_memory: Optional[bool] = None
    enable_heartbeat_memory: Optional[bool] = None
    retrieval_top_k: Optional[int] = Field(default=None, ge=1, le=50)
    dedup_similarity_threshold: Optional[float] = Field(default=None, ge=0.5, le=1.0)


class PreferenceUpdateRequest(BaseModel):
    """偏好规则人工纠偏请求（PATCH）"""

    preference_rule: Optional[str] = Field(default=None, min_length=1, max_length=2000, description="规则正文")
    polarity: Optional[str] = Field(default=None, description='极性 "do" / "dont"')
    target_context: Optional[str] = Field(
        default=None, max_length=128, description="绑定上下文（能力域/工具名/general）"
    )
    is_active: Optional[bool] = Field(default=None, description="启停（软停用而非删除，保留审计）")


# 1. 记忆检索 API


@app.post("/api/ai/memory/search", summary="记忆双路检索", tags=AI_MEMORY)
async def search_memory(
    req: MemorySearchRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
        from gsuid_core.ai_core.kits.base import inject_memory_cap
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
            memory_intent=req.memory_intent,
        )

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "episodes": mem_ctx.episodes,
                "entities": mem_ctx.entities,
                "edges": mem_ctx.edges,
                # 本次会命中并注入的偏好/纠错硬约束，便于控制台排查"为什么 Agent 还调错"
                "preferences": mem_ctx.preferences,
                "retrieval_meta": mem_ctx.retrieval_meta,
                "prompt_text": mem_ctx.to_prompt_text(
                    inject_memory_cap(req.query, covered=mem_ctx.covered, memory_intent=req.memory_intent),
                    query=req.query,
                    memory_intent=req.memory_intent,
                ),
            },
        }
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"记忆检索失败: {str(e)}",
            "data": None,
        }


# 1.5 批量摄入 API（评测 / 回灌专用，无需 web 控制台鉴权，但受 local-test 守卫保护）


# §14.1 窗口化实体/边抽取（评测/回灌专用） Episode 粒度（granular，由 create_episodes_bulk 写入）与抽取批次粒度在此解耦


def _build_extraction_windows(
    parsed_turns: List[Tuple[BatchObserveTurn, Optional[datetime]]],
    *,
    user_id: str,
    scope_key: str,
    max_chars: int,
    max_turns: int,
    now_default: datetime,
) -> List[List["ObservationRecord"]]:
    """把连续 turn 切成抽取窗口；每窗口是一组 ObservationRecord（HIGH 价值）。

    窗口边界：累计字符数超过 ``max_chars`` 或 turn 数达 ``max_turns`` 即收口（取先到者）。
    每条 record 的 ``valid_at`` 用 turn 时间戳（缺省退当前时间），供按 valid_at 找代表性
    Episode 与时序探针；``is_correction`` 仅在偏好记忆开启时按纯规则探测（喂给偏好蒸馏门控）。
    """
    from gsuid_core.ai_core.memory.observer import ObservationRecord

    windows: List[List["ObservationRecord"]] = []
    cur: List["ObservationRecord"] = []
    cur_chars = 0
    for turn, ts_obj in parsed_turns:
        content = turn.content.strip()
        if not content:
            continue
        speaker_id = "assistant" if turn.role == "assistant" else str(user_id)
        ts = ts_obj if ts_obj is not None else now_default
        rec = ObservationRecord(
            raw_content=content,
            speaker_id=speaker_id,
            group_id=None,
            scope_key=scope_key,
            timestamp=ts,
            message_type="private_msg",
            value_tier="HIGH",
        )
        if cur and (cur_chars + len(content) > max_chars or len(cur) >= max_turns):
            windows.append(cur)
            cur = []
            cur_chars = 0
        cur.append(rec)
        cur_chars += len(content)
    if cur:
        windows.append(cur)
    return windows


async def _find_episode_id_in_range(scope_key: str, start_ts: datetime, end_ts: datetime) -> str:
    """取本 scope 内 valid_at 落在 [start_ts, end_ts] 的最早一条 Episode id，作为窗口的
    代表性 granular Episode（用于 mem_episode_entity_mentions 关联，保留可解释性）。无则返回空串。
    """
    try:
        async with async_maker() as session:
            result = await session.execute(
                select(AIMemEpisode.id)
                .where(
                    AIMemEpisode.scope_key == scope_key,
                    col(AIMemEpisode.valid_at) >= start_ts,
                    col(AIMemEpisode.valid_at) <= end_ts,
                )
                .order_by(col(AIMemEpisode.valid_at).asc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return str(row) if row else ""
    except _RUNTIME_ERRORS:
        return ""


async def _reconcile_scope_index(scope_key: str) -> Dict[str, Any]:
    """零 LLM 对账：FTS5 重建 + Qdrant 缺失向量补偿。"""
    import time as _time

    from sqlalchemy import text as sql_text

    from gsuid_core.utils.database.base_models import _db_type
    from gsuid_core.ai_core.memory.database.fts import index_episode

    started = _time.perf_counter()
    fts_rows = 0
    async with async_maker() as session:
        await ensure_fts_table(session)
        if _db_type == "sqlite":
            await session.execute(
                sql_text("DELETE FROM aimemepisode_fts WHERE scope_key = :sk"),
                {"sk": scope_key},
            )
        result = await session.execute(select(AIMemEpisode).where(AIMemEpisode.scope_key == scope_key))
        episodes = list(result.scalars().all())
        for episode in episodes:
            await index_episode(session, episode)
            fts_rows += 1
        await session.commit()

    repaired = 0
    try:
        from sqlalchemy import inspect as _inspect

        from gsuid_core.ai_core.rag.base import client
        from gsuid_core.ai_core.memory.vector.ops import (
            upsert_edge_vectors_batch,
            upsert_entity_vectors_batch,
            upsert_episode_vectors_batch,
        )
        from gsuid_core.ai_core.memory.vector.collections import (
            MEMORY_EDGES_COLLECTION,
            MEMORY_ENTITIES_COLLECTION,
            MEMORY_EPISODES_COLLECTION,
        )

        del _inspect
        if client is None:
            return {"scope_key": scope_key, "fts_rows": fts_rows, "vectors_repaired": 0, "elapsed_ms": 0}

        # Episode 向量补偿
        async with async_maker() as session:
            rows = list(
                (await session.execute(select(AIMemEpisode).where(AIMemEpisode.scope_key == scope_key))).scalars().all()
            )
        ids = [r.qdrant_id or r.id for r in rows]
        if ids:
            points = await client.retrieve(
                collection_name=MEMORY_EPISODES_COLLECTION,
                ids=ids[:512],
                with_payload=False,
                with_vectors=False,
            )
            existing = {str(p.id) for p in points}
            missing = [
                {
                    "episode_id": r.qdrant_id or r.id,
                    "content": r.content,
                    "scope_key": r.scope_key,
                    "valid_at_ts": r.valid_at.timestamp() if r.valid_at else 0.0,
                    "speaker_ids": r.speaker_ids,
                    "session_id": r.session_id,
                    "thread_id": r.thread_id,
                    "turn_index": r.turn_index,
                }
                for r in rows
                if (r.qdrant_id or r.id) not in existing
            ]
            if missing:
                await upsert_episode_vectors_batch(missing)
                repaired += len(missing)

        # Entity / Edge 向量补偿（只补首页，避免按钮触发全库重嵌）
        async with async_maker() as session:
            entities = list(
                (await session.execute(select(AIMemEntity).where(AIMemEntity.scope_key == scope_key).limit(200)))
                .scalars()
                .all()
            )
            edges = list(
                (await session.execute(select(AIMemEdge).where(AIMemEdge.scope_key == scope_key).limit(200)))
                .scalars()
                .all()
            )
        if entities:
            existing = {
                str(p.id)
                for p in await client.retrieve(
                    collection_name=MEMORY_ENTITIES_COLLECTION,
                    ids=[e.qdrant_id or e.id for e in entities],
                    with_payload=False,
                    with_vectors=False,
                )
            }
            missing_entities = [
                {
                    "entity_id": e.qdrant_id or e.id,
                    "name": e.name,
                    "summary": e.summary,
                    "scope_key": e.scope_key,
                    "is_speaker": e.is_speaker,
                    "user_id": e.user_id,
                    "tag": e.tag,
                }
                for e in entities
                if (e.qdrant_id or e.id) not in existing
            ]
            if missing_entities:
                await upsert_entity_vectors_batch(missing_entities)
                repaired += len(missing_entities)
        if edges:
            existing = {
                str(p.id)
                for p in await client.retrieve(
                    collection_name=MEMORY_EDGES_COLLECTION,
                    ids=[e.qdrant_id or e.id for e in edges],
                    with_payload=False,
                    with_vectors=False,
                )
            }
            missing_edges = [
                {
                    "edge_id": e.qdrant_id or e.id,
                    "fact": e.fact,
                    "scope_key": e.scope_key,
                    "valid_at_ts": e.valid_at.timestamp() if e.valid_at else None,
                    "invalid_at_ts": e.invalid_at.timestamp() if e.invalid_at else None,
                    "source_entity_id": e.source_entity_id,
                    "target_entity_id": e.target_entity_id,
                }
                for e in edges
                if (e.qdrant_id or e.id) not in existing
            ]
            if missing_edges:
                await upsert_edge_vectors_batch(missing_edges)
                repaired += len(missing_edges)
    except _RUNTIME_ERRORS as e:
        logger.warning(t("log.webconsole.memory_reconcile_vectors_fail", e=e))

    return {
        "scope_key": scope_key,
        "fts_rows": fts_rows,
        "vectors_repaired": repaired,
        "elapsed_ms": round((_time.perf_counter() - started) * 1000, 2),
    }


async def _count_entities_edges(scope_key: str) -> Tuple[int, int]:
    """统计某 scope 当前的 Entity / Edge 数量（用于抽取前后差值上报）。"""
    async with async_maker() as session:
        ent = (
            await session.execute(
                select(func.count()).select_from(AIMemEntity).where(AIMemEntity.scope_key == scope_key)
            )
        ).scalar() or 0
        edg = (
            await session.execute(select(func.count()).select_from(AIMemEdge).where(AIMemEdge.scope_key == scope_key))
        ).scalar() or 0
    return int(ent), int(edg)


async def _run_extract_pass(
    parsed_turns: List[Tuple[BatchObserveTurn, Optional[datetime]]],
    *,
    user_id: str,
    scope_key: str,
    req: "BatchObserveRequest",
    now_default: datetime,
) -> Dict[str, Any]:
    """对本批 turn 做窗口化抽取：构造窗口 → 限流并发 → 每窗口宽松超时 + 跳过不丢整 plan。

    复用 worker.extract_window（其内部走 _extract_and_upsert_from_episode 全下游：实体/边/
    user_global/偏好蒸馏）。返回窗口与实体/边增量统计，供断点续跑脚本逐 plan 校验。
    """
    from gsuid_core.ai_core.memory.ingestion.worker import extract_window

    windows = _build_extraction_windows(
        parsed_turns,
        user_id=user_id,
        scope_key=scope_key,
        max_chars=req.extract_window_chars,
        max_turns=req.extract_window_turns,
        now_default=now_default,
    )
    if req.extract_max_windows and req.extract_max_windows > 0:
        windows = windows[: req.extract_max_windows]

    ent_before, edge_before = await _count_entities_edges(scope_key)

    concurrency = req.extract_concurrency or memory_config.llm_semaphore_limit
    sem = asyncio.Semaphore(max(1, concurrency))
    stats = {"done": 0, "failed": 0}

    async def _run_one(recs: List["ObservationRecord"]) -> None:
        async with sem:
            # 代表性 Episode：用窗口时间范围找一条已存在的 granular Episode 关联（可解释性）
            timestamps = [r.timestamp for r in recs]
            rep_id = await _find_episode_id_in_range(scope_key, min(timestamps), max(timestamps))
            try:
                await asyncio.wait_for(
                    extract_window(scope_key=scope_key, records=recs, episode_id=rep_id),
                    timeout=req.extract_window_timeout,
                )
                stats["done"] += 1
            except asyncio.TimeoutError:
                stats["failed"] += 1
                logger.warning(
                    t(
                        "log.webconsole.memory_scope_key_window_fail_2",
                        scope_key=scope_key,
                        p0=req.extract_window_timeout,
                    )
                )
            except Exception as e:  # noqa: BLE001 抽取窗口失败仅跳过，绝不丢整 plan（§14.2）
                stats["failed"] += 1
                logger.warning(t("log.webconsole.memory_scope_key_window_fail", scope_key=scope_key, e=e))

    if windows:
        await asyncio.gather(*[_run_one(w) for w in windows])

    ent_after, edge_after = await _count_entities_edges(scope_key)
    return {
        "windows_total": len(windows),
        "windows_done": stats["done"],
        "windows_failed": stats["failed"],
        "entities_before": ent_before,
        "entities_after": ent_after,
        "entities_added": ent_after - ent_before,
        "edges_before": edge_before,
        "edges_after": edge_after,
        "edges_added": edge_after - edge_before,
    }


@app.post("/api/ai/memory/batch_observe", include_in_schema=LOCAL_TEST_MODE, summary="批量写入记忆观察", tags=AI_MEMORY)
async def batch_observe(
    req: BatchObserveRequest,
    _gate: None = Depends(require_local_test),
) -> Dict[str, Any]:
    """批量摄入多轮对话到记忆观测队列。

    与 ``/api/chat_with_history`` 的差别：仅摄入，不创建 Agent、不调用 LLM、
    不污染 ChatSession；适合历史语料的大批量回灌场景。

    鉴权：默认 404，需开启 ``GSUID_LOCAL_TEST_MODE=1``（可选 ``GSUID_LOCAL_TEST_TOKEN``）。
    与 ``/api/chat_with_history`` 共用同一守卫，避免生产环境被裸调。

    Args:
        req: BatchObserveRequest，详见 Pydantic 文档。

    Returns:
        status: 0 成功 / 1 失败
        data: {observed, dropped, ts_failures, scope_key, flush, rebuild}
    """
    try:
        # 一次性定出 scope_type / scope_id / group_id，并就地收窄 group_id 非空
        # 让后续 make_scope_key(scope_id: str) 不再吃到 ``str | None``
        scope_norm = (req.scope_type or "user_global").lower()
        if scope_norm == "group":
            group_id = req.group_id
            if not group_id:
                return {
                    "status": 1,
                    "msg": "scope_type=group 时必须填写 group_id",
                    "data": None,
                }
            scope_type = ScopeType.GROUP
            scope_id = group_id
        elif scope_norm == "user_global":
            group_id = None
            scope_type = ScopeType.USER_GLOBAL
            scope_id = req.user_id
        else:
            return {
                "status": 1,
                "msg": f"非法 scope_type: {req.scope_type}（仅支持 user_global / group）",
                "data": None,
            }

        # 解析每条 turn 的可选 timestamp，失败时累计 ts_failures（不静默吞）
        parsed_turns: List[Tuple[BatchObserveTurn, Optional[datetime]]] = []
        ts_failures = 0
        raw_ts: List[Optional[datetime]] = []
        for turn in req.turns:
            ts_obj = parse_iso_or_unix_timestamp(turn.timestamp)
            if turn.timestamp is not None and ts_obj is None:
                ts_failures += 1
            raw_ts.append(ts_obj)
        from gsuid_core.ai_core.memory.ingest_time import spread_datetimes

        spread_ts = spread_datetimes(raw_ts)
        for turn, ts_obj in zip(req.turns, spread_ts):
            parsed_turns.append((turn, ts_obj))

        # 评测回放：user / assistant 两侧都是对话内容，统一落到目标 scope。提前算好
        # scope_key，所有 turn 写入同一 scope（探针检索此 scope 时即可召回两侧事实）。
        scope_key = make_scope_key(scope_type, scope_id)

        # 细粒度摄入：**每 turn 一条小 Episode**（单独 embedding，可被 System-1 精确召回）
        # 不走 observe 队列 + worker 的 80-turn 聚合。回灌大语料（单 plan ~1.2M token）若按
        observed = 0
        dropped = 0
        now_default = datetime.now(timezone.utc)
        items: List[Dict[str, Any]] = []
        for turn, ts_obj in parsed_turns:
            # Pydantic min_length=1 已拦截空 content，strip 仅清掉首尾空白
            content = turn.content.strip()
            if not content:
                dropped += 1
                continue
            # role 已被 Pydantic Literal 收窄为 "user" | "assistant"；保留发言者标签供归属/可读
            speaker_id = "assistant" if turn.role == "assistant" else str(req.user_id)
            ts = ts_obj if ts_obj is not None else now_default
            # 长 turn 切块：每块一条 Episode（同一 turn 的块共享时间戳），保证完整入嵌入窗口
            for chunk in _chunk_text(content, target=1200):
                items.append(
                    {
                        "content": f"{speaker_id}: {chunk}",
                        "speaker_ids": [speaker_id],
                        "valid_at": ts,
                    }
                )
            observed += 1

        # 批量写入 Episode 到数据库与向量库。
        if items and req.write_episodes:
            await AIMemEpisode.create_episodes_bulk(scope_key, items)

        # §14.1 窗口化实体/边抽取：把连续 turn 拼窗口、每窗口一次 LLM 抽取写实体/边/偏好。
        # eval_mode=true 时下游不每批触发分层图重建（由外部抽取全完成后统一 rebuild）。
        extract_stats: Optional[Dict[str, Any]] = None
        if req.extract:
            extract_stats = await _run_extract_pass(
                parsed_turns,
                user_id=req.user_id,
                scope_key=scope_key,
                req=req,
                now_default=now_default,
            )

        # trigger_rebuild：schema_v2 下只做零 LLM 的索引对账补偿。
        if req.trigger_rebuild:
            await _reconcile_scope_index(scope_key)

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "observed": observed,
                "dropped": dropped,
                "ts_failures": ts_failures,
                "scope_key": scope_key,
                "flush": req.flush,
                "rebuild": req.trigger_rebuild,
                "wrote_episodes": bool(items and req.write_episodes),
                "extract": extract_stats,
            },
        }
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"批量摄入失败: {str(e)}",
            "data": None,
        }


# 2. Episode 浏览 API


@app.get("/api/ai/memory/episodes", summary="Episode 列表", tags=AI_MEMORY)
async def list_episodes(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    all_scopes: bool = False,
    page: int = 1,
    page_size: int = 20,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"获取 Episode 列表失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/memory/episodes/{episode_id}", summary="Episode 详情", tags=AI_MEMORY)
async def get_episode_detail(
    episode_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"获取 Episode 详情失败: {str(e)}",
            "data": None,
        }


@app.delete("/api/ai/memory/episodes/{episode_id}", summary="删除 Episode", tags=AI_MEMORY)
async def delete_episode(
    episode_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
            except _RUNTIME_ERRORS:
                pass

            await session.delete(episode)
            await session.commit()

            return {"status": 0, "msg": "ok", "data": None}
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"删除 Episode 失败: {str(e)}",
            "data": None,
        }


# 3. Entity 浏览 API


@app.get("/api/ai/memory/entities", summary="Entity 列表", tags=AI_MEMORY)
async def list_entities(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    all_scopes: bool = False,
    is_speaker: Optional[bool] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"获取 Entity 列表失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/memory/entities/{entity_id}", summary="Entity 详情", tags=AI_MEMORY)
async def get_entity_detail(
    entity_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"获取 Entity 详情失败: {str(e)}",
            "data": None,
        }


@app.delete("/api/ai/memory/entities/{entity_id}", summary="删除 Entity", tags=AI_MEMORY)
async def delete_entity(
    entity_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
            except _RUNTIME_ERRORS:
                pass

            await session.delete(entity)
            await session.commit()

            return {"status": 0, "msg": "ok", "data": None}
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"删除 Entity 失败: {str(e)}",
            "data": None,
        }


# 4. Edge 浏览 API


@app.get("/api/ai/memory/edges", summary="Edge 列表", tags=AI_MEMORY)
async def list_edges(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    all_scopes: bool = False,
    entity_id: Optional[str] = None,
    include_history: bool = False,
    page: int = 1,
    page_size: int = 20,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
            if not include_history:
                query = query.where(col(AIMemEdge.resolution_state).in_(["active", "contradicted"]))
                count_query = count_query.where(col(AIMemEdge.resolution_state).in_(["active", "contradicted"]))

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
                        "edge_kind": edge.edge_kind,
                        "slot_key": edge.slot_key,
                        "resolution_state": edge.resolution_state,
                        "provenance_tier": "transcribed" if edge.resolution_state == "staging" else "self",
                        "superseded_by_id": edge.superseded_by_id,
                        "contradicts_edge_id": edge.contradicts_edge_id,
                        "thread_id": edge.thread_id,
                        "event_at": str(edge.event_at) if edge.event_at else None,
                        "mention_count": edge.mention_count,
                        "decay_score": edge.decay_score,
                        "last_accessed": str(edge.last_accessed) if edge.last_accessed else None,
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
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"获取 Edge 列表失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/memory/edges/{edge_id}", summary="Edge 详情", tags=AI_MEMORY)
async def get_edge_detail(
    edge_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
                    "edge_kind": edge.edge_kind,
                    "slot_key": edge.slot_key,
                    "resolution_state": edge.resolution_state,
                    "provenance_tier": "transcribed" if edge.resolution_state == "staging" else "self",
                    "superseded_by_id": edge.superseded_by_id,
                    "contradicts_edge_id": edge.contradicts_edge_id,
                    "thread_id": edge.thread_id,
                    "event_at": str(edge.event_at) if edge.event_at else None,
                    "mention_count": edge.mention_count,
                    "decay_score": edge.decay_score,
                    "source_entity": source_entity,
                    "target_entity": target_entity,
                },
            }
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"获取 Edge 详情失败: {str(e)}",
            "data": None,
        }


@app.delete("/api/ai/memory/edges/{edge_id}", summary="删除 Edge", tags=AI_MEMORY)
async def delete_edge(
    edge_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
            except _RUNTIME_ERRORS:
                pass

            await session.delete(edge)
            await session.commit()

            return {"status": 0, "msg": "ok", "data": None}
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"删除 Edge 失败: {str(e)}",
            "data": None,
        }


# 4.5 程序性 / 偏好记忆（Preference）治理 API
# 偏好规则会硬约束工具调用，误抽的规则会持续误导 Agent，故人工治理比 Episode 更关键。


def _preference_to_dict(p: AIMemPreference) -> Dict[str, Any]:
    """把 AIMemPreference 行序列化为 API 输出字典。"""
    return {
        "id": p.id,
        "scope_key": p.scope_key,
        "user_id": p.user_id,
        "target_context": p.target_context,
        "preference_rule": p.preference_rule,
        "polarity": p.polarity,
        "is_correction": p.is_correction,
        "is_active": p.is_active,
        "mention_count": p.mention_count,
        "source_episode_id": p.source_episode_id,
        "created_at": str(p.created_at) if p.created_at else None,
        "updated_at": str(p.updated_at) if p.updated_at else None,
        "last_applied_at": str(p.last_applied_at) if p.last_applied_at else None,
    }


@app.get("/api/ai/memory/preferences", summary="偏好规则列表", tags=AI_MEMORY)
async def list_preferences(
    scope_key: Optional[str] = None,
    user_id: Optional[str] = None,
    target_context: Optional[str] = None,
    is_correction: Optional[bool] = None,
    polarity: Optional[str] = None,
    is_active: Optional[bool] = None,
    all_scopes: bool = False,
    page: int = 1,
    page_size: int = 20,
    keyword: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """获取程序性/偏好规则列表。

    支持按 scope_key / user_id / target_context / is_correction / polarity / is_active 过滤，
    分页返回（排序：纠错优先 → 高频强化 → 最近更新）。仅传 user_id 时自动按 USER_GLOBAL 取。
    """
    try:
        actual_scope_key = scope_key
        if not all_scopes and actual_scope_key is None and user_id is not None:
            actual_scope_key = make_scope_key(ScopeType.USER_GLOBAL, user_id)

        async with async_maker() as session:
            query = select(AIMemPreference)
            count_query = select(func.count()).select_from(AIMemPreference)

            conditions = []
            if actual_scope_key:
                if ":" in actual_scope_key:
                    conditions.append(col(AIMemPreference.scope_key) == actual_scope_key)
                else:
                    conditions.append(
                        or_(
                            col(AIMemPreference.scope_key) == actual_scope_key,
                            col(AIMemPreference.scope_key) == f"user_global:{actual_scope_key}",
                            col(AIMemPreference.user_id) == actual_scope_key,
                        )
                    )
            if user_id:
                conditions.append(col(AIMemPreference.user_id) == user_id)
            if target_context:
                conditions.append(col(AIMemPreference.target_context) == target_context)
            if is_correction is not None:
                conditions.append(col(AIMemPreference.is_correction).is_(is_correction))
            if polarity:
                conditions.append(col(AIMemPreference.polarity) == polarity)
            if is_active is not None:
                conditions.append(col(AIMemPreference.is_active).is_(is_active))
            if keyword and keyword.strip():
                kw = f"%{keyword.strip()}%"
                conditions.append(
                    or_(
                        col(AIMemPreference.preference_rule).ilike(kw),
                        col(AIMemPreference.target_context).ilike(kw),
                        col(AIMemPreference.user_id).ilike(kw),
                        col(AIMemPreference.scope_key).ilike(kw),
                    )
                )
            for cond in conditions:
                query = query.where(cond)
                count_query = count_query.where(cond)

            total = (await session.execute(count_query)).scalar() or 0

            query = (
                query.order_by(
                    col(AIMemPreference.is_correction).desc(),
                    col(AIMemPreference.mention_count).desc(),
                    col(AIMemPreference.updated_at).desc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            rows = (await session.execute(query)).scalars().all()
            items = [_preference_to_dict(p) for p in rows]

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
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"获取偏好列表失败: {str(e)}", "data": None}


@app.get("/api/ai/memory/preferences/{pref_id}", summary="偏好规则详情", tags=AI_MEMORY)
async def get_preference_detail(
    pref_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """获取单条偏好规则详情（含 source_episode_id 溯源、mention_count、last_applied_at）。"""
    try:
        async with async_maker() as session:
            result = await session.execute(select(AIMemPreference).where(AIMemPreference.id == pref_id))
            pref = result.scalar_one_or_none()
            if pref is None:
                return {"status": 1, "msg": f"未找到偏好规则: {pref_id}", "data": None}
            return {"status": 0, "msg": "ok", "data": _preference_to_dict(pref)}
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"获取偏好详情失败: {str(e)}", "data": None}


@app.patch("/api/ai/memory/preferences/{pref_id}", summary="更新偏好规则（人工纠偏）", tags=AI_MEMORY)
async def update_preference(
    pref_id: str,
    req: PreferenceUpdateRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """人工纠偏：改规则正文 / 极性 / 绑定上下文 / 启停（软停用而非删除，保留审计）。"""
    from datetime import datetime, timezone

    try:
        async with async_maker() as session:
            result = await session.execute(select(AIMemPreference).where(AIMemPreference.id == pref_id))
            pref = result.scalar_one_or_none()
            if pref is None:
                return {"status": 1, "msg": f"未找到偏好规则: {pref_id}", "data": None}

            changed = False
            if req.preference_rule is not None:
                pref.preference_rule = req.preference_rule.strip()
                changed = True
            if req.polarity is not None:
                pref.polarity = "dont" if req.polarity == "dont" else "do"
                changed = True
            if req.target_context is not None:
                pref.target_context = req.target_context.strip()[:128] or "general"
                changed = True
            if req.is_active is not None:
                pref.is_active = req.is_active
                changed = True

            if changed:
                pref.updated_at = datetime.now(timezone.utc)
                session.add(pref)
                await session.commit()
                await session.refresh(pref)

            return {"status": 0, "msg": "ok", "data": _preference_to_dict(pref)}
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"更新偏好规则失败: {str(e)}", "data": None}


@app.delete("/api/ai/memory/preferences/{pref_id}", summary="删除偏好规则", tags=AI_MEMORY)
async def delete_preference(
    pref_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """删除单条误抽的偏好规则（SQL-only，无向量需清理）。"""
    try:
        async with async_maker() as session:
            result = await session.execute(select(AIMemPreference).where(AIMemPreference.id == pref_id))
            pref = result.scalar_one_or_none()
            if pref is None:
                return {"status": 1, "msg": f"未找到偏好规则: {pref_id}", "data": None}
            await session.delete(pref)
            await session.commit()
            return {"status": 0, "msg": "ok", "data": None}
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"删除偏好规则失败: {str(e)}", "data": None}


# 5. 分层语义图（Category）浏览 API


@app.get("/api/ai/memory/categories", summary="记忆虚拟分类投影", tags=AI_MEMORY)
async def list_categories(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    all_scopes: bool = False,
    layer: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """零 Token 虚拟分类：四大原语 + 故事线专题。"""
    try:
        actual_scope_key = scope_key
        if not all_scopes and actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)

        async with async_maker() as session:
            items: List[Dict[str, Any]] = []
            if layer is None or layer == 0:
                if actual_scope_key:
                    threads = await AIMemThread.list_by_scope(actual_scope_key, limit=100)
                else:
                    rows = await session.execute(
                        select(AIMemThread).order_by(col(AIMemThread.last_at).desc()).limit(100)
                    )
                    threads = list(rows.scalars().all())
                for th in threads:
                    items.append(
                        {
                            "id": f"virt:thread:{th.id}",
                            "scope_key": th.scope_key,
                            "name": th.title or "未命名故事线",
                            "summary": th.summary or "追加式里程碑故事线",
                            "tag": ["thread", th.status],
                            "layer": 0,
                            "parent_id": None,
                            "child_categories_count": 0,
                            "member_entities_count": th.milestone_count,
                            "created_at": str(th.first_at) if th.first_at else None,
                            "updated_at": str(th.last_at) if th.last_at else None,
                        }
                    )
            if layer is None or layer == 1:
                for vid, name, kind in _VIRTUAL_L1:
                    base = select(func.count()).select_from(AIMemEdge)
                    if actual_scope_key:
                        base = base.where(AIMemEdge.scope_key == actual_scope_key)
                    if kind == "conflict":
                        base = base.where(col(AIMemEdge.resolution_state).in_(["contradicted", "staging"]))
                    else:
                        base = base.where(
                            col(AIMemEdge.edge_kind) == kind,
                            col(AIMemEdge.resolution_state) == "active",
                        )
                    count = (await session.execute(base)).scalar() or 0
                    items.append(
                        {
                            "id": vid,
                            "scope_key": actual_scope_key or "",
                            "name": name,
                            "summary": f"实时 SQL 投影，共 {count} 条",
                            "tag": ["virtual", kind],
                            "layer": 1,
                            "parent_id": None,
                            "child_categories_count": 0,
                            "member_entities_count": count,
                            "created_at": None,
                            "updated_at": None,
                        }
                    )
            total = len(items)
            start = (page - 1) * page_size
            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "items": items[start : start + page_size],
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                },
            }
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"获取虚拟分类失败: {str(e)}", "data": None}


@app.get("/api/ai/memory/categories/{category_id}", summary="虚拟分类详情", tags=AI_MEMORY)
async def get_category_detail(
    category_id: str,
    scope_key: Optional[str] = None,
    group_id: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """下钻虚拟分类：L1 原语返回成员实体/边，故事线返回里程碑与片段。"""
    try:
        actual_scope_key = scope_key
        if actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)
        async with async_maker() as session:
            if category_id.startswith("virt:thread:"):
                thread_id = category_id[len("virt:thread:") :]
                thread = await session.get(AIMemThread, thread_id)
                if thread is None:
                    return {"status": 1, "msg": f"未找到故事线: {thread_id}", "data": None}
                milestones = await AIMemEdge.list_thread_milestones(thread.scope_key, thread.id, limit=200)
                episode_ids = [m.source_episode_id for m in milestones if m.source_episode_id]
                episodes = []
                for eid in episode_ids:
                    row = await session.get(AIMemEpisode, eid)
                    if row is not None:
                        episodes.append(
                            {
                                "id": row.id,
                                "content": row.content,
                                "valid_at": str(row.valid_at) if row.valid_at else None,
                                "session_id": row.session_id,
                                "thread_id": row.thread_id,
                            }
                        )
                entity_ids = list({m.source_entity_id for m in milestones})
                rows = await session.execute(select(AIMemEntity).where(col(AIMemEntity.id).in_(entity_ids)))
                entities = list(rows.scalars().all()) if entity_ids else []
                return {
                    "status": 0,
                    "msg": "ok",
                    "data": {
                        "id": category_id,
                        "scope_key": thread.scope_key,
                        "name": thread.title or "未命名故事线",
                        "summary": thread.summary,
                        "tag": ["thread", thread.status],
                        "layer": 0,
                        "parent_id": None,
                        "created_at": str(thread.first_at) if thread.first_at else None,
                        "updated_at": str(thread.last_at) if thread.last_at else None,
                        "parent_categories": [],
                        "child_categories": [],
                        "member_entities": [
                            {"id": e.id, "name": e.name, "summary": e.summary, "is_speaker": e.is_speaker}
                            for e in entities
                        ],
                        "edges": [
                            {"id": m.id, "fact": m.fact, "valid_at": str(m.valid_at) if m.valid_at else None}
                            for m in milestones
                        ],
                        "episodes": episodes,
                    },
                }
            if category_id.startswith("virt:L1:"):
                kind = category_id.rsplit(":", 1)[-1]
                query = select(AIMemEdge)
                if actual_scope_key:
                    query = query.where(AIMemEdge.scope_key == actual_scope_key)
                if kind == "conflict":
                    cond = col(AIMemEdge.resolution_state).in_(["contradicted", "staging"])
                else:
                    cond = and_(
                        col(AIMemEdge.edge_kind) == kind,
                        col(AIMemEdge.resolution_state) == "active",
                    )
                query = query.where(cond).order_by(col(AIMemEdge.valid_at).desc()).limit(200)
                edges = list((await session.execute(query)).scalars().all())
                entity_ids = list({e.source_entity_id for e in edges} | {e.target_entity_id for e in edges})
                rows = await session.execute(select(AIMemEntity).where(col(AIMemEntity.id).in_(entity_ids)))
                entities = list(rows.scalars().all()) if entity_ids else []
                label = category_id
                for vid, name, _kind in _VIRTUAL_L1:
                    if vid == category_id:
                        label = name
                return {
                    "status": 0,
                    "msg": "ok",
                    "data": {
                        "id": category_id,
                        "scope_key": actual_scope_key or "",
                        "name": label,
                        "summary": f"实时 SQL 投影，共 {len(edges)} 条",
                        "tag": ["virtual", kind],
                        "layer": 1,
                        "parent_id": None,
                        "created_at": None,
                        "updated_at": None,
                        "parent_categories": [],
                        "child_categories": [],
                        "member_entities": [
                            {"id": e.id, "name": e.name, "summary": e.summary, "is_speaker": e.is_speaker}
                            for e in entities
                        ],
                        "edges": [
                            {
                                "id": e.id,
                                "fact": e.fact,
                                "resolution_state": e.resolution_state,
                                "valid_at": str(e.valid_at) if e.valid_at else None,
                            }
                            for e in edges
                        ],
                        "episodes": [],
                    },
                }
            return {"status": 1, "msg": f"未知虚拟分类: {category_id}", "data": None}
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"获取虚拟分类详情失败: {str(e)}", "data": None}


@app.get("/api/ai/memory/hiergraph/status", summary="虚拟分类投影状态", tags=AI_MEMORY)
async def get_hiergraph_status(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        actual_scope_key = scope_key
        if actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)
        async with async_maker() as session:
            thread_q = select(func.count()).select_from(AIMemThread)
            edge_q = select(func.count()).select_from(AIMemEdge).where(col(AIMemEdge.resolution_state) == "active")
            if actual_scope_key:
                thread_q = thread_q.where(AIMemThread.scope_key == actual_scope_key)
                edge_q = edge_q.where(AIMemEdge.scope_key == actual_scope_key)
            thread_count = (await session.execute(thread_q)).scalar() or 0
            active_edges = (await session.execute(edge_q)).scalar() or 0
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "enabled": True,
                "mode": "virtual_projection_v2",
                "scope_key": actual_scope_key,
                "initialized": True,
                "l1_count": len(_VIRTUAL_L1),
                "thread_count": thread_count,
                "active_edges": active_edges,
                "max_layer": 1,
                "last_rebuild_at": None,
                "entity_count_at_last_rebuild": 0,
                "current_entity_count": active_edges,
                "group_summary_cache": None,
                "group_summary_updated_at": None,
            },
        }
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"获取投影状态失败: {str(e)}", "data": None}


@app.post("/api/ai/memory/hiergraph/rebuild", summary="零 LLM 索引对账", tags=AI_MEMORY)
async def trigger_hiergraph_rebuild(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    _: Optional[Dict[str, Any]] = Depends(require_auth_or_local_test),
) -> Dict[str, Any]:
    try:
        actual_scope_key = scope_key
        if actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)
        if actual_scope_key is None:
            return {"status": 1, "msg": "必须提供 group_id 或 scope_key", "data": None}
        stats = await _reconcile_scope_index(actual_scope_key)
        return {"status": 0, "msg": "ok", "data": stats}
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"索引对账失败: {str(e)}", "data": None}


@app.get("/api/ai/memory/threads", summary="故事线列表", tags=AI_MEMORY)
async def list_threads(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        actual_scope_key = scope_key
        if actual_scope_key is None and group_id is not None:
            actual_scope_key = make_scope_key(ScopeType.GROUP, group_id)
        async with async_maker() as session:
            query = select(AIMemThread)
            count_query = select(func.count()).select_from(AIMemThread)
            if actual_scope_key:
                query = query.where(AIMemThread.scope_key == actual_scope_key)
                count_query = count_query.where(AIMemThread.scope_key == actual_scope_key)
            if status:
                query = query.where(col(AIMemThread.status) == status)
                count_query = count_query.where(col(AIMemThread.status) == status)
            total = (await session.execute(count_query)).scalar() or 0
            rows = await session.execute(
                query.order_by(col(AIMemThread.last_at).desc()).offset((page - 1) * page_size).limit(page_size)
            )
            items = [
                {
                    "id": th.id,
                    "scope_key": th.scope_key,
                    "title": th.title,
                    "aliases": th.aliases,
                    "summary": th.summary,
                    "status": th.status,
                    "milestone_count": th.milestone_count,
                    "first_at": str(th.first_at) if th.first_at else None,
                    "last_at": str(th.last_at) if th.last_at else None,
                }
                for th in rows.scalars().all()
            ]
        return {
            "status": 0,
            "msg": "ok",
            "data": {"items": items, "total": total, "page": page, "page_size": page_size},
        }
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"获取故事线失败: {str(e)}", "data": None}


@app.get("/api/ai/memory/threads/{thread_id}", summary="故事线详情", tags=AI_MEMORY)
async def get_thread_detail(
    thread_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        async with async_maker() as session:
            thread = await session.get(AIMemThread, thread_id)
            if thread is None:
                return {"status": 1, "msg": f"未找到故事线: {thread_id}", "data": None}
            milestones = await AIMemEdge.list_thread_milestones(thread.scope_key, thread.id, limit=200, ascending=True)
            episodes = await AIMemEpisode.list_by_thread(thread.scope_key, thread.id, limit=200, ascending=True)
            entity_ids: set[str] = set()
            for edge in milestones:
                entity_ids.add(edge.source_entity_id)
                entity_ids.add(edge.target_entity_id)
            entities = []
            if entity_ids:
                entity_rows = await session.execute(
                    select(AIMemEntity).where(col(AIMemEntity.id).in_(list(entity_ids)))
                )
                entities = [
                    {
                        "id": ent.id,
                        "name": ent.name,
                        "summary": ent.summary,
                        "entity_type": ent.entity_kind,
                        "is_speaker": ent.is_speaker,
                    }
                    for ent in entity_rows.scalars().all()
                ]
            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "id": thread.id,
                    "scope_key": thread.scope_key,
                    "title": thread.title,
                    "aliases": thread.aliases,
                    "summary": thread.summary,
                    "status": thread.status,
                    "milestone_count": thread.milestone_count,
                    "first_at": str(thread.first_at) if thread.first_at else None,
                    "last_at": str(thread.last_at) if thread.last_at else None,
                    "milestones": [
                        {
                            "id": edge.id,
                            "fact": edge.fact,
                            "slot_key": edge.slot_key,
                            "source_entity_id": edge.source_entity_id,
                            "target_entity_id": edge.target_entity_id,
                            "event_at": str(edge.event_at) if edge.event_at else None,
                            "valid_at": str(edge.valid_at) if edge.valid_at else None,
                        }
                        for edge in milestones
                    ],
                    "episodes": [
                        {
                            "id": ep.id,
                            "content": ep.content,
                            "valid_at": str(ep.valid_at) if ep.valid_at else None,
                            "session_id": ep.session_id,
                        }
                        for ep in episodes
                    ],
                    "entities": entities,
                },
            }
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"获取故事线详情失败: {str(e)}", "data": None}


# 6. 记忆统计 API


@app.get("/api/ai/memory/stats", summary="记忆统计", tags=AI_MEMORY)
async def get_memory_stats(
    group_id: Optional[str] = None,
    scope_key: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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

            # 故事线 / 四态边统计（category_count 保留为 0 兼容旧前端）
            thread_query = select(func.count()).select_from(AIMemThread)
            if actual_scope_key:
                thread_query = thread_query.where(AIMemThread.scope_key == actual_scope_key)
            thread_count = (await session.execute(thread_query)).scalar() or 0
            state_counts: Dict[str, int] = {}
            for state in ("active", "staging", "superseded", "contradicted"):
                state_query = (
                    select(func.count()).select_from(AIMemEdge).where(col(AIMemEdge.resolution_state) == state)
                )
                if actual_scope_key:
                    state_query = state_query.where(AIMemEdge.scope_key == actual_scope_key)
                state_counts[state] = (await session.execute(state_query)).scalar() or 0
            category_count = 0

            # 程序性/偏好规则统计（活跃规则数 + 纠错规则数）
            pref_query = (
                select(func.count()).select_from(AIMemPreference).where(col(AIMemPreference.is_active).is_(True))
            )
            if actual_scope_key:
                pref_query = pref_query.where(AIMemPreference.scope_key == actual_scope_key)
            preference_count = (await session.execute(pref_query)).scalar() or 0

            pref_corr_query = (
                select(func.count())
                .select_from(AIMemPreference)
                .where(col(AIMemPreference.is_active).is_(True), col(AIMemPreference.is_correction).is_(True))
            )
            if actual_scope_key:
                pref_corr_query = pref_corr_query.where(AIMemPreference.scope_key == actual_scope_key)
            preference_correction_count = (await session.execute(pref_corr_query)).scalar() or 0

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
                    "thread_count": thread_count,
                    "edge_state_counts": state_counts,
                    "preference_count": preference_count,
                    "preference_correction_count": preference_correction_count,
                    "observation_queue_size": queue_size,
                    "scope_keys": scope_keys,
                },
            }
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"获取记忆统计失败: {str(e)}",
            "data": None,
        }


# 7. 配置管理 API


def _serialize_memory_config() -> Dict[str, Any]:
    return {
        "observer_enabled": memory_config.observer_enabled,
        "observer_blacklist": memory_config.observer_blacklist,
        "ingestion_enabled": memory_config.ingestion_enabled,
        "batch_interval_seconds": memory_config.batch_interval_seconds,
        "batch_max_size": memory_config.batch_max_size,
        "llm_semaphore_limit": memory_config.llm_semaphore_limit,
        "idle_flush_seconds": memory_config.idle_flush_seconds,
        "enable_retrieval": memory_config.enable_retrieval,
        "enable_user_global_memory": memory_config.enable_user_global_memory,
        "enable_heartbeat_memory": memory_config.enable_heartbeat_memory,
        "retrieval_top_k": memory_config.retrieval_top_k,
        "memory_inject_max_chars": memory_config.memory_inject_max_chars,
        "memory_inject_wide_chars": memory_config.memory_inject_wide_chars,
        "session_gap_seconds": memory_config.session_gap_seconds,
        "fact_max_inject": memory_config.fact_max_inject,
        "dedup_similarity_threshold": memory_config.dedup_similarity_threshold,
        # 程序性/偏好记忆（只读反射；经 MEMORY_CONFIG 持久化，于"记忆配置"分组调整）
        "enable_preference_memory": memory_config.enable_preference_memory,
        "preference_max_inject": memory_config.preference_max_inject,
        "preference_max_per_context": memory_config.preference_max_per_context,
        "preference_inject_budget_ratio": memory_config.preference_inject_budget_ratio,
        "preference_immediate_flush": memory_config.preference_immediate_flush,
    }


@app.get("/api/ai/memory/config", summary="获取记忆配置", tags=AI_MEMORY)
async def get_memory_config(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
            "data": _serialize_memory_config(),
        }
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"获取记忆配置失败: {str(e)}",
            "data": None,
        }


@app.put("/api/ai/memory/config", summary="更新记忆配置", tags=AI_MEMORY)
async def update_memory_config(
    req: MemoryConfigUpdateRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
            "data": _serialize_memory_config(),
        }
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"更新记忆配置失败: {str(e)}",
            "data": None,
        }


# 8. Scope 浏览 API


@app.get("/api/ai/memory/scopes", summary="Scope 列表", tags=AI_MEMORY)
async def list_scopes(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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

            # 故事线统计（旧前端 category_count 保留为 0）
            thread_result = await session.execute(
                select(AIMemThread.scope_key, func.count().label("thread_count")).group_by(AIMemThread.scope_key)
            )
            thread_stats = {row[0]: row[1] for row in thread_result.fetchall()}

            # 偏好规则统计
            pref_result = await session.execute(
                select(AIMemPreference.scope_key, func.count().label("preference_count")).group_by(
                    AIMemPreference.scope_key
                )
            )
            pref_stats = {row[0]: row[1] for row in pref_result.fetchall()}

            # 合并所有 scope_key
            all_scope_keys = (
                set(ep_stats.keys())
                | set(ent_stats.keys())
                | set(edge_stats.keys())
                | set(thread_stats.keys())
                | set(pref_stats.keys())
            )
            known_group_ids = {
                sk[len("group:") :] for sk in all_scope_keys if isinstance(sk, str) and sk.startswith("group:")
            }

            scopes = []
            for sk in sorted(all_scope_keys):
                scope_type = ""
                scope_id = ""
                if ":" in sk:
                    parts = sk.split(":", 1)
                    scope_type = parts[0]
                    scope_id = parts[1] if len(parts) > 1 else ""
                if scope_type == "user_global" and (
                    scope_id.startswith(("__assistant_", "self:", "group:")) or scope_id in known_group_ids
                ):
                    continue

                scopes.append(
                    {
                        "scope_key": sk,
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "episode_count": ep_stats[sk] if sk in ep_stats else 0,
                        "entity_count": ent_stats[sk] if sk in ent_stats else 0,
                        "edge_count": edge_stats[sk] if sk in edge_stats else 0,
                        "category_count": 0,
                        "thread_count": thread_stats[sk] if sk in thread_stats else 0,
                        "preference_count": pref_stats[sk] if sk in pref_stats else 0,
                    }
                )

            return {
                "status": 0,
                "msg": "ok",
                "data": scopes,
            }
    except _RUNTIME_ERRORS as e:
        return {
            "status": 1,
            "msg": f"获取 Scope 列表失败: {str(e)}",
            "data": None,
        }


# 9. 批量删除 API


@app.delete("/api/ai/memory/scopes/{scope_key}", summary="删除 Scope 记忆", tags=AI_MEMORY)
async def delete_scope_memory(
    scope_key: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
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
    res = await clear_memories_for_scope_async(scope_key=scope_key)
    if isinstance(res["data"], dict):
        res["data"]["scope_key"] = scope_key
    return res


# 10. 清空记忆 API（高级批量删除）


@app.post("/api/ai/memory/clear", summary="清空记忆（高级批量删除）", tags=AI_MEMORY)
async def clear_memory(
    req: MemoryClearRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    清空指定 Scope 下的所有记忆数据（支持精确匹配或前缀模糊匹配）

    支持通过 scope_key 精确匹配，或 scope_pattern 前缀模糊匹配。
    当 scope_pattern 以 @ 结尾时，会自动使用模糊匹配来匹配 user_in_group 的 scope_key。

    Args:
        req: 清空请求参数

    Returns:
        status: 0成功，1失败
        data: 删除的记录数量统计
    """
    result = await clear_memories_for_scope_async(
        scope_key=req.scope_key,
        scope_pattern=req.scope_pattern,
        dry_run=req.dry_run,
    )
    return result


@app.delete("/api/ai/memory/groups/{group_id}/clear", summary="清空群记忆", tags=AI_MEMORY)
async def clear_group_memory(
    group_id: str,
    include_user_in_group: bool = True,
    dry_run: bool = False,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    清空某个群的全部记忆

    会删除以下内容：
    1. group:{group_id} 下的所有记忆（Episode/Entity/Edge/Category）
    2. 如果 include_user_in_group=True（默认），还会删除 user_in_group:*@{group_id} 下所有用户的群内记忆档案

    同时删除数据库记录和 Qdrant 中的向量。

    > ⚠️ 此操作不可逆，请谨慎使用！

    Args:
        group_id: 群组 ID
        include_user_in_group: 是否同时清空该群内所有用户的 user_in_group 记忆档案
        dry_run: 为 True 时仅统计数量，不执行删除

    Returns:
        status: 0成功，1失败
        data: 删除的记录数量统计
    """
    result = await clear_group_memories(
        group_id=group_id,
        include_user_in_group=include_user_in_group,
        dry_run=dry_run,
    )
    return result


@app.delete("/api/ai/memory/users/{user_id}/global/clear", summary="清空用户全局记忆", tags=AI_MEMORY)
async def clear_user_global_memory(
    user_id: str,
    dry_run: bool = False,
    _: Optional[Dict[str, Any]] = Depends(require_auth_or_local_test),
) -> Dict[str, Any]:
    """
    清空某个用户的跨群全局记忆画像

    删除 user_global:{user_id} 下的所有记忆数据。
    此操作不可逆，请谨慎使用。

    Args:
        user_id: 用户 ID
        dry_run: 为 True 时仅统计数量，不执行删除

    Returns:
        status: 0成功，1失败
        data: 删除的记录数量统计
    """
    result = await clear_user_global_memories(
        user_id=user_id,
        dry_run=dry_run,
    )
    return result
