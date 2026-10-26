"""插件商店列表 WebAPI：服务端分页 / 页签筛选 / 搜索 + 页签计数（总数）。

页签徽标上的数字必须恒为**总数**，不能因为翻页或搜索而变——徽标是「全店概览」，
不是「当前页有几个」。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from gsuid_core.webconsole import plugins_api
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

DEPRECATED_TEXT = "停止维护"


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    """替代 httpx.AsyncClient：直接返回构造好的远程插件表。"""

    payload: Dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def get(self, url: str, timeout: int = 0) -> _FakeResponse:
        return _FakeResponse(self.payload)


def _make_remote(count: int = 40) -> Dict[str, Any]:
    plugins: Dict[str, Any] = {}
    for i in range(count):
        plugin_id = f"Plugin{i:02d}"
        plugins[plugin_id] = {
            "info": f"插件 {i:02d} 的描述",
            "alias": [f"tag{i % 3}"],
            "type": "tip",
            "content": "普通",
            "link": "https://github.com/owner/repo",
        }
    # 一个停止维护的：应排在最后
    plugins["OldPlugin"] = {
        "info": "停止维护的插件",
        "alias": ["old"],
        "type": "danger",
        "content": DEPRECATED_TEXT,
        "link": "https://github.com/owner/old",
    }
    return {
        "plugins": plugins,
        "fun_plugins": ["Plugin00", "Plugin01"],
        "tool_plugins": ["Plugin02"],
    }


@pytest.fixture
def store_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _FakeAsyncClient.payload = _make_remote()
    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)

    async def _fake_installed() -> Dict[str, Any]:
        # 本地已安装：Plugin00（同时是 fun）
        return {"plugin00": {"name": "Plugin00"}}

    monkeypatch.setattr(plugins_api, "get_local_plugins_list", _fake_installed)
    app.dependency_overrides[require_auth] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_auth, None)


def _get(client: TestClient, **params: str | int | bool) -> dict:
    resp = client.get("/api/plugin-store/list", params=params)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 0
    return body["data"]


def test_store_legacy_contract_returns_everything(store_client: TestClient):
    """不传 limit → 全量（旧契约）。"""
    data = _get(store_client)
    assert data["limit"] is None
    assert data["has_more"] is False
    # 40 个 + 1 个停止维护
    assert len(data["plugins"]) == 41
    assert data["count"] == data["total_count"] == 41
    assert isinstance(data["fun_plugins"], list)
    assert isinstance(data["tool_plugins"], list)


def test_store_pagination_slices_the_list(store_client: TestClient):
    first = _get(store_client, limit=32, offset=0)
    second = _get(store_client, limit=32, offset=32)

    assert len(first["plugins"]) == 32
    assert len(second["plugins"]) == 9
    assert first["count"] == second["count"] == 41
    assert first["has_more"] is True
    assert second["has_more"] is False

    ids = [p["id"] for p in first["plugins"]] + [p["id"] for p in second["plugins"]]
    assert len(set(ids)) == 41


def test_store_deprecated_plugins_sort_last(store_client: TestClient):
    """停止维护的插件排最后——分页前排，否则会夹在正常插件中间。"""
    data = _get(store_client, limit=32, offset=0)
    assert data["plugins"][-1]["id"] != "OldPlugin"
    last_page = _get(store_client, limit=32, offset=32)
    assert last_page["plugins"][-1]["id"] == "OldPlugin"


def test_store_tab_filter_applies_before_paging(store_client: TestClient):
    """先筛后切：页签过滤后分页，第二页不能为空。"""
    page1 = _get(store_client, tab="fun", limit=1, offset=0)
    page2 = _get(store_client, tab="fun", limit=1, offset=1)

    assert [p["id"] for p in page1["plugins"]] == ["Plugin00"]
    assert [p["id"] for p in page2["plugins"]] == ["Plugin01"]
    assert page1["count"] == page2["count"] == 2


def test_store_installed_tab_uses_installed_flag(store_client: TestClient):
    data = _get(store_client, tab="installed")
    assert [p["id"] for p in data["plugins"]] == ["Plugin00"]
    assert data["count"] == 1


def test_store_search_filters_before_paging(store_client: TestClient):
    data = _get(store_client, search="tag1")
    # tag1 的插件：Plugin01/04/07/.../Plugin37
    assert data["count"] == len(data["plugins"]) == 13
    assert all("tag1" in p["tags"] for p in data["plugins"])


def test_store_counts_are_totals_regardless_of_paging(store_client: TestClient):
    """页签徽标的数字恒为总数：不随翻页、页签、搜索变化。"""
    page1 = _get(store_client, limit=32, offset=0)
    last = _get(store_client, limit=32, offset=32)
    fun = _get(store_client, tab="fun")
    searched = _get(store_client, search="tag1")

    for data in (page1, last, fun, searched):
        assert data["counts"]["all"] == 41
        assert data["counts"]["installed"] == 1
        assert data["counts"]["fun"] == 2
        assert data["counts"]["tool"] == 1


def test_store_offset_beyond_end_returns_empty_page(store_client: TestClient):
    data = _get(store_client, limit=10, offset=999)
    assert data["plugins"] == []
    assert data["count"] == 41
    assert data["has_more"] is False
