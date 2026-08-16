"""Persona 启用范围：global* 全互斥与会话匹配。"""

from pathlib import Path

from gsuid_core.ai_core.persona.config import (
    PERSONA_SCOPE_VALUES,
    PersonaConfigManager,
    scope_covers_session,
    persona_config_manager,
    global_channels_for_scope,
    persona_should_inspect_session,
)


def _mgr(tmp_path: Path) -> PersonaConfigManager:
    mgr = PersonaConfigManager()
    mgr._base_path = tmp_path
    mgr._cache = {}
    return mgr


def _sid(*, kind: str, target: str) -> str:
    return f"ws-onebot:onebot:bot_001:{kind}:{target}"


def test_scope_channel_helpers() -> None:
    assert global_channels_for_scope("global") == frozenset({"group", "private"})
    assert global_channels_for_scope("global_group") == frozenset({"group"})
    assert global_channels_for_scope("global_private") == frozenset({"private"})
    assert global_channels_for_scope("specific") == frozenset()
    assert global_channels_for_scope("disabled") == frozenset()
    assert scope_covers_session("global", is_private=False)
    assert scope_covers_session("global", is_private=True)
    assert scope_covers_session("global_group", is_private=False)
    assert not scope_covers_session("global_group", is_private=True)
    assert scope_covers_session("global_private", is_private=True)
    assert not scope_covers_session("global_private", is_private=False)
    assert not scope_covers_session("specific", is_private=False)


def test_set_scope_accepts_new_values(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    for scope in PERSONA_SCOPE_VALUES:
        ok, msg = mgr.set_scope("alice", scope)
        assert ok, msg
        assert mgr.get_config("alice").get_config("scope").data == scope
    ok, msg = mgr.set_scope("alice", "not_a_scope")
    assert not ok
    assert "无效的启用范围" in msg


def test_global_like_scopes_are_fully_exclusive(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.set_scope("group_bot", "global_group")[0]
    ok, msg = mgr.set_scope("dm_bot", "global_private")
    assert not ok
    assert "group_bot" in msg
    ok, msg = mgr.set_scope("all_bot", "global")
    assert not ok
    assert "group_bot" in msg
    assert mgr.find_conflicting_global_personas("dm_bot", "global_private") == ["group_bot"]
    assert mgr.find_conflicting_global_personas("all_bot", "global") == ["group_bot"]
    assert mgr.set_scope("group_bot", "disabled")[0]
    assert mgr.set_scope("dm_bot", "global_private")[0]
    ok, msg = mgr.set_scope("all_bot", "global")
    assert not ok
    assert "dm_bot" in msg


def test_same_global_scope_is_unique(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.set_scope("a", "global_group")[0]
    ok, msg = mgr.set_scope("b", "global_group")
    assert not ok
    assert "a" in msg
    assert mgr.set_scope("a", "global")[0]
    ok, msg = mgr.set_scope("b", "global_private")
    assert not ok
    assert "a" in msg


def test_get_persona_for_session_prefers_specific(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.set_scope("fallback", "global")[0]
    assert mgr.set_scope("special", "specific")[0]
    assert mgr.set_target_groups("special", ["g1"])[0]
    assert mgr.get_persona_for_session(_sid(kind="group", target="g1")) == "special"
    assert mgr.get_persona_for_session(_sid(kind="group", target="g2")) == "fallback"
    assert mgr.get_persona_for_session(_sid(kind="private", target="u1")) == "fallback"


def test_get_persona_for_session_channel_fallback(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.set_scope("group_bot", "global_group")[0]
    assert mgr.get_persona_for_session(_sid(kind="group", target="g1")) == "group_bot"
    assert mgr.get_persona_for_session(_sid(kind="private", target="u1")) is None
    assert mgr.get_fallback_persona(is_private=False) == "group_bot"
    assert mgr.get_fallback_persona(is_private=True) is None
    assert mgr.get_global_persona() is None
    assert mgr.set_scope("group_bot", "disabled")[0]
    assert mgr.set_scope("dm_bot", "global_private")[0]
    assert mgr.get_persona_for_session(_sid(kind="group", target="g1")) is None
    assert mgr.get_persona_for_session(_sid(kind="private", target="u1")) == "dm_bot"


def test_get_persona_for_session_global_beats_channel(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.set_scope("all_bot", "global")[0]
    # 同人格改通道后，global 仍覆盖两侧
    assert mgr.get_persona_for_session(_sid(kind="group", target="g1")) == "all_bot"
    assert mgr.get_persona_for_session(_sid(kind="private", target="u1")) == "all_bot"


def test_disabled_and_empty_have_no_fallback(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.set_scope("off", "disabled")[0]
    assert mgr.get_persona_for_session(_sid(kind="group", target="g1")) is None
    assert mgr.get_persona_for_session(_sid(kind="private", target="u1")) is None


def test_inspector_should_inspect_respects_channel() -> None:
    assert persona_should_inspect_session("global", group_id="g1", target_groups=[])
    assert persona_should_inspect_session("global", group_id=None, target_groups=[])
    assert persona_should_inspect_session("global_group", group_id="g1", target_groups=[])
    assert not persona_should_inspect_session("global_group", group_id=None, target_groups=[])
    assert not persona_should_inspect_session("global_private", group_id="g1", target_groups=[])
    assert persona_should_inspect_session("global_private", group_id=None, target_groups=[])
    assert not persona_should_inspect_session("disabled", group_id="g1", target_groups=[])
    assert persona_should_inspect_session("specific", group_id="g1", target_groups=["g1"])
    assert not persona_should_inspect_session("specific", group_id="g1", target_groups=["g2"])
    assert not persona_should_inspect_session("specific", group_id=None, target_groups=["u1"])


def test_template_options_include_new_scopes() -> None:
    options = persona_config_manager._config_template["scope"].options
    assert list(options) == list(PERSONA_SCOPE_VALUES)
