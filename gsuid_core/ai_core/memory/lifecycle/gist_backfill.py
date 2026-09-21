"""Turn gist 回填（V5 步骤 2–3）。rule 零 LLM；llm 睡眠期覆盖。不改 Episode 行。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.config import memory_config


async def backfill_rule_scope(scope_key: str, limit: int = 4000) -> int:
    """把该 scope 用户 turn 写成 gist_source=rule。已有 llm 行不覆盖。"""
    if not scope_key:
        return 0
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode, AIMemTurnGist
    from gsuid_core.ai_core.memory.retrieval.lexical import _assistant_turn
    from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard
    from gsuid_core.ai_core.memory.retrieval.ledger_timeline import LEDGER_LINE_CHARS, rule_gist

    eps = await AIMemEpisode.list_by_scope(scope_key, limit=limit)
    existing = {g.episode_id: g for g in await AIMemTurnGist.list_by_scope(scope_key, limit=limit)}
    line_chars = LEDGER_LINE_CHARS
    rows: list[AIMemTurnGist] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for ep in eps:
        raw = ep.content or ""
        if _assistant_turn(raw):
            continue
        if ep.id in existing and existing[ep.id].gist_source == "llm":
            continue
        at = ep.valid_at
        if at is None:
            continue
        naive = at.replace(tzinfo=None) if at.tzinfo is not None else at
        gist, digest = rule_gist(raw, line_chars)
        rows.append(
            AIMemTurnGist(
                episode_id=ep.id,
                scope_key=scope_key,
                session_id=ep.session_id or "",
                turn_index=int(ep.turn_index or 0),
                valid_at=naive,
                gist=gist,
                gist_source="rule",
                is_new_aspect=None,
                code_digest=digest,
                source_tag="",
                model="",
                created_at=now,
            )
        )
    if not rows:
        return 0
    async with db_write_guard():
        n = await AIMemTurnGist.upsert_rows(rows)
    logger.info(i18n_t("log.memory.gist_backfill", scope_key=scope_key, n=n, source="rule"))
    return n


async def backfill_llm_session(session_id: str, *, force: bool = False) -> int:
    """睡眠期：每 session 一次小模型，覆盖 rule 行。eval_mode 仅 force 才跑。"""
    if not session_id or (memory_config.eval_mode and not force):
        return 0
    from gsuid_core.ai_core.utils import extract_json_from_text
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode, AIMemSession, AIMemTurnGist
    from gsuid_core.ai_core.memory.retrieval.lexical import _assistant_turn, _speaker_stripped
    from gsuid_core.ai_core.memory.prompts.extraction import GIST_EXTRACTION_USER, GIST_EXTRACTION_SYSTEM
    from gsuid_core.ai_core.memory.ingestion.eval_write_lock import db_write_guard
    from gsuid_core.ai_core.memory.retrieval.ledger_timeline import LEDGER_LINE_CHARS, rule_gist

    sess_rows = await AIMemSession.get_by_ids([session_id])
    if not sess_rows:
        return 0
    sess = sess_rows[0]
    rows = await AIMemEpisode.get_session(session_id)
    turns: list[str] = []
    by_idx: dict[int, AIMemEpisode] = {}
    line_chars = LEDGER_LINE_CHARS
    for row in rows:
        raw = row.content or ""
        if _assistant_turn(raw):
            continue
        idx = int(row.turn_index or 0)
        by_idx[idx] = row
        gist, _d = rule_gist(raw, line_chars)
        day = row.valid_at.strftime("%Y-%m-%d") if row.valid_at else ""
        body = _speaker_stripped(raw).replace("\n", " ").strip()[:600]
        turns.append(f"{idx} · {day} · {gist or body}")
    if not turns:
        return 0
    prompt = GIST_EXTRACTION_USER.format(turns="\n".join(turns))
    try:
        agent = create_agent(task_level="low")
        raw_out = await agent.run(GIST_EXTRACTION_SYSTEM + "\n" + prompt)
    except (TimeoutError, OSError, RuntimeError) as e:
        logger.debug(i18n_t("log.memory.gist_backfill_fail", scope_key=sess.scope_key, e=e))
        return 0
    text = raw_out if isinstance(raw_out, str) else str(raw_out)
    doc = extract_json_from_text(text)
    if not isinstance(doc, dict) or "turns" not in doc:
        return 0
    items = doc["turns"]
    if not isinstance(items, list):
        return 0
    title = str(doc["title"]).strip() if "title" in doc else ""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out: list[AIMemTurnGist] = []
    for item in items:
        if not isinstance(item, dict) or "i" not in item or "g" not in item:
            continue
        try:
            idx = int(item["i"])
        except (TypeError, ValueError):
            continue
        if idx not in by_idx:
            continue
        ep = by_idx[idx]
        at = ep.valid_at
        naive = at.replace(tzinfo=None) if at is not None and at.tzinfo is not None else at
        if naive is None:
            continue
        new_flag = item["new"] if "new" in item and isinstance(item["new"], bool) else None
        out.append(
            AIMemTurnGist(
                episode_id=ep.id,
                scope_key=sess.scope_key,
                session_id=session_id,
                turn_index=idx,
                valid_at=naive,
                gist=str(item["g"]).strip()[:200],
                gist_source="llm",
                is_new_aspect=new_flag,
                code_digest="",
                source_tag="",
                model="low",
                created_at=now,
            )
        )
    if not out:
        return 0
    async with db_write_guard():
        n = await AIMemTurnGist.upsert_rows(out)
    if title:
        async with db_write_guard():
            await AIMemSession.set_title(session_id, title, "llm")
    logger.info(i18n_t("log.memory.gist_backfill", scope_key=sess.scope_key, n=n, source="llm"))
    return n


async def run_gist_backfill_tick(limit: int = 8) -> int:
    """浅睡：先 rule 回填未挂 gist 的 scope，再可选 llm。"""
    from gsuid_core.ai_core.memory.database.models import AIMemSession

    leftover = await AIMemSession.list_untitled(limit=limit)
    seen: set[str] = set()
    done = 0
    for sess in leftover:
        if sess.scope_key in seen:
            continue
        seen.add(sess.scope_key)
        try:
            done += await backfill_rule_scope(sess.scope_key)
        except (OSError, SQLAlchemyError) as e:
            logger.debug(i18n_t("log.memory.gist_backfill_fail", scope_key=sess.scope_key, e=e))
        if done >= limit:
            break
    return done
