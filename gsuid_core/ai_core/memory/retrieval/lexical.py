"""生产词面召回：query 实词 + 命中专名跨会话补条。

时间线/计数问句在 apply_query_episode_pack 整形；点查保持检索序。
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.ai_core.memory.retrieval.types import Episode
from gsuid_core.ai_core.memory.retrieval.lexical_pack import pack_timeline_episodes
from gsuid_core.ai_core.memory.retrieval.lexical_query import (
    _HIT_GENERIC,
    _HAS_DIGIT_RE,
    query_tokens,
    token_in_text,
    sql_like_tokens,
    topic_pin_tokens,
    fact_sweep_tokens,
    strip_clock_lines,
    token_search_forms,
    spread_topic_tokens,
    looks_like_sum_query,
    looks_like_count_query,
    attribute_content_tokens,
)
from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import (
    _SENT_SPLIT_RE,
    _FACT_SHEET_KIND,
    _extra_names,
    is_fact_sheet,
    states_a_value,
    _assistant_turn,
    keep_sum_values,
    _topic_hit_count,
    diversify_episodes,
    prefer_named_lines,
    merge_episode_lists,
    merge_strided_groups,
    spread_value_episodes,
    parse_episode_valid_at,
    sentence_has_extra_name,
    episode_mentions_speaker,
)


@runtime_checkable
class _SessionBearing(Protocol):
    session_id: str | None
    turn_index: int


@runtime_checkable
class _EpisodeOrmRow(Protocol):
    id: str
    content: str | None
    valid_at: object
    scope_key: str


def _row_session_pair(row: object) -> tuple[str | None, int | None]:
    if isinstance(row, _SessionBearing):
        return row.session_id, row.turn_index
    return None, None


def _episode_from_orm(row: object) -> Episode:
    if not isinstance(row, _EpisodeOrmRow):
        raise TypeError("episode row")
    sid, turn = _row_session_pair(row)
    return _episode_from_row(row.id, row.content or "", row.valid_at, row.scope_key, sid, turn)


_LEXICAL_CAP = 72


_HOP_TOKEN_CAP = 12


_WINDOW_EPISODE_CAP = 64


_ORDER_POOL_CAP = 400


_ORDER_TAIL_CAP = 80


def _polarity_samples(episodes: list[Episode], per_side: int) -> list[Episode]:
    """用户话正反极性各取几条，避免 hop 只从单侧簇长出来。"""
    from gsuid_core.ai_core.memory.ingestion.algebra import _fact_polarity

    pos: list[Episode] = []
    neg: list[Episode] = []
    for ep in episodes:
        raw = ep["content"] or ""
        if _assistant_turn(raw):
            continue
        bucket = neg if _fact_polarity(raw) else pos
        if len(bucket) < per_side:
            bucket.append(ep)
        if len(pos) >= per_side and len(neg) >= per_side:
            break
    return pos + neg


def extra_tokens_from_hits(episodes: list[Episode], query: str, cap: int = _HOP_TOKEN_CAP) -> list[str]:
    """从已命中片段抽专名，用来跨会话 LIKE。泛词（class/session）不进。"""
    qset = {t.lower() for t in query_tokens(query)}
    counts: dict[str, int] = {}
    sample = diversify_episodes(episodes, cap=16) if len(episodes) > 6 else list(episodes)
    seen_ids = {str(e["id"]) for e in sample if "id" in e}
    for ep in _polarity_samples(episodes, per_side=4):
        eid = str(ep["id"]) if "id" in ep else ""
        if eid and eid not in seen_ids:
            sample.append(ep)
            seen_ids.add(eid)
    for ep in sample:
        content = str(ep["content"]) if "content" in ep else ""
        if not content:
            continue
        for tok in query_tokens(content):
            if " " in tok:
                continue
            key = tok.lower()
            if key in qset or key in _HIT_GENERIC:
                continue
            if tok.isascii() and len(tok) < 4:
                continue
            counts[tok] = (counts[tok] if tok in counts else 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    return [tok for tok, _n in ranked[:cap]]


def memory_scope_key(user_id: str, group_id: str | None) -> str:
    """与 dual_route 一致：群用 group:，私聊才是 user_global:。"""
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

    if group_id:
        return make_scope_key(ScopeType.GROUP, group_id)
    return make_scope_key(ScopeType.USER_GLOBAL, user_id)


def _recall_db_failed(exc: BaseException, backend: str) -> bool:
    """库没初始化时的 TypeError 当空结果。库在时的 TypeError 不吞。"""
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.i18n import t
    from gsuid_core.logger import logger
    from gsuid_core.utils.database.base_models import async_maker

    offline = async_maker is None and isinstance(exc, TypeError)
    if not offline and not isinstance(exc, (OSError, SQLAlchemyError)):
        return False
    logger.debug(t("log.ai.cognition_backend_fail", backend=backend, e=exc))
    return True


async def expand_episode_neighbors(
    episodes: list[Episode],
    *,
    seed: int = 8,
    before: int = 3,
    after: int = 3,
    cap: int = 72,
) -> list[Episode]:
    """命中后再取同 scope 前后轮次，补足同会话指代但不挤占主召回预算。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    if not episodes or (before <= 0 and after <= 0):
        return episodes
    extra: list[Episode] = []
    seen = {str(ep["id"]) for ep in episodes if "id" in ep}
    for ep in list(episodes)[: max(1, seed)]:
        dt = parse_episode_valid_at(str(ep["valid_at"]) if "valid_at" in ep else "")
        scope_key = str(ep["scope_key"]) if "scope_key" in ep else ""
        if dt is None or not scope_key:
            continue
        from sqlalchemy.exc import SQLAlchemyError

        try:
            ep_id = str(ep["id"]) if "id" in ep else None
            rows = await AIMemEpisode.neighbors_by_time(scope_key, dt, before=before, after=after, episode_id=ep_id)
        except (OSError, SQLAlchemyError, TypeError) as e:
            if not _recall_db_failed(e, "neighbors"):
                raise
            return episodes
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            extra.append(_episode_from_orm(row))
    if not extra:
        return episodes
    return merge_episode_lists(list(episodes), extra, prefer_extras=True, limit=cap)


def turn_near_seed(seed_turn: int | None, row_turn: int | None, radius: int = 12) -> bool:
    """同一会话里，离命中轮太远的原句不补。"""
    if seed_turn is None or row_turn is None:
        return True
    return abs(seed_turn - row_turn) <= radius


async def expand_topic_session_turns(
    episodes: list[Episode],
    query: str,
    *,
    cap: int = 96,
    radius: int = 12,
    include_assistant: bool = False,
    named_only: bool = False,
    extra_cap: int | None = None,
) -> list[Episode]:
    """推荐题把命中会话附近的用户原句补进来。约束句往往不带问句里的主题词。"""
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    toks = [t.lower() for t in topic_pin_tokens(query, limit=3)]
    if not episodes or not toks:
        return episodes
    query_words = {t.lower() for t in query_tokens(query)}
    seeds: list[Episode] = []
    for ep in episodes:
        raw = ep["content"] or ""
        if not include_assistant and _assistant_turn(raw):
            continue
        blob = raw.lower()
        if any(token_in_text(tok, blob) for tok in toks):
            seeds.append(ep)
    extra: list[Episode] = []
    seen = {str(ep["id"]) for ep in episodes if "id" in ep}
    seen_sid: set[str] = set()
    seed_cap = 12 if named_only else 6
    for ep in seeds[:seed_cap]:
        sid = str(ep["session_id"]) if "session_id" in ep and ep["session_id"] else ""
        seed_turn = ep["turn_index"] if "turn_index" in ep else None
        seed_i = seed_turn if isinstance(seed_turn, int) else None
        if not sid:
            eid = str(ep["id"]) if "id" in ep else ""
            try:
                row = await AIMemEpisode.get_one(eid)
            except (OSError, SQLAlchemyError, TypeError) as e:
                if not _recall_db_failed(e, "topic_session"):
                    raise
                return episodes
            if row is None or not row.session_id:
                continue
            sid = row.session_id
            if seed_i is None and isinstance(row.turn_index, int):
                seed_i = row.turn_index
        if sid in seen_sid:
            continue
        seen_sid.add(sid)
        try:
            rows = await AIMemEpisode.get_session(sid)
        except (OSError, SQLAlchemyError, TypeError) as e:
            if not _recall_db_failed(e, "topic_session"):
                raise
            return episodes
        for row in rows:
            raw = row.content or ""
            if row.id in seen or (not include_assistant and _assistant_turn(raw)):
                continue
            if named_only and not sentence_has_extra_name(raw, query_words):
                continue
            if not turn_near_seed(seed_i, row.turn_index, radius):
                continue
            seen.add(row.id)
            extra.append(_episode_from_orm(row))
            if extra_cap is not None and len(extra) >= extra_cap:
                break
        if extra_cap is not None and len(extra) >= extra_cap:
            break
    if not extra:
        return episodes
    # 约束句放前面，避免预算先被向量命中占满。
    return merge_episode_lists(extra, list(episodes), prefer_extras=False, limit=cap)


def _episode_from_row(
    row_id: str,
    content: str,
    valid_at: object,
    scope_key: str,
    session_id: str | None = None,
    turn_index: int | None = None,
) -> Episode:
    if isinstance(valid_at, datetime):
        stamp = valid_at.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(valid_at, str):
        stamp = valid_at.replace("T", " ")[:19]
    else:
        stamp = ""
    ep = Episode(
        id=row_id,
        content=content,
        valid_at=stamp,
        scope_key=scope_key,
        embedding=[],
    )
    if session_id:
        ep["session_id"] = session_id
    if turn_index is not None:
        ep["turn_index"] = turn_index
    return ep


async def lexical_search_episodes(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
    hits: list[Episode],
    limit: int = _LEXICAL_CAP,
    start: datetime | None = None,
    end: datetime | None = None,
    user_only: bool = False,
) -> list[Episode]:
    """SQL LIKE 跨会话补条。一次 UNION 往返；单测无库则空列表。"""
    if not user_id or not (query or "").strip():
        return []
    raw: list[str] = query_tokens(query)
    for tok in extra_tokens_from_hits(hits, query):
        key = tok.lower()
        if any(t.lower() == key for t in raw):
            continue
        raw.append(tok)
        if len(raw) >= 22:
            break
    tokens = sql_like_tokens(raw)
    if not tokens:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    try:
        rows = await AIMemEpisode.search_by_tokens(
            memory_scope_key(user_id, group_id),
            tokens,
            limit=limit,
            start=start,
            end=end,
            user_only=user_only,
        )
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "lexical"):
            raise
        return []
    scored: dict[str, tuple[int, Episode]] = {}
    for row in rows:
        content = row.content or ""
        n = sum(1 for t in tokens if token_in_text(t, content))
        ep = _episode_from_orm(row)
        prev = scored[row.id] if row.id in scored else None
        if prev is None or n > prev[0]:
            scored[row.id] = (n, ep)
    ranked = sorted(
        scored.values(),
        key=lambda item: (-item[0], str(item[1]["valid_at"] if "valid_at" in item[1] else "")),
    )
    return [ep for _n, ep in ranked[:limit]]


async def attribute_pin_episodes(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
    memory_intent: str = "point_latest",
) -> list[Episode]:
    """话题词上带数字或日期的原句，早中晚取样。两个词的交集对不上原话。"""
    if not user_id or (
        memory_intent != "version_diff" and not looks_like_sum_query(query, memory_intent=memory_intent)
    ):
        return []
    # 主题词优先，再补一个问句实词，避免 expenses 把 bike 挤出前两名。
    toks = topic_pin_tokens(query, limit=2)
    for extra in attribute_content_tokens(query, limit=1):
        if extra.lower() not in {t.lower() for t in toks}:
            toks.append(extra)
    if not toks:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope = memory_scope_key(user_id, group_id)
    speaker = user_id if group_id else None
    found: list[Episode] = []
    seen: set[str] = set()
    try:
        for tok in toks[:3]:
            newest = await AIMemEpisode.search_by_all_tokens(
                scope,
                [tok],
                limit=36,
                ascending=False,
                user_only=True,
                speaker=speaker,
            )
            oldest = await AIMemEpisode.search_by_all_tokens(
                scope,
                [tok],
                limit=16,
                ascending=True,
                user_only=True,
                speaker=speaker,
            )
            for row in list(newest) + list(oldest):
                if row.id in seen:
                    continue
                raw = row.content or ""
                if _assistant_turn(raw) or not states_a_value(raw):
                    continue
                if speaker and not episode_mentions_speaker(raw, speaker):
                    continue
                seen.add(row.id)
                found.append(_episode_from_orm(row))
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "attribute_pin"):
            raise
        return []
    # 加总要留下每一笔，早中晚抽样会把中段金额丢掉。
    if looks_like_sum_query(query, memory_intent=memory_intent):
        return keep_sum_values(found, query, cap=24)
    return spread_value_episodes(found, cap=8)


async def episodes_in_time_window(
    *,
    user_id: str,
    group_id: str | None,
    start: datetime,
    end: datetime,
    limit: int = _WINDOW_EPISODE_CAP,
    one_per_day: bool = True,
) -> list[Episode]:
    """用户话两端取样。同日 00:00:00 时 GROUP BY 会把 LIMIT 打满在第一天。"""
    if not user_id:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    fetch_n = max(limit, 80)
    scope = memory_scope_key(user_id, group_id)
    openers: list[AIMemEpisode] = []
    try:
        openers = await AIMemEpisode.search_user_day_openers(scope, start, end, limit=max(limit, 40))
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "time_window_openers"):
            raise
        openers = []
    try:
        n = await AIMemEpisode.count_by_valid_at_range(scope, start, end, user_only=True)
        jobs = [
            AIMemEpisode.search_by_valid_at_range(scope, start, end, limit=fetch_n, user_only=True, ascending=True),
            AIMemEpisode.search_by_valid_at_range(scope, start, end, limit=fetch_n, user_only=True),
        ]
        if n > fetch_n * 2:
            for frac in (0.25, 0.5, 0.75):
                jobs.append(
                    AIMemEpisode.search_by_valid_at_range(
                        scope,
                        start,
                        end,
                        limit=fetch_n,
                        user_only=True,
                        ascending=True,
                        offset=int(n * frac),
                    )
                )
        batches = await asyncio.gather(*jobs)
        oldest = batches[0]
        newest = batches[1]
        extra_rows = [row for batch in batches[2:] for row in batch]
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "time_window"):
            raise
        return []
    seen: set[str] = set()
    eps: list[Episode] = []
    # 日开场在前，pack 才不会被同日作业题占掉；DESC 切片倒过来取当天最早而非最晚。
    for row in list(openers) + list(oldest) + extra_rows + list(reversed(list(newest))):
        if row.id in seen:
            continue
        seen.add(row.id)
        eps.append(_episode_from_orm(row))
    eps.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    if not one_per_day:
        return eps[:limit]
    return pack_timeline_episodes(eps, cap=limit)


async def user_episodes_chrono_sample(
    *,
    user_id: str,
    group_id: str | None,
    end: datetime | None = None,
    limit: int = _LEXICAL_CAP,
) -> list[Episode]:
    """无日期窗的排序/摘要：头 400 + 尾 80，交给 first-mention/里程碑再收。"""
    if not user_id:
        return []
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    lo = datetime(2000, 1, 1)
    hi = end if end is not None else datetime(2100, 1, 1)
    scope = memory_scope_key(user_id, group_id)
    try:
        n = await AIMemEpisode.count_by_valid_at_range(scope, lo, hi, user_only=True)
        head = min(n, max(limit, _ORDER_POOL_CAP)) if n else max(16, limit)
        tail = min(n, _ORDER_TAIL_CAP) if n else max(16, limit)
        jobs = [
            AIMemEpisode.search_by_valid_at_range(scope, lo, hi, limit=head, user_only=True, ascending=True),
            AIMemEpisode.search_by_valid_at_range(scope, lo, hi, limit=tail, user_only=True),
        ]
        if n > head:
            for frac in (0.2, 0.4, 0.6, 0.8):
                jobs.append(
                    AIMemEpisode.search_by_valid_at_range(
                        scope,
                        lo,
                        hi,
                        limit=_ORDER_TAIL_CAP,
                        user_only=True,
                        ascending=True,
                        offset=int(n * frac),
                    )
                )
        batches = await asyncio.gather(*jobs)
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "chrono_sample"):
            raise
        return []
    seen: set[str] = set()
    eps: list[Episode] = []
    for batch in batches:
        for row in batch:
            if row.id in seen:
                continue
            seen.add(row.id)
            eps.append(_episode_from_orm(row))
    eps.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return eps


async def spread_topic_episodes(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
    cap: int = 32,
) -> list[Episode]:
    """类名词的命中按时间铺开。头尾各十几条会丢掉中间的专名。"""
    if not user_id:
        return []
    toks = spread_topic_tokens(query)
    if not toks:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope = memory_scope_key(user_id, group_id)
    groups: list[list[Episode]] = []
    forms: list[str] = []
    for tok in toks:
        for form in token_search_forms(tok):
            if form.lower() not in {f.lower() for f in forms}:
                forms.append(form)
    try:
        for form in forms[:4]:
            local: list[Episode] = []
            local_seen: set[str] = set()
            newest = await AIMemEpisode.search_by_all_tokens(
                scope,
                [form],
                limit=48,
                ascending=False,
                user_only=True,
            )
            oldest = await AIMemEpisode.search_by_all_tokens(
                scope,
                [form],
                limit=24,
                ascending=True,
                user_only=True,
            )
            mids = []
            if len(newest) >= 48:
                for off in (32, 64, 96):
                    page = await AIMemEpisode.search_by_all_tokens(
                        scope,
                        [form],
                        limit=16,
                        ascending=True,
                        user_only=True,
                        offset=off,
                    )
                    mids.extend(page)
            for row in list(oldest) + mids + list(newest):
                if row.id in local_seen:
                    continue
                raw = row.content or ""
                if _assistant_turn(raw):
                    continue
                local_seen.add(row.id)
                local.append(_episode_from_orm(row))
            if len(local) > 16:
                local = prefer_named_lines(local, query, 24)
            if local:
                groups.append(local)
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "spread_topic"):
            raise
        return []
    return merge_strided_groups(groups, cap)


async def _assistant_topic_hits(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
) -> list[Episode]:
    """主题词命中的助手回复。序数保留，避免被前两个实词挤掉。"""
    if not user_id:
        return []
    toks = spread_topic_tokens(query)
    if not toks:
        return []
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope = memory_scope_key(user_id, group_id)
    hits: list[Episode] = []
    seen: set[str] = set()
    try:
        for tok in toks:
            rows = await AIMemEpisode.search_by_all_tokens(
                scope,
                [tok],
                limit=8,
                user_only=False,
            )
            for row in rows:
                raw = row.content or ""
                if row.id in seen or not _assistant_turn(raw):
                    continue
                seen.add(row.id)
                hits.append(_episode_from_orm(row))
                if len(hits) >= 4:
                    return hits
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "assistant_topic"):
            raise
        return []
    return hits


def _fact_pieces(content: str, token: str, *, require_token: bool) -> list[str]:
    pieces: list[str] = []
    for sent in _SENT_SPLIT_RE.split(content or ""):
        piece = " ".join(sent.split()).strip()
        if len(piece) < 24:
            continue
        if len(piece) > 260:
            piece = piece[:260].rstrip()
        low = piece.lower()
        topical = token_in_text(token, low) if token else False
        marked = bool(_HAS_DIGIT_RE.search(piece) or _extra_names(piece, set()))
        if require_token and not topical and not marked:
            continue
        if not require_token and not topical and not marked and len(piece) < 40:
            continue
        pieces.append(piece)
        if len(pieces) >= 2:
            break
    return pieces


def _clause_around_focus(piece: str, focus: str) -> str:
    if not focus:
        return piece[:360].rstrip()
    at = piece.lower().find(focus.lower())
    if at < 0:
        return piece[:360].rstrip()
    win_start = max(0, at - 100)
    space = piece.rfind(" ", win_start, at)
    start = space + 1 if space >= win_start else win_start
    end = min(len(piece), at + len(focus) + 240)
    return piece[start:end].strip(" ,.")


def _keep_event_piece(piece: str, *, strict_names: bool = False) -> bool:
    if piece.rstrip().endswith("?"):
        return False
    names: set[str] = set()
    has_name = bool(_extra_names(piece, names))
    if strict_names:
        return has_name
    return bool(has_name or _HAS_DIGIT_RE.search(piece))


def _primary_name(piece: str) -> str:
    empty: set[str] = set()
    found = _extra_names(piece, empty)
    if not found:
        return ""
    generic = {"hour", "hours", "day", "days"}
    specific = [name for name in found if name.lower() not in generic]
    pool = specific if specific else list(found)
    low = piece.lower()
    caps = [
        name for name in pool if (at := low.find(name)) >= 0 and piece[at : at + len(name)].isupper() and len(name) >= 3
    ]
    if caps:
        return max(caps, key=len)
    return max(pool, key=len)


def _fact_sweep_relevant(piece: str, query: str, *, memory_intent: str = "point_latest") -> bool:
    """清单只留与问句同主题的事实句。"""
    body = strip_clock_lines(query or "")
    if looks_like_sum_query(body, memory_intent=memory_intent):
        toks = [t for t in fact_sweep_tokens(body) if t.lower() not in {"total", "spent", "have", "in"}]
        topical = any(token_in_text(form, piece.lower()) for t in toks for form in token_search_forms(t))
        return topical and bool(_HAS_DIGIT_RE.search(piece))
    if looks_like_count_query(body) and memory_intent != "timeline_order":
        toks = fact_sweep_tokens(body)
        return any(token_in_text(t, piece.lower()) for t in toks) or _topic_hit_count(body, piece) >= 2
    return True


def compact_event_lines(rows: list[tuple[str, str]], *, collapse_names: bool) -> list[tuple[str, str]]:
    """每个专名只留最早、最短的那句用户原话。同一句不因多个专名重复。"""
    best: dict[str, tuple[str, str]] = {}
    strict = collapse_names
    for day, piece in sorted(rows):
        if not _keep_event_piece(piece, strict_names=strict):
            continue
        focus = _primary_name(piece)
        clause = _clause_around_focus(piece, focus)
        key = focus if collapse_names and focus else clause.lower()[:80]
        prev = best[key] if key in best else None
        if prev is None or day < prev[0] or (day == prev[0] and len(clause) < len(prev[1])):
            best[key] = (day, clause)
    emitted: list[tuple[str, str]] = []
    seen_clause: set[str] = set()
    for day, clause in sorted(best.values()):
        if clause in seen_clause:
            continue
        seen_clause.add(clause)
        emitted.append((day, f"用户说过：{clause}"))
    return emitted


async def build_fact_sweep(
    query: str,
    *,
    user_id: str,
    group_id: str | None,
    window: tuple[datetime, datetime] | None,
    memory_intent: str = "point_latest",
) -> Episode | None:
    """把主题词和点查当天的原句收成一张时间清单，避免被打包挤出注入。"""
    if not user_id:
        return None
    toks = fact_sweep_tokens(query)
    if not toks:
        return None
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope = memory_scope_key(user_id, group_id)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _push(valid_at: object, content: str, token: str, *, require_token: bool) -> None:
        if isinstance(valid_at, datetime):
            day = valid_at.strftime("%Y-%m-%d")
        else:
            day = str(valid_at)[:10]
        for piece in _fact_pieces(content, token, require_token=require_token):
            key = piece.lower()[:96]
            if key in seen or len(found) >= 96:
                continue
            seen.add(key)
            found.append((day, piece))

    async def _collect_token(tok: str, *, require_token: bool) -> None:
        forms = token_search_forms(tok)[:2]
        for form in forms:
            newest = await AIMemEpisode.search_by_all_tokens(scope, [form], limit=36, ascending=False, user_only=True)
            oldest = await AIMemEpisode.search_by_all_tokens(scope, [form], limit=16, ascending=True, user_only=True)
            rows = list(oldest) + list(newest)
            if len(newest) >= 36:
                for off in (20, 40, 60):
                    rows.extend(
                        await AIMemEpisode.search_by_all_tokens(
                            scope, [form], limit=12, ascending=True, user_only=True, offset=off
                        )
                    )
            for row in rows:
                raw = row.content or ""
                if _assistant_turn(raw):
                    continue
                _push(row.valid_at, raw, form, require_token=require_token)

    try:
        for tok in toks:
            await _collect_token(tok, require_token=True)
        if not looks_like_sum_query(query, memory_intent=memory_intent):
            hop_seed = [
                _episode_from_row(f"hop{i}", piece, f"{day} 12:00:00", scope)
                for i, (day, piece) in enumerate(found[:24])
                if piece
            ]
            hop_toks = extra_tokens_from_hits(hop_seed, query, cap=_HOP_TOKEN_CAP)
            seen_tok = {t.lower() for t in toks}
            for hop in hop_toks:
                if hop.lower() in seen_tok:
                    continue
                seen_tok.add(hop.lower())
                await _collect_token(hop, require_token=True)
        if window is not None:
            day_eps = await episodes_in_time_window(
                user_id=user_id,
                group_id=group_id,
                start=window[0],
                end=window[1],
                limit=20,
                one_per_day=False,
            )
            for ep in day_eps:
                raw = ep["content"] if "content" in ep else ""
                if _assistant_turn(raw):
                    continue
                _push(ep["valid_at"] if "valid_at" in ep else "", raw, toks[0], require_token=False)
    except (OSError, SQLAlchemyError, TypeError) as e:
        if not _recall_db_failed(e, "fact_sweep"):
            raise
        return None
    if not found:
        return None
    collapse = (looks_like_count_query(query) or memory_intent == "timeline_order") and not looks_like_sum_query(
        query, memory_intent=memory_intent
    )
    sorted_found = sorted((d, p) for d, p in found if _fact_sweep_relevant(p, query, memory_intent=memory_intent))
    if not sorted_found:
        sorted_found = sorted(found)
    if collapse:
        compacted = compact_event_lines(sorted_found, collapse_names=True)
        chosen = compacted
        if len(chosen) > 36:
            step = len(chosen) / 36
            chosen = [chosen[int(i * step)] for i in range(36)]
    elif looks_like_count_query(query) or looks_like_sum_query(query, memory_intent=memory_intent):
        pool = sorted_found
        if looks_like_sum_query(query, memory_intent=memory_intent):
            numbered = [(d, p) for d, p in sorted_found if _HAS_DIGIT_RE.search(p)]
            pool = numbered or sorted_found
        compacted = compact_event_lines(pool, collapse_names=False)
        chosen = compacted if compacted else pool
        if len(chosen) > 40:
            step = len(chosen) / 40
            chosen = [chosen[int(i * step)] for i in range(40)]
    else:
        lo = window[0].strftime("%Y-%m-%d") if window is not None else ""
        hi = window[1].strftime("%Y-%m-%d") if window is not None else ""
        window_lines = [item for item in found if lo and lo <= item[0] <= hi]
        topic_lines = [item for item in found if item not in window_lines]
        novel = [item for item in window_lines if not any(token_in_text(tok, item[1].lower()) for tok in toks)]
        known = [item for item in window_lines if item not in novel]
        topic_lines.sort()
        if len(topic_lines) > 28:
            step = len(topic_lines) / 28
            topic_lines = [topic_lines[int(i * step)] for i in range(28)]
        novel.sort()
        known.sort()
        chosen = [(d, p) for d, p in novel[:8] + known[:4] + topic_lines]
    body_lines: list[str] = []
    used = 0
    for day, piece in chosen:
        line = f"[{day}] {piece}"
        if used + len(line) > 4800:
            break
        body_lines.append(line)
        used += len(line) + 1
    if not body_lines:
        return None
    body = "\n".join(body_lines)
    days = [day for day, _piece in chosen if day]
    stamp = f"{days[-1]} 23:59:59" if days else ""
    sheet = _episode_from_row("fact-sheet", body, stamp, scope)
    sheet["kind"] = _FACT_SHEET_KIND
    return sheet


async def expand_lexical_recall(
    episodes: list[Episode],
    *,
    query: str,
    user_id: str,
    group_id: str | None,
    limit: int = _LEXICAL_CAP,
    clock: datetime | None = None,
    reserved: list[Episode] | None = None,
    memory_intent: str = "",
    bot_id: str = "",
    dm_group_scopes: tuple[str, ...] | list[str] | None = None,
) -> list[Episode]:
    """向量命中后再词面跨会话补齐；相对日窗口用显式 clock，缺省墙上时钟。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        query_time_window,
        temporal_search_query,
        has_relative_time_span,
        query_explicit_time_range,
        strip_relative_time_spans,
    )

    body = strip_clock_lines(query or "")
    relative = has_relative_time_span(body)
    search_q = strip_relative_time_spans(body) if relative else body
    search_q = search_q or body
    orderish = memory_intent in ("timeline_order", "broad_summary")
    if orderish:
        topic = temporal_search_query(body)
        if topic:
            search_q = topic
    clock_used = clock if clock is not None else datetime.now()
    # 无显式日期的排序/摘要不按墙上相对日切窗。
    if orderish:
        window = query_explicit_time_range(body)
    else:
        window = query_time_window(body, clock_used)
        if window is None:
            window = query_explicit_time_range(body)
    in_win = window is not None
    win_start = window[0] if window is not None else None
    win_end = window[1] if window is not None else None
    point_ago = relative and not orderish
    # 点查的「N 天前」是事件日，不是发言日。词面仍全库搜，窗口只用来加当天原话。
    lex_start = None if point_ago else win_start
    lex_end = None if point_ago else win_end
    count_q = looks_like_count_query(body)
    extras = await lexical_search_episodes(
        search_q,
        user_id=user_id,
        group_id=group_id,
        hits=episodes,
        limit=limit,
        start=lex_start,
        end=lex_end,
        user_only=in_win or count_q,
    )
    if group_id is None and user_id:
        # 私聊跨群词面补召回：只检索与本人直接相关的群聊 Episode，并附 [群:gid] 标注
        import asyncio as _asyncio

        from gsuid_core.ai_core.memory.privacy import (
            parse_group_from_scope,
            resolve_user_group_scopes,
            filter_dm_episodes_for_user,
        )
        from gsuid_core.ai_core.migrations.cross_group_isolation_v1 import ensure_isolation_watermark_ts

        dm_gscopes = (
            tuple(dm_group_scopes)
            if dm_group_scopes is not None
            else await resolve_user_group_scopes(user_id, bot_id=bot_id)
        )
        gids_to_query = [gid_item for gsk in dm_gscopes[:12] if (gid_item := parse_group_from_scope(gsk))]
        dm_group_extras: list[Episode] = []
        if gids_to_query:
            batches = await _asyncio.gather(
                *(
                    lexical_search_episodes(
                        search_q,
                        user_id=user_id,
                        group_id=gid_item,
                        hits=episodes,
                        limit=max(4, limit // 4),
                        start=lex_start,
                        end=lex_end,
                        user_only=in_win or count_q,
                    )
                    for gid_item in gids_to_query
                )
            )
            for grp_hits in batches:
                dm_group_extras.extend(grp_hits)
        if dm_group_extras:
            from gsuid_core.ai_core.memory.privacy import load_episode_speaker_map

            sp_map = await load_episode_speaker_map([ep["id"] for ep in dm_group_extras if "id" in ep and ep["id"]])
            filtered_dm = filter_dm_episodes_for_user(
                dm_group_extras,
                user_id=user_id,
                watermark_ts=await ensure_isolation_watermark_ts(),
                speaker_map=sp_map,
            )
            extras = merge_episode_lists(extras, filtered_dm, prefer_extras=False, limit=limit)
    merged = merge_episode_lists(episodes, extras, prefer_extras=False, limit=limit)
    if extras:
        names = extra_tokens_from_hits(extras, search_q, cap=_HOP_TOKEN_CAP)
        if names:
            hop = await lexical_search_episodes(
                " ".join(names),
                user_id=user_id,
                group_id=group_id,
                hits=merged,
                limit=limit,
                start=lex_start,
                end=lex_end,
                user_only=in_win or count_q,
            )
            merged = merge_episode_lists(merged, hop, prefer_extras=True, limit=limit)
    sum_q = looks_like_sum_query(body, memory_intent=memory_intent)
    need_spread = (not orderish) and (count_q or sum_q or memory_intent == "version_diff")
    spread_q = ""
    if orderish or need_spread:
        spread_q = search_q or body
    # 点查不得被主题扩散/事实清单重排：FTS/向量命中的证据句必须留在头部。
    if spread_q:
        spread = await spread_topic_episodes(spread_q, user_id=user_id, group_id=group_id)
        if spread:
            if memory_intent == "point_latest" and not (count_q or sum_q or orderish):
                merged = merge_episode_lists(merged, spread, prefer_extras=False, limit=max(limit, 96))
            else:
                merged = merge_episode_lists(spread, merged, prefer_extras=False, limit=max(limit, 96))
        assist = await _assistant_topic_hits(spread_q, user_id=user_id, group_id=group_id)
        seeded = await expand_topic_session_turns(
            [*merged, *assist],
            spread_q,
            cap=max(limit, 96),
            radius=40,
            include_assistant=True,
            named_only=True,
            extra_cap=20,
        )
        # 长回复默认只当邻句种子。全历程摘要保留关键助手句。
        if memory_intent == "broad_summary":
            merged = merge_episode_lists(assist, seeded, prefer_extras=True, limit=max(limit, 96))
        else:
            drop = {ep["id"] for ep in assist if "id" in ep}
            merged = [ep for ep in seeded if "id" not in ep or ep["id"] not in drop]
        sheet = await build_fact_sweep(
            spread_q,
            user_id=user_id,
            group_id=group_id,
            window=window if point_ago else None,
            memory_intent=memory_intent,
        )
        if sheet is not None:
            merged = [sheet, *merged]
    if window is not None:
        ranged = await episodes_in_time_window(
            user_id=user_id,
            group_id=group_id,
            start=window[0],
            end=window[1],
            limit=24 if point_ago else _WINDOW_EPISODE_CAP,
            one_per_day=not point_ago,
        )
        if ranged:
            if point_ago:
                user_day = [ep for ep in ranged if not _assistant_turn(ep["content"] or "")]
                merged = merge_episode_lists(user_day[:8], merged, prefer_extras=False, limit=max(limit, 96))
            else:
                merged = merge_episode_lists(merged, ranged, prefer_extras=True, limit=limit)
    elif orderish:
        # 主题线程召回只在 dual_route 跑一次；这里只在候选过薄时退回时间采样。
        if len(merged) < 8:
            sample = await user_episodes_chrono_sample(
                user_id=user_id,
                group_id=group_id,
                end=None,
                limit=_ORDER_POOL_CAP,
            )
            if sample:
                pool = max(limit, len(sample))
                merged = merge_episode_lists(merged, sample, prefer_extras=True, limit=pool)
        missing_ids = [
            str(ep["id"])
            for ep in merged
            if "id" in ep and not is_fact_sheet(ep) and not (ep["embedding"] if "embedding" in ep else [])
        ]
        if missing_ids:
            from gsuid_core.ai_core.memory.vector.ops import retrieve_episode_dense_vectors

            vecs = await retrieve_episode_dense_vectors(missing_ids)
            if vecs:
                for ep in merged:
                    eid = str(ep["id"]) if "id" in ep else ""
                    if eid and eid in vecs:
                        ep["embedding"] = vecs[eid]
        return merged
    if memory_intent == "version_diff":
        pins = await attribute_pin_episodes(
            body,
            user_id=user_id,
            group_id=group_id,
            memory_intent=memory_intent,
        )
        if reserved is not None:
            reserved.extend(pins)
        if pins:
            merged = merge_episode_lists(pins, merged, prefer_extras=False, limit=max(limit, 96))
        return merged
    return merged[: max(limit, 1)]
