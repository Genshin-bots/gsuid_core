"""V5 Turn Ledger：rule gist / 编号 / picks 排序。离线无 LLM。"""

import os
from datetime import datetime

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
from gsuid_core.ai_core.kits.memory.kit import format_retrieved_memory
from gsuid_core.ai_core.agent_run.order_answer import parse_picks, render_sorted, sort_and_fill, set_order_rendered
from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext
from gsuid_core.ai_core.memory.retrieval.ledger_timeline import (
    LedgerLine,
    LedgerView,
    rule_gist,
    _assign_marks,
    format_ledger_block,
)


def _line(eid: str, day: str, gist: str, *, is_new: bool = True, turn: int = 0, sid: str = "s1") -> LedgerLine:
    return LedgerLine(
        mark="",
        episode_id=eid,
        session_id=sid,
        valid_at=datetime.fromisoformat(f"{day}T00:00:00"),
        turn_index=turn,
        gist=gist,
        is_new=is_new,
        day=day,
    )


def test_rule_gist_keeps_turn_and_strips_code() -> None:
    text = "```python\nimport flask\ndef login():\n    pass\n```\nI want Flask-Login next."
    gist, digest = rule_gist(text, 160)
    assert "Flask-Login" in gist
    assert "import flask" in digest or "code:" in gist


def test_rule_gist_keeps_first_and_last_sentence() -> None:
    text = "I'm 28 years old. I started the budget tracker with Flask."
    gist, _digest = rule_gist(text, 160)
    assert "budget tracker" in gist


def test_order_focus_keeps_more_lines_than_summary() -> None:
    from gsuid_core.ai_core.memory.retrieval.ledger_timeline import ledger_focus_limit, ledger_focus_width

    order_q = "Can you list the order in which I brought up the budget tracker?"
    summary_q = "Can you give me a comprehensive summary of how my budget tracker progressed?"
    assert ledger_focus_limit(order_q) > ledger_focus_limit(summary_q)
    assert ledger_focus_width(order_q) > 0


def test_focus_gist_keeps_middle_clause() -> None:
    from gsuid_core.ai_core.memory.retrieval.ledger_timeline import focus_gist

    pad = "thanks for the earlier notes " * 40
    text = pad + " the account lockout uses Redis and resets the counter after login. " + pad
    gist = focus_gist(text, "Summarize the security lockout and Redis counter work", 180)
    assert "Redis" in gist
    assert "lockout" in gist


def test_rule_gist_keeps_tail_event() -> None:
    text = "Thanks for the recap, that all sounds fine. Patrick then gave leadership advice on the July 15 call."
    gist, _digest = rule_gist(text, 80)
    assert "leadership" in gist or "July 15" in gist


def test_assign_marks_and_format_has_ids() -> None:
    view = LedgerView(lines=_assign_marks([_line("e1", "2024-03-15", "opened tracker")]))
    block = format_ledger_block(view, "Can you list the order in which I brought up Flask?")
    assert "#1" in block
    assert "2024-03-15" in block
    assert "Cite timeline #id" in block


def test_parse_picks_json_id_field() -> None:
    raw = (
        '{"picks":[{"id":"#2","label":"Budget tracker components"},'
        '{"id":"#3","label":"Craig Python Flask"},'
        '{"id":"#7","label":"MVP April 15"}]}'
    )
    assert parse_picks(raw) == [
        ("#2", "Budget tracker components"),
        ("#3", "Craig Python Flask"),
        ("#7", "MVP April 15"),
    ]


def test_parse_picks_and_time_sort() -> None:
    raw = 'noise {"picks":[{"turn":"#2","label":"later"},{"turn":"#1","label":"first"}]}'
    picks = parse_picks(raw)
    assert picks == [("#2", "later"), ("#1", "first")]
    view = LedgerView(
        lines=_assign_marks(
            [
                _line("e1", "2024-03-15", "first", turn=0),
                _line("e2", "2024-04-05", "later", turn=1),
            ]
        )
    )
    pairs = sort_and_fill(picks, view, 2, "order")
    assert [ln.mark for ln, _lab in pairs] == ["#1", "#2"]
    text = render_sorted(pairs)
    assert text.startswith("1. 2024-03-15 · first")


def test_legacy_switch_constant() -> None:
    from gsuid_core.ai_core.memory.config import memory_config

    assert memory_config.eo_strategy in ("legacy", "ledger")


def test_memory_console_keeps_four_eo_keys() -> None:
    from gsuid_core.ai_core.configs.ai_config import MEMORY_CONFIG

    shown = {"session_gap_seconds", "eo_strategy", "eo_selector", "ledger_max_chars"}
    hidden = {
        "eo_direct_answer",
        "eo_reader_mode",
        "eo_no_trim",
        "eo_pad_stars",
        "eo_render",
        "eo_session_spread",
        "eo_selector_retries",
        "eo_two_pass",
        "eo_shortlist",
        "eo_shortlist_floor",
        "eo_shortlist_max",
        "eo_shortlist_chars",
        "eo_llm_on_fail",
        "eo_pick",
        "eo_ground",
        "ledger_full_turns",
        "ledger_line_chars",
        "ledger_per_session_floor",
    }
    assert shown <= set(MEMORY_CONFIG)
    assert hidden.isdisjoint(MEMORY_CONFIG)


def test_star_and_floor_not_trimmed() -> None:
    from gsuid_core.ai_core.memory.retrieval.ledger_timeline import protected_ids

    lines = [
        _line("e1", "2024-03-15", "open", is_new=True, turn=0),
        _line("e2", "2024-03-15", "follow", is_new=False, turn=1),
        _line("e3", "2024-03-15", "later", is_new=False, turn=2),
        _line("e4", "2024-05-02", "new sess", is_new=True, turn=0),
    ]
    prot = protected_ids(lines, floor=1)
    assert "e1" in prot
    assert "e4" in prot
    assert "e2" not in prot


def test_pad_picks_keeps_time_order() -> None:
    view = LedgerView(
        lines=_assign_marks(
            [
                _line("e1", "2024-03-15", "alpha", is_new=True, turn=0),
                _line("e2", "2024-04-01", "beta", is_new=True, turn=1),
                _line("e3", "2024-05-02", "gamma", is_new=True, turn=2),
            ]
        )
    )
    pairs = sort_and_fill([("#3", "gamma")], view, 3, "list the order of alpha beta gamma")
    assert [ln.mark for ln, _lab in pairs] == ["#1", "#2", "#3"]


def test_format_hides_ledger_only_when_selector_rendered() -> None:
    q = "Can you list the order in which I brought up aspects, in order? Mention ONLY three items."
    view = LedgerView(lines=_assign_marks([_line("e1", "2024-03-15", "opened tracker")]))
    mem = MemoryContext(ledger=view)
    ctx = AgentHookContext(point=AgentHookPoint.RETRIEVE_CONTEXT, create_by="Http_Chat", query=q)
    os.environ["GSUID_EO_STRATEGY"] = "ledger"
    os.environ["GSUID_EO_SELECTOR"] = "dedicated"
    try:
        set_order_rendered("")
        dumped = format_retrieved_memory(ctx, mem)
        assert "opened tracker" in dumped or "#1" in dumped
        set_order_rendered("1. 2024-03-15 · opened tracker")
        assert format_retrieved_memory(ctx, mem) == ""
    finally:
        set_order_rendered("")
        os.environ.pop("GSUID_EO_STRATEGY", None)
        os.environ.pop("GSUID_EO_SELECTOR", None)


def test_mark_evidence_context() -> None:
    from gsuid_core.ai_core.agent_run.evidence_stage import mark_turn_ids, reset_evidence, get_evidence_marks

    reset_evidence()
    assert mark_turn_ids(["#1", "#1", "#2"]) == 2
    assert get_evidence_marks() == ["#1", "#2"]
    reset_evidence()


def test_render_sorted_uses_gist_not_label() -> None:
    from gsuid_core.ai_core.agent_run.order_answer import picks_collapsed

    view = LedgerView(
        lines=_assign_marks(
            [
                _line("e1", "2024-03-15", "opened a budget tracker with charts", turn=0, sid="s0"),
                _line("e2", "2024-04-05", "added transaction error handling", turn=1, sid="s1"),
            ]
        )
    )
    pairs = [(view.lines[0], "short label"), (view.lines[1], "other")]
    text = render_sorted(pairs, mode="gist")
    assert "opened a budget tracker with charts" in text
    assert "short label" not in text
    labeled = render_sorted(pairs, mode="label")
    assert "short label" in labeled
    assert parse_picks('{"picks":[{"id":"#3"}]}') == [("#3", "")]
    wide = LedgerView(
        lines=_assign_marks(
            [
                _line("a", "2024-03-15", "a", turn=0, sid="s0"),
                _line("b", "2024-04-01", "b", turn=0, sid="s1"),
                _line("c", "2024-05-01", "c", turn=0, sid="s2"),
            ]
        )
    )
    collapsed = [(wide.lines[0], "x"), (wide.lines[0], "y"), (wide.lines[0], "z")]
    assert picks_collapsed(collapsed, wide, 3) is True
    spread = [(wide.lines[0], "x"), (wide.lines[1], "y"), (wide.lines[2], "z")]
    assert picks_collapsed(spread, wide, 3) is False
