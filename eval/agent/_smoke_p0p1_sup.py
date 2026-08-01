"""Smoke: C-1 ellipsis is tool-trajectory only; soft_continue covers text-only history."""

from gsuid_core.ai_core.interaction_scaffold import (
    SOFT_CONTINUE_HINT,
    CheapGate,
    build_turn_graph,
    detect_ellipsis_followup,
    scaffold_hints_from_graph,
)


def main() -> None:
    h = [
        ("user", "阿北(用户ID:9101)：@早柚 帮我看下模拟标的乙今天怎么样"),
        ("assistant", "唔…今天偏弱…绿的。"),
    ]
    msg = "阿北(用户ID:9101)：那它所在的板块呢"
    # 无 tool 轨迹 → ellipsis 不得因历史正文词触发
    assert not detect_ellipsis_followup(msg, h, speaker_id="9101", recent_tool_call=False)
    assert detect_ellipsis_followup(msg, h, speaker_id="9101", recent_tool_call=True)

    tg = build_turn_graph(
        msg,
        persona_name="早柚",
        is_tome=False,
        user_type="group",
        primary_speaker="9101",
        recent=h,
        recent_tool_call=False,
    )
    assert tg.soft_continue, "text-only short continue should still be soft_continue"
    assert not tg.ellipsis_followup
    hints = scaffold_hints_from_graph(tg, cheap=CheapGate.FULL)
    assert SOFT_CONTINUE_HINT in hints
    print("ok soft_continue without prior_action_re")


if __name__ == "__main__":
    main()
