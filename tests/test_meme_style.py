"""人格表情频率只改系统提示里的一句说明。"""

from gsuid_core.ai_core.persona.meme_style import (
    DEFAULT_MEME_FREQUENCY,
    meme_frequency_line_for,
    resolve_meme_frequency_line,
)

_RARE = "频率：少用。只在对方先发表情，或一个表情就能回完时才发。"
_MID = "频率：闲聊时大约每五到八次回复用一次。"
_OFTEN = "频率：闲聊时大约每两到三次回复用一次。"


def test_default_fifty_is_mid() -> None:
    assert DEFAULT_MEME_FREQUENCY == 50
    assert meme_frequency_line_for(50) == _MID
    assert resolve_meme_frequency_line(None) == _MID


def test_frequency_tiers() -> None:
    assert meme_frequency_line_for(0) == _RARE
    assert meme_frequency_line_for(24) == _RARE
    assert meme_frequency_line_for(25) == _MID
    assert meme_frequency_line_for(50) == _MID
    assert meme_frequency_line_for(74) == _MID
    assert meme_frequency_line_for(75) == _OFTEN
    assert meme_frequency_line_for(100) == _OFTEN


def test_resolve_falls_back_when_key_or_type_is_wrong(tmp_path, monkeypatch) -> None:
    from gsuid_core.ai_core import resource as core_res
    from gsuid_core.ai_core.persona import settings as settings_mod
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.utils.plugins_config.models import GsStrConfig, GsBoolConfig

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    old_base = persona_config_manager._base_path
    old_settings = settings_mod.persona_settings_manager._base_path
    persona_config_manager._base_path = tmp_path
    settings_mod.persona_settings_manager._base_path = tmp_path
    persona_config_manager._cache.clear()
    try:
        (tmp_path / "测").mkdir()
        cfg = persona_config_manager.get_config("测")
        assert resolve_meme_frequency_line("测") == _MID

        cfg.config["meme_frequency"] = GsBoolConfig("表情频率", "x", True)
        assert resolve_meme_frequency_line("测") == _MID

        cfg.config["meme_frequency"] = GsStrConfig("表情频率", "x", "50")
        assert resolve_meme_frequency_line("测") == _MID
    finally:
        persona_config_manager._cache.clear()
        persona_config_manager._base_path = old_base
        settings_mod.persona_settings_manager._base_path = old_settings
