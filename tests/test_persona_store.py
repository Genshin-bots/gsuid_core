"""人格市场：来源标记、整包根定位、只读门禁、更新同步、副本脱离。"""

import json
import asyncio
from typing import Any, Dict, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gsuid_core.ai_core.persona import store as persona_store
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.persona.bundle import BundleError


def _patch_persona_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core import resource as core_res
    from gsuid_core.ai_core.persona import persona as persona_mod

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr(persona_mod, "PERSONA_PATH", tmp_path)


def _make_persona(root: Path, name: str) -> Path:
    p = root / name
    p.mkdir(parents=True)
    (p / "persona.md").write_text(f"# {name}\n", encoding="utf-8")
    return p


def _make_source() -> persona_store.PersonaSource:
    return persona_store.PersonaSource(
        source="git",
        repo_url="https://github.com/x/persona_a",
        repo_name="persona_a",
        branch="main",
        bundle_dir="persona_a",
        commit="abc1234",
        installed_at=1.0,
        packs=["早柚"],
    )


def test_source_marker_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")
    assert persona_store.read_source("sayu") is None
    assert not persona_store.is_git_managed("sayu")

    persona_store.write_source("sayu", _make_source())
    src = persona_store.read_source("sayu")
    assert src is not None
    assert src["repo_name"] == "persona_a"
    assert src["packs"] == ["早柚"]
    assert persona_store.is_git_managed("sayu")

    # 非法 JSON / 缺字段标记一律视为非托管
    (tmp_path / "sayu" / persona_store.SOURCE_MARKER).write_text("{bad", encoding="utf-8")
    assert persona_store.read_source("sayu") is None


def test_drop_source_detaches_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")
    persona_store.write_source("sayu", _make_source())
    persona_store.drop_source("sayu")
    assert not persona_store.is_git_managed("sayu")
    persona_store.drop_source("sayu")  # 幂等


def test_find_bundle_root_root_and_subdir(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    assert persona_store.find_bundle_root(tmp_path) == tmp_path

    sub = tmp_path / "persona_x"
    sub.mkdir()
    (sub / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "manifest.json").unlink()
    assert persona_store.find_bundle_root(tmp_path) == sub

    sub.joinpath("manifest.json").unlink()
    with pytest.raises(BundleError):
        persona_store.find_bundle_root(tmp_path)


def test_managed_block_rejects_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")
    from gsuid_core.webconsole.persona_api import _managed_block

    assert _managed_block("sayu") is None
    persona_store.write_source("sayu", _make_source())
    blocked = _managed_block("sayu")
    assert blocked is not None and blocked["status"] == 1


def test_reconcile_files_removes_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    persona_dir = _make_persona(tmp_path, "sayu")
    for name in ("avatar.png", "image.png", "persona.json", "appearance.txt", "audio.ogg", "config.json"):
        (persona_dir / name).write_bytes(b"x")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "manifest.json").write_text("{}", encoding="utf-8")
    (repo / "avatar.png").write_bytes(b"a")  # 只有 avatar 还在仓库

    from gsuid_core.ai_core.persona.persona import Persona

    persona_store._reconcile_files(Persona("sayu"), repo)

    assert (persona_dir / "avatar.png").is_file()
    assert (persona_dir / "config.json").is_file()  # 运行时配置不动
    assert not (persona_dir / "image.png").exists()
    assert not (persona_dir / "persona.json").exists()
    assert not (persona_dir / "appearance.txt").exists()
    assert not (persona_dir / "audio.ogg").exists()


def test_copy_persona_drops_marker_and_binds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.persona.resource import copy_persona

    monkeypatch.setattr(persona_config_manager, "_base_path", tmp_path)
    persona_config_manager._cache.clear()

    _make_persona(tmp_path, "sayu")
    persona_store.write_source("sayu", _make_source())

    async def _noop(_name: str) -> None:
        return None

    bound_calls: list[tuple[str, str]] = []

    async def _pack_ids(persona_name: str) -> list[str]:
        return ["p1"] if persona_name == "sayu" else []

    async def _bind(persona_name: str, pack_id: str) -> bool:
        bound_calls.append((persona_name, pack_id))
        return True

    monkeypatch.setattr("gsuid_core.ai_core.meme.pack_model.mount_default_pack", _noop)
    from gsuid_core.ai_core.meme.pack_model import PackStore

    monkeypatch.setattr(PackStore, "pack_ids_for_persona", staticmethod(_pack_ids))
    monkeypatch.setattr(PackStore, "add_bind_if_missing", staticmethod(_bind))

    dest = asyncio.run(copy_persona("sayu"))
    assert dest == "sayu2"
    assert not persona_store.is_git_managed(dest)
    assert ("sayu2", "p1") in bound_calls
    # 源人格的标记不被副本影响
    assert persona_store.is_git_managed("sayu")


def test_update_rejects_persona_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    repo = tmp_path / "repos" / "persona_a"
    bundle = repo / "persona_a"
    bundle.mkdir(parents=True)
    (repo / ".git").mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps({"format": "gsuid-persona-bundle", "version": "1.0", "persona_name": "other"}),
        encoding="utf-8",
    )
    (bundle / "persona.md").write_text("# other", encoding="utf-8")

    _make_persona(tmp_path, "sayu")
    persona_store.write_source("sayu", _make_source())
    monkeypatch.setattr(persona_store, "PERSONA_REPO_PATH", tmp_path / "repos")

    async def _ok_pull(_path: Path) -> tuple[bool, str]:
        return True, "ok"

    monkeypatch.setattr(
        "gsuid_core.utils.plugins_update.git_async.git_pull",
        _ok_pull,
    )

    from gsuid_core.ai_core.persona.config import persona_config_manager

    monkeypatch.setattr(persona_config_manager, "_base_path", tmp_path)
    persona_config_manager._cache.clear()

    ok, msg = asyncio.run(persona_store.update_persona_from_git("sayu"))
    assert not ok
    assert "人格名" in msg


def test_update_rejects_non_dict_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    repo = tmp_path / "repos" / "persona_a"
    bundle = repo / "persona_a"
    bundle.mkdir(parents=True)
    (repo / ".git").mkdir()
    (bundle / "manifest.json").write_text("[]", encoding="utf-8")
    (bundle / "persona.md").write_text("# sayu", encoding="utf-8")

    _make_persona(tmp_path, "sayu")
    persona_store.write_source("sayu", _make_source())
    monkeypatch.setattr(persona_store, "PERSONA_REPO_PATH", tmp_path / "repos")

    async def _ok_pull(_path: Path) -> tuple[bool, str]:
        return True, "ok"

    monkeypatch.setattr("gsuid_core.utils.plugins_update.git_async.git_pull", _ok_pull)

    from gsuid_core.ai_core.persona.config import persona_config_manager

    monkeypatch.setattr(persona_config_manager, "_base_path", tmp_path)
    persona_config_manager._cache.clear()

    ok, msg = asyncio.run(persona_store.update_persona_from_git("sayu"))
    assert not ok
    assert "整包" in msg


def test_reconcile_packs_keeps_non_bundle_members(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同名用户包被整包接管后，同步只清整包来源的成员，用户自己加的图保留。"""
    _patch_persona_path(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    (repo / "packs" / "早柚").mkdir(parents=True)

    src = _make_source()
    src["packs"] = ["早柚"]

    from gsuid_core.ai_core.meme.pack_model import PackRef, PackStore, MemePackSummary

    async def _bound(_persona: str) -> list[str]:
        return ["p1"]

    async def _get_pack(pack_id: str) -> PackRef:
        return PackRef(pack_id=pack_id, name="早柚")

    async def _by_name(name: str) -> PackRef:
        return PackRef(pack_id="p1", name=name)

    async def _summaries() -> list[MemePackSummary]:
        return [MemePackSummary(pack_id="p1", name="早柚", member_count=4, tagged_count=0, bound_personas=["sayu"])]

    async def _members(_pack_id: str) -> list[str]:
        return ["repo_m1", "user_m1", "gone_m1", "bundle_m2"]

    removed: list[str] = []
    replaced: list[list[str]] = []

    async def _remove(_pack_id: str, meme_ids: list[str]) -> list[str]:
        removed.extend(meme_ids)
        return meme_ids

    async def _replace(_persona: str, pack_ids: list[str]) -> None:
        replaced.append(pack_ids)

    monkeypatch.setattr(PackStore, "pack_ids_for_persona", staticmethod(_bound))
    monkeypatch.setattr(PackStore, "get_pack", staticmethod(_get_pack))
    monkeypatch.setattr(PackStore, "get_by_name", staticmethod(_by_name))
    monkeypatch.setattr(PackStore, "list_summaries", staticmethod(_summaries))
    monkeypatch.setattr(PackStore, "member_meme_ids", staticmethod(_members))
    monkeypatch.setattr(PackStore, "remove_members", staticmethod(_remove))
    monkeypatch.setattr(PackStore, "replace_binds", staticmethod(_replace))
    monkeypatch.setattr(persona_store, "_expected_meme_ids", lambda _dir: {"repo_m1"})

    from gsuid_core.ai_core.meme.database_model import AiMemeRecord

    class _Rec:
        def __init__(self, source_group: str) -> None:
            self.source_group = source_group

    async def _record(meme_id: str) -> _Rec | None:
        if meme_id == "gone_m1":
            return None
        return _Rec("manual" if meme_id == "user_m1" else "persona_bundle")

    monkeypatch.setattr(AiMemeRecord, "get_by_meme_id", staticmethod(_record))

    asyncio.run(persona_store._reconcile_packs("sayu", src, repo))

    assert removed == ["bundle_m2", "gone_m1"]
    assert replaced == []


def test_installed_git_personas_without_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """persona.md 被手动删掉后，残留目录里的来源标记仍算托管。"""
    _patch_persona_path(tmp_path, monkeypatch)
    persona_dir = tmp_path / "sayu"
    persona_dir.mkdir()
    persona_store.write_source("sayu", _make_source())
    assert persona_store.is_git_managed("sayu")
    assert "sayu" in persona_store.installed_git_personas()


def test_snapshot_and_restore_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    from gsuid_core.ai_core.persona.config import persona_config_manager

    monkeypatch.setattr(persona_config_manager, "_base_path", tmp_path)
    persona_config_manager._cache.clear()
    _make_persona(tmp_path, "sayu")

    cfg = persona_config_manager.get_config("sayu")
    cfg.set_config("scope", "global")
    cfg.set_config("target_groups", ["123"])

    # 模拟仓库覆盖 config.json 后缓存重读
    persona_config_manager._cache.pop("sayu", None)
    cfg2 = persona_config_manager.get_config("sayu")
    assert cfg2.get_config("scope").data == "global"

    saved = persona_store._snapshot_overrides("sayu")
    cfg2.set_config("scope", "disabled")
    persona_store._restore_overrides("sayu", saved)
    cfg3 = persona_config_manager.get_config("sayu")
    assert cfg3.get_config("scope").data == "global"
    assert cfg3.get_config("target_groups").data == ["123"]


def _patch_clone(monkeypatch: pytest.MonkeyPatch, persona_name: str = "sayu") -> None:
    """让 git_clone 直接造一个整包仓库目录，并跳过镜像改写。"""

    monkeypatch.setattr(persona_store, "_clone_target", lambda url: (url, None))

    def _write_repo(dest: Path) -> None:
        dest.mkdir(parents=True)
        (dest / "manifest.json").write_text(
            json.dumps(
                {
                    "format": "gsuid-persona-bundle",
                    "version": "1.0",
                    "persona_name": persona_name,
                }
            ),
            encoding="utf-8",
        )
        (dest / "persona.md").write_text(f"# {persona_name}", encoding="utf-8")

    async def _clone(url: str, dest: Path, **_kwargs: Any) -> tuple[bool, str]:
        await asyncio.to_thread(_write_repo, dest)
        return True, "ok"

    monkeypatch.setattr("gsuid_core.utils.plugins_update.git_async.git_clone", _clone)


def test_install_refuses_existing_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")  # 本地手工人格，未托管
    monkeypatch.setattr(persona_store, "PERSONA_REPO_PATH", tmp_path / "repos")
    _patch_clone(monkeypatch)

    result, error = asyncio.run(persona_store.install_persona_from_git("https://github.com/x/persona_a"))
    assert result is None
    assert error is not None and "同名" in error
    assert not (tmp_path / "repos" / "persona_a").exists()


def test_install_refuses_other_repo_managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")
    src = _make_source()
    src["repo_name"] = "persona_b"
    src["repo_url"] = "https://github.com/x/persona_b"
    persona_store.write_source("sayu", src)
    monkeypatch.setattr(persona_store, "PERSONA_REPO_PATH", tmp_path / "repos")
    _patch_clone(monkeypatch)

    result, error = asyncio.run(persona_store.install_persona_from_git("https://github.com/x/persona_a"))
    assert result is None
    assert error is not None and "其他仓库" in error


def test_install_same_repo_reinstall_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")
    persona_store.write_source("sayu", _make_source())  # repo_name=persona_a
    monkeypatch.setattr(persona_store, "PERSONA_REPO_PATH", tmp_path / "repos")
    _patch_clone(monkeypatch)

    async def _pack_ids(persona_name: str) -> list[str]:
        return []

    from gsuid_core.ai_core.meme.pack_model import PackStore

    monkeypatch.setattr(PackStore, "pack_ids_for_persona", staticmethod(_pack_ids))

    result, error = asyncio.run(persona_store.install_persona_from_git("https://github.com/x/persona_a"))
    assert error is None
    assert result is not None and result["persona_name"] == "sayu" and result["created"] is False
    src = persona_store.read_source("sayu")
    assert src is not None and src["repo_name"] == "persona_a"


def test_is_repo_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")
    assert not persona_store.is_repo_pack("早柚")
    persona_store.write_source("sayu", _make_source())  # packs=["早柚"]
    assert persona_store.is_repo_pack("早柚")
    assert not persona_store.is_repo_pack("其他包")


def test_clone_target_ssh_mode_parses_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

    monkeypatch.setattr(core_plugins_config, "get_config", lambda _key: SimpleNamespace(data="ssh://"))

    url, fallback = persona_store._clone_target("git@github.com:o/r")
    assert url == "ssh://git@ssh.github.com:443/o/r.git"
    assert fallback is None

    url, _ = persona_store._clone_target("https://github.com/o/r")
    assert url == "ssh://git@ssh.github.com:443/o/r.git"

    url, _ = persona_store._clone_target("https://gitlab.com/o/r")
    assert url == "https://gitlab.com/o/r"


def test_check_all_updates_tolerates_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")
    persona_store.write_source("sayu", _make_source())

    async def _boom(_name: str) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(persona_store, "check_persona_update", _boom)
    assert asyncio.run(persona_store.check_all_updates()) == {"sayu": False}


# ─────────────────────────── 市场列表 API ───────────────────────────


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    payload: Dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def get(self, url: str, timeout: int = 0) -> _FakeResponse:
        return _FakeResponse(self.payload)


def _remote_personas(count: int = 8) -> Dict[str, Any]:
    personas: Dict[str, Any] = {}
    for i in range(count):
        pid = f"persona_{i:02d}"
        personas[pid] = {
            "link": f"https://github.com/owner/{pid}",
            "avatar": "https://github.com/owner.png",
            "cover": "",
            "branch": "main",
            "type": "tip",
            "content": "普通",
            "info": f"人格 {i:02d} 整包",
            "installMsg": "",
            "tags": ["表情包"] if i % 2 == 0 else ["配置"],
            "alias": [pid, f"别名{i}"],
        }
    return {"personas": personas, "extra": {}}


@pytest.fixture
def persona_store_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _FakeAsyncClient.payload = _remote_personas()
    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)

    installed = {
        "sayu": persona_store.PersonaSource(
            source="git",
            repo_url="https://github.com/owner/persona_00",
            repo_name="persona_00",
            branch="main",
            bundle_dir="",
            commit="abc1234",
            installed_at=1.0,
            packs=[],
        )
    }
    monkeypatch.setattr(persona_store, "installed_git_personas", lambda: installed)

    async def _no_updates() -> dict[str, bool]:
        return {}

    monkeypatch.setattr(persona_store, "check_all_updates", _no_updates)
    app.dependency_overrides[require_auth] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_auth, None)


def test_store_list_normalizes_and_marks_installed(persona_store_client: TestClient) -> None:
    resp = persona_store_client.get("/api/persona-store/list")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 0
    data = body["data"]
    assert data["total_count"] == 8
    assert data["counts"] == {"all": 8, "installed": 1, "updates": 0}
    first = next(p for p in data["personas"] if p["id"] == "persona_00")
    assert first["installed"] is True
    assert first["persona_name"] == "sayu"
    assert first["tags"] == ["表情包"]
    assert first["status"] == "installed"
    other = next(p for p in data["personas"] if p["id"] == "persona_01")
    assert other["installed"] is False
    assert other["status"] == "not_installed"


def test_store_list_tab_and_search(persona_store_client: TestClient) -> None:
    data = persona_store_client.get("/api/persona-store/list", params={"tab": "installed"}).json()["data"]
    assert data["count"] == 1
    assert data["personas"][0]["persona_name"] == "sayu"

    data = persona_store_client.get("/api/persona-store/list", params={"search": "别名3"}).json()["data"]
    assert data["count"] == 1
    assert data["personas"][0]["id"] == "persona_03"

    data = persona_store_client.get("/api/persona-store/list", params={"limit": 3, "offset": 3}).json()["data"]
    assert data["count"] == 8
    assert len(data["personas"]) == 3
    assert data["has_more"] is True


def test_store_list_malformed_remote(persona_store_client: TestClient) -> None:
    _FakeAsyncClient.payload = {"personas": "not-a-dict"}
    data = persona_store_client.get("/api/persona-store/list").json()["data"]
    assert data["total_count"] == 0
    assert data["personas"] == []

    _FakeAsyncClient.payload = {"personas": {"bad": "not-a-dict", "ok": {"link": "https://github.com/a/b"}}}
    data = persona_store_client.get("/api/persona-store/list").json()["data"]
    assert data["total_count"] == 1
    assert data["personas"][0]["id"] == "ok"
