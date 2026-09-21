"""排序题专用 Selector：与人格分离，只挑 #id，不排序。"""

from __future__ import annotations

import time
import asyncio

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.agent_run.order_answer import (
    OrderMeta,
    parse_picks,
    render_sorted,
    sort_and_fill,
    set_order_meta,
    get_turn_ledger,
    set_order_rendered,
)
from gsuid_core.ai_core.memory.retrieval.event_time import query_only_item_cap
from gsuid_core.ai_core.memory.retrieval.ledger_timeline import LedgerLine, LedgerView

SELECTOR_SYSTEM = (
    "You select lines from a dated timeline of what one user said.\n"
    "Pick distinct sub-topics of the asked storyline. Discard other storylines.\n"
    'Output ONLY JSON {"picks":[{"id":"#7","label":"<=14 words"}, ...]}.\n'
    "If the question names a count N, return exactly N picks. "
    "Use only ids that appear. Do NOT order them. Label language = question language."
)

_JSON_ONLY = 'Output ONLY JSON {"picks":[{"id":"#7","label":"..."}]}.'
_ASK_TIMEOUT = 25.0


def _empty_meta() -> OrderMeta:
    view = get_turn_ledger()
    return {
        "picks_raw": [],
        "picks_sorted": [],
        "fallback_used": "raw",
        "inject_ids": view.inject_ids if view is not None else [],
        "pool_ids": view.pool_ids if view is not None else [],
        "inject_chars": view.chars if view is not None else 0,
        "selector_ms": 0,
    }


async def _ask_selector(system: str, user: str, timeout: float = _ASK_TIMEOUT) -> str:
    from gsuid_core.ai_core.gs_agent import create_agent

    agent = create_agent(
        system_prompt=system,
        create_by="EoSelector",
        task_level="low",
        dynamic_tools=False,
        max_iterations=1,
        wall_clock_budget=timeout,
    )
    out = await asyncio.wait_for(agent.run(user, return_mode="return"), timeout=timeout)
    return out if isinstance(out, str) else str(out)


def _finish(
    picks: list[tuple[str, str]],
    pairs: list[tuple[LedgerLine, str]],
    view: LedgerView,
    n: int | None,
    ms: int,
    fallback: str,
) -> tuple[str, OrderMeta]:
    want = n if n and n > 0 else len(pairs)
    short = bool(want and len(pairs) != want)
    fb = fallback
    if not fb and short:
        fb = "short"
    rendered = render_sorted(pairs)
    meta: OrderMeta = {
        "picks_raw": [f"{m}:{lab}" for m, lab in picks],
        "picks_sorted": [f"{ln.mark}:{lab}" for ln, lab in pairs],
        "fallback_used": fb,
        "inject_ids": view.inject_ids,
        "pool_ids": view.pool_ids,
        "inject_chars": view.chars,
        "selector_ms": ms,
    }
    set_order_meta(meta)
    set_order_rendered(rendered)
    logger.info(i18n_t("log.memory.eo_selector", n=len(pairs), fb=fb or "-", ms=ms))
    return rendered, meta


def _fill_labels(picks: list[tuple[str, str]], view: LedgerView) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for mark, lab in picks:
        if lab.strip():
            out.append((mark, lab))
            continue
        gist = ""
        for ln in view.lines:
            if ln.mark == mark:
                gist = ln.gist[:80]
                break
        out.append((mark, gist))
    return out


async def select_from_ledger(query: str) -> tuple[str, OrderMeta]:
    """账本上调一次 LLM 挑 #id。模型失败则调用方继续用原文时间线。"""
    view = get_turn_ledger()
    empty = _empty_meta()
    if view is None or not view.lines:
        set_order_meta(empty)
        set_order_rendered("")
        return "", empty
    n = query_only_item_cap(query)
    n_txt = str(n) if n else "N"
    t0 = time.monotonic()
    user = f"Question:\n{query}\n\nN = {n_txt}\n\nTimeline:\n{view.header}\n\n{_JSON_ONLY}"
    raw = ""
    try:
        raw = await _ask_selector(SELECTOR_SYSTEM, user)
    except (TimeoutError, OSError, ConnectionError) as e:
        logger.debug(i18n_t("log.memory.eo_selector_fail", e=e))
    picks = _fill_labels(parse_picks(raw), view)
    pairs = sort_and_fill(picks, view, n, query) if picks else []
    ms = int((time.monotonic() - t0) * 1000)
    if pairs:
        return _finish(picks, pairs, view, n, ms, "")
    empty["selector_ms"] = ms
    set_order_meta(empty)
    set_order_rendered("")
    logger.info(i18n_t("log.memory.eo_selector", n=0, fb="raw", ms=ms))
    return "", empty


def restatement_hint(rendered: str) -> str:
    return f"Repeat the numbered list below to the user. Do not add, drop, or reorder items.\n{rendered}"
