"""说话强度与字数：默认值跟随档位，控制台预览用发送时的字。"""

from gsuid_core.ai_core.persona.chat_style import speech_shape_spec, resolve_chat_style


def test_speech_shape_spec_matches_runtime_tiers() -> None:
    spec = speech_shape_spec()
    assert spec["soft_default"] == 60
    assert spec["len_min"] == 5
    assert spec["len_max"] == 400
    assert "hard_default" not in spec
    assert [tier["floor"] for tier in spec["tiers"]] == [0, 25, 75]
    assert "hard" not in spec["tiers"][0]
    assert resolve_chat_style(None).soft == spec["tiers"][1]["soft"]


def test_template_default_follows_tier_and_other_numbers_pin(tmp_path, monkeypatch) -> None:
    from gsuid_core.ai_core import resource as core_res
    from gsuid_core.ai_core.persona import settings as settings_mod, appearance as appearance_mod
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.persona.placeholder_preview import preview_persona

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr(appearance_mod, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr(settings_mod, "PERSONA_PATH", tmp_path)
    old_base = persona_config_manager._base_path
    old_settings = settings_mod.persona_settings_manager._base_path
    persona_config_manager._base_path = tmp_path
    settings_mod.persona_settings_manager._base_path = tmp_path
    persona_config_manager._cache.clear()
    settings_mod.persona_settings_manager._cache.clear()
    try:
        (tmp_path / "测").mkdir()
        assert persona_config_manager.set_chat_style("测", True)[0] is False
        assert persona_config_manager.set_speech_len_soft("测", True)[0] is False
        assert persona_config_manager.set_chat_style("测", 101)[0] is False
        assert persona_config_manager.set_chat_style("测", 0)[0] is True
        assert persona_config_manager.set_speech_len_soft("测", 60)[0] is True
        followed = resolve_chat_style("测")
        assert followed.soft == 30
        assert followed.hard == 80
        preview = preview_persona("测")
        assert preview["values"]["soft"] == "30"
        assert "hard" not in preview["values"]
        assert preview["soft_follows"] is True

        assert persona_config_manager.set_speech_len_soft("测", 45)[0] is True
        assert persona_config_manager.set_speech_len_soft("测", 4)[0] is False
        assert persona_config_manager.set_speech_len_soft("测", 401)[0] is False
        pinned = resolve_chat_style("测")
        assert pinned.soft == 45
        assert pinned.hard == 80
        pinned_preview = preview_persona("测")
        assert pinned_preview["values"]["soft"] == "45"
        assert pinned_preview["soft_follows"] is False

        saved = persona_config_manager.get_persona_config_dict("测")
        assert saved is not None
        assert saved["chat_style"] == 0
        assert saved["speech_len_soft"] == 45
        assert "speech_len_hard" not in saved
        assert saved["speech_shape"]["soft_default"] == 60
    finally:
        persona_config_manager._cache.clear()
        settings_mod.persona_settings_manager._cache.clear()
        persona_config_manager._base_path = old_base
        settings_mod.persona_settings_manager._base_path = old_settings
