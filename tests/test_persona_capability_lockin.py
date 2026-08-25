"""AGENTS.md §1.9：框架运行时不得人格锁定 / 能力锁定。"""

from pathlib import Path

from gsuid_core.ai_core.output_firewall import MACHINE_FALLBACK_TEXT, PERSONA_FALLBACK_TEXT
from gsuid_core.ai_core.classifier.mode_classifier import KNOWLEDGE_NOUNS, FUNCTIONAL_NOUNS

_CORE = Path(__file__).resolve().parent.parent / "gsuid_core"
_AI_CORE = _CORE / "ai_core"

# 默认人格口癖 / 自称：禁止出现在框架用户可见兜底与闸门
_PERSONA_TICKS = ("唔…", "…呼。", "zzz…", "本貉", "卷轴里")
# 业务插件专属域词：禁止进框架意图词表
_PLUGIN_DOMAIN_NOUNS = frozenset(
    {
        "圣遗物",
        "声骸",
        "练度",
        "命座",
        "元素精通",
        "单手剑",
        "法器",
        "大招",
        "战技",
        "配队",
        "模拟盘",
        "研报",
    }
)


def test_fallback_texts_are_persona_neutral() -> None:
    for s in (PERSONA_FALLBACK_TEXT, MACHINE_FALLBACK_TEXT):
        for tick in _PERSONA_TICKS:
            assert tick not in s, f"{s!r} contains persona tick {tick!r}"
    assert "早柚" not in PERSONA_FALLBACK_TEXT
    assert PERSONA_FALLBACK_TEXT.strip()


def test_classifier_nouns_exclude_plugin_domains() -> None:
    leaked = (_PLUGIN_DOMAIN_NOUNS & FUNCTIONAL_NOUNS) | (_PLUGIN_DOMAIN_NOUNS & KNOWLEDGE_NOUNS)
    assert not leaked, f"intent lexicon contains plugin-domain nouns: {sorted(leaked)}"
    assert "Q" not in KNOWLEDGE_NOUNS
    assert "E" not in KNOWLEDGE_NOUNS


def test_runtime_sources_have_no_sayu_ticks() -> None:
    """用户可见兜底路径不得抄默认人格口头禅。"""
    files = (
        _AI_CORE / "output_firewall.py",
        _AI_CORE / "utils.py",
        _AI_CORE / "planning" / "kanban_executor.py",
        _AI_CORE / "agent_run" / "speech_policy.py",
        _AI_CORE / "kits" / "scaffold" / "kit.py",
    )
    for path in files:
        src = path.read_text(encoding="utf-8")
        assert 'endswith(("zzz"' not in src
        assert 'endswith(("zzz", "呼", "唔"' not in src
        for tick in ("唔…搞定了", "唔…这个不太想说", "唔…脑子转不动", "卷轴里有"):
            assert tick not in src, f"{path.name} still has {tick!r}"


def test_scaffold_reads_tone_markers_from_persona() -> None:
    src = (_AI_CORE / "kits" / "scaffold" / "kit.py").read_text(encoding="utf-8")
    assert "get_tone_markers" in src
    assert "reply_ends_with_tone_marker" in src
    assert 'endswith(("zzz"' not in src
    assert '"呼"' not in src and '"唔"' not in src


def test_ai_config_persona_options_not_hardcoded() -> None:
    src = (_AI_CORE / "configs" / "ai_config.py").read_text(encoding="utf-8")
    assert 'options=["早柚"]' not in src


def test_tone_markers_come_from_persona_card() -> None:
    from gsuid_core.ai_core.persona.resource import extract_tone_markers, reply_ends_with_tone_marker

    card = "Tone Markers (语气词):\n        啧、哈、呵\n        配额：每 3-5 条至多 1 条带语气词结尾；其余条不带。\n"
    markers = extract_tone_markers(card)
    assert markers == ("啧", "哈", "呵")
    assert "唔" not in markers and "zzz" not in markers
    assert reply_ends_with_tone_marker("行吧啧", markers)
    assert not reply_ends_with_tone_marker("行吧唔…呼zzz", markers)
    assert extract_tone_markers("Style (风格):\n        短句。\n") == ()
