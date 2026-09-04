"""生产集合召回 / 更新取最晚：词面跨会话补条，不是全库 dump。"""

from __future__ import annotations

import asyncio
from typing import TypeVar
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch
from collections.abc import Coroutine

from gsuid_core.ai_core.cognition.types import CogKind, CognitiveHit
from gsuid_core.ai_core.cognition.facade import render_cognition_block
from gsuid_core.ai_core.memory.retrieval.types import Episode
from gsuid_core.ai_core.memory.retrieval.lexical import (
    SET_RECALL_HINT,
    LATEST_WINS_HINT,
    query_tokens,
    diversify_episodes,
    merge_episode_lists,
    expand_lexical_recall,
    extra_tokens_from_hits,
)

T = TypeVar("T")


def _run(coro: Coroutine[object, object, T]) -> T:
    return asyncio.run(coro)


def _ep(eid: str, content: str, valid_at: str) -> Episode:
    return Episode(
        id=eid,
        content=content,
        valid_at=valid_at,
        scope_key="user_global:u1",
        embedding=[],
    )


def test_query_tokens_alias_matches_eval() -> None:
    from gsuid_core.ai_core.kits.memory.eval_protocol import eval_query_tokens

    q = "What was my previous occupation in Miami?"
    assert query_tokens(q) == eval_query_tokens(q)


def test_extra_tokens_from_hits_keeps_class_names() -> None:
    hits = [
        _ep("1", "User: I went to Zumba on Monday after yoga.", "2023-05-01 12:00:00"),
        _ep("2", "User: Pilates was scheduled for Friday.", "2023-05-08 12:00:00"),
    ]
    toks = {t.lower() for t in extra_tokens_from_hits(hits, "How many fitness classes have I taken?")}
    assert "zumba" in toks
    assert "pilates" in toks
    assert "class" not in toks
    assert "classes" not in toks


def test_merge_prefer_extras_keeps_cross_session_hits() -> None:
    primary = [_ep(f"p{i}", f"hit {i}", "2023-01-01 12:00:00") for i in range(20)]
    extras = [_ep("zumba", "User: Zumba class downtown.", "2023-04-01 12:00:00")]
    kept = merge_episode_lists(primary, extras, prefer_extras=True, limit=18)
    assert "zumba" in {e["id"] for e in kept}
    dropped = merge_episode_lists(primary, extras, prefer_extras=False, limit=20)
    assert "zumba" not in {e["id"] for e in dropped}


def test_diversify_episodes_round_robins_sessions() -> None:
    t0 = datetime(2023, 5, 1, 12, 0, 0)
    eps: list[Episode] = []
    for s in range(4):
        for j in range(3):
            dt = t0 + timedelta(seconds=s * 120 + j)
            eps.append(_ep(f"s{s}t{j}", f"session {s} turn {j}", dt.strftime("%Y-%m-%d %H:%M:%S")))
    picked = diversify_episodes(eps, cap=6)
    ids = [e["id"] for e in picked]
    assert ids[:2] == ["s0t0", "s0t1"]
    sessions = {eid[:2] for eid in ids}
    assert sessions >= {"s0", "s1", "s2"}


def test_clock_lines_not_in_query_tokens() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import strip_clock_lines

    q = "当前时间：2023/05/30 23:32\n\nCan you recommend Premiere Pro tutorials?"
    assert strip_clock_lines(q) == "Can you recommend Premiere Pro tutorials?"
    toks = {t.lower() for t in query_tokens(q)}
    assert "2023" not in toks
    assert "premiere" in toks


def test_sql_like_tokens_drop_short_english() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import sql_like_tokens

    q = "How many projects have I led or am currently leading?"
    toks = {t.lower() for t in sql_like_tokens(query_tokens(q))}
    assert "led" not in toks
    assert "projects" in toks


def test_relative_query_strips_last_saturday_keeps_event_nouns() -> None:
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        parse_query_clock,
        query_time_window,
        has_relative_time_span,
        strip_relative_time_spans,
    )

    q = "I received a piece of jewelry last Saturday from whom?"
    assert has_relative_time_span(q)
    stripped = strip_relative_time_spans(q)
    toks = {t.lower() for t in query_tokens(stripped)}
    assert "jewelry" in toks
    assert "saturday" not in toks
    assert not has_relative_time_span("What is my current salary?")
    assert not has_relative_time_span("How many weeks ago did I attend the festival?")
    clock = parse_query_clock("当前时间：2023/03/11 12:00\n\n" + q)
    assert clock is not None
    window = query_time_window(q, clock)
    assert window is not None
    lo, hi = window
    assert lo.date() <= datetime(2023, 3, 4).date() <= hi.date()


def test_couple_of_days_and_past_month_windows() -> None:
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        query_time_window,
        event_times_in_text,
        strip_relative_time_spans,
    )

    said = datetime(2023, 4, 18, 16, 50, 0)
    couple = event_times_in_text("I cooked a chocolate cake a couple of days ago", said)
    assert couple
    assert couple[0].date() == datetime(2023, 4, 16).date()
    clock = datetime(2023, 5, 30, 12, 0, 0)
    window = query_time_window("Which grocery store did I spend the most money at in the past month?", clock)
    assert window is not None
    assert window[0].date() <= datetime(2023, 5, 1).date()
    assert "saturday" not in strip_relative_time_spans("music event last Saturday").lower()


def test_expand_lexical_recall_second_hop_uses_hit_names() -> None:
    calls: list[str] = []

    async def _lex(query: str, **kwargs: object) -> list[Episode]:
        _ = kwargs
        calls.append(query)
        if len(calls) == 1:
            return [_ep("z", "User: I went to Zumba on Monday after yoga.", "2023-05-01 12:00:00")]
        return [_ep("p", "User: Pilates was scheduled for Friday.", "2023-05-08 12:00:00")]

    with patch(
        "gsuid_core.ai_core.memory.retrieval.lexical.lexical_search_episodes",
        new=_lex,
    ):
        out = _run(
            expand_lexical_recall(
                [_ep("seed", "User: fitness class", "2023-04-01 12:00:00")],
                query="How many fitness classes have I taken?",
                user_id="u1",
                group_id=None,
            )
        )
    assert len(calls) >= 2
    assert "zumba" in calls[1].lower()
    ids = {e["id"] for e in out}
    assert "z" in ids
    assert "p" in ids


def test_expand_lexical_recall_date_window_merges_range_hits() -> None:
    async def _lex(*args: object, **kwargs: object) -> list[Episode]:
        _ = (args, kwargs)
        return []

    async def _win(*, start: datetime, end: datetime, **kwargs: object) -> list[Episode]:
        _ = kwargs
        assert start.date() <= datetime(2023, 3, 4).date() <= end.date()
        return [_ep("aunt", "User: My aunt gave me a necklace.", "2023-03-04 15:00:00")]

    clock = datetime(2023, 3, 11, 12, 0, 0)
    with (
        patch("gsuid_core.ai_core.memory.retrieval.lexical.lexical_search_episodes", new=_lex),
        patch("gsuid_core.ai_core.memory.retrieval.lexical.episodes_in_time_window", new=_win),
    ):
        out = _run(
            expand_lexical_recall(
                [],
                query="I received a piece of jewelry last Saturday from whom?",
                user_id="u1",
                group_id=None,
                clock=clock,
            )
        )
    assert [e["id"] for e in out] == ["aunt"]


def test_expand_lexical_recall_uses_wall_clock_without_explicit_clock() -> None:
    called: dict[str, int] = {"n": 0}

    async def _lex(*args: object, **kwargs: object) -> list[Episode]:
        _ = (args, kwargs)
        return []

    async def _win(*, start: datetime, end: datetime, **kwargs: object) -> list[Episode]:
        _ = (start, end, kwargs)
        called["n"] += 1
        return []

    with (
        patch("gsuid_core.ai_core.memory.retrieval.lexical.lexical_search_episodes", new=_lex),
        patch("gsuid_core.ai_core.memory.retrieval.lexical.episodes_in_time_window", new=_win),
    ):
        _run(
            expand_lexical_recall(
                [],
                query="What did I do last Saturday?",
                user_id="u1",
                group_id=None,
            )
        )
    assert called["n"] == 1


def test_expand_lexical_recall_keeps_vector_and_lexical_hits() -> None:
    older = _ep("old", "User: pre-approved for $350,000", "2023-01-01 10:00:00")
    newer = _ep("new", "User: pre-approved for $400,000", "2023-06-01 10:00:00")

    async def _lex(*args: object, **kwargs: object) -> list[Episode]:
        _ = (args, kwargs)
        return [newer]

    with patch(
        "gsuid_core.ai_core.memory.retrieval.lexical.lexical_search_episodes",
        new=_lex,
    ):
        out = _run(
            expand_lexical_recall(
                [older],
                query="What is my current pre-approved amount?",
                user_id="u1",
                group_id=None,
            )
        )
    assert {e["id"] for e in out} == {"new", "old"}


def test_render_block_hints_follow_as_of_and_episodes() -> None:
    dated = [
        CognitiveHit(
            kind=CogKind.EPISODE,
            id="e1",
            title="",
            summary="pre-approved for $400,000",
            score=0.9,
            as_of="2023-06-01 10:00",
            high_confidence=True,
        )
    ]
    block = render_cognition_block("竖图偏好", dated)
    assert LATEST_WINS_HINT in block
    assert SET_RECALL_HINT in block
    undated = [
        CognitiveHit(
            kind=CogKind.KNOWLEDGE,
            id="k1",
            title="资料",
            summary="竖图偏好",
            score=0.9,
            high_confidence=True,
        )
    ]
    plain = render_cognition_block("竖图偏好", undated)
    assert LATEST_WINS_HINT not in plain
    assert SET_RECALL_HINT not in plain


def test_query_overlaps_text_skips_unrelated_rules() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import query_overlaps_text

    assert query_overlaps_text("slow cooker recipes", "I made a delicious beef stew in the slow cooker")
    assert not query_overlaps_text(
        "recommend a movie tonight",
        "进行面试/访谈类逐题问答时每次只提一个问题",
    )
    assert query_overlaps_text("", "任何规则")


def test_catalog_timestamp_hint_without_recency_sort() -> None:
    from gsuid_core.ai_core.kits.memory.kit import _format_memory_catalog
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    mc = MemoryContext(
        episodes=[
            _ep("old", "User: salary is 350000", "2023-01-01 10:00:00"),
            _ep("new", "User: salary is 400000", "2023-06-01 10:00:00"),
        ]
    )
    text = _format_memory_catalog(mc, "What is my current salary?")
    assert "350000" in text
    assert "400000" in text


def test_production_paths_do_not_import_eval_protocol() -> None:
    root = Path(__file__).resolve().parent.parent
    facade = (root / "gsuid_core/ai_core/cognition/facade.py").read_text(encoding="utf-8")
    lexical = (root / "gsuid_core/ai_core/memory/retrieval/lexical.py").read_text(encoding="utf-8")
    assert "kits.memory.eval_protocol" not in facade
    assert "kits.memory.eval_protocol" not in lexical
    assert "expand_lexical_recall" in facade
    assert "is_set_query" not in facade
    assert "is_latest_query" not in facade
