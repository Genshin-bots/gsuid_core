"""复制人格：新名称追加数字，整目录复制，scope 强制 disabled。"""

from gsuid_core.ai_core.persona.resource import copy_persona, allocate_copy_name


def test_allocate_copy_name_appends_incrementing_number() -> None:
    assert allocate_copy_name("早柚", set()) == "早柚2"
    assert allocate_copy_name("早柚", {"早柚2"}) == "早柚3"
    assert allocate_copy_name("早柚", {"早柚2", "早柚3"}) == "早柚4"
    assert allocate_copy_name("早柚2", {"早柚2", "早柚22"}) == "早柚23"


def test_allocate_copy_name_exhausted() -> None:
    occupied = {f"角色{n}" for n in range(2, 1000)}
    assert allocate_copy_name("角色", occupied) is None


def test_copy_persona_creates_numbered_dir(tmp_path, monkeypatch) -> None:
    from gsuid_core.ai_core import resource as core_res
    from gsuid_core.ai_core.persona import persona as persona_mod
    from gsuid_core.ai_core.persona.config import persona_config_manager

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr(persona_mod, "PERSONA_PATH", tmp_path)
    persona_config_manager._base_path = tmp_path
    persona_config_manager._cache.clear()

    src = tmp_path / "早柚"
    src.mkdir()
    (src / "persona.md").write_text("# 早柚\nhello", encoding="utf-8")
    (src / "avatar.png").write_bytes(b"png")

    dest = copy_persona("早柚")
    assert dest == "早柚2"
    copied = tmp_path / "早柚2"
    assert copied.is_dir()
    assert (copied / "persona.md").read_text(encoding="utf-8") == "# 早柚\nhello"
    assert (copied / "avatar.png").read_bytes() == b"png"
    assert persona_config_manager.get_config(dest).get_config("scope").data == "disabled"

    dest2 = copy_persona("早柚")
    assert dest2 == "早柚3"
    assert (tmp_path / "早柚3" / "persona.md").is_file()
