"""排序题：reader 只挑 #id，代码按 (valid_at, turn_index) 排序。"""

from __future__ import annotations

import re
import json
from typing import TypedDict
from contextvars import ContextVar

from gsuid_core.ai_core.memory.retrieval.event_time import order_topic_span, query_only_item_cap
from gsuid_core.ai_core.memory.retrieval.ledger_timeline import LedgerLine, LedgerView, _gist_tokens

_LEDGER: ContextVar[LedgerView | None] = ContextVar("eo_ledger", default=None)
_RENDERED: ContextVar[str] = ContextVar("eo_rendered", default="")
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class OrderMeta(TypedDict):
    picks_raw: list[str]
    picks_sorted: list[str]
    fallback_used: str
    inject_ids: list[str]
    pool_ids: list[str]
    inject_chars: int
    selector_ms: int


_ORDER_META: ContextVar[OrderMeta | None] = ContextVar("eo_order_meta", default=None)


def set_turn_ledger(view: LedgerView | None) -> None:
    _LEDGER.set(view)


def get_turn_ledger() -> LedgerView | None:
    return _LEDGER.get()


def set_order_meta(meta: OrderMeta | None) -> None:
    _ORDER_META.set(meta)


def get_order_meta() -> OrderMeta | None:
    return _ORDER_META.get()


def set_order_rendered(text: str) -> None:
    _RENDERED.set(text)


def get_order_rendered() -> str:
    return _RENDERED.get()


def maybe_override_persona(text: str, rendered: str) -> tuple[str, bool]:
    """编号序列对不上渲染清单时用清单替换。"""
    if not rendered.strip():
        return text, False
    if _numbered_keys(text) == _numbered_keys(rendered):
        return text, False
    return rendered, True


def _numbered_keys(text: str) -> list[str]:
    keys: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        m = re.match(r"^(\d+)\.\s+(\d{4}-\d{2}-\d{2})", s)
        if m is not None:
            keys.append(m.group(1) + "." + m.group(2))
    return keys


def order_protocol_hint(query: str) -> str:
    topic = order_topic_span(query) or "the asked topic"
    n = query_only_item_cap(query)
    n_en = f" at most {n} items" if n else ""
    n_zh = f"，最多 {n} 条" if n else ""
    if re.search(r"[A-Za-z]{4,}", query or ""):
        return (
            f'Answer from the timeline about "{topic}"{n_en}. '
            "One item per #id line, in that order, using the words on the line. "
            "Do not merge lines into a stage the line does not say."
        )
    return (
        f"按时间线 #id 的顺序回答与「{topic}」相关的发言{n_zh}。"
        "一条原句一条，用该句里的说法，不要合成原话里没有的阶段。"
    )


def parse_picks(text: str) -> list[tuple[str, str]]:
    blob = text or ""
    m = _JSON_RE.search(blob)
    if m is None:
        return []
    try:
        doc = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(doc, dict) or "picks" not in doc:
        return []
    raw = doc["picks"]
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mark = str(item["id"]).strip() if "id" in item else ""
        if not mark:
            mark = str(item["turn"]).strip() if "turn" in item else ""
        if mark.isdigit():
            mark = "#" + mark
        label = str(item["label"]).strip() if "label" in item else ""
        if mark.startswith("#"):
            out.append((mark, label))
    return out


def ledger_session_count(view: LedgerView) -> int:
    seen: set[str] = set()
    for ln in view.lines:
        if ln.session_id:
            seen.add(ln.session_id)
    return len(seen)


def _mark_index(mark: str) -> int:
    if mark.startswith("#") and mark[1:].isdigit():
        return int(mark[1:])
    return 0


def picks_collapsed(
    pairs: list[tuple[LedgerLine, str]],
    view: LedgerView,
    n: int | None,
) -> bool:
    """N≥3 且时间线≥3 session 时，全落同一 session 或前 20% 标号视为坍缩。"""
    if not pairs or not n or n < 3:
        return False
    if ledger_session_count(view) < 3:
        return False
    pick_sess = {ln.session_id for ln, _lab in pairs if ln.session_id}
    if len(pick_sess) <= 1:
        return True
    total = len(view.lines)
    if total < 10:
        return False
    cutoff = max(1, int(total * 0.2))
    return all(_mark_index(ln.mark) <= cutoff for ln, _lab in pairs)


def _line_by_mark(view: LedgerView, mark: str) -> LedgerLine | None:
    for ln in view.lines:
        if ln.mark == mark:
            return ln
    return None


def sort_and_fill(
    picks: list[tuple[str, str]],
    view: LedgerView,
    n: int | None,
    query: str,
) -> list[tuple[LedgerLine, str]]:
    chosen: list[tuple[LedgerLine, str]] = []
    seen: set[str] = set()
    for mark, label in picks:
        ln = _line_by_mark(view, mark)
        if ln is None or ln.episode_id in seen:
            continue
        seen.add(ln.episode_id)
        chosen.append((ln, label))
    chosen.sort(key=lambda pair: (pair[0].valid_at, pair[0].turn_index, pair[0].episode_id))
    want = n if n and n > 0 else len(chosen)
    if want <= 0:
        return chosen
    if len(chosen) > want:
        return _merge_closest(chosen, want)
    if len(chosen) < want:
        return _pad_stars(chosen, view, want, query)
    return chosen


def _merge_closest(chosen: list[tuple[LedgerLine, str]], want: int) -> list[tuple[LedgerLine, str]]:
    cur = list(chosen)
    while len(cur) > want and len(cur) >= 2:
        best_i = 1
        best = 1.0
        for i in range(1, len(cur)):
            a = _gist_tokens(cur[i - 1][0].gist)
            b = _gist_tokens(cur[i][0].gist)
            if not a or not b:
                sim = 0.0
            else:
                sim = len(a & b) / min(len(a), len(b))
            if sim > best:
                best = sim
                best_i = i
        del cur[best_i]
    return cur[:want]


def _pad_stars(
    chosen: list[tuple[LedgerLine, str]],
    view: LedgerView,
    want: int,
    query: str,
) -> list[tuple[LedgerLine, str]]:
    have = {ln.episode_id for ln, _lab in chosen}
    qtok = _gist_tokens(query)
    extras: list[tuple[float, LedgerLine]] = []
    for ln in view.lines:
        if ln.episode_id in have:
            continue
        if not ln.is_new:
            continue
        tok = _gist_tokens(ln.gist)
        score = (len(qtok & tok) / len(qtok)) if qtok and tok else 0.0
        extras.append((score, ln))
    extras.sort(key=lambda p: (-p[0], p[1].valid_at, p[1].turn_index))
    out = list(chosen)
    for _sc, ln in extras:
        if len(out) >= want:
            break
        out.append((ln, ln.gist[:80]))
        have.add(ln.episode_id)
    out.sort(key=lambda pair: (pair[0].valid_at, pair[0].turn_index, pair[0].episode_id))
    return out[:want]


def render_sorted(pairs: list[tuple[LedgerLine, str]], *, mode: str | None = None) -> str:
    use = mode if mode in ("label", "gist") else "label"
    lines: list[str] = []
    for i, (ln, label) in enumerate(pairs, 1):
        raw = ln.gist if use == "gist" and ln.gist.strip() else label
        body = " ".join((raw or "").split())
        cap = 200 if use == "gist" else 80
        if len(body) > cap:
            body = body[: cap - 1].rstrip() + "…"
        lines.append(f"{i}. {ln.day} · {body}")
    return "\n".join(lines)


def apply_order_answer(text: str, query: str) -> tuple[str, OrderMeta]:
    """解析 picks → 按时间排序 → 渲染。失败则原文原样返回。"""
    view = get_turn_ledger()
    empty: OrderMeta = {
        "picks_raw": [],
        "picks_sorted": [],
        "fallback_used": "raw",
        "inject_ids": view.inject_ids if view is not None else [],
        "pool_ids": view.pool_ids if view is not None else [],
        "inject_chars": view.chars if view is not None else 0,
        "selector_ms": 0,
    }
    if view is None or not view.lines:
        set_order_meta(empty)
        set_order_rendered("")
        return text, empty
    picks = parse_picks(text)
    if not picks:
        set_order_meta(empty)
        set_order_rendered("")
        return text, empty
    n = query_only_item_cap(query)
    pairs = sort_and_fill(picks, view, n, query)
    if not pairs:
        set_order_meta(empty)
        return text, empty
    rendered = render_sorted(pairs)
    want = n if n and n > 0 else len(pairs)
    short = bool(want and len(pairs) != want)
    meta: OrderMeta = {
        "picks_raw": [f"{m}:{lab}" for m, lab in picks],
        "picks_sorted": [f"{ln.mark}:{lab}" for ln, lab in pairs],
        "fallback_used": "short" if short else "",
        "inject_ids": view.inject_ids,
        "pool_ids": view.pool_ids,
        "inject_chars": view.chars,
        "selector_ms": 0,
    }
    set_order_meta(meta)
    set_order_rendered(rendered)
    return rendered, meta
