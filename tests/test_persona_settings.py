"""persona.json：称呼与用户可见短句。"""

from gsuid_core.ai_core.utils import sanitize_error_for_user
from gsuid_core.ai_core.persona.prompts import (
    SYSTEM_CONSTRAINTS,
    TOOL_ORCHESTRATION_CONSTRAINTS,
    sayu_persona_prompt,
)
from gsuid_core.ai_core.persona.resource import extract_tone_markers
from gsuid_core.ai_core.persona.settings import (
    DEFAULT_FALLBACK_OOC,
    DEFAULT_MASTER_TITLE,
    DEFAULT_ERROR_GENERIC,
    DEFAULT_FALLBACK_MACHINE,
    default_phrase,
    get_master_title,
    get_persona_setting,
)
from gsuid_core.ai_core.persona.processor import build_persona_prompt


def test_defaults_are_persona_neutral() -> None:
    for s in (
        DEFAULT_MASTER_TITLE,
        DEFAULT_ERROR_GENERIC,
        DEFAULT_FALLBACK_OOC,
        DEFAULT_FALLBACK_MACHINE,
        default_phrase("error_timeout"),
        default_phrase("error_content_policy"),
    ):
        assert s.strip()
        assert "早柚" not in s
        assert "唔…" not in s
        assert "本貉" not in s
    assert default_phrase("task_ack") == ""
    from gsuid_core.ai_core.agent_run.loop import task_ack_phrase

    assert task_ack_phrase(None) == "收到。"
    assert "早柚" not in task_ack_phrase(None)
    assert "唔…" not in task_ack_phrase(None)


def test_task_ack_uses_persona_tone_markers(tmp_path, monkeypatch) -> None:
    import gsuid_core.ai_core.resource as core_res
    from gsuid_core.ai_core.persona import resource as pres, settings as settings_mod
    from gsuid_core.ai_core.agent_run.loop import task_ack_phrase

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr(settings_mod, "PERSONA_PATH", tmp_path)
    settings_mod.persona_settings_manager._base_path = tmp_path
    settings_mod.persona_settings_manager._cache.clear()
    pres._tone_marker_cache.clear()
    name = "设定测试丙"
    (tmp_path / name).mkdir()
    (tmp_path / name / "persona.md").write_text(
        "Tone Markers (语气词):\n        唔…\n        呼\n",
        encoding="utf-8",
    )
    phrase = task_ack_phrase(name)
    assert phrase == "唔…好。"
    assert "收到" not in phrase
    cfg = settings_mod.persona_settings_manager.get_config(name)
    assert cfg.set_config("task_ack", "行，去翻。")
    settings_mod.persona_settings_manager._cache.clear()
    assert task_ack_phrase(name) == "行，去翻。"


def test_get_persona_setting_without_persona_uses_template() -> None:
    assert get_master_title(None) == DEFAULT_MASTER_TITLE
    assert get_persona_setting(None, "error_generic") == DEFAULT_ERROR_GENERIC
    assert get_persona_setting("", "fallback_ooc") == DEFAULT_FALLBACK_OOC
    assert get_persona_setting("不存在的人格xyz", "master_title") == DEFAULT_MASTER_TITLE


def test_settings_roundtrip(tmp_path, monkeypatch) -> None:
    from gsuid_core.ai_core.persona import settings as settings_mod

    monkeypatch.setattr(settings_mod, "PERSONA_PATH", tmp_path)
    settings_mod.persona_settings_manager._base_path = tmp_path
    settings_mod.persona_settings_manager._cache.clear()
    name = "设定测试甲"
    (tmp_path / name).mkdir()
    cfg = settings_mod.persona_settings_manager.get_config(name)
    assert cfg.set_config("master_title", "老师")
    assert cfg.set_config("error_generic", "这条先放一放，回头再试。")
    settings_mod.persona_settings_manager._cache.clear()
    assert settings_mod.get_persona_setting(name, "master_title") == "老师"
    assert settings_mod.get_persona_setting(name, "error_generic") == "这条先放一放，回头再试。"
    assert settings_mod.get_persona_setting(name, "error_timeout") == settings_mod.DEFAULT_ERROR_TIMEOUT


def test_empty_value_falls_back_to_default(tmp_path, monkeypatch) -> None:
    from gsuid_core.ai_core.persona import settings as settings_mod

    monkeypatch.setattr(settings_mod, "PERSONA_PATH", tmp_path)
    settings_mod.persona_settings_manager._base_path = tmp_path
    settings_mod.persona_settings_manager._cache.clear()
    name = "设定测试乙"
    (tmp_path / name).mkdir()
    cfg = settings_mod.persona_settings_manager.get_config(name)
    assert cfg.set_config("master_title", "   ")
    settings_mod.persona_settings_manager._cache.clear()
    assert settings_mod.get_persona_setting(name, "master_title") == DEFAULT_MASTER_TITLE


def test_sanitize_error_reads_persona_setting(tmp_path, monkeypatch) -> None:
    from gsuid_core.ai_core.persona import settings as settings_mod

    monkeypatch.setattr(settings_mod, "PERSONA_PATH", tmp_path)
    settings_mod.persona_settings_manager._base_path = tmp_path
    settings_mod.persona_settings_manager._cache.clear()
    name = "设定测试丙"
    (tmp_path / name).mkdir()
    cfg = settings_mod.persona_settings_manager.get_config(name)
    assert cfg.set_config("error_generic", "先搁着吧。")
    assert cfg.set_config("error_timeout", "网太慢了。")
    settings_mod.persona_settings_manager._cache.clear()
    assert sanitize_error_for_user("执行出错: boom", name) == "先搁着吧。"
    assert sanitize_error_for_user("执行出错: 请求超时", name) == "网太慢了。"
    assert sanitize_error_for_user("执行出错: boom") == DEFAULT_ERROR_GENERIC


def test_system_constraints_keeps_placeholders() -> None:
    assert "__MASTER_TITLE__" in SYSTEM_CONSTRAINTS
    assert "__MASTERS__" in SYSTEM_CONSTRAINTS


def test_shared_system_prompt_is_compact() -> None:
    assert len(SYSTEM_CONSTRAINTS) <= 1600
    assert len(TOOL_ORCHESTRATION_CONSTRAINTS) <= 700
    assert len(sayu_persona_prompt.strip()) <= 2300
    src = build_persona_prompt.__code__.co_names
    assert "format_capability_family_overview" not in src
    markers = extract_tone_markers(sayu_persona_prompt)
    assert "唔" in markers and "zzz" in markers


def test_sanitized_error_texts_survive_persona_cleanup() -> None:
    from gsuid_core.ai_core.utils import NO_RESULT_TEXT, ERROR_RESULT_PREFIX, _strip_persona_markdown

    samples = [
        f"{ERROR_RESULT_PREFIX}: 内容被模型安全策略拒绝",
        f"{ERROR_RESULT_PREFIX}: 请求超时",
        f"{ERROR_RESULT_PREFIX}: status_code: 400, body: {{'x': 1}}",
        NO_RESULT_TEXT,
    ]
    for raw in samples:
        cleaned = _strip_persona_markdown(sanitize_error_for_user(raw)).strip()
        assert cleaned, raw
        assert "status_code" not in cleaned
    note = _strip_persona_markdown("BTC 跌破 60000，快跑\n⏰ 定时任务 sched_ab12")
    assert "sched_ab12" in note


def test_render_system_constraints_fills_title(monkeypatch) -> None:
    from gsuid_core.ai_core.persona import processor

    monkeypatch.setattr(processor, "get_master_title", lambda _n: "阁下")
    text = processor._render_system_constraints("任意")
    assert "阁下" in text
    assert "__MASTER_TITLE__" not in text
