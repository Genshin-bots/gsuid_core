"""LOCAL_TEST 评测端点：分段检索 / user turn 列表 / extract-light。"""

from __future__ import annotations

from typing import Literal

from fastapi import Depends
from pydantic import Field, BaseModel

from gsuid_core.webconsole.app_app import app
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.webconsole._api_tags import AI_MEMORY
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.webconsole._local_test_gate import LOCAL_TEST_MODE, require_local_test


class EvalRetrieveRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    query: str = Field(..., min_length=1, max_length=4000)
    enable_system2: bool = True


class EvalEpisodesRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    limit: int = Field(default=400, ge=1, le=2000)


class EvalExtractRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    limit: int = Field(default=16, ge=1, le=80)


class EvalGistRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    source: Literal["rule", "llm"] = "rule"
    limit: int = Field(default=4000, ge=1, le=8000)


class EvalEpisodeRow(BaseModel):
    id: str
    valid_at: str
    content: str
    session_id: str
    turn_index: int


class EvalRetrieveOut(BaseModel):
    status: Literal[0] = 0
    pool_ids: list[str]
    inject_ids: list[str]
    skeleton_ids: list[str]
    inject_chars: int
    event_count: int
    prompt_head: str


class EvalEpisodesOut(BaseModel):
    status: Literal[0] = 0
    rows: list[EvalEpisodeRow]


class EvalLedgerLine(BaseModel):
    mark: str
    episode_id: str
    session_id: str
    gist: str
    is_new: bool
    day: str
    turn_index: int
    blob: str


class EvalLedgerOut(BaseModel):
    status: Literal[0] = 0
    inject_ids: list[str]
    lines: list[EvalLedgerLine]


class EvalVectorsRequest(BaseModel):
    episode_ids: list[str] = Field(..., min_length=1, max_length=200)


class EvalVectorsOut(BaseModel):
    status: Literal[0] = 0
    vectors: dict[str, list[float]]


class EvalEmbedRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=96)


class EvalEmbedOut(BaseModel):
    status: Literal[0] = 0
    vectors: list[list[float]]


class EvalExtractOut(BaseModel):
    status: Literal[0] = 0
    written: int
    scope_key: str


@app.post(
    "/api/ai/memory/eval/retrieve",
    include_in_schema=LOCAL_TEST_MODE,
    summary="评测检索分段（不调答题 LLM）",
    tags=AI_MEMORY,
)
async def eval_retrieve(
    req: EvalRetrieveRequest,
    _gate: None = Depends(require_local_test),
) -> EvalRetrieveOut:
    from gsuid_core.ai_core.memory.retrieval.dual_route import dual_route_retrieve

    ctx = await dual_route_retrieve(
        query=req.query,
        user_id=req.user_id,
        group_id=None,
        top_k=memory_config.retrieval_top_k,
        enable_system2=req.enable_system2,
        enable_user_global=True,
        inject_preferences=True,
        preference_contexts=None,
        bot_id="HTTP",
        bot_self_id="ai",
        include_self=True,
    )
    cap = int(memory_config.memory_inject_max_chars)
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        looks_like_span_query,
        looks_like_order_query,
        looks_like_summary_query,
    )

    if looks_like_order_query(req.query) or looks_like_span_query(req.query) or looks_like_summary_query(req.query):
        if memory_config.eo_strategy == "ledger":
            cap = max(cap, int(memory_config.ledger_max_chars))
        else:
            cap = max(cap, 16000)
    prompt = ctx.to_prompt_text(max_chars=cap, query=req.query)
    return EvalRetrieveOut(
        pool_ids=list(ctx.pool_ids),
        inject_ids=list(ctx.inject_ids),
        skeleton_ids=list(ctx.skeleton_ids),
        inject_chars=len(prompt),
        event_count=len(ctx.events),
        prompt_head=prompt[:400],
    )


@app.post(
    "/api/ai/memory/eval/episodes",
    include_in_schema=LOCAL_TEST_MODE,
    summary="评测列出 user turn（oracle 对齐）",
    tags=AI_MEMORY,
)
async def eval_list_user_episodes(
    req: EvalEpisodesRequest,
    _gate: None = Depends(require_local_test),
) -> EvalEpisodesOut:
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.lexical import _assistant_turn, _speaker_stripped

    scope_key = make_scope_key(ScopeType.USER_GLOBAL, req.user_id)
    rows = await AIMemEpisode.list_by_scope(scope_key, limit=req.limit)
    out: list[EvalEpisodeRow] = []
    for row in rows:
        raw = row.content or ""
        if _assistant_turn(raw):
            continue
        body = _speaker_stripped(raw).replace("\n", " ").strip()
        if len(body) < 8:
            continue
        stamp = row.valid_at.strftime("%Y-%m-%d %H:%M:%S") if row.valid_at else ""
        out.append(
            EvalEpisodeRow(
                id=row.id,
                valid_at=stamp,
                content=body[:480],
                session_id=row.session_id or "",
                turn_index=int(row.turn_index or 0),
            )
        )
    return EvalEpisodesOut(rows=out)


@app.post(
    "/api/ai/memory/eval/ledger",
    include_in_schema=LOCAL_TEST_MODE,
    summary="评测全量时间线（含短名单打分 blob）",
    tags=AI_MEMORY,
)
async def eval_ledger(
    req: EvalRetrieveRequest,
    _gate: None = Depends(require_local_test),
) -> EvalLedgerOut:
    from gsuid_core.ai_core.memory.retrieval.ledger_timeline import build_ledger

    scope_key = make_scope_key(ScopeType.USER_GLOBAL, req.user_id)
    view = await build_ledger([scope_key], req.query)
    lines = [
        EvalLedgerLine(
            mark=ln.mark,
            episode_id=ln.episode_id,
            session_id=ln.session_id,
            gist=ln.gist,
            is_new=ln.is_new,
            day=ln.day,
            turn_index=ln.turn_index,
            blob=ln.blob,
        )
        for ln in view.lines
    ]
    return EvalLedgerOut(inject_ids=list(view.inject_ids), lines=lines)


@app.post(
    "/api/ai/memory/eval/episode_vectors",
    include_in_schema=LOCAL_TEST_MODE,
    summary="评测按 id 拉 Episode dense（只读）",
    tags=AI_MEMORY,
)
async def eval_episode_vectors(
    req: EvalVectorsRequest,
    _gate: None = Depends(require_local_test),
) -> EvalVectorsOut:
    from gsuid_core.ai_core.memory.vector.ops import retrieve_episode_dense_vectors

    ids = [x.strip() for x in req.episode_ids if x.strip()]
    got = await retrieve_episode_dense_vectors(ids[:200])
    return EvalVectorsOut(vectors=got)


@app.post(
    "/api/ai/memory/eval/embed_texts",
    include_in_schema=LOCAL_TEST_MODE,
    summary="评测用记忆 dense 模型嵌文本（只读）",
    tags=AI_MEMORY,
)
async def eval_embed_texts(
    req: EvalEmbedRequest,
    _gate: None = Depends(require_local_test),
) -> EvalEmbedOut:
    from gsuid_core.ai_core.memory.vector.ops import embed_texts_dense

    texts = [x[:400] for x in req.texts[:96]]
    got = await embed_texts_dense(texts)
    return EvalEmbedOut(vectors=got)


@app.post(
    "/api/ai/memory/eval/extract_aspects",
    include_in_schema=LOCAL_TEST_MODE,
    summary="评测 extract-light：只抽 turn 级 aspect",
    tags=AI_MEMORY,
)
async def eval_extract_aspects(
    req: EvalExtractRequest,
    _gate: None = Depends(require_local_test),
) -> EvalExtractOut:
    from gsuid_core.ai_core.memory.lifecycle.sleep_extract import extract_aspects_for_scope

    scope_key = make_scope_key(ScopeType.USER_GLOBAL, req.user_id)
    written = await extract_aspects_for_scope(scope_key, limit=req.limit)
    return EvalExtractOut(written=written, scope_key=scope_key)


@app.post(
    "/api/ai/memory/eval/gist_backfill",
    include_in_schema=LOCAL_TEST_MODE,
    summary="评测 gist 回填（rule 或 llm）",
    tags=AI_MEMORY,
)
async def eval_gist_backfill(
    req: EvalGistRequest,
    _gate: None = Depends(require_local_test),
) -> EvalExtractOut:
    from gsuid_core.ai_core.memory.database.models import AIMemSession
    from gsuid_core.ai_core.memory.lifecycle.gist_backfill import backfill_rule_scope, backfill_llm_session

    scope_key = make_scope_key(ScopeType.USER_GLOBAL, req.user_id)
    if req.source == "rule":
        written = await backfill_rule_scope(scope_key, limit=req.limit)
        return EvalExtractOut(written=written, scope_key=scope_key)
    written = 0
    sessions = await AIMemSession.list_by_scope(scope_key, limit=200)
    for sess in sessions:
        written += await backfill_llm_session(sess.id, force=True)
        if written >= req.limit:
            break
    return EvalExtractOut(written=written, scope_key=scope_key)
