"""时间线上的 # 标记必须能指回唯一一条 episode。"""

from __future__ import annotations

from gsuid_core.ai_core.buildin_tools.memory_timeline import _locate_turn


class _Row:
    def __init__(self, eid: str) -> None:
        self.id = eid


def test_mark_prefix_finds_one_turn_and_reports_collisions() -> None:
    rows = [
        _Row("abcdef12-1111-1111-1111-111111111111"),
        _Row("abcdef12-2222-2222-2222-222222222222"),
        _Row("99999999-3333-3333-3333-333333333333"),
    ]
    idx, note = _locate_turn(rows, "#99999999")
    assert idx == 2 and note == ""
    full, full_note = _locate_turn(rows, rows[2].id)
    assert full == 2 and full_note == ""
    hashed, hashed_note = _locate_turn(rows, "#" + rows[2].id)
    assert hashed == 2 and hashed_note == ""
    ambiguous, why = _locate_turn(rows, "#abcdef12")
    assert ambiguous < 0 and "多条" in why
    missing, miss = _locate_turn(rows, "#00000000")
    assert missing < 0 and miss
