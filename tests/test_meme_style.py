"""人格表情频率与形态档测试。"""

import asyncio

from gsuid_core.ai_core.persona.meme_style import (
    DEFAULT_MEME_FREQUENCY,
    DEFAULT_MEME_COOLDOWN_SEC,
    resolve_meme_style,
    meme_frequency_line_for,
    resolve_meme_frequency_line,
)


def test_default_fifty_is_mid() -> None:
    assert DEFAULT_MEME_FREQUENCY == 50
    style = resolve_meme_style(None)
    assert style.enabled is True
    assert style.frequency == 50
    assert style.cooldown_sec == DEFAULT_MEME_COOLDOWN_SEC
    assert "自然适中" in style.frequency_line


def test_frequency_tiers() -> None:
    # 0 禁用
    assert "已禁用表情包" in meme_frequency_line_for(0)
    # 1-19 极少
    assert "极少使用" in meme_frequency_line_for(1)
    assert "极少使用" in meme_frequency_line_for(19)
    # 20-39 低频
    assert "低频点缀" in meme_frequency_line_for(20)
    assert "低频点缀" in meme_frequency_line_for(39)
    # 40-64 适中
    assert "自然适中" in meme_frequency_line_for(40)
    assert "自然适中" in meme_frequency_line_for(64)
    # 65-84 活跃
    assert "高频活跃" in meme_frequency_line_for(65)
    assert "高频活跃" in meme_frequency_line_for(84)
    # 85-100 必配
    assert "极高频必配" in meme_frequency_line_for(85)
    assert "极高频必配" in meme_frequency_line_for(100)


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
        assert "自然适中" in resolve_meme_frequency_line("测")

        cfg.config["meme_frequency"] = GsBoolConfig("表情频率", "x", True)
        assert "自然适中" in resolve_meme_frequency_line("测")

        cfg.config["meme_frequency"] = GsStrConfig("表情频率", "x", "50")
        assert "自然适中" in resolve_meme_frequency_line("测")
    finally:
        persona_config_manager._cache.clear()
        persona_config_manager._base_path = old_base
        settings_mod.persona_settings_manager._base_path = old_settings


def test_resolve_meme_style_cooldown_override(tmp_path, monkeypatch) -> None:
    from gsuid_core.ai_core import resource as core_res
    from gsuid_core.ai_core.persona import settings as settings_mod
    from gsuid_core.ai_core.persona.config import persona_config_manager

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    old_base = persona_config_manager._base_path
    old_settings = settings_mod.persona_settings_manager._base_path
    persona_config_manager._base_path = tmp_path
    settings_mod.persona_settings_manager._base_path = tmp_path
    persona_config_manager._cache.clear()
    try:
        (tmp_path / "测").mkdir(exist_ok=True)
        # 1. 默认派生冷却
        persona_config_manager.set_meme_frequency("测", 20)  # 派生 120
        style = resolve_meme_style("测")
        assert style.cooldown_sec == 120

        # 2. 显式覆盖冷却
        persona_config_manager.set_meme_send_cooldown_sec("测", 45)
        style2 = resolve_meme_style("测")
        assert style2.cooldown_sec == 45

        # 3. 设回 0 恢复自适应
        persona_config_manager.set_meme_send_cooldown_sec("测", 0)
        style3 = resolve_meme_style("测")
        assert style3.cooldown_sec == 120
    finally:
        persona_config_manager._cache.clear()
        persona_config_manager._base_path = old_base
        settings_mod.persona_settings_manager._base_path = old_settings


def test_pick_reports_disabled_for_disabled_persona(tmp_path, monkeypatch) -> None:
    from gsuid_core.ai_core import resource as core_res
    from gsuid_core.ai_core.persona import settings as settings_mod
    from gsuid_core.ai_core.meme.selector import PICK_DISABLED, pick
    from gsuid_core.ai_core.persona.config import persona_config_manager

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    old_base = persona_config_manager._base_path
    old_settings = settings_mod.persona_settings_manager._base_path
    persona_config_manager._base_path = tmp_path
    settings_mod.persona_settings_manager._base_path = tmp_path
    persona_config_manager._cache.clear()
    try:
        (tmp_path / "测").mkdir(exist_ok=True)
        (tmp_path / "测" / "persona.md").write_text("# 测", encoding="utf-8")
        persona_config_manager.set_meme_frequency("测", 0)
        record, reason = asyncio.run(pick(mood="开心", scene="", persona="测", session_id="s_test"))
        assert record is None
        assert reason == PICK_DISABLED
    finally:
        persona_config_manager._cache.clear()
        persona_config_manager._base_path = old_base
        settings_mod.persona_settings_manager._base_path = old_settings


def test_update_persona_settings_rejects_out_of_range(tmp_path, monkeypatch) -> None:
    from gsuid_core.ai_core import resource as core_res
    from gsuid_core.ai_core.persona import settings as settings_mod
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.webconsole.persona_api import update_persona_settings

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr("gsuid_core.ai_core.persona.persona.PERSONA_PATH", tmp_path)
    monkeypatch.setattr("gsuid_core.ai_core.persona.store.is_git_managed", lambda _name: False)
    old_base = persona_config_manager._base_path
    old_settings = settings_mod.persona_settings_manager._base_path
    persona_config_manager._base_path = tmp_path
    settings_mod.persona_settings_manager._base_path = tmp_path
    persona_config_manager._cache.clear()
    settings_mod.persona_settings_manager._cache.clear()
    try:
        (tmp_path / "测").mkdir(exist_ok=True)
        (tmp_path / "测" / "persona.md").write_text("# 测", encoding="utf-8")
        # 1. 超界频率被拦截且不落盘
        res = asyncio.run(update_persona_settings("测", {"meme_frequency": 150}))
        assert res["status"] == 1
        cfg = settings_mod.persona_settings_manager.get_config("测")
        assert cfg.get_config("meme_frequency").data == 50

        # 2. 超界冷却被拦截
        res_cd = asyncio.run(update_persona_settings("测", {"meme_send_cooldown_sec": -5}))
        assert res_cd["status"] == 1

        # 3. 合法值成功同步双端
        res_ok = asyncio.run(update_persona_settings("测", {"meme_frequency": 75, "meme_send_cooldown_sec": 45}))
        assert res_ok["status"] == 0
        assert cfg.get_config("meme_frequency").data == 75
        assert cfg.get_config("meme_send_cooldown_sec").data == 45
        p_cfg = persona_config_manager.get_config("测")
        assert p_cfg.get_config("meme_frequency").data == 75
        assert p_cfg.get_config("meme_send_cooldown_sec").data == 45
    finally:
        persona_config_manager._cache.clear()
        settings_mod.persona_settings_manager._cache.clear()
        persona_config_manager._base_path = old_base
        settings_mod.persona_settings_manager._base_path = old_settings


def test_update_persona_config_mirrors_meme_keys(tmp_path, monkeypatch) -> None:
    """config 端点写 config.json 时同步镜像 persona.json，两文件保持一致。"""
    from gsuid_core.ai_core import resource as core_res
    from gsuid_core.ai_core.persona import settings as settings_mod
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.webconsole.persona_api import update_persona_config

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr("gsuid_core.ai_core.persona.persona.PERSONA_PATH", tmp_path)
    monkeypatch.setattr("gsuid_core.ai_core.persona.store.is_git_managed", lambda _name: False)
    old_base = persona_config_manager._base_path
    old_settings = settings_mod.persona_settings_manager._base_path
    persona_config_manager._base_path = tmp_path
    settings_mod.persona_settings_manager._base_path = tmp_path
    persona_config_manager._cache.clear()
    settings_mod.persona_settings_manager._cache.clear()
    try:
        (tmp_path / "测").mkdir(exist_ok=True)
        (tmp_path / "测" / "persona.md").write_text("# 测", encoding="utf-8")
        res = asyncio.run(
            update_persona_config(
                "测",
                {"allow_meme_reply": False, "meme_frequency": 80, "meme_send_cooldown_sec": 45},
            )
        )
        assert res["status"] == 0
        p_cfg = persona_config_manager.get_config("测")
        s_cfg = settings_mod.persona_settings_manager.get_config("测")
        assert p_cfg.get_config("allow_meme_reply").data is False
        assert s_cfg.get_config("allow_meme_reply").data is False
        assert s_cfg.get_config("meme_frequency").data == 80
        assert s_cfg.get_config("meme_send_cooldown_sec").data == 45
    finally:
        persona_config_manager._cache.clear()
        settings_mod.persona_settings_manager._cache.clear()
        persona_config_manager._base_path = old_base
        settings_mod.persona_settings_manager._base_path = old_settings
