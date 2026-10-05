"""Regression locks for temporal_search_query topic extraction.

BEAM 1M summarization failures traced to two defects here:
  1. ``_TEMPORAL_ENUM_RE`` matched bare prefixes, so ``summary`` became a lone
     ``y`` and ``developed`` became ``ed`` -- noise that crowded real topic
     words out of the 14-slot ``query_tokens`` budget.
  2. Request boilerplate (``give me a``, ``comprehensive``) survived stripping
     while the topic it was wrapped around was destroyed.
"""

from __future__ import annotations

import pytest

from gsuid_core.ai_core.memory.retrieval.lexical import query_tokens
from gsuid_core.ai_core.memory.retrieval.event_time import temporal_search_query


@pytest.mark.parametrize(
    ("query", "must_keep"),
    [
        (
            "Can you give me a summary of how my interactions and efforts to improve "
            "sleep habits with Nicolas and Bruce have developed over time?",
            ("sleep", "habits", "Nicolas", "Bruce"),
        ),
        (
            "Can you give me a comprehensive summary of how my concerns about chest "
            "tightness and blood pressure have been addressed?",
            ("chest", "tightness", "blood", "pressure"),
        ),
        (
            "Can you give me a complete summary of the planning, budgeting, safety, "
            "and installation process for my attic insulation upgrade?",
            ("attic", "insulation", "upgrade"),
        ),
    ],
)
def test_summary_query_keeps_topic_words(query: str, must_keep: tuple[str, ...]) -> None:
    """BEAM 摘要题的专名与主题词必须活到检索串里。"""
    out = temporal_search_query(query)
    for word in must_keep:
        assert word in out, f"{word!r} lost from topic query: {out!r}"


def test_prefix_strip_does_not_leave_word_fragments() -> None:
    """``summary`` 不能被砍成孤立 ``y``，``developed`` 不能变 ``ed``。"""
    out = temporal_search_query("Give me a summary of how my symptoms have developed and my sleep improved")
    assert " y " not in f" {out} ", f"lone fragment 'y' survived: {out!r}"
    assert " ed " not in f" {out} ", f"lone fragment 'ed' survived: {out!r}"
    assert "improve" in out, f"improve was chopped: {out!r}"


def test_boilerplate_is_stripped() -> None:
    """请求套话不占 token 名额。"""
    out = temporal_search_query("Can you give me a comprehensive summary of my attic insulation")
    for junk in ("give", "comprehensive", "summary"):
        assert junk not in out.lower(), f"boilerplate {junk!r} survived: {out!r}"


def test_boilerplate_only_query_falls_back_to_original() -> None:
    """全是套话时退回原问句，不返回空串。"""
    query = "Give me a summary"
    assert temporal_search_query(query) == query


def test_abbreviations_do_not_split_words() -> None:
    """``i'm`` / ``we're`` 只在独立成词时剥，不能切开 improve / answer。"""
    assert "improve" in temporal_search_query("Tell me how my symptoms have improved")
    out = temporal_search_query("I'm managing stress and we've been discussing the budget")
    assert "managing" in out and "stress" in out
    assert "discussing" in out and "budget" in out


def test_all_caps_acronyms_survive_boilerplate_strip() -> None:
    """全大写 IT/US 是专名，不能跟小写代词一起被当成套话剥掉。"""
    out = temporal_search_query("Give me a summary of the US IT outage timeline")
    assert "US" in out and "IT" in out


def test_capital_article_lookalike_survives() -> None:
    """大写 A 是主题记号。只有小写 a/an 才当冠词剥掉。"""
    out = temporal_search_query("Give me a summary of Plan A and vitamin A")
    assert "Plan" in out and "A" in out
    assert "vitamin" in out


def test_curly_apostrophe_contractions_are_stripped() -> None:
    """U+2019 弯引号也算词内字符，不能留下裸 ``'ve`` 挂在句首。"""
    out = temporal_search_query(
        "Can you give me a thorough summary of everything we’ve covered about my relationship with April?"
    )
    assert "'ve" not in out and "’ve" not in out, f"contraction residue: {out!r}"
    assert "April" in out and "relationship" in out


def test_topic_tokens_stay_within_budget_and_are_relevant() -> None:
    """剥完套话后，14 个 token 名额应被实词占满。"""
    toks = query_tokens(
        temporal_search_query(
            "Can you give me a comprehensive summary of how my concerns about chest "
            "tightness and blood pressure have been addressed?"
        )
    )
    singles = [t for t in toks if " " not in t]
    assert len(singles) >= 6, f"topic words too few: {toks}"
    # 请求套话必须被剥掉。
    for junk in ("give", "comprehensive", "summary"):
        assert junk not in singles, f"套话 {junk!r} 没剥掉: {singles}"
    # 但内容词要留下。原写法 `junk not in singles or junk == "concerns"`
    # 对 concerns 恒真（`False or True`），等于没锁这个词。
    for keep in ("concerns", "chest", "tightness", "pressure"):
        assert keep in singles, f"内容词 {keep!r} 被误剥: {singles}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
