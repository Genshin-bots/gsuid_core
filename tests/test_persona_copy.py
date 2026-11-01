"""复制人格：新名称追加数字，整目录复制，scope 强制 disabled。"""

import asyncio

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
    monkeypatch.setattr(persona_config_manager, "_base_path", tmp_path)
    persona_config_manager._cache.clear()

    src = tmp_path / "早柚"
    src.mkdir()
    (src / "persona.md").write_text("# 早柚\nhello", encoding="utf-8")
    (src / "avatar.png").write_bytes(b"png")

    async def _noop(_name: str) -> None:
        return None

    bound_calls: list[tuple[str, str]] = []

    async def _pack_ids(persona_name: str) -> list[str]:
        return []

    async def _bind(persona_name: str, pack_id: str) -> bool:
        bound_calls.append((persona_name, pack_id))
        return True

    monkeypatch.setattr("gsuid_core.ai_core.meme.pack_model.mount_default_pack", _noop)
    from gsuid_core.ai_core.meme.pack_model import PackStore

    monkeypatch.setattr(PackStore, "pack_ids_for_persona", staticmethod(_pack_ids))
    monkeypatch.setattr(PackStore, "add_bind_if_missing", staticmethod(_bind))

    dest = asyncio.run(copy_persona("早柚"))
    assert dest == "早柚2"
    copied = tmp_path / "早柚2"
    assert copied.is_dir()
    assert (copied / "persona.md").read_text(encoding="utf-8") == "# 早柚\nhello"
    assert (copied / "avatar.png").read_bytes() == b"png"
    assert persona_config_manager.get_config(dest).get_config("scope").data == "disabled"

    dest2 = asyncio.run(copy_persona("早柚"))
    assert dest2 == "早柚3"
    assert (tmp_path / "早柚3" / "persona.md").is_file()


def test_copy_persona_caps_binds_at_pack_limit(tmp_path, monkeypatch) -> None:
    """源人格挂载已满时，副本绑定数封顶且不再挂默认包。"""
    from gsuid_core.ai_core import resource as core_res
    from gsuid_core.ai_core.persona import persona as persona_mod
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.meme.pack_model import MAX_PACKS_PER_PERSONA, PackStore

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr(persona_mod, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr(persona_config_manager, "_base_path", tmp_path)
    persona_config_manager._cache.clear()

    src = tmp_path / "sayu"
    src.mkdir()
    (src / "persona.md").write_text("# sayu", encoding="utf-8")

    bound: list[str] = []
    mount_calls: list[str] = []

    async def _pack_ids(_persona_name: str) -> list[str]:
        return [f"p{i}" for i in range(MAX_PACKS_PER_PERSONA + 2)]

    async def _bind(_persona_name: str, pack_id: str) -> bool:
        bound.append(pack_id)
        return True

    async def _mount(persona_name: str) -> None:
        mount_calls.append(persona_name)

    monkeypatch.setattr(PackStore, "pack_ids_for_persona", staticmethod(_pack_ids))
    monkeypatch.setattr(PackStore, "add_bind_if_missing", staticmethod(_bind))
    monkeypatch.setattr("gsuid_core.ai_core.meme.pack_model.mount_default_pack", _mount)

    dest = asyncio.run(copy_persona("sayu"))
    assert dest == "sayu2"
    assert len(bound) == MAX_PACKS_PER_PERSONA
    assert not mount_calls
