"""联邦检索的补路：History A / record_* / 图片 / 表情。

失败 fail-open。过滤下推到各源，禁止先搜全球再内存筛。
"""

from __future__ import annotations

import re
import json
from typing import Dict, List, Tuple
from datetime import datetime

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.cognition.types import CogKind, CogScope, CognitiveHit

_BackendResult = Tuple[List[str], Dict[str, CognitiveHit]]
_EMPTY: _BackendResult = ([], {})
_TOKEN_RE = re.compile(r"[^\s，。！？、；：,.!?;:]{2,}")


def _score_text(query: str, text: str) -> float:
    q = (query or "").strip().lower()
    body = (text or "").lower()
    if not q or not body:
        return 0.0
    if q in body:
        return 1.0
    tokens = _TOKEN_RE.findall(q)
    if not tokens:
        return 0.0
    hits = sum(1 for tok in tokens if tok in body)
    return hits / len(tokens)


async def search_history(query: str, *, scope: CogScope, limit: int) -> _BackendResult:
    try:
        from gsuid_core.message_history.manager import get_history_manager

        scored = get_history_manager().search_recent_for_cognition(
            group_id=scope.group_id,
            user_id=scope.user_id,
            bot_id=scope.bot_id,
            query=query,
            limit=limit,
        )
    except Exception as e:
        logger.debug(t("log.ai.cognition_backend_fail", backend="history", e=e))
        return _EMPTY
    ids: List[str] = []
    hits: Dict[str, CognitiveHit] = {}
    for rec, score in scored:
        hid = f"hist_{rec.user_id}_{int(rec.timestamp)}"
        if hid in hits:
            continue
        as_of = datetime.fromtimestamp(rec.timestamp).strftime("%Y-%m-%d %H:%M")
        who = rec.user_name or rec.user_id or rec.role
        hits[hid] = CognitiveHit(
            kind=CogKind.EPISODE,
            id=hid,
            title=f"{who} · {rec.role}",
            summary=(rec.content or "")[:200],
            score=max(0.35, min(score, 0.95)),
            as_of=as_of,
            source="history_a",
        )
        ids.append(hid)
    return ids, hits


async def search_records(query: str, *, scope: CogScope, limit: int) -> _BackendResult:
    from gsuid_core.ai_core.state_store.store import state_get_value, state_list_keys

    if not scope.user_id:
        return _EMPTY
    scopes: List[str] = [f"user:{scope.user_id}"]
    if scope.group_id:
        scopes.append(f"group:{scope.group_id}")
    ids: List[str] = []
    hits: Dict[str, CognitiveHit] = {}
    q = query.strip().lower()
    try:
        for rec_scope in scopes:
            keys = await state_list_keys(rec_scope, prefix="record:")
            for key in keys:
                if len(ids) >= limit:
                    return ids, hits
                coll_name = key[7:] if key.startswith("record:") else key
                coll = await state_get_value(rec_scope, key)
                if not isinstance(coll, dict):
                    continue
                for rid, rec in coll.items():
                    if not isinstance(rec, dict):
                        continue
                    blob = json.dumps(rec, ensure_ascii=False)
                    score = _score_text(q, f"{coll_name} {blob}")
                    if score <= 0:
                        continue
                    hid = f"rec_{rec_scope}_{coll_name}_{rid}"
                    if hid in hits:
                        continue
                    hits[hid] = CognitiveHit(
                        kind=CogKind.RECORD,
                        id=hid,
                        title=f"{coll_name}/{rid}",
                        summary=blob[:200],
                        score=0.4 + 0.4 * score,
                        source="record",
                    )
                    ids.append(hid)
    except Exception as e:
        logger.debug(t("log.ai.cognition_backend_fail", backend="record", e=e))
        return _EMPTY
    return ids[:limit], hits


async def search_images_backend(query: str, *, scope: CogScope, limit: int) -> _BackendResult:
    _ = scope
    try:
        from gsuid_core.ai_core.rag import search_images

        points = await search_images(query=query, limit=limit)
    except Exception as e:
        logger.debug(t("log.ai.cognition_backend_fail", backend="image", e=e))
        return _EMPTY
    ids: List[str] = []
    hits: Dict[str, CognitiveHit] = {}
    for point in points:
        payload = point.payload
        if not payload:
            continue
        path = str(payload["path"]) if "path" in payload else ""
        kid = str(payload["id"]) if "id" in payload else str(point.id)
        hid = f"imgkb_{kid}"
        if hid in hits:
            continue
        hits[hid] = CognitiveHit(
            kind=CogKind.IMAGE,
            id=hid,
            title=str(payload["content"])[:80] if "content" in payload else path,
            summary=path,
            score=float(point.score),
            handle=path if path.startswith("img_") else "",
            source=str(payload["plugin"]) if "plugin" in payload else "image",
        )
        ids.append(hid)
    return ids, hits


async def search_memes_backend(query: str, *, scope: CogScope, limit: int) -> _BackendResult:
    _ = scope
    try:
        from gsuid_core.ai_core.meme.config import meme_config
        from gsuid_core.ai_core.meme.library import MemeLibrary

        if not bool(meme_config.get_config("meme_enable").data):
            return _EMPTY
        raw_th = meme_config.get_config("meme_search_threshold").data
        threshold = float(raw_th) if isinstance(raw_th, (int, float)) else None
        records = await MemeLibrary.search_by_text(
            query_text=query,
            top_k=limit,
            score_threshold=threshold,
        )
    except Exception as e:
        logger.debug(t("log.ai.cognition_backend_fail", backend="meme", e=e))
        return _EMPTY
    ids: List[str] = []
    hits: Dict[str, CognitiveHit] = {}
    for rec in records:
        hid = f"meme_{rec.meme_id}"
        if hid in hits:
            continue
        tags = ", ".join(rec.all_tags) if rec.all_tags else ""
        hits[hid] = CognitiveHit(
            kind=CogKind.MEME,
            id=hid,
            title=rec.description or rec.meme_id,
            summary=f"{rec.meme_id} {tags}".strip(),
            score=0.55,
            source="meme",
        )
        ids.append(hid)
    return ids, hits


async def search_meme_knowledge_backend(query: str, *, scope: CogScope, limit: int) -> _BackendResult:
    try:
        from gsuid_core.ai_core.meme.database_model import AiMemeKnowledge

        scope_key = f"group:{scope.group_id}" if scope.group_id else ""
        rows = await AiMemeKnowledge.match_terms(
            query,
            bot_id=scope.bot_id or "",
            scope_key=scope_key,
            limit=limit,
        )
    except Exception as e:
        logger.debug(t("log.ai.cognition_backend_fail", backend="meme_knowledge", e=e))
        return _EMPTY
    ids: List[str] = []
    hits: Dict[str, CognitiveHit] = {}
    for row in rows:
        hid = f"memeknow_{row.id}"
        meaning = (row.meaning or "")[:120]
        hits[hid] = CognitiveHit(
            kind=CogKind.MEME_KNOWLEDGE,
            id=hid,
            title=row.term,
            summary=f"{meaning}（来源：{row.source or '未知'}）",
            score=min(0.95, 0.5 + row.confidence * 0.4),
            source="meme_knowledge",
        )
        ids.append(hid)
    return ids, hits
