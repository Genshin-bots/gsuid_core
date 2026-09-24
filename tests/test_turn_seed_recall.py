"""本轮种子不进快照；find_tools 按触发词钉扎，不按注册顺序截掉对口工具。"""

from dataclasses import dataclass

import pytest

from gsuid_core.ai_core import register as ai_register
from gsuid_core.ai_core.rag import tools as rag_tools
from gsuid_core.ai_core.agent_run.tools import append_turn_seeds, stabilize_session_tool_names


@dataclass
class FakeTool:
    name: str


@dataclass
class FakeTB:
    name: str
    covers: list[str]
    hide_from_main: bool
    capability_domain: str
    tool: FakeTool


def _tb(name: str, covers: list[str], domain: str = "", *, hidden: bool = False) -> FakeTB:
    return FakeTB(name, covers, hidden, domain, FakeTool(name))


def _seeds(*names: str):
    return [FakeTool(name) for name in names]


def _install_triggers(monkeypatch: pytest.MonkeyPatch, tools: list[FakeTB]) -> None:
    bucket = {tb.name: tb for tb in tools}
    monkeypatch.setattr(rag_tools, "get_registered_tools", lambda: {"by_trigger": bucket})


def test_view_gacha_outranks_refresh_when_user_asks_to_look(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _tb("send_gacha_log_card_info", ["抽卡记录"])
    refresh = _tb("send_refresh_gacha_info", ["刷新抽卡记录"])
    _install_triggers(monkeypatch, [view, refresh])
    hits = rag_tools.trigger_keyword_hits("看看我的抽卡记录")
    assert [tb.name for tb in hits] == ["send_gacha_log_card_info"]


def test_longer_cover_wins_when_both_match(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _tb("send_gacha_log_card_info", ["抽卡记录"])
    refresh = _tb("send_refresh_gacha_info", ["刷新抽卡记录"])
    _install_triggers(monkeypatch, [view, refresh])
    hits = rag_tools.trigger_keyword_hits("刷新抽卡记录")
    assert [tb.name for tb in hits] == ["send_refresh_gacha_info", "send_gacha_log_card_info"]


def test_cover_hit_is_only_the_current_utterance(monkeypatch: pytest.MonkeyPatch) -> None:
    abyss = _tb("send_abyss_review", ["深渊怎么打"])
    _install_triggers(monkeypatch, [abyss])
    assert rag_tools.trigger_keyword_hits("深渊怎么打")
    assert rag_tools.trigger_keyword_hits("那你倒是看啊") == []


def test_cover_command_ignores_a_longer_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _tb("send_status", ["刷新状态"])
    _install_triggers(monkeypatch, [status])
    assert rag_tools.cover_dominates_utterance("刷新状态")
    assert rag_tools.cover_dominates_utterance("请帮我看看刷新状态？")
    mentioned = "刷新状态这个说法先别查，你说说思路"
    assert rag_tools.trigger_keyword_hits(mentioned)
    assert not rag_tools.cover_dominates_utterance(mentioned)


def test_abyss_howto_pins_short_cover(monkeypatch: pytest.MonkeyPatch) -> None:
    lineup = _tb("send_abyss_review", ["深渊怎么打", "深渊阵容"])
    floor = _tb("get_user_genshin_player_info", ["深渊层数", "原神冒险等阶"])
    _install_triggers(monkeypatch, [lineup, floor])
    hits = rag_tools.trigger_keyword_hits("深渊怎么打")
    assert [tb.name for tb in hits] == ["send_abyss_review"]


def test_artifact_advice_pins_kb_cover(monkeypatch: pytest.MonkeyPatch) -> None:
    kb = _tb("search_genshin_kb", ["带什么圣遗物", "原神角色配队"])
    monkeypatch.setattr(rag_tools, "get_registered_tools", lambda: {"common": {kb.name: kb}})
    hits = rag_tools.trigger_keyword_hits("芙宁娜带千岩了，沃雅妮莎带什么圣遗物")
    assert [tb.name for tb in hits] == ["search_genshin_kb"]


def test_abyss_howto_does_not_pin_full_command(monkeypatch: pytest.MonkeyPatch) -> None:
    abyss = _tb("send_abyss_info", ["查询深渊", "上期深渊"])
    _install_triggers(monkeypatch, [abyss])
    assert rag_tools.trigger_keyword_hits("深渊怎么打") == []


def test_hidden_trigger_is_not_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    hidden = _tb("send_secret", ["抽卡记录"], hidden=True)
    _install_triggers(monkeypatch, [hidden])
    assert rag_tools.trigger_keyword_hits("看看我的抽卡记录") == []


def test_seed_survives_registration_order_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """族有 8 个成员、上限 6：种子若按注册顺序在第 8 位，仍必须留下。"""
    members = [_tb(f"m{i}", [], domain="抽卡") for i in range(8)]
    members[7] = _tb("m7", ["抽卡记录"], domain="抽卡")
    by_name = {tb.name: tb for tb in members}

    monkeypatch.setattr(ai_register, "find_tool_base", lambda name: by_name.get(name))
    monkeypatch.setattr(ai_register, "get_tools_by_capability_domain", lambda domain: list(members))
    monkeypatch.setattr(rag_tools, "get_registered_tools", lambda: {})

    out, slots = rag_tools.collect_domain_tools(
        "看看我的抽卡记录",
        _seeds("m7"),
        domain_limit=1,
        per_domain_limit=6,
    )
    names = [tool.name for tool in out]
    assert slots == 1
    assert names[0] == "m7"
    assert len(names) == 6


def test_domainless_tools_share_one_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_register, "find_tool_base", lambda name: _tb(name, []))
    monkeypatch.setattr(rag_tools, "get_registered_tools", lambda: {})
    seeds = _seeds(
        "send_refresh_gacha_info",
        "send_gachas",
        "send_full_refresh_gacha_info",
        "send_gacha_log_card_info",
    )
    out, slots = rag_tools.collect_domain_tools("看看我的抽卡记录", seeds, domain_limit=3, per_domain_limit=6)
    assert slots == 1
    assert [tool.name for tool in out] == [seed.name for seed in seeds]


def test_keyword_pin_does_not_spend_a_domain_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _tb("send_gacha_log_card_info", ["抽卡记录"])
    domains = [_tb(f"d{i}", [], domain=f"域{i}") for i in range(3)]
    by_name: dict[str, FakeTB] = {tb.name: tb for tb in domains}
    by_name[view.name] = view

    monkeypatch.setattr(ai_register, "find_tool_base", lambda name: by_name.get(name))
    monkeypatch.setattr(
        ai_register,
        "get_tools_by_capability_domain",
        lambda domain: [tb for tb in domains if tb.capability_domain == domain],
    )
    monkeypatch.setattr(rag_tools, "get_registered_tools", lambda: {"by_trigger": {view.name: view}})

    out, slots = rag_tools.collect_domain_tools(
        "看看我的抽卡记录",
        _seeds(*(tb.name for tb in domains)),
        domain_limit=3,
        per_domain_limit=6,
    )
    names = [tool.name for tool in out]
    assert names[0] == "send_gacha_log_card_info"
    assert slots == 3
    assert {tb.name for tb in domains} <= set(names)


def test_append_turn_seeds_keeps_send_and_skips_exclusive() -> None:
    tools = _seeds("find_tools")
    added = append_turn_seeds(
        tools,
        _seeds("send_abyss_info", "render_html_to_image", "find_tools"),
        exclusive={"render_html_to_image"},
    )
    assert added == ["send_abyss_info"]
    assert [tool.name for tool in tools] == ["find_tools", "send_abyss_info"]


def test_snapshot_ignores_names_that_were_not_incoming() -> None:
    frozen = stabilize_session_tool_names(None, ["find_tools"], exclusive=set(), ceiling=24)
    nxt = stabilize_session_tool_names(frozen, ["web_search_tool"], exclusive=set(), ceiling=24)
    assert nxt == ["find_tools", "create_subagent", "capability_map", "web_search_tool"]
