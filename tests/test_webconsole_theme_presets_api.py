"""主题预设列表 WebAPI：分页 + 全部预设名。

「同名已存在」判定要用**全部**预设名，分页后当前页只是一部分——所以 `names`
必须始终给全，否则前端会把重名预设当成新名字保存。
"""

from __future__ import annotations

from typing import Any, Dict, List, Iterator

import pytest
from fastapi.testclient import TestClient

from gsuid_core.webconsole import theme_api
from gsuid_core.webconsole.app_app import app


@pytest.fixture
def presets_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """固定 5 个预设（名字有序），绕开真实文件系统。"""
    presets: List[Dict[str, Any]] = [
        {
            "name": f"preset_{i:02d}",
            "filename": f"preset_{i:02d}.json",
            "size_bytes": 128,
            "mtime": 1700000000,
            "is_active": False,
            "valid": True,
        }
        for i in range(5)
    ]
    monkeypatch.setattr(theme_api, "list_theme_presets", lambda: presets)
    yield TestClient(app)


def _get(client: TestClient, **params: str | int | bool) -> dict:
    resp = client.get("/api/theme/presets", params=params)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 0
    return body["data"]


def test_presets_legacy_contract_returns_everything(presets_client: TestClient):
    """不传 limit → 全量（旧契约）。"""
    data = _get(presets_client)
    assert data["limit"] is None
    assert len(data["presets"]) == 5
    assert data["count"] == data["total"] == 5
    assert data["has_more"] is False


def test_presets_pagination_slices_the_list(presets_client: TestClient):
    first = _get(presets_client, limit=2, offset=0)
    last = _get(presets_client, limit=2, offset=4)

    assert [p["name"] for p in first["presets"]] == ["preset_00", "preset_01"]
    assert [p["name"] for p in last["presets"]] == ["preset_04"]
    assert first["count"] == last["count"] == 5
    assert first["has_more"] is True
    assert last["has_more"] is False


def test_presets_offset_beyond_end_returns_empty_page(presets_client: TestClient):
    data = _get(presets_client, limit=10, offset=99)
    assert data["presets"] == []
    assert data["count"] == 5
    assert data["has_more"] is False


def test_presets_names_cover_every_preset_regardless_of_page(presets_client: TestClient):
    """分页后前端仍要能判断「同名已存在」——names 必须给全。"""
    data = _get(presets_client, limit=1, offset=0)
    assert [p["name"] for p in data["presets"]] == ["preset_00"]
    assert data["names"] == [f"preset_{i:02d}" for i in range(5)]


def test_presets_keep_the_directory_names(presets_client: TestClient):
    """目录名不能被分页参数改掉，也不应泄露绝对路径。"""
    data = _get(presets_client, limit=2)
    assert data["user_presets_path"] == theme_api.THEME_CONFIGS_PATH.name
    assert data["builtin_presets_path"] == theme_api.BUILTIN_THEMES_DIR_NAME
