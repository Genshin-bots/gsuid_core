"""Turn Ledger：全量压缩时间线（零 LLM rule gist）。ledger 路径不替 reader 选 N。"""

from __future__ import annotations

import re
from datetime import datetime
from dataclasses import field, dataclass

from gsuid_core.ai_core.memory.config import memory_config

LEDGER_LINE_CHARS = 160
LEDGER_FULL_TURNS = 260
LEDGER_SESSION_FLOOR = 3

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", re.M)
_DEF_RE = re.compile(r"^\s*def\s+(\w+)", re.M)
_ERR_RE = re.compile(r"(?i)\b(?:error|exception|traceback)\b[:\s]+(.{0,40})")
_SENT_RE = re.compile(r"(?<=[.!?。！？])\s+")


@dataclass
class LedgerLine:
    mark: str
    episode_id: str
    session_id: str
    valid_at: datetime
    turn_index: int
    gist: str
    is_new: bool
    day: str
    blob: str = ""
    focus: bool = False


@dataclass
class LedgerView:
    lines: list[LedgerLine] = field(default_factory=list)
    ledger_ids: dict[str, str] = field(default_factory=dict)
    pool_ids: list[str] = field(default_factory=list)
    inject_ids: list[str] = field(default_factory=list)
    chars: int = 0
    header: str = ""


def rule_gist(content: str, line_chars: int) -> tuple[str, str]:
    """零 LLM：去说话人前缀 / 代码块，丢掉自我介绍，保留首句+尾句。不丢 turn。"""
    from gsuid_core.ai_core.memory.retrieval.lexical import _speaker_stripped, _prose_without_markup

    raw = _speaker_stripped(content or "")
    digest_parts: list[str] = []
    fences = _FENCE_RE.findall(raw)
    if fences:
        blob = "\n".join(fences)
        imps = _IMPORT_RE.findall(blob)[:4]
        defs = _DEF_RE.findall(blob)[:4]
        err = _ERR_RE.search(blob)
        nlines = sum(x.count("\n") + 1 for x in fences)
        bits = [f"{nlines} lines"]
        if imps:
            bits.append("import " + ",".join(imps))
        if defs:
            bits.append("def " + ",".join(defs))
        if err is not None:
            bits.append("error: " + err.group(1).strip())
        digest_parts.append("[code: " + "; ".join(bits) + "]")
    prose = _prose_without_markup(_FENCE_RE.sub(" ", raw))
    kept: list[str] = []
    for sent in _SENT_RE.split(prose) if prose else []:
        s = sent.strip()
        if not s:
            continue
        kept.append(s)
    if not kept:
        body = prose
    elif len(kept) == 1:
        body = kept[0]
    else:
        body = kept[0] + " " + kept[-1]
    if digest_parts and body:
        body = digest_parts[0] + " " + body
    elif digest_parts and not body:
        body = digest_parts[0]
    body = " ".join(body.split())
    if len(body) > line_chars:
        half = max(24, (line_chars - 1) // 2)
        body = body[:half].rstrip() + "…" + body[-half:].lstrip()
    return body or "…", " ".join(digest_parts)


def ledger_focus_limit(query: str) -> int:
    """排序要看见整段先后，不只 12 个亮点。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_order_query

    if looks_like_order_query(query):
        return 24
    return 12


def ledger_focus_width(query: str) -> int:
    from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_order_query

    if looks_like_order_query(query):
        return 280
    return 400


def focus_gist(content: str, query: str, width: int) -> str:
    """问句对得上的 turn 截到实词附近，而不是只留 160 字首尾。"""
    from gsuid_core.ai_core.memory.retrieval.lexical import _speaker_stripped, excerpt_around_tokens

    raw = _speaker_stripped(content or "")
    prose = _FENCE_RE.sub(" ", raw)
    return excerpt_around_tokens(prose, query, width) or "…"


def _gist_tokens(text: str) -> set[str]:
    from gsuid_core.ai_core.memory.retrieval.lexical import query_tokens

    return {t.lower() for t in query_tokens(text) if len(t) >= 4}


def protected_ids(lines: list[LedgerLine], floor: int) -> set[str]:
    """★、每 session 前 floor 行、最后 20% 时间（或末尾 20% 行）不裁。"""
    out: set[str] = set()
    for ln in lines:
        if ln.focus:
            out.add(ln.episode_id)
    seen_sess: dict[str, int] = {}
    for ln in lines:
        sid = ln.session_id or ln.episode_id
        n = seen_sess[sid] if sid in seen_sess else 0
        if n < floor or ln.is_new:
            out.add(ln.episode_id)
        seen_sess[sid] = n + 1
    if not lines:
        return out
    t0 = lines[0].valid_at
    t1 = lines[-1].valid_at
    span = (t1 - t0).total_seconds()
    if span > 0:
        from datetime import timedelta

        cut = t1 - timedelta(seconds=span * 0.2)
        for ln in lines:
            if ln.valid_at >= cut:
                out.add(ln.episode_id)
    else:
        tail = max(1, len(lines) // 5)
        for ln in lines[-tail:]:
            out.add(ln.episode_id)
    return out


def _is_new(gist: str, prev: list[set[str]]) -> bool:
    now = _gist_tokens(gist)
    if not now:
        return True
    if not prev:
        return True
    for old in prev[-8:]:
        if not old:
            continue
        if (len(now & old) / min(len(now), len(old))) >= 0.35:
            return False
    return True


def session_groups(lines: list[LedgerLine]) -> list[tuple[int, str, list[LedgerLine]]]:
    order: list[str] = []
    groups: dict[str, list[LedgerLine]] = {}
    for ln in lines:
        sid = ln.session_id or "_"
        if sid not in groups:
            order.append(sid)
            groups[sid] = []
        groups[sid].append(ln)
    return [(i, sid, groups[sid]) for i, sid in enumerate(order, 1)]


def score_blob(content: str) -> str:
    """头尾各留一段，避免 gist 丢掉中段。"""
    from gsuid_core.ai_core.memory.retrieval.lexical import _speaker_stripped

    prose = " ".join(_speaker_stripped(content or "").split())
    if len(prose) <= 400:
        return prose
    return (prose[:240] + " " + prose[-160:]).strip()


def format_ledger_block(view: LedgerView, query: str, *, subset: bool = False) -> str:
    from gsuid_core.ai_core.memory.retrieval.event_time import order_topic_span, query_only_item_cap

    topic = order_topic_span(query) or "the topic in the question"
    n = query_only_item_cap(query)
    n_en = f" (up to {n} items)" if n else ""
    n_zh = f"，最多 {n} 条" if n else ""
    english = bool(re.search(r"[A-Za-z]{4,}", query or ""))
    if english:
        head = (
            f"Cite timeline #id lines about {topic}{n_en}. Stay on the asked storyline. "
            "★ marks a new sub-topic versus earlier lines."
        )
        title = (
            "【User-turn timeline (subset, chronological; #id = full timeline)】"
            if subset
            else "【User-turn timeline (complete, chronological; ★ = new sub-topic vs earlier; #id for answers)】"
        )
    else:
        head = f"按时间线 #id 引用与「{topic}」相关的发言{n_zh}。★ 表示相对之前出现了新子话题。"
        title = (
            "【用户发言时间线（节选；#id 与全量时间线一致）】"
            if subset
            else "【用户发言时间线（完整，按时间；★ = 相对之前提出了新子话题；#id 供作答引用）】"
        )
    groups: list[str] = [title]
    last_sid = ""
    session_n = 0
    for line in view.lines:
        if line.session_id and line.session_id != last_sid:
            session_n += 1
            last_sid = line.session_id
            groups.append(f"── session {session_n} · {line.day} ──")
        star = "★ " if line.is_new else "  "
        groups.append(f"{line.mark}  {line.day} {star}{line.gist}")
    groups.append(head)
    return "\n".join(groups)


async def build_ledger(scope_keys: list[str], query: str) -> LedgerView:
    """按时间列出用户 turn 的 gist。有侧表用侧表；否则现场 rule，行尾标 †。"""
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode, AIMemSession, AIMemTurnGist
    from gsuid_core.ai_core.memory.retrieval.lexical import token_in_text, attribute_content_tokens

    line_chars = LEDGER_LINE_CHARS
    max_chars = int(memory_config.ledger_max_chars)
    full_n = LEDGER_FULL_TURNS
    floor = LEDGER_SESSION_FLOOR
    rows: list[tuple[str, str, datetime, int, str]] = []
    gist_by_ep: dict[str, tuple[str, bool | None]] = {}
    for sk in scope_keys:
        eps = await AIMemEpisode.list_by_scope(sk, limit=4000)
        from gsuid_core.ai_core.memory.retrieval.lexical import _assistant_turn

        for ep in eps:
            raw = ep.content or ""
            if _assistant_turn(raw):
                continue
            at = ep.valid_at
            if at is None:
                continue
            naive = at.replace(tzinfo=None) if at.tzinfo is not None else at
            sid = ep.session_id or ""
            turn = int(ep.turn_index or 0)
            rows.append((ep.id, sid, naive, turn, raw))
        try:
            for g in await AIMemTurnGist.list_by_scope(sk, limit=4000):
                gist_by_ep[g.episode_id] = (g.gist, g.is_new_aspect)
        except SQLAlchemyError:
            pass
    rows.sort(key=lambda r: (r[2], r[3], r[0]))
    sess_day: dict[str, str] = {}
    for sk in scope_keys:
        for sess in await AIMemSession.list_by_scope(sk, limit=400):
            sess_day[sess.id] = sess.start_at.strftime("%Y-%m-%d") if sess.start_at else ""

    prev_tok: dict[str, list[set[str]]] = {}
    focus_toks = attribute_content_tokens(query)
    ranked_focus: list[tuple[int, int, str]] = []
    for idx, (eid, _sid, _at, _turn, raw) in enumerate(rows):
        blob = raw.lower()
        overlap = sum(1 for tok in focus_toks if token_in_text(tok, blob))
        ranked_focus.append((overlap, idx, eid))
    ranked_focus.sort(key=lambda item: (-item[0], item[1]))
    focus_ids: set[str] = set()
    focus_cap = ledger_focus_limit(query)
    focus_width = ledger_focus_width(query)
    for overlap, _idx, eid in ranked_focus:
        if overlap < 1 or len(focus_ids) >= focus_cap:
            break
        focus_ids.add(eid)
    raw_lines: list[LedgerLine] = []
    for eid, sid, at, turn, raw in rows:
        focused = eid in focus_ids
        if focused:
            gist = focus_gist(raw, query, focus_width)
            flag = gist_by_ep[eid][1] if eid in gist_by_ep else None
            if not gist.endswith(" †") and eid not in gist_by_ep:
                gist = gist + " †"
        elif eid in gist_by_ep:
            gist, flag = gist_by_ep[eid]
            if len(gist) > line_chars:
                gist = gist[: line_chars - 1].rstrip() + "…"
        else:
            gist, _digest = rule_gist(raw, line_chars)
            gist = gist + " †"
            flag = None
        bucket = prev_tok[sid] if sid in prev_tok else []
        is_new = flag if flag is not None else _is_new(gist, bucket)
        if sid not in prev_tok:
            prev_tok[sid] = []
        prev_tok[sid].append(_gist_tokens(gist))
        day = sess_day[sid] if sid in sess_day and sess_day[sid] else at.strftime("%Y-%m-%d")
        raw_lines.append(
            LedgerLine(
                mark="",
                episode_id=eid,
                session_id=sid,
                valid_at=at,
                turn_index=turn,
                gist=gist,
                is_new=is_new,
                day=day,
                blob=score_blob(raw),
                focus=focused,
            )
        )
    pool = [ln.episode_id for ln in raw_lines]
    kept = list(raw_lines)
    over_n = len(kept) > full_n
    draft = _assign_marks(kept)
    if over_n or _block_chars(draft, query) > max_chars:
        kept = fit_ledger(kept, query, max_chars, floor, line_chars)
    numbered = _assign_marks(kept)
    ids = {ln.mark: ln.episode_id for ln in numbered}
    view = LedgerView(
        lines=numbered,
        ledger_ids=ids,
        pool_ids=pool,
        inject_ids=[ln.episode_id for ln in numbered],
        chars=0,
    )
    block = format_ledger_block(view, query)
    view.chars = len(block)
    view.header = block
    return view


def _assign_marks(lines: list[LedgerLine]) -> list[LedgerLine]:
    out: list[LedgerLine] = []
    for i, ln in enumerate(lines, 1):
        out.append(
            LedgerLine(
                mark=f"#{i}",
                episode_id=ln.episode_id,
                session_id=ln.session_id,
                valid_at=ln.valid_at,
                turn_index=ln.turn_index,
                gist=ln.gist,
                is_new=ln.is_new,
                day=ln.day,
                blob=ln.blob,
                focus=ln.focus,
            )
        )
    return out


def _block_chars(lines: list[LedgerLine], query: str) -> int:
    tmp = LedgerView(lines=_assign_marks(lines))
    return len(format_ledger_block(tmp, query))


def _shrink_gist(gist: str, width: int) -> str:
    body = gist
    if body.endswith(" †"):
        core = body[:-2]
        marked = True
    else:
        core = body
        marked = False
    if len(core) <= width:
        return body
    half = max(16, (width - 1) // 2)
    cut = core[:half].rstrip() + "…" + core[-half:].lstrip()
    return cut + (" †" if marked else "")


def _copy_line(ln: LedgerLine, gist: str) -> LedgerLine:
    return LedgerLine(
        mark=ln.mark,
        episode_id=ln.episode_id,
        session_id=ln.session_id,
        valid_at=ln.valid_at,
        turn_index=ln.turn_index,
        gist=gist,
        is_new=ln.is_new,
        day=ln.day,
        blob=ln.blob,
        focus=ln.focus,
    )


def fit_ledger(
    lines: list[LedgerLine],
    query: str,
    max_chars: int,
    floor: int,
    line_chars: int,
) -> list[LedgerLine]:
    """先缩行宽；仍超预算则按时间均匀抽。问句对得上的行最后才缩。"""
    if any(ln.focus for ln in lines):
        kept = list(lines)
        for width in (120, 80):
            shrunk: list[LedgerLine] = []
            for ln in lines:
                if ln.focus:
                    shrunk.append(ln)
                else:
                    shrunk.append(_copy_line(ln, _shrink_gist(ln.gist, width)))
            kept = shrunk
            if _block_chars(kept, query) <= max_chars:
                return kept
        for width in (240, 160):
            nxt: list[LedgerLine] = []
            for ln in kept:
                if ln.focus:
                    nxt.append(_copy_line(ln, _shrink_gist(ln.gist, width)))
                else:
                    nxt.append(ln)
            kept = nxt
            if _block_chars(kept, query) <= max_chars:
                return kept
        return _uniform_keep(kept, query, max_chars, floor)
    widths = [line_chars]
    for w in (120, 80):
        if w < line_chars and w not in widths:
            widths.append(w)
    kept_plain = list(lines)
    for width in widths:
        kept_plain = [_copy_line(ln, _shrink_gist(ln.gist, width)) for ln in lines]
        if _block_chars(kept_plain, query) <= max_chars:
            return kept_plain
    return _uniform_keep(kept_plain, query, max_chars, floor)


def _uniform_keep(
    lines: list[LedgerLine],
    query: str,
    max_chars: int,
    floor: int,
) -> list[LedgerLine]:
    protected = protected_ids(lines, floor)
    must = [ln for ln in lines if ln.episode_id in protected]
    extra = [ln for ln in lines if ln.episode_id not in protected]
    if _block_chars(must, query) > max_chars:
        return must
    if not extra:
        return must
    lo = 0
    hi = len(extra)
    best = must
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid == 0:
            cand = must
        else:
            step = max(1, len(extra) // mid)
            picked = extra[::step][:mid]
            cand = sorted(
                must + picked,
                key=lambda ln: (ln.valid_at, ln.turn_index, ln.episode_id),
            )
        if _block_chars(cand, query) <= max_chars:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best
