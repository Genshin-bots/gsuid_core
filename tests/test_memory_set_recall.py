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


def _ep(
    eid: str,
    content: str,
    valid_at: str,
    session_id: str = "",
    embedding: list[float] | None = None,
) -> Episode:
    row = Episode(
        id=eid,
        content=content,
        valid_at=valid_at,
        scope_key="user_global:u1",
        embedding=list(embedding) if embedding is not None else [],
    )
    if session_id:
        row["session_id"] = session_id
        row["turn_index"] = 0
    return row


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


def test_wants_self_visual_skips_news_questions() -> None:
    from gsuid_core.ai_core.buildin_tools.image_reader import wants_self_visual

    assert not wants_self_visual("图中内容是什么？涉及什么事件或情况？")
    assert not wants_self_visual(None)
    assert wants_self_visual("这是你吗")


def test_query_required_needles_require_proper_names() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import query_required_needles, text_has_query_needles

    needles = query_required_needles("Does Johnny have expertise in tuning logic?")
    assert "Johnny" in needles
    assert "Johnny" in query_required_needles("Johnny code review tuning logic collaboration")
    assert not text_has_query_needles(
        "Does Johnny have expertise in tuning logic?",
        "I worked on RAG sharding and dense search today.",
    )
    assert text_has_query_needles(
        "Does Johnny have expertise in tuning logic?",
        "Johnny reviewed the tuning logic PR.",
    )
    assert text_has_query_needles(
        "How would you structure the cost calculation?",
        "I need a cost calculation for different cloud providers.",
    )
    assert not text_has_query_needles("How would you structure the cost calculation?", "any cloud text")
    assert not text_has_query_needles(
        "couch storage move furniture",
        "We discussed RAG sharding and dense search today.",
    )
    assert text_has_query_needles(
        "couch storage move furniture",
        "I moved the couch into storage last week.",
    )


def test_stride_episodes_chrono_keeps_both_ends() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import stride_episodes_chrono

    t0 = datetime(2025, 3, 1, 8, 0, 0)
    eps = [_ep(f"d{d}", f"day {d}", (t0 + timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")) for d in range(40)]
    packed = stride_episodes_chrono(eps, cap=24)
    assert packed[0]["id"] == "d0"
    assert packed[-1]["id"] == "d39"
    ids = {e["id"] for e in packed}
    assert "d0" in ids
    assert "d39" in ids
    assert len(packed) == 24


def test_stride_episodes_chrono_keeps_last_of_31() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import stride_episodes_chrono

    t0 = datetime(2025, 3, 1, 8, 0, 0)
    eps = [_ep(f"d{d}", f"day {d}", (t0 + timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")) for d in range(31)]
    packed = stride_episodes_chrono(eps, cap=16)
    assert packed[0]["id"] == "d0"
    assert packed[-1]["id"] == "d30"
    assert len(packed) == 16


def test_latest_slot_pack_puts_newer_number_first() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import (
        apply_query_episode_pack,
        looks_like_latest_slot_query,
    )

    q = "How many commits have been merged into the main branch of my Git repository?"
    assert looks_like_latest_slot_query(q)
    assert looks_like_latest_slot_query("What is the deadline for completing the first sprint?")
    assert looks_like_latest_slot_query("What is the daily call quota for the API key used in my application?")
    assert looks_like_latest_slot_query("What is the average response time of the dashboard API?")
    assert not looks_like_latest_slot_query("Have I ever formulated heat equation problems before?")
    eps = [
        _ep("old", "User: my repository had 150 commits as of the v1.0.0 tag.", "2024-04-24 16:00:32"),
        _ep("new", "User: 165 commits have been merged into the main branch.", "2024-06-10 12:00:00"),
        _ep("sql", "User: db.session.commit() returned 201 for the expense insert.", "2024-06-12 09:00:00"),
        _ep("noise", "User: I refactored the auth module yesterday.", "2024-06-11 09:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    assert packed[0]["id"] == "new"
    assert packed[1]["id"] == "old"


def test_pack_timeline_prefers_earliest_same_day() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import pack_timeline_episodes

    later = _ep("late", "User: homework later that day.", "2025-03-01 18:00:00")
    early = _ep("early", "User: I started the core feature first.", "2025-03-01 08:00:00")
    packed = pack_timeline_episodes([later, early], cap=4)
    assert packed[0]["id"] == "early"


def test_span_query_pack_strides_hits_not_day_openers() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    eps = [
        _ep("d0", "User: I started the budget tracker core auth.", "2024-03-14 10:00:00"),
        _ep("mid", "User: Implementing transaction error handling.", "2024-04-05 10:00:00"),
        _ep("dN", "User: Finalizing security and deployment.", "2024-04-25 10:00:00"),
        _ep("greet", "User: I'm Craig hello.", "2024-03-20 08:00:00"),
    ]
    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order?"
    )
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "d0"
    assert ids[-1] == "dN"
    assert "mid" in ids


def test_pack_timeline_prefers_topic_overlap_same_day() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import pack_timeline_episodes

    greet = _ep("greet", "User: I'm Craig, let's plan the week.", "2025-03-01 08:00:00")
    aspect = _ep(
        "aspect",
        "User: Implementing transaction creation with proper error handling in the budget tracker.",
        "2025-03-01 18:00:00",
    )
    packed = pack_timeline_episodes(
        [greet, aspect],
        cap=4,
        query="list the order of budget tracker transaction error handling aspects",
    )
    assert packed[0]["id"] == "aspect"


def test_pack_timeline_episodes_keeps_each_day() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import pack_timeline_episodes

    t0 = datetime(2025, 3, 1, 8, 0, 0)
    eps: list[Episode] = []
    for d in range(8):
        for j in range(4):
            dt = t0 + timedelta(days=d, hours=j)
            eps.append(_ep(f"d{d}t{j}", f"day {d} turn {j}", dt.strftime("%Y-%m-%d %H:%M:%S")))
    packed = pack_timeline_episodes(eps, cap=10)
    days = {(e["valid_at"] or "")[:10] for e in packed}
    assert len(days) >= 8
    assert packed[0]["id"] == "d0t0"


def test_diversify_episodes_round_robins_sessions() -> None:
    t0 = datetime(2023, 5, 1, 12, 0, 0)
    eps: list[Episode] = []
    for s in range(4):
        for j in range(3):
            dt = t0 + timedelta(seconds=s * 2000 + j)
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


def test_query_explicit_time_range_needs_enum_intent() -> None:
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        temporal_search_query,
        query_explicit_time_range,
    )

    span = query_explicit_time_range("list the topics in order from 2025-03-01 to 2025-03-31")
    assert span is not None
    assert span[0].strftime("%Y-%m-%d") == "2025-03-01"
    assert span[1].strftime("%Y-%m-%d") == "2025-04-01"
    assert query_explicit_time_range("What was my salary on 2023-01-01 versus 2023-06-01?") is None
    topic = temporal_search_query("list the order of Green's functions from 2025-03-01 to 2025-03-31 in order")
    assert "Green's" in topic or "functions" in topic.lower()
    assert "2025" not in topic


def test_collect_user_stance_skips_not_sure() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import collect_user_stance_conflicts

    q = "summarize my Green's functions learning"
    eps = [
        _ep("n", "User: I'm not sure how to track Green's functions progress.", "2025-03-07 10:00:00"),
        _ep("p", "User: I'm starting my deep dive into Green's functions.", "2025-03-01 10:00:00"),
    ]
    assert collect_user_stance_conflicts(eps, q) == []


def test_collect_user_stance_conflicts_needs_both_sides() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import collect_user_stance_conflicts

    q = "Have I ever formulated heat equation problems before?"
    only_neg = [_ep("n", "User: I've never formulated any heat equation problems before today.", "2024-11-02 10:00:00")]
    assert collect_user_stance_conflicts(only_neg, q) == []
    both = only_neg + [_ep("p", "User: I completed 5 heat equation problems this week.", "2024-12-01 10:00:00")]
    hits = collect_user_stance_conflicts(both, q)
    assert hits
    assert "never formulated" in hits[0]
    assert "completed 5" in hits[0]


def test_episodes_in_time_window_merges_both_ends() -> None:
    from datetime import datetime as dt

    from gsuid_core.ai_core.memory.retrieval.lexical import episodes_in_time_window

    class _Row:
        def __init__(self, eid: str, content: str, valid_at: dt) -> None:
            self.id = eid
            self.content = content
            self.valid_at = valid_at
            self.scope_key = "user_global:u1"

    newest = [_Row("new", "newest topic", dt(2025, 3, 31, 12, 0, 0))]
    oldest = [_Row("old", "oldest topic", dt(2025, 3, 1, 12, 0, 0))]

    async def fake_search(
        scope_key: str,
        start: dt,
        end: dt,
        limit: int = 24,
        ascending: bool = False,
        offset: int = 0,
        user_only: bool = False,
    ) -> list[_Row]:
        return oldest if ascending else newest

    async def fake_count(
        scope_key: str,
        start: dt,
        end: dt,
        user_only: bool = False,
    ) -> int:
        return 40

    async def fake_openers(
        scope_key: str,
        start: dt,
        end: dt,
        limit: int = 40,
    ) -> list[_Row]:
        return []

    with (
        patch(
            "gsuid_core.ai_core.memory.database.models.AIMemEpisode.search_by_valid_at_range",
            new=fake_search,
        ),
        patch(
            "gsuid_core.ai_core.memory.database.models.AIMemEpisode.count_by_valid_at_range",
            new=fake_count,
        ),
        patch(
            "gsuid_core.ai_core.memory.database.models.AIMemEpisode.search_user_day_openers",
            new=fake_openers,
        ),
    ):
        eps = _run(
            episodes_in_time_window(
                user_id="u1",
                group_id=None,
                start=dt(2025, 3, 1),
                end=dt(2025, 4, 1),
                limit=8,
            )
        )
    ids = [e["id"] for e in eps]
    assert "old" in ids
    assert "new" in ids


def test_production_paths_do_not_import_eval_protocol() -> None:
    root = Path(__file__).resolve().parent.parent
    facade = (root / "gsuid_core/ai_core/cognition/facade.py").read_text(encoding="utf-8")
    lexical = (root / "gsuid_core/ai_core/memory/retrieval/lexical.py").read_text(encoding="utf-8")
    assert "kits.memory.eval_protocol" not in facade
    assert "kits.memory.eval_protocol" not in lexical
    assert "expand_lexical_recall" in facade
    assert "is_set_query" not in facade
    assert "is_latest_query" not in lexical and "is_latest_query" not in facade


def test_first_mention_pack_keeps_earliest_per_aspect() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep("core", "User: I started the budget tracker core authentication module.", "2024-03-14 10:00:00", "s1"),
        _ep("core2", "User: Still polishing the budget tracker core authentication.", "2024-03-20 10:00:00", "s1"),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00", "s2"),
        _ep("sec", "User: Finalizing security hashing and deployment checklist.", "2024-04-25 10:00:00", "s3"),
        _ep("greet", "User: I'm Craig hello.", "2024-03-16 08:00:00", "s1"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "core"
    assert "core2" not in ids
    assert "err" in ids
    assert "sec" in ids
    assert ids.index("core") < ids.index("err") < ids.index("sec")
    assert "greet" not in ids
    assert len(ids) == 3


def test_order_skeleton_numbered_and_ignores_input_shuffle() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import (
        format_order_skeleton,
        apply_query_episode_pack,
    )

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep("sec", "User: Finalizing security hashing and deployment checklist.", "2024-04-25 10:00:00", "s3"),
        _ep("core2", "User: Still polishing the budget tracker core authentication.", "2024-03-20 10:00:00", "s1"),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00", "s2"),
        _ep("core", "User: I started the budget tracker core authentication module.", "2024-03-14 10:00:00", "s1"),
        _ep("greet", "User: I'm Craig hello.", "2024-03-16 08:00:00", "s1"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    gold = ["core", "err", "sec"]
    assert ids == gold
    skel = format_order_skeleton(packed)
    assert len(skel) == 3
    assert skel[0].startswith("1. 2024-03-14 ·")
    assert skel[1].startswith("2. 2024-04-05 ·")
    assert skel[2].startswith("3. 2024-04-25 ·")
    assert "core" in skel[0].lower()
    assert "authentication" in skel[0].lower() or "expense" in skel[0].lower()
    assert "error" in skel[1].lower()
    assert "security" in skel[2].lower() or "deploy" in skel[2].lower()
    rank = {x: i for i, x in enumerate(ids)}
    conc = disc = 0
    for i in range(len(gold)):
        for j in range(i + 1, len(gold)):
            if rank[gold[i]] < rank[gold[j]]:
                conc += 1
            else:
                disc += 1
    assert (conc - disc) / (conc + disc) == 1.0


def test_order_query_wins_over_temporal_mode_day_pack() -> None:
    from datetime import datetime

    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep("core", "User: I started the budget tracker core authentication module.", "2024-03-14 10:00:00", "s1"),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00", "s2"),
        _ep("sec", "User: Finalizing security hashing and deployment checklist.", "2024-04-25 10:00:00", "s3"),
        _ep("d1", "User: budget tracker daily standup notes about caching.", "2024-03-21 10:00:00", "s1"),
        _ep("d2", "User: budget tracker daily standup notes about indexes.", "2024-03-28 10:00:00", "s1"),
    ]
    packed = apply_query_episode_pack(
        eps,
        q,
        temporal_mode=True,
        time_range=(datetime(2024, 3, 1), datetime(2024, 5, 1)),
    )
    ids = [e["id"] for e in packed]
    assert ids == ["core", "err", "sec"]
    assert len(ids) == 3


def test_synthetic_full_history_window_still_first_mention() -> None:
    """Chat 路径：span 合成 (2000,2100) 不得改走按日 pack，同日两 session 都要留下。"""
    from datetime import datetime

    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack
    from gsuid_core.ai_core.memory.retrieval.dual_route import _extract_time_range, _span_search_window

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order?"
    )
    assert _extract_time_range(q) is None
    win = _span_search_window(q)
    assert win == (datetime(2000, 1, 1), datetime(2100, 1, 1))
    eps = [
        _ep("am", "User: I started the budget tracker core authentication module.", "2024-03-14 09:00:00", "s1"),
        _ep("noise", "User: budget tracker daily standup notes about caching.", "2024-03-14 12:00:00", "s1"),
        _ep("pm", "User: Implementing transaction creation with proper error handling.", "2024-03-14 18:00:00", "s2"),
        _ep("later", "User: Finalizing security hashing and deployment checklist.", "2024-04-25 10:00:00", "s3"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=True, time_range=win)
    ids = [e["id"] for e in packed]
    assert "am" in ids
    assert "pm" in ids
    assert "later" in ids


def test_duration_pack_orders_by_event_at_not_said_at() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = "How many days passed between when I obtained my API key and when I completed the UI wireframe?"
    eps = [
        _ep("ui", "User: I completed the UI wireframe for my weather app.", "2024-06-15 10:00:00"),
        _ep("key", "User: I obtained my OpenWeather API key three months ago.", "2024-07-01 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "key" in ids
    assert "ui" in ids
    assert ids.index("key") < ids.index("ui")


def test_first_mention_pack_keeps_late_aspects_across_weeks() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep("core", "User: I started the budget tracker core authentication module.", "2024-03-14 10:00:00"),
        _ep("plan", "User: Breaking down milestones for the budget tracker components.", "2024-03-14 16:00:00"),
        _ep("schema", "User: Drafting the users and expenses table schema locally.", "2024-03-21 10:00:00"),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00"),
        _ep("sec", "User: Finalizing security hashing and deployment checklist.", "2024-04-25 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "core"
    assert "err" in ids
    assert ids[-1] == "sec"
    assert "plan" not in ids
    assert len(ids) == 3


def test_first_mention_shared_stack_does_not_magnet() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep(
            "core",
            "User: Flask 2.3.1 and SQLite budget tracker core authentication and expense tracking.",
            "2024-03-14 10:00:00",
        ),
        _ep(
            "plan",
            "User: Flask SQLite budget tracker components and milestones still planning.",
            "2024-03-14 16:00:00",
        ),
        _ep(
            "err",
            "User: Flask SQLite transaction creation with proper error handling.",
            "2024-04-05 10:00:00",
        ),
        _ep(
            "sec",
            "User: Flask SQLite security hashing and deployment checklist.",
            "2024-04-25 10:00:00",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "core"
    assert "err" in ids
    assert "sec" in ids
    assert len(ids) == 3


def test_first_mention_skips_time_anchor_and_glue() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep(
            "anchor",
            "User: I'm working on a project with a Time Anchor of March 15, 2024, "
            "and I need to plan my tasks accordingly, can you help me create a schedule "
            "to ensure I meet my deadlines by then?",
            "2024-03-15 00:00:00",
        ),
        _ep(
            "core",
            "User: Sure, let's break it down for my budget tracker. "
            "User Authentication, Transaction Management, and Basic Analytics.",
            "2024-03-15 16:00:00",
        ),
        _ep(
            "err",
            "User: I'm trying to implement transaction creation with proper error handling "
            "for my project, can you help me?",
            "2024-04-05 16:00:00",
        ),
        _ep(
            "sec",
            "User: I'm trying to finalize security hashing and deployment checklist for my project, can you help me?",
            "2024-04-25 16:00:00",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "anchor" in ids
    assert "err" in ids
    assert "sec" in ids


def test_first_mention_skips_meeting_opener_keeps_debounce() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of implementing "
        "the city autocomplete feature across our conversations, in order? "
        "Mention ONLY and ONLY five items."
    )
    eps = [
        _ep(
            "meet",
            "User: I'm trying to schedule a meeting for March 15, 2024, at 09:00 CET, "
            "and I want to make sure I don't overlap with any other important events.",
            "2024-03-15 00:00:00",
        ),
        _ep(
            "deb",
            "User: Implement city autocomplete with OpenWeather Geocoding and 300ms debounce.",
            "2024-03-27 16:00:00",
            "s1",
        ),
        _ep(
            "lat",
            "User: API response time for autocomplete exceeds the debounce delay.",
            "2024-03-28 10:00:00",
            "s2",
        ),
        _ep(
            "rapid",
            "User: Rapid typing in the autocomplete box can bypass debounce.",
            "2024-04-02 10:00:00",
            "s3",
        ),
        _ep(
            "drop",
            "User: Autocomplete 5-item dropdown must handle HTTP 401 Unauthorized.",
            "2024-04-08 10:00:00",
            "s4",
        ),
        _ep(
            "leak",
            "User: Remove autocomplete event listeners to prevent memory leaks.",
            "2024-04-10 10:00:00",
            "s5",
        ),
        _ep(
            "wx",
            "User: weatherDisplay should toggle units between C and F on the home card.",
            "2024-04-10 12:00:00",
            "s5",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "meet" not in ids
    assert ids[0] == "deb"
    assert "wx" not in ids
    assert "lat" in ids
    assert "leak" in ids
    assert len(ids) == 5


def test_first_mention_skips_html_item_list_dump() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of integrating "
        "and customizing the framework in my projects across our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    html = "User: <div class='item'>A</div><div class='item'>B</div><span class='x'>C</span><ul><li>D</li></ul>"
    eps = [
        _ep("html", html, "2024-03-14 10:00:00"),
        _ep(
            "boot",
            "User: Setting up a responsive grid, navbar and cards with Bootstrap v5.3.0.",
            "2024-03-14 16:00:00",
        ),
        _ep(
            "css",
            "User: Integrating form-control and btn-primary with custom CSS hover.",
            "2024-04-10 10:00:00",
        ),
        _ep(
            "modal",
            "User: Bootstrap modal accessibility bug; upgrade framework 5.3.0 to 5.3.1.",
            "2024-05-05 10:00:00",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "html" not in ids
    assert ids[0] == "boot"
    assert "modal" in ids
    assert len(ids) == 3


def test_first_mention_same_day_keeps_earliest_session_turn() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of integrating "
        "and customizing the framework in my projects across our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep(
            "flex",
            "User: I'm trying to customize the layout of my project gallery using Flexbox.",
            "2024-03-14 10:00:00",
            "s1",
        ),
        _ep(
            "boot",
            "User: Setting up navbar and cards with Bootstrap v5.3.0 framework components.",
            "2024-03-14 16:00:00",
            "s1",
        ),
        _ep(
            "css",
            "User: Integrating form-control and btn-primary classes plus custom CSS.",
            "2024-04-10 10:00:00",
            "s2",
        ),
        _ep(
            "modal",
            "User: Fix Bootstrap modal bug by upgrading the framework from 5.3.0 to 5.3.1.",
            "2024-05-05 10:00:00",
            "s3",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "flex"
    assert "boot" not in ids
    assert "modal" in ids
    assert len(ids) == 3


def test_first_mention_last_day_keeps_security_over_git_tag() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep("core", "User: I started the budget tracker core authentication module.", "2024-03-14 10:00:00"),
        _ep(
            "err",
            "User: Implementing transaction creation with proper error handling for my budget tracker.",
            "2024-04-05 10:00:00",
        ),
        _ep(
            "sec",
            "User: Finalizing security hashing and deployment checklist.",
            "2024-04-25 10:00:00",
        ),
        _ep(
            "git",
            "User: I tagged v1.0.0 of the budget tracker and pushed 150 commits to GitHub.",
            "2024-04-25 16:00:00",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids == ["core", "err", "sec"]
    assert "git" not in ids


def test_first_mention_skips_yaml_dump_same_week_as_error() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep("core", "User: I started the budget tracker core authentication module.", "2024-03-14 10:00:00"),
        _ep(
            "yaml",
            "User: ```\nfrom flask_yaml import FlaskYaml\nfrom flask_toml import FlaskToml\n"
            "app.config.from_file('settings.yaml')\n``` I want Flask-Yaml and Flask-Toml.",
            "2024-04-05 09:00:00",
        ),
        _ep(
            "err",
            "User: Implementing transaction creation with proper error handling.",
            "2024-04-05 16:00:00",
        ),
        _ep("sec", "User: Finalizing security hashing and deployment checklist.", "2024-04-25 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "yaml" not in ids
    assert ids[0] == "core"
    assert "err" in ids
    assert "sec" in ids
    assert len(ids) == 3


def test_first_mention_skips_unfenced_yaml_toml() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep("core", "User: I started the budget tracker core authentication module.", "2024-03-14 10:00:00", "s1"),
        _ep("yaml", "User: I want Flask-Yaml and Flask-Toml for configuration.", "2024-04-05 09:00:00", "s2"),
        _ep(
            "err",
            "User: Implementing transaction creation with proper error handling.",
            "2024-04-05 16:00:00",
            "s2",
        ),
        _ep("sec", "User: Finalizing security hashing and deployment checklist.", "2024-04-25 10:00:00", "s3"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids == ["core", "yaml", "sec"]
    assert "err" not in ids


def test_first_mention_keeps_error_handling_with_code_sample() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    err = (
        "User: I'm currently working on the transaction CRUD and analytics integration "
        "for my personal budget tracker. I want to make sure I'm handling errors properly, "
        "so can you help me implement a try-except block during transaction creation?\n"
        "```python\nfrom flask_sqlalchemy import SQLAlchemy\ndb = SQLAlchemy(app)\n```"
    )
    sec = (
        "User: I'm finalizing the deployment of my application and I want to add some "
        "security hardening before the public launch, including authentication and "
        "authorization review.\n"
        "```python\nfrom flask_login import LoginManager\n```"
    )
    eps = [
        _ep("core", "User: I started the budget tracker core authentication module.", "2024-03-14 10:00:00"),
        _ep(
            "mig",
            "User: chosen Flask-Migrate 3.1.0 for migrations, and I've estimated 12 hours "
            "to implement user registration with password hashing.",
            "2024-04-05 00:00:06",
        ),
        _ep("err", err, "2024-04-05 00:00:00"),
        _ep("pref", "User: Always provide security best practices when I ask about auth.", "2024-04-25 00:01:08"),
        _ep("sec", sec, "2024-04-25 00:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "core"
    assert "err" in ids
    assert "sec" in ids
    assert "mig" not in ids
    assert "pref" not in ids
    assert len(ids) == 3


def test_milestone_pack_skips_code_dump_keeps_unique_constraint() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Give me a comprehensive summary of how my budget tracker project progressed, "
        "including key features, security, and database?"
    )
    dump = "User: ```\n" + "\n".join(f"import flask_yaml_{i}" for i in range(10)) + "\n``` UNIQUE dump"
    eps = [
        _ep("core", "User: MVP budget tracker authentication shipped March 15.", "2024-03-15 10:00:00"),
        _ep("dump", dump, "2024-04-05 09:00:00"),
        _ep(
            "uniq",
            "User: I added a SQLite UNIQUE constraint on the transactions table.",
            "2024-04-05 16:00:00",
        ),
        _ep("sec", "User: Security hashing and deployment checklist in late April.", "2024-04-25 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "dump" not in ids
    assert "uniq" in ids
    assert "core" in ids
    assert "sec" in ids


def test_first_mention_only_five_fills_later_days() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you walk me through the order in which I brought up different aspects of my "
        "app development and deployment across our conversations? Mention ONLY and ONLY five items."
    )
    eps = [
        _ep("setup", "User: Setting up the initial project with database schema.", "2024-03-14 10:00:00", "s1"),
        _ep("routes", "User: I started writing Flask routes and hashed password login.", "2024-03-14 16:00:00", "s1"),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00", "s2"),
        _ep("fields", "User: How do I add more fields to the transaction model?", "2024-04-05 16:00:00", "s2"),
        _ep("guni", "User: Gunicorn config uses 3 workers and listen on port 10000.", "2024-04-25 10:00:00", "s3"),
        _ep("tests", "User: Integration tests cover auth and transaction CRUD at 95%.", "2024-04-25 11:00:00", "s4"),
        _ep(
            "sec",
            "User: Expanding the test suite with additional security-related tests.",
            "2024-04-25 12:00:00",
            "s5",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids == ["setup", "err", "guni", "tests", "sec"]
    assert "fields" not in ids
    assert "routes" not in ids


def test_first_mention_only_five_reserves_last_day() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you walk me through the order in which I brought up different aspects of my "
        "app development and deployment across our conversations? Mention ONLY and ONLY five items."
    )
    eps = [
        _ep("setup", "User: Setting up the initial project with database schema.", "2024-03-14 10:00:00", "s1"),
        _ep("routes", "User: I started writing Flask routes and hashed password login.", "2024-03-15 10:00:00", "s1"),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00", "s2"),
        _ep("uiux", "User: Improve UI/UX based on user feedback before public launch.", "2024-04-24 10:00:00", "s1"),
        _ep("auth", "User: Here is my /login and /protected token route for review.", "2024-04-25 09:00:00", "s3"),
        _ep("guni", "User: Gunicorn config uses 3 workers and listen on port 10000.", "2024-04-25 10:00:00", "s3"),
        _ep("tests", "User: Integration tests cover auth and transaction CRUD at 95%.", "2024-04-25 11:00:00", "s4"),
        _ep(
            "sec",
            "User: Expanding the test suite with additional security-related tests.",
            "2024-04-25 12:00:00",
            "s5",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "setup"
    assert "err" in ids
    assert "auth" in ids
    assert "tests" in ids
    assert "sec" in ids
    assert "routes" not in ids
    assert "uiux" not in ids
    assert "guni" not in ids
    assert len(ids) == 5


def test_first_mention_same_day_keeps_session_openers_only() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you walk me through the order in which I brought up different aspects of my "
        "app development and deployment across our conversations? Mention ONLY and ONLY three items."
    )
    eps = [
        _ep("setup", "User: Setting up the initial project with a local server.", "2024-03-14 10:00:00", "s1"),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00", "s2"),
        _ep(
            "launch",
            "User: I'm finalizing the deployment of my application before the public launch.",
            "2024-04-25 00:00:00",
            "s3",
        ),
        _ep("late", "User: Same-session follow-up about form tokens and coverage.", "2024-04-25 00:00:22", "s3"),
        _ep("cov", "User: Still the same session, polishing coverage numbers.", "2024-04-25 00:00:38", "s3"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids == ["setup", "err", "launch"]


def test_pick_session_bundle_keeps_top_hit_not_earliest() -> None:
    from gsuid_core.ai_core.memory.retrieval.thread_recall import pick_session_bundle

    opener = _ep("op", "User: I'm worried about using AI for hiring.", "2024-05-02 00:00:00", "s1")
    early = _ep("early", "User: still worried about next steps for hiring AI.", "2024-05-02 00:00:04", "s1")
    gold = _ep("gold", "User: Michael suggested Pymetrics for soft skills.", "2024-05-02 00:00:12", "s1")
    bundled = pick_session_bundle([opener], [gold, early], per_session_cap=2)
    ids = [e["id"] for e in bundled]
    assert ids == ["op", "gold"]


def test_chrono_diverse_prefers_pymetrics_over_hiring_recap() -> None:
    from gsuid_core.ai_core.memory.retrieval.thread_recall import chrono_diverse_episodes

    eps = [
        _ep("op", "User: I'm considering using AI to automate hiring in my company.", "2024-03-15 00:00:00", "s1"),
        _ep("r1", "User: I'm considering using AI to automate hiring, but safely.", "2024-03-15 00:00:20", "s1"),
        _ep("r2", "User: What are the implications of using AI to automate hiring?", "2024-05-02 00:00:00", "s2"),
        _ep(
            "pym",
            "User: Michael suggested integrating Pymetrics for soft skills assessment.",
            "2024-05-02 00:00:12",
            "s2",
        ),
        _ep(
            "psy",
            "User: I want psychometric tests to integrate with the AI hiring flow.",
            "2024-07-18 00:00:20",
            "s3",
        ),
    ]
    ids = [e["id"] for e in chrono_diverse_episodes(eps, 3)]
    assert ids[0] == "op"
    assert "pym" in ids
    assert "psy" in ids


def test_expand_kept_sessions_rpe_keeps_mid_session_gold() -> None:
    from gsuid_core.ai_core.memory.retrieval.thread_recall import expand_kept_sessions

    pool = [
        _ep("op", "User: I'm worried about using AI for hiring.", "2024-05-02 00:00:00", "s1"),
        _ep("r1", "User: still considering using AI to automate hiring.", "2024-05-02 00:00:04", "s1"),
        _ep("pym", "User: Michael suggested Pymetrics for soft skills.", "2024-05-02 00:00:12", "s1"),
        _ep("pilot", "User: we should run a small bias-aware hiring pilot.", "2024-05-02 00:00:20", "s1"),
    ]
    out = expand_kept_sessions(["s1"], {"s1": pool}, cap_per=3)
    ids = [e["id"] for e in out]
    assert ids[0] == "op"
    assert "pym" in ids or "pilot" in ids


def test_pick_session_bundle_skips_recap_for_distinct_hit() -> None:
    from gsuid_core.ai_core.memory.retrieval.thread_recall import pick_session_bundle

    opener = _ep("op", "User: I'm considering using AI to automate hiring in my company.", "2024-03-15 00:00:00", "s1")
    recap = _ep(
        "recap",
        "User: I'm considering using AI to automate hiring, but I want to do it safely.",
        "2024-05-02 00:00:00",
        "s1",
    )
    gold = _ep(
        "gold",
        "User: Michael suggested integrating Pymetrics for soft skills assessment.",
        "2024-05-02 00:00:12",
        "s1",
    )
    bundled = pick_session_bundle([opener], [recap, gold], per_session_cap=2)
    ids = [e["id"] for e in bundled]
    assert ids == ["op", "gold"]


def test_cluster_first_mentions_keeps_late_distinct_aspect() -> None:
    from gsuid_core.ai_core.memory.retrieval.order_reconstruct import cluster_first_mentions

    hire = [1.0, 0.0, 0.0]
    pym = [0.0, 1.0, 0.0]
    psy = [0.0, 0.0, 1.0]
    eps = [
        _ep("h1", "User: worried about using AI for hiring.", "2024-03-15 00:00:00", "s1", hire),
        _ep("h2", "User: considering using AI to automate hiring.", "2024-05-02 00:00:00", "s2", hire),
        _ep("h3", "User: implications of using AI to automate hiring.", "2024-10-03 00:00:00", "s3", hire),
        _ep("h4", "User: hybrid hiring with AI screening.", "2025-01-12 00:00:00", "s4", hire),
        _ep("pym", "User: Michael suggested Pymetrics for soft skills.", "2024-05-02 00:00:12", "s2", pym),
        _ep("psy", "User: psychometric tests to integrate with AI.", "2024-07-18 00:00:20", "s5", psy),
    ]
    vecs = {e["id"]: e["embedding"] for e in eps}
    packed = cluster_first_mentions(eps, 3, vecs)
    ids = [e["id"] for e in packed]
    assert "pym" in ids
    assert "psy" in ids
    assert ids[0] == "h1"


def test_pack_first_mention_with_vectors_keeps_intra_session_hit() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you walk me through the order in which I brought up different aspects of using "
        "AI in our hiring process across our conversations, in order? Mention ONLY and ONLY three items."
    )
    hire = [1.0, 0.0, 0.0]
    pym = [0.0, 1.0, 0.0]
    soft = [0.0, 0.0, 1.0]
    eps = [
        _ep("op", "User: I'm worried about using AI for hiring.", "2024-03-15 00:00:00", "s1", hire),
        _ep("dup", "User: considering using AI to automate hiring.", "2024-05-02 00:00:00", "s2", hire),
        _ep("pym", "User: Michael suggested Pymetrics for soft skills.", "2024-05-02 00:00:12", "s2", pym),
        _ep("soft", "User: ensuring AI recognizes candidates' soft skills.", "2024-07-18 00:00:08", "s3", soft),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "pym" in ids
    assert "soft" in ids
    assert ids[0] == "op"
    assert "dup" not in ids
    assert len(ids) == 3


def test_first_mention_cluster_strides_when_over_cap() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of implementing the "
        "city search feature throughout our conversations, in order? Mention ONLY and ONLY three items."
    )
    eps = [
        _ep("a", "User: Wire geocoding with a short delay on keystrokes.", "2024-03-15 10:00:00", "s1"),
        _ep("b", "User: Show a 5-item dropdown under the city input.", "2024-03-20 10:00:00", "s2"),
        _ep("c", "User: API average response time is 280ms so timeouts need handling.", "2024-03-22 10:00:00", "s3"),
        _ep("d", "User: I scored 85% on 10 homework problems unique-hw-0.", "2024-03-25 10:00:00", "s4"),
        _ep("e", "User: Remove event listeners on unmount to prevent memory leaks.", "2024-03-28 10:00:00", "s5"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "a"
    assert ids[-1] == "e"
    assert len(ids) == 3


def test_order_topic_span_from_aspects_of() -> None:
    from gsuid_core.ai_core.memory.retrieval.event_time import order_topic_span

    q = (
        "Can you list the order in which I brought up different aspects of classifying triangles "
        "throughout our conversations, including how I first approached understanding their types, "
        "then moved on to calculating areas, identifying key characteristics, comparing types, "
        "and finally applying these concepts to more complex problems, in order? "
        "Mention ONLY and ONLY nine items."
    )
    span = order_topic_span(q).lower()
    assert "triangles" in span


def test_pack_by_stage_hints_finds_area_not_same_day_congruence() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of classifying triangles "
        "throughout our conversations, including how I first approached understanding their types, "
        "then moved on to calculating areas, identifying key characteristics, comparing types, "
        "and finally applying these concepts to more complex problems, in order? "
        "Mention ONLY and ONLY nine items."
    )
    eps = [
        _ep(
            "types",
            "User: classifying triangles by sides and angles, equilateral isosceles scalene.",
            "2024-03-03 10:00:00",
            "s1",
        ),
        _ep(
            "area",
            "User: calculating the area of an equilateral triangle with side 6 cm.",
            "2024-04-01 10:00:00",
            "s2",
        ),
        _ep(
            "chars",
            "User: key characteristics of isosceles triangles and example calculations.",
            "2024-04-10 10:00:00",
            "s3",
        ),
        _ep(
            "cmp",
            "User: comparing scalene and isosceles types and clarifying differences.",
            "2024-04-20 10:00:00",
            "s4",
        ),
        _ep("quiz", "User: my quiz score improved from 65% to 82%.", "2024-03-03 12:00:00", "s1"),
        _ep("cong", "User: verify congruence by SSS with sides 6 8 10 and 9 12 15.", "2024-06-10 10:00:00", "s5"),
        _ep("cong2", "User: estimate missing side length using ratio 4:5.", "2024-06-10 10:00:08", "s5"),
        _ep("cong3", "User: proof outline for congruence by SSS with labeled diagrams.", "2024-06-10 10:00:16", "s5"),
        _ep(
            "apply",
            "User: applying these concepts to more complex construction problems.",
            "2024-05-01 10:00:00",
            "s6",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "types" in ids
    assert "area" in ids
    assert "chars" in ids
    assert ids.index("types") < ids.index("area")
    assert ids.index("area") < ids.index("cong") if "cong" in ids else True


def test_book_club_skeleton_is_not_transaction_error() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import format_order_skeleton

    q = (
        "Can you list the order in which I brought up different aspects of my book club "
        "activities throughout our conversations in order? Mention ONLY and ONLY five items."
    )
    eps = [
        _ep(
            "err",
            "User: add error handling so the script does not crash on invalid image files.",
            "2024-03-15 10:00:00",
        )
    ]
    skel = format_order_skeleton(eps, query=q)
    assert skel
    assert "transaction" not in skel[0].lower()


def test_first_mention_last_day_skips_duplicate_deploy() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you walk me through the order in which I brought up different aspects of my "
        "app development and deployment across our conversations? Mention ONLY and ONLY five items."
    )
    eps = [
        _ep("setup", "User: Setting up the initial project with database schema.", "2024-03-14 10:00:00", "s1"),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00", "s2"),
        _ep("guni", "User: Gunicorn config uses 3 workers and listen on port 10000.", "2024-04-25 10:00:00", "s3"),
        _ep("https", "User: Once HTTPS is sorted, update Render.com deployment scripts.", "2024-04-25 10:00:08", "s3"),
        _ep("ci", "User: GitHub Actions workflow to run tests and deploy on push.", "2024-04-25 10:00:54", "s3"),
        _ep("tests", "User: Integration tests cover auth and transaction CRUD at 95%.", "2024-04-25 11:00:00", "s4"),
        _ep(
            "sec",
            "User: Expanding the test suite with additional security-related tests.",
            "2024-04-25 12:00:00",
            "s5",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "setup"
    assert "err" in ids
    assert "guni" in ids
    assert "tests" in ids
    assert "sec" in ids
    assert "https" not in ids
    assert "ci" not in ids
    assert len(ids) == 5


def test_first_mention_skips_breakdown_outline_keeps_core() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep(
            "plan",
            "User: Sure, let's break it down for my budget tracker project.\n\n"
            "### Components:\n1. User Authentication\n2. Transaction Management\n\n"
            "### Milestones:\n- Nov 1 - Nov 15, 2023: Setup Flask project and initial database schema.\n"
            "Does this breakdown work for you?",
            "2024-03-14 16:00:02",
        ),
        _ep(
            "core",
            "User: Can you help me implement the core functionality of my budget tracker, "
            "including user authentication, expense tracking, and data visualization?",
            "2024-03-15 00:00:04",
        ),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00"),
        _ep("sec", "User: Finalizing security hashing and deployment checklist.", "2024-04-25 10:00:00"),
        _ep("https", "User: Once HTTPS is sorted, update Render.com deployment scripts.", "2024-04-25 10:00:08"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "plan" in ids
    assert "err" in ids
    assert "sec" in ids
    assert "https" not in ids


def test_first_mention_skips_pytest_stub_keeps_security() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep(
            "core",
            "User: Can you help me implement the core functionality of my budget tracker, "
            "including user authentication, expense tracking, and data visualization?",
            "2024-03-15 00:00:04",
        ),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00"),
        _ep("sec", "User: Finalizing security hashing and deployment checklist.", "2024-04-25 00:00:00"),
        _ep(
            "stub",
            "User: # test transaction CRUD endpoints pass @pytest.mark.integration\ndef test_analytics(): pass",
            "2024-04-25 00:00:02",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids == ["core", "err", "sec"]
    assert "stub" not in ids


def test_first_mention_does_not_merge_deploy_into_tests() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you walk me through the order in which I brought up different aspects of my "
        "app development and deployment across our conversations? Mention ONLY and ONLY five items."
    )
    eps = [
        _ep(
            "setup",
            "User: Setting up the initial project with database schema and local server.",
            "2024-03-14 10:00",
            "s1",
        ),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00", "s2"),
        _ep(
            "guni",
            "User: Gunicorn config uses 3 workers and listen on port 10000. "
            "I've also completed integration tests covering auth endpoints.",
            "2024-04-25 10:00:02",
            "s3",
        ),
        _ep("https", "User: Once HTTPS is sorted, update Render.com deployment scripts.", "2024-04-25 10:00:08", "s3"),
        _ep("chart", "User: Upgrade Chart.js 4.3.0 with Flask 2.3.1 for rendering.", "2024-04-25 10:00:16", "s3"),
        _ep(
            "tests",
            "User: Integration tests cover auth and transaction CRUD at 95% coverage.",
            "2024-04-25 11:00:00",
            "s4",
        ),
        _ep(
            "sec",
            "User: Expanding the test suite with additional security-related tests.",
            "2024-04-25 12:00:00",
            "s5",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "setup"
    assert "err" in ids
    assert "guni" in ids
    assert "tests" in ids
    assert "sec" in ids
    assert "https" not in ids
    assert "chart" not in ids
    assert len(ids) == 5


def test_first_mention_first_day_prefers_schema_over_blueprint() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you walk me through the order in which I brought up different aspects of my "
        "app development and deployment across our conversations? Mention ONLY and ONLY five items."
    )
    eps = [
        _ep(
            "boot",
            "User: I'm trying to modularize my app into blueprints for better maintainability.",
            "2024-03-14 10:00:00",
            "s1",
        ),
        _ep(
            "setup",
            "User: Setting up the initial project with database schema and local server.",
            "2024-03-14 16:00:00",
            "s1",
        ),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05 10:00:00", "s2"),
        _ep("guni", "User: Gunicorn config uses 3 workers and listen on port 10000.", "2024-04-25 10:00:00", "s3"),
        _ep("tests", "User: Integration tests cover auth and transaction CRUD at 95%.", "2024-04-25 11:00:00", "s4"),
        _ep(
            "sec",
            "User: Expanding the test suite with additional security-related tests.",
            "2024-04-25 12:00:00",
            "s5",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "boot"
    assert "setup" not in ids
    assert len(ids) == 5


def test_milestone_pins_database_facet_over_time_anchor() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Give me a comprehensive summary of how my budget tracker project progressed, "
        "including key features, security, and database?"
    )
    eps = [
        _ep(
            "anchor",
            "User: I'm working on a project with a Time Anchor of March 15, 2024, "
            "and I need to plan my tasks accordingly.",
            "2024-04-05 00:00:00",
        ),
        _ep(
            "err",
            "User: Implementing transaction creation with proper error handling.",
            "2024-04-05 10:00:00",
        ),
        _ep(
            "uniq",
            "User: I added a SQLite UNIQUE constraint on the transactions table.",
            "2024-04-05 16:00:00",
            "s2",
        ),
        _ep("core", "User: MVP budget tracker authentication shipped March 15.", "2024-03-15 10:00:00", "s3"),
        _ep("sec", "User: Security hashing and deployment checklist in late April.", "2024-04-25 10:00:00", "s4"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "uniq" in ids
    assert "sec" in ids
    assert "core" in ids


def test_milestone_security_database_challenges_pin_unique_and_csrf() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you give me a comprehensive summary of how I handled the security and database "
        "challenges in my budget tracker app across our discussions?"
    )
    eps = [
        _ep("core", "User: MVP budget tracker authentication shipped March 15.", "2024-03-15 10:00:00"),
        _ep(
            "uniq",
            "User: sqlite3.IntegrityError UNIQUE constraint failed; I switched to UUID keys.",
            "2024-04-05 16:00:00",
        ),
        _ep(
            "csrf",
            "User: Flask-WTF CSRF token errors; I enabled CSRF protection and checked cookies.",
            "2024-04-12 10:00:00",
        ),
        _ep("git", "User: I tagged v1.0.0 and pushed 150 commits to GitHub.", "2024-04-24 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "uniq" in ids
    assert "csrf" in ids


def test_milestone_database_facet_keeps_unique_and_postgres() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you give me a comprehensive summary of how I handled the security and database "
        "challenges in my budget tracker app across our discussions?"
    )
    eps = [
        _ep("core", "User: MVP budget tracker authentication shipped March 15.", "2024-03-15 10:00:00"),
        _ep(
            "pg",
            "User: I want to switch the database from SQLite to PostgreSQL 15 on Render.com.",
            "2024-04-25 10:00:00",
        ),
        _ep(
            "uniq",
            "User: sqlite3.IntegrityError UNIQUE constraint failed; I switched to UUID keys.",
            "2024-04-05 16:00:00",
        ),
        _ep(
            "csrf",
            "User: Flask-WTF CSRF token errors; I enabled CSRF protection.",
            "2024-04-12 10:00:00",
        ),
        _ep(
            "hash",
            "User: Password hashing uses Werkzeug pbkdf2:sha256 during login verification.",
            "2024-03-20 10:00:00",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "uniq" in ids
    assert "pg" in ids
    assert "csrf" in ids
    assert "hash" in ids


def test_milestone_pins_unique_inside_traceback_dump() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you give me a comprehensive summary of how I handled the security and database "
        "challenges in my budget tracker app across our discussions?"
    )
    dump = "User: Traceback (most recent call last):\n" + "\n".join(
        f"  File app.py, line {i}, in create" for i in range(12)
    )
    dump += "\nsqlite3.IntegrityError: UNIQUE constraint failed: transactions.id"
    eps = [
        _ep("core", "User: MVP budget tracker authentication shipped March 15.", "2024-03-15 10:00:00"),
        _ep("uniq", dump, "2024-04-05 16:00:00"),
        _ep(
            "csrf",
            "User: Flask-WTF CSRF token errors; I enabled CSRF protection.",
            "2024-04-12 10:00:00",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "uniq" in ids
    assert "csrf" in ids


def test_milestone_pins_april_mvp_deadline_over_week_opener() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Give me a comprehensive summary of how my budget tracker project progressed, "
        "including key features, the development timeline, security enhancements, "
        "and documentation efforts?"
    )
    eps = [
        _ep(
            "plan",
            "User: Budget tracker components: auth, transactions, analytics. "
            "Nov 1 setup, Dec 15 auth, Jan 15 transactions.",
            "2024-03-14 16:00:00",
        ),
        _ep(
            "mvp",
            "User: Detailed project schedule to deliver the MVP by April 15, 2024.",
            "2024-04-02 10:00:00",
        ),
        _ep("tag", "User: I tagged v1.0.0 on April 20 after 150 commits.", "2024-04-20 10:00:00"),
        _ep("sec", "User: Security hashing and CSRF tokens before launch.", "2024-04-25 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "mvp" in ids
    assert "sec" in ids


def test_summarize_what_is_summary_query() -> None:
    from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_summary_query

    q = (
        "Can you summarize what I learned about implementing and improving city "
        "autocomplete features in my weather app?"
    )
    assert looks_like_summary_query(q)


def test_chrono_sample_keeps_newest_when_over_limit() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import user_episodes_chrono_sample

    class _Row:
        def __init__(self, eid: str, content: str, valid_at: datetime) -> None:
            self.id = eid
            self.content = content
            self.valid_at = valid_at
            self.scope_key = "user_global:u1"

    rows = [
        _Row(f"e{i}", f"User: turn {i} budget tracker", datetime(2024, 3, 1) + timedelta(days=i)) for i in range(90)
    ]

    async def fake_search(
        scope_key: str,
        start: datetime,
        end: datetime,
        limit: int = 24,
        ascending: bool = False,
        offset: int = 0,
        user_only: bool = False,
    ) -> list[_Row]:
        _ = (scope_key, start, end, user_only)
        ordered = list(rows) if ascending else list(reversed(rows))
        return ordered[offset : offset + limit]

    async def fake_count(scope_key: str, start: datetime, end: datetime, user_only: bool = False) -> int:
        _ = (scope_key, start, end, user_only)
        return 90

    with (
        patch(
            "gsuid_core.ai_core.memory.database.models.AIMemEpisode.search_by_valid_at_range",
            new=fake_search,
        ),
        patch(
            "gsuid_core.ai_core.memory.database.models.AIMemEpisode.count_by_valid_at_range",
            new=fake_count,
        ),
    ):
        eps = _run(user_episodes_chrono_sample(user_id="u1", group_id=None, end=None, limit=72))
    ids = [e["id"] for e in eps]
    assert "e0" in ids
    assert "e89" in ids
    assert "e45" in ids


def test_chrono_sample_keeps_mid_month_error_handling() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import user_episodes_chrono_sample

    class _Row:
        def __init__(self, eid: str, content: str, valid_at: datetime) -> None:
            self.id = eid
            self.content = content
            self.valid_at = valid_at
            self.scope_key = "user_global:u1"

    rows = [
        _Row(f"e{i}", f"User: turn {i} budget tracker", datetime(2024, 3, 1) + timedelta(days=i)) for i in range(200)
    ]
    rows[100] = _Row(
        "err",
        "User: Implementing transaction creation with proper error handling.",
        datetime(2024, 4, 5, 0, 0, 0),
    )

    async def fake_search(
        scope_key: str,
        start: datetime,
        end: datetime,
        limit: int = 24,
        ascending: bool = False,
        offset: int = 0,
        user_only: bool = False,
    ) -> list[_Row]:
        _ = (scope_key, start, end, user_only)
        ordered = list(rows) if ascending else list(reversed(rows))
        return ordered[offset : offset + limit]

    async def fake_count(scope_key: str, start: datetime, end: datetime, user_only: bool = False) -> int:
        _ = (scope_key, start, end, user_only)
        return 200

    with (
        patch(
            "gsuid_core.ai_core.memory.database.models.AIMemEpisode.search_by_valid_at_range",
            new=fake_search,
        ),
        patch(
            "gsuid_core.ai_core.memory.database.models.AIMemEpisode.count_by_valid_at_range",
            new=fake_count,
        ),
    ):
        eps = _run(user_episodes_chrono_sample(user_id="u1", group_id=None, end=None, limit=72))
    ids = [e["id"] for e in eps]
    assert "err" in ids
    assert len(ids) == 200


def test_milestone_pack_spreads_weeks() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you provide a comprehensive summary of how my budget tracker project has "
        "progressed, including the key features implemented, security enhancements, "
        "and database choices?"
    )
    eps = [
        _ep("m1", "User: Flask auth and expense tracking for the budget tracker.", "2024-03-14 10:00:00"),
        _ep("m1b", "User: More Flask auth tweaks the same week.", "2024-03-16 10:00:00"),
        _ep("apr", "User: MVP deadline April 15 with SQLite UNIQUE constraints.", "2024-04-12 10:00:00"),
        _ep("may", "User: Added Redis rate limits for security enhancements.", "2024-05-10 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "m1" in ids
    assert "apr" in ids
    assert "may" in ids
    assert ids.index("m1") < ids.index("apr") < ids.index("may")


def test_duration_pack_keeps_both_anchors() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        duration_anchor_queries,
        looks_like_duration_query,
    )

    q = "How many days passed between when I obtained my API key and when I completed the UI wireframe?"
    assert looks_like_duration_query(q)
    clauses = duration_anchor_queries(q)
    assert any("api key" in c.lower() for c in clauses)
    assert any("wireframe" in c.lower() for c in clauses)
    eps = [
        _ep("key", "User: I obtained my OpenWeather API key today.", "2024-03-10 10:00:00"),
        _ep("ui", "User: I completed the UI wireframe for my weather app.", "2024-03-12 10:00:00"),
        _ep("late", "User: I obtained a second API key rotation later.", "2024-04-01 10:00:00"),
        _ep("noise", "User: Debugging fetch latency in the weather widget.", "2024-03-20 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "key" in ids
    assert "ui" in ids
    assert ids.index("key") < ids.index("ui")
    assert ids[0] != "late"


def test_duration_pack_prefers_deployment_over_generic_deadline() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "How many weeks do I have between finishing the transaction management features "
        "and the final deployment deadline?"
    )
    eps = [
        _ep(
            "plan",
            "User: Dec 16 to Jan 15 develop transaction management. Feb 16 to Mar 15 deployment.",
            "2024-03-14 16:00:00",
        ),
        _ep("mvp", "User: I want to meet the April 15 deadline for the MVP scope.", "2024-03-14 16:00:08"),
        _ep("fin", "User: I finished the transaction management features on January 15.", "2024-03-14 16:00:10"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "plan" in ids
    assert "fin" in ids
    blob = " ".join(e["content"] or "" for e in packed)
    assert "Mar 15" in blob or "deployment" in blob.lower()


def test_expand_lexical_recall_duration_searches_both_clauses() -> None:
    calls: list[str] = []

    async def _lex(query: str, **kwargs: object) -> list[Episode]:
        _ = kwargs
        calls.append(query)
        return []

    q = "How many days passed between when I obtained my OpenWeather API key and when I completed the UI wireframe?"
    with patch(
        "gsuid_core.ai_core.memory.retrieval.lexical.lexical_search_episodes",
        new=_lex,
    ):
        _run(
            expand_lexical_recall(
                [],
                query=q,
                user_id="u1",
                group_id=None,
            )
        )
    blob = " ".join(calls).lower()
    assert "api" in blob or "obtained" in blob
    assert "wireframe" in blob or "completed" in blob
    assert len(calls) >= 3


def test_refine_skips_conflicts_on_duration() -> None:
    from gsuid_core.ai_core.kits.memory.kit import refine_retrieved_memory
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    mem = MemoryContext(
        episodes=[
            _ep("n", "User: I've never actually obtained an API key for this project.", "2024-03-14 10:00:00"),
            _ep("p", "User: I obtained my OpenWeather API key on March 10.", "2024-03-10 10:00:00"),
        ]
    )
    refine_retrieved_memory(
        mem,
        "How many days passed between when I obtained my OpenWeather API key and when I completed the UI wireframe?",
    )
    assert mem.conflicts == []


def test_refine_skips_conflicts_on_span_summary() -> None:
    from gsuid_core.ai_core.kits.memory.kit import refine_retrieved_memory
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    mem = MemoryContext(
        episodes=[
            _ep("n", "User: I've never actually obtained an API key for this project.", "2024-03-14 10:00:00"),
            _ep("p", "User: I obtained my OpenWeather API key on March 10.", "2024-03-10 10:00:00"),
        ]
    )
    refine_retrieved_memory(
        mem,
        "Can you provide a comprehensive summary of how my weather app project has progressed, "
        "including the key features implemented, security enhancements, and database choices?",
    )
    assert mem.conflicts == []


def test_expand_lexical_recall_order_uses_chrono_sample() -> None:
    called: dict[str, object] = {"n": 0}

    async def _lex(*args: object, **kwargs: object) -> list[Episode]:
        _ = (args, kwargs)
        return []

    async def _sample(
        *, user_id: str, group_id: str | None, end: datetime | None = None, limit: int = 72
    ) -> list[Episode]:
        _ = (user_id, group_id)
        called["n"] = int(called["n"]) + 1 if isinstance(called["n"], int) else 1
        called["end"] = end
        called["limit"] = limit
        return [_ep("late", "User: Finalizing security hashing and deployment.", "2024-04-25 10:00:00")]

    async def _thread(*args: object, **kwargs: object) -> list[Episode]:
        _ = (args, kwargs)
        return []

    with (
        patch("gsuid_core.ai_core.memory.retrieval.lexical.lexical_search_episodes", new=_lex),
        patch("gsuid_core.ai_core.memory.retrieval.lexical.user_episodes_chrono_sample", new=_sample),
        patch(
            "gsuid_core.ai_core.memory.retrieval.thread_recall.recall_thread_candidates",
            new=_thread,
        ),
    ):
        out = _run(
            expand_lexical_recall(
                [_ep("early", "User: I started the budget tracker core auth.", "2024-03-14 10:00:00")],
                query=(
                    "Can you list the order in which I brought up different aspects of developing "
                    "my personal budget tracker throughout our conversations, in order? "
                    "Mention ONLY and ONLY three items."
                ),
                user_id="u1",
                group_id=None,
            )
        )
    assert called["n"] == 1
    assert called["end"] is None
    assert called["limit"] == 400
    assert "late" in [e["id"] for e in out]


def test_order_topic_span_is_user_theme_not_framework_list() -> None:
    from gsuid_core.ai_core.memory.retrieval.event_time import order_topic_span

    q = (
        "Can you list the order in which I brought up different aspects of classifying "
        "triangles throughout our conversations, in order? Mention ONLY and ONLY nine items."
    )
    span = order_topic_span(q).lower()
    assert "triangles" in span
    assert "workers" not in span


def test_first_mention_skips_salary_intro_keeps_ats() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of improving my "
        "professional profile and resume throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep(
            "bio",
            "User: I'm 60 years old with an annual salary of $85,000 as a TV producer.",
            "2024-03-15 10:00:00",
        ),
        _ep(
            "ats",
            "User: I'm worried my resume will not pass applicant tracking systems.",
            "2024-03-16 10:00:00",
        ),
        _ep("scan", "User: I used Jobscan to improve resume keyword match to 80%.", "2024-04-10 10:00:00"),
        _ep("head", "User: Updated my LinkedIn profile headline for executive producer.", "2024-05-02 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "bio" not in ids
    assert ids[0] == "ats"
    assert "scan" in ids
    assert "head" in ids
    assert len(ids) == 3


def test_first_mention_skips_resume_salary_worry() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of improving my "
        "professional profile and resume throughout our conversations, in order? "
        "Mention ONLY and ONLY three items."
    )
    eps = [
        _ep(
            "bio",
            "User: I'm kinda worried about my resume, I'm 60 and earning $85,000 annually, "
            "will that be a problem when applying for jobs?",
            "2024-03-15 10:00:00",
        ),
        _ep(
            "ats",
            "User: I'm worried my resume will not pass applicant tracking systems. "
            "My partner suggested budgeting and networking.",
            "2024-03-16 10:00:00",
        ),
        _ep("scan", "User: I used Jobscan to improve resume keyword match to 80%.", "2024-04-10 10:00:00"),
        _ep("head", "User: Updated my LinkedIn profile headline for executive producer.", "2024-05-02 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] in {"bio", "ats"}
    assert "scan" in ids
    assert "head" in ids
    assert len(ids) == 3


def test_milestone_skips_breakdown_keeps_unique_csrf() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you give me a comprehensive summary of how I handled the security and database "
        "challenges in my budget tracker app across our discussions?"
    )
    eps = [
        _ep(
            "plan",
            "User: Sure, let's break it down for my budget tracker project.\n\n"
            "### Components:\n1. User Authentication\n2. Transaction Management\n\n"
            "### Milestones:\n- Nov 1 - Nov 15, 2023: Setup Flask project and initial database schema.\n"
            "Does this breakdown work for you?",
            "2024-03-14 16:00:02",
        ),
        _ep(
            "uniq",
            "User: sqlite3.IntegrityError UNIQUE constraint failed; I switched to UUID keys.",
            "2024-04-05 16:00:00",
        ),
        _ep(
            "csrf",
            "User: Flask-WTF CSRF token errors; I enabled CSRF protection and checked cookies.",
            "2024-04-12 10:00:00",
        ),
        _ep(
            "redis",
            "User: I implemented an account lockout mechanism using Redis to limit login attempts.",
            "2024-04-20 10:00:00",
        ),
        _ep(
            "hash",
            "User: Password hashing uses Werkzeug.security pbkdf2:sha256 during login verification.",
            "2024-03-20 10:00:00",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "uniq" in ids
    assert "csrf" in ids
    assert "hash" in ids


def test_first_mention_triangle_not_filled_by_later_same_day() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you list the order in which I brought up different aspects of classifying "
        "triangles throughout our conversations, in order? Mention ONLY and ONLY three items."
    )
    eps = [
        _ep(
            "eq",
            "User: Classify this triangle by sides: equilateral with all angles 60 degrees.",
            "2024-03-02 10:00:00",
        ),
        _ep(
            "iso",
            "User: Next I compared isosceles and scalene triangles with an example.",
            "2024-04-14 10:00:00",
        ),
        _ep("cos", "User: I used the law of cosines to find an unknown angle.", "2024-05-01 10:00:00"),
        _ep("extra", "User: Later the same day I also configured three workers on port 10000.", "2024-05-01 12:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "extra" not in ids
    assert ids[0] == "eq"
    assert "cos" in ids


def test_summary_throughout_is_not_order_query() -> None:
    from gsuid_core.ai_core.memory.retrieval.event_time import (
        looks_like_order_query,
        looks_like_summary_query,
    )

    q = (
        "Can you give me a clear summary of how my understanding and application of "
        "triangle similarity and congruence developed throughout our conversations?"
    )
    assert looks_like_summary_query(q)
    assert not looks_like_order_query(q)


def test_quick_summary_is_summary_query() -> None:
    from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_summary_query

    q = "Can you give me a quick summary of the sneaker options and advice we've talked about?"
    assert looks_like_summary_query(q)


def test_milestone_skips_craig_intro() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you provide a comprehensive summary of how my budget tracker project has progressed, "
        "including the key features implemented, the development timeline, security enhancements, "
        "and documentation efforts?"
    )
    eps = [
        _ep(
            "intro",
            "User: I'm Craig, a hands-on developer with a practical mindset, currently based in "
            "Vancouver and eager to build a budget tracker that actually helps me stay on top "
            "of my finances.",
            "2024-03-14 16:00:04",
        ),
        _ep(
            "uniq",
            "User: sqlite3.IntegrityError UNIQUE constraint failed; I switched to UUID keys.",
            "2024-03-28 10:00:00",
        ),
        _ep("csrf", "User: Flask-WTF CSRF token errors; I enabled CSRF protection.", "2024-04-12 10:00:00"),
        _ep("redis", "User: Redis lockout after failed logins, pbkdf2:sha256 hashing.", "2024-04-20 10:00:00"),
        _ep("conf", "User: Documented API endpoints in Confluence architecture pages.", "2024-05-02 10:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "intro"
    assert "uniq" in ids
    assert "redis" in ids
    assert "conf" in ids


def test_expand_lexical_recall_summary_past_months_uses_chrono() -> None:
    called: dict[str, int] = {"win": 0, "sample": 0}

    async def _lex(*args: object, **kwargs: object) -> list[Episode]:
        _ = (args, kwargs)
        return []

    async def _win(**kwargs: object) -> list[Episode]:
        _ = kwargs
        called["win"] += 1
        return []

    async def _sample(
        *, user_id: str, group_id: str | None, end: datetime | None = None, limit: int = 72
    ) -> list[Episode]:
        _ = (user_id, group_id, end, limit)
        called["sample"] += 1
        return [_ep("apr", "User: Family movie marathon in April with five films.", "2024-04-12 10:00:00")]

    async def _thread(*args: object, **kwargs: object) -> list[Episode]:
        _ = (args, kwargs)
        return []

    with (
        patch("gsuid_core.ai_core.memory.retrieval.lexical.lexical_search_episodes", new=_lex),
        patch("gsuid_core.ai_core.memory.retrieval.lexical.episodes_in_time_window", new=_win),
        patch("gsuid_core.ai_core.memory.retrieval.lexical.user_episodes_chrono_sample", new=_sample),
        patch(
            "gsuid_core.ai_core.memory.retrieval.thread_recall.recall_thread_candidates",
            new=_thread,
        ),
    ):
        out = _run(
            expand_lexical_recall(
                [_ep("mar", "User: Planning a family movie night.", "2024-03-12 10:00:00")],
                query=(
                    "Can you give me a summary of how I planned and organized my family "
                    "movie events and related activities over the past few months?"
                ),
                user_id="u1",
                group_id=None,
            )
        )
    assert called["win"] == 0
    assert called["sample"] == 1
    assert "apr" in [e["id"] for e in out]


def test_milestone_security_rescues_lockout() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = (
        "Can you give me a comprehensive summary of how I handled the security and database "
        "challenges in my budget tracker app across our discussions?"
    )
    eps = [
        _ep(
            f"w{i}",
            f"User: Flask schema week {i} auth forms and transaction CRUD notes.",
            f"2024-03-{(i % 28) + 1:02d} 10:00:00",
        )
        for i in range(16)
    ]
    eps.append(
        _ep(
            "lock",
            "User: Redis lockout after failed logins with atomic INCR and TTL expiry.",
            "2024-04-22 10:00:00",
        )
    )
    eps.append(
        _ep(
            "hash",
            "User: Werkzeug.security generate_password_hash default pbkdf2:sha256.",
            "2024-04-05 10:00:00",
        )
    )
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert "lock" in ids
    assert "hash" in ids


def test_fallback_clock_from_chat_uses_last_turn() -> None:
    from eval.BEAM_official.run_official import _fallback_clock_from_chat

    chat = [
        {
            "batch_number": 1,
            "time_anchor": "2024-03-14 16:00:00",
            "turns": [
                {"role": "user", "content": "hi", "time_anchor": "2024-03-14 16:00:00"},
                {"role": "assistant", "content": "hello", "time_anchor": "2024-03-14 16:00:02"},
            ],
        },
        {
            "batch_number": 2,
            "time_anchor": "2024-05-02 09:00:00",
            "turns": [
                {"role": "user", "content": "later", "time_anchor": "2024-05-02 09:00:00"},
            ],
        },
    ]
    clock = _fallback_clock_from_chat(chat)
    assert clock is not None
    assert clock.startswith("2024-05-02")


def test_timeline_summary_one_line_per_day_in_order() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import build_timeline_summary

    eps = [
        _ep("d2a", "User: second day chat about weather.", "2024-05-02 09:00:00"),
        _ep("d1a", "User: first day chat about the budget tracker schema.", "2024-03-15 10:00:00"),
        _ep("d1b", "User: more budget tracker work.", "2024-03-15 11:00:00"),
        _ep("d3a", "User: third day follow-up.", "2024-07-10 09:00:00"),
    ]
    lines = build_timeline_summary(eps, "How did my budget tracker project progress?")
    assert len(lines) == 3
    assert lines[0].startswith("2024-03-15")
    assert lines[-1].startswith("2024-07-10")
    assert "budget tracker" in lines[0]


def test_timeline_summary_skips_assistant_turns() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import build_timeline_summary

    eps = [
        _ep("a1", "assistant: here is a long answer about the topic.", "2024-03-15 10:00:00"),
        _ep("u1", "User: my own question about the topic.", "2024-03-16 10:00:00"),
    ]
    lines = build_timeline_summary(eps, "topic")
    assert len(lines) == 1
    assert lines[0].startswith("2024-03-16")


def test_timeline_summary_caps_days_by_stride() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import build_timeline_summary

    eps = [_ep(f"d{i}", f"User: session {i} about topic.", f"2024-01-{i + 1:02d} 10:00:00") for i in range(20)]
    lines = build_timeline_summary(eps, "topic", cap=5)
    assert len(lines) == 5
    assert lines[0].startswith("2024-01-01")
    assert lines[-1].startswith("2024-01-20")


def test_summary_pack_char_budget_fills_beyond_week_representatives() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = "Can you give me a comprehensive summary of how my budget tracker project has progressed?"
    rows = [
        ("csv-export", "timezone", "sqlite3", "2024-03-01 09:00:00"),
        ("budget-alert", "rounding", "redis", "2024-03-01 10:00:00"),
        ("email-digest", "smtp", "celery", "2024-03-01 11:00:00"),
        ("backup-restore", "corruption", "postgres", "2024-03-01 12:00:00"),
        ("category-chart", "legend", "matplotlib", "2024-03-08 09:00:00"),
        ("recurring-payment", "duplicate", "apscheduler", "2024-03-08 10:00:00"),
        ("csv-import", "encoding", "chardet", "2024-03-08 11:00:00"),
        ("audit-log", "truncation", "logrotate", "2024-03-08 12:00:00"),
    ]
    eps = [
        _ep(
            f"m{i}",
            (
                f"User: Day {i} of the budget tracker: shipped the {feat} module, fixed the {bug} bug and "
                f"tuned {tool} for batch-{i} with {100 + i} records in the report."
            ),
            ts,
        )
        for i, (feat, bug, tool, ts) in enumerate(rows)
    ]
    plain = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    filled = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None, char_budget=6000)
    plain_ids = [str(e["id"]) for e in plain]
    filled_ids = [str(e["id"]) for e in filled]
    assert plain_ids == filled_ids
    assert plain_ids[0] == "m0"
    assert "m4" in plain_ids
    times = [str(e["valid_at"]) for e in filled]
    assert times == sorted(times)


def test_attribute_tokens_skip_measure_words() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import attribute_content_tokens
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    toks = [
        t.lower()
        for t in attribute_content_tokens("What is the test coverage percentage for my API integration module?")
    ]
    assert toks[:2] == ["integration", "coverage"]
    assert "percentage" not in toks
    q = "What specific criteria did I use to prioritize tasks on the Trello board?"
    text = MemoryContext(
        episodes=[_ep("n1", "User: the budget tracker uses Flask and a dashboard.", "2024-04-01 10:00:00")]
    ).to_prompt_text(max_chars=4000, query=q)
    assert "budget tracker" in text
    assert "召回核对" not in text


def test_value_timeline_keeps_middle_and_survives_neighbor_budget() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import states_a_value, spread_value_episodes
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    assert states_a_value("coverage improved to 78%")
    assert states_a_value("extended the goal to 12 books by March 1")
    assert not states_a_value("the project started in 2024")
    series = [
        _ep(f"e{i}", f"User: coverage note {i} reached {20 + i}%", f"2024-02-{i + 1:02d} 10:00:00") for i in range(18)
    ]
    kept = {ep["id"] for ep in spread_value_episodes(series, cap=8)}
    assert "e0" in kept and "e17" in kept
    assert "e6" in kept or "e12" in kept
    gold = _ep(
        "gold",
        "beam_off_100k_1: unit test coverage for my API integration improved to 78%",
        "2024-03-28 00:00:56",
    )
    neighbors = [
        _ep(f"n{i}", "User: " + ("unrelated planning notes for another module. " * 30), f"2024-08-{i + 1:02d} 10:00:00")
        for i in range(8)
    ]
    text = MemoryContext(episodes=neighbors, reserved_episodes=[gold]).to_prompt_text(
        max_chars=4200,
        query="What is the test coverage percentage for my API integration module?",
    )
    assert "78%" in text
    assert "该事项的原话" in text


def test_attribute_pack_keeps_same_day_later_statement_first() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = "What is the test coverage percentage for my API integration module?"
    eps = [
        _ep(
            "old",
            "beam_off_100k_1: coverage on my API integration module reached 65% after the first run.",
            "2024-03-28 00:00:42",
        ),
        _ep(
            "new",
            "beam_off_100k_1: unit test coverage for my API integration improved to 78% this morning.",
            "2024-03-28 00:00:56",
        ),
        _ep(
            "other",
            "beam_off_100k_1: coverage on the core modules including API fetch reached 85%.",
            "2024-04-10 00:00:14",
        ),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None)
    ids = [e["id"] for e in packed]
    assert ids[0] == "new"
    assert ids.index("new") < ids.index("old")


def test_attribute_pack_group_keeps_asker_ahead_of_later_stranger() -> None:
    from gsuid_core.ai_core.memory.retrieval.lexical import apply_query_episode_pack

    q = "What is my monthly budget for books and subscriptions?"
    eps = [
        _ep("mine", "alice: my monthly budget for books is 50 dollars.", "2024-03-01 00:00:00"),
        _ep("other", "bob: my monthly budget for books is 90 dollars.", "2024-06-01 00:00:00"),
    ]
    packed = apply_query_episode_pack(eps, q, temporal_mode=False, time_range=None, asker_id="alice")
    assert packed[0]["id"] == "mine"


def test_count_prompt_prefers_stated_total_over_listing_every_mention() -> None:
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    q = "How many books am I aiming to read in my winter reading challenge?"
    mc = MemoryContext(
        episodes=[
            _ep(
                "stated",
                "User: I extended my reading challenge goal to 12 books by March 1.",
                "2024-02-01 00:01:00",
            )
        ],
        conflicts=["用户曾说没定过目标，也说过定过目标"],
    )
    text = mc.to_prompt_text(max_chars=8000, query=q)
    assert "总数优先" in text
    assert "逐条列出" not in text
    assert "极性相反" in text
    assert "较晚的用户原话" in text
    assert "12 books" in text
