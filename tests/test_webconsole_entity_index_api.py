"""实体身份索引 WebAPI：服务端分页 / 冲突筛选 / 搜索，以及旧契约（不传 limit 返回全量）。

评测脚本 ``eval/tool_selection/run.py`` 依赖「不传参数 → 拿到整个索引」的旧契约，
这里的用例把该契约钉死，防止后来者给 ``limit`` 设默认值把评测打空。
"""

from __future__ import annotations

from typing import Dict, Iterator, TypedDict

import pytest
from fastapi.testclient import TestClient

from gsuid_core.ai_core import entity_index as entity_index_mod
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole._local_test_gate import require_auth_or_local_test


class EntityEntry(TypedDict):
    """实体索引单条记录。"""

    surface: str
    plugins: list[str]
    ambiguous: bool


class EntityPage(TypedDict):
    """实体索引分页响应体。"""

    items: list[EntityEntry]
    entries: list[EntityEntry]
    count: int
    total: int
    total_all: int
    limit: int | None
    offset: int
    has_more: bool


@pytest.fixture
def client() -> Iterator[TestClient]:
    """注册路由 + 跳过鉴权。直接用 TestClient（不进 `with`）不会跑 lifespan，不起调度器。"""
    from gsuid_core.webconsole import ai_tools_api  # noqa: F401  导入即注册路由

    app.dependency_overrides[require_auth_or_local_test] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_auth_or_local_test, None)


@pytest.fixture
def seeded_index() -> Iterator[None]:
    """塞一份固定的索引：3 个普通 surface + 1 个多插件冲突 surface。"""
    saved: Dict[str, entity_index_mod.EntityRef] = dict(entity_index_mod.get_entity_index())
    entity_index_mod.clear_entity_index()
    entity_index_mod.register_entity_surface("刻晴", "刻晴", "GenshinUID")
    entity_index_mod.register_entity_surface("刻晴", "刻晴", "OtherPlugin")
    entity_index_mod.register_entity_surface("秧秧", "秧秧", "WutheringWaves")
    entity_index_mod.register_entity_surface("玄翎秧秧", "玄翎秧秧", "WutheringWaves")
    try:
        yield
    finally:
        entity_index_mod.clear_entity_index()
        entity_index_mod.get_entity_index().update(saved)


def _get(client: TestClient, **params: str | int | bool) -> EntityPage:
    resp = client.get("/api/ai/entity_index", params=params)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 0
    return body["data"]


def test_legacy_contract_returns_whole_index(client: TestClient, seeded_index: None):
    """不传 limit → 全量 + entries 字段（评测脚本读的就是它）。"""
    data = _get(client)
    assert data["limit"] is None
    assert data["offset"] == 0
    assert data["has_more"] is False
    # 4 次注册 → 3 个 surface（「刻晴」被两个插件注册，只占一条）
    assert data["count"] == data["total"] == 3
    assert len(data["entries"]) == 3
    assert data["entries"] == data["items"]


def test_pagination_slices_without_overlap(client: TestClient, seeded_index: None):
    first = _get(client, limit=2, offset=0)
    second = _get(client, limit=2, offset=2)

    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert first["total"] == second["total"] == 3
    assert first["has_more"] is True
    assert second["has_more"] is False

    surfaces = [e["surface"] for e in first["items"]] + [e["surface"] for e in second["items"]]
    assert len(set(surfaces)) == 3
    assert set(surfaces) == {"刻晴", "秧秧", "玄翎秧秧"}


def test_pages_are_sorted_and_stable(client: TestClient, seeded_index: None):
    """注册顺序随插件加载顺序变，分页必须按稳定序切，翻页才不漏条目。"""
    page = _get(client, limit=50, offset=0)
    surfaces = [e["surface"] for e in page["items"]]
    assert surfaces == sorted(surfaces)

    repeat = _get(client, limit=50, offset=0)
    assert [e["surface"] for e in repeat["items"]] == surfaces


def test_ambiguous_only_filters_conflicts(client: TestClient, seeded_index: None):
    data = _get(client, ambiguous_only=True)
    assert data["count"] == data["total"] == 1
    assert data["total_all"] == 3
    assert [e["surface"] for e in data["items"]] == ["刻晴"]
    assert data["items"][0]["ambiguous"] is True
    assert data["items"][0]["plugins"] == ["GenshinUID", "OtherPlugin"]


def test_conflict_filter_keeps_page_size(client: TestClient, seeded_index: None):
    """冲突项多于一页时也要照常分页。"""
    data = _get(client, ambiguous_only=True, limit=1, offset=0)
    assert len(data["items"]) == 1
    assert data["total"] == 1
    assert data["has_more"] is False


def test_search_matches_surface_and_plugin(client: TestClient, seeded_index: None):
    by_surface = _get(client, search="秧")
    assert {e["surface"] for e in by_surface["items"]} == {"秧秧", "玄翎秧秧"}

    by_plugin = _get(client, search="otherplugin")
    assert {e["surface"] for e in by_plugin["items"]} == {"刻晴"}
    assert by_plugin["total_all"] == 3


def test_search_and_conflict_filter_combine(client: TestClient, seeded_index: None):
    data = _get(client, search="刻晴", ambiguous_only=True)
    assert [e["surface"] for e in data["items"]] == ["刻晴"]
    assert data["total"] == 1


def test_offset_beyond_end_returns_empty_page(client: TestClient, seeded_index: None):
    data = _get(client, limit=10, offset=99)
    assert data["items"] == []
    assert data["total"] == 3
    assert data["has_more"] is False
