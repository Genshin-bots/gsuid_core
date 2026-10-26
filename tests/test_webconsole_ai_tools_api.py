"""工具列表 / 技能列表 WebAPI：服务端分页 + 分面计数（总数，不随分页变）。

分面计数是这套接口的关键约定：下拉里的「某插件有多少工具」必须恒为**总数**，
不能因为翻页而变成当前页的条数。
"""

from __future__ import annotations

from typing import Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from gsuid_core.webconsole import ai_tools_api, ai_skills_api
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


class _StubTool:
    """工具列表端点只读 name / description / plugin 三个字段。"""

    def __init__(self, name: str, description: str, plugin: str) -> None:
        self.name = name
        self.description = description
        self.plugin = plugin


class _StubSkill:
    """技能列表端点用到的字段。"""

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description
        self.content = f"# {name}"
        self.license = None
        self.compatibility = None
        self.uri = ""
        self.metadata: Dict[str, str] = {}


@pytest.fixture
def tools_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """固定一份工具注册表：5 个工具，跨 3 个分类 / 3 个插件，含空描述与 meta 分类。"""
    registry: Dict[str, Dict[str, _StubTool]] = {
        "self": {
            "alpha": _StubTool("alpha", "第一个工具", "core"),
            "beta": _StubTool("beta", "第二个工具", "PluginA"),
        },
        "default": {
            "gamma": _StubTool("gamma", "", "PluginA"),
            "delta": _StubTool("delta", "普通描述", "PluginB"),
        },
        "meta": {
            "epsilon": _StubTool("epsilon", "meta 分类工具", "PluginB"),
        },
    }
    monkeypatch.setattr(ai_tools_api, "get_registered_tools", lambda: registry)
    app.dependency_overrides[require_auth] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_auth, None)


@pytest.fixture
def skills_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """固定 3 个技能。"""
    fake_skills = {
        "skill-a": _StubSkill("skill-a", "甲"),
        "skill-b": _StubSkill("skill-b", "乙"),
        "skill-c": _StubSkill("skill-c", "丙"),
    }
    monkeypatch.setattr(ai_skills_api, "skills", fake_skills)
    monkeypatch.setattr(ai_skills_api, "get_skill_source", lambda name: ("data", None))
    app.dependency_overrides[require_auth] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_auth, None)


def _get_tools(client: TestClient, **params: str | int | bool) -> dict:
    resp = client.get("/api/ai/tools/list", params=params)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 0
    return body["data"]


def _get_skills(client: TestClient, **params: str | int | bool) -> dict:
    resp = client.get("/api/ai/skills/list", params=params)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 0
    return body["data"]


# ── 工具列表 ────────────────────────────────────────────────────────────


def test_tools_legacy_contract_returns_everything(tools_client: TestClient):
    """不传 limit → 全量（旧契约）。"""
    data = _get_tools(tools_client)
    assert data["limit"] is None
    assert len(data["tools"]) == 5
    assert data["count"] == 5
    assert data["total_count"] == 5
    assert data["has_more"] is False


def test_tools_pagination_slices_the_sorted_set(tools_client: TestClient):
    first = _get_tools(tools_client, limit=2, offset=0)
    second = _get_tools(tools_client, limit=2, offset=2)
    third = _get_tools(tools_client, limit=2, offset=4)

    assert [t["name"] for t in first["tools"]] == ["alpha", "beta"]
    assert [t["name"] for t in second["tools"]] == ["delta", "epsilon"]
    assert [t["name"] for t in third["tools"]] == ["gamma"]
    assert first["count"] == second["count"] == 5
    assert first["has_more"] is True
    assert third["has_more"] is False


def test_tools_filter_composes_with_pagination(tools_client: TestClient):
    """先筛后切：分类过滤后再分页，第二页不能为空。"""
    page1 = _get_tools(tools_client, category="default", limit=1, offset=0)
    page2 = _get_tools(tools_client, category="default", limit=1, offset=1)

    assert [t["name"] for t in page1["tools"]] == ["delta"]
    assert [t["name"] for t in page2["tools"]] == ["gamma"]
    assert page1["count"] == page2["count"] == 2


def test_tools_search_matches_name_and_description(tools_client: TestClient):
    by_name = _get_tools(tools_client, search="alpha")
    assert [t["name"] for t in by_name["tools"]] == ["alpha"]

    by_desc = _get_tools(tools_client, search="第二个")
    assert [t["name"] for t in by_desc["tools"]] == ["beta"]


def test_tools_diagnostic_issues_keeps_error_and_warn(tools_client: TestClient):
    data = _get_tools(tools_client, diagnostic="issues")
    assert {t["name"] for t in data["tools"]} == {"gamma", "epsilon"}
    assert data["count"] == 2


def test_tools_diagnostic_empty_description(tools_client: TestClient):
    data = _get_tools(tools_client, diagnostic="empty_description")
    assert [t["name"] for t in data["tools"]] == ["gamma"]
    assert data["count"] == 1


def test_tools_facet_counts_are_totals_and_ignore_paging(tools_client: TestClient):
    """下拉里的数字必须是总数，不能跟着翻页变。"""
    page1 = _get_tools(tools_client, limit=2, offset=0)
    page3 = _get_tools(tools_client, limit=2, offset=4)

    assert page1["plugin_counts"] == page3["plugin_counts"]
    assert page1["category_counts"] == page3["category_counts"]
    assert page1["diagnostic_counts"] == page3["diagnostic_counts"]

    assert page1["plugin_counts"] == {"core": 1, "PluginA": 2, "PluginB": 2}
    assert page1["category_counts"] == {"self": 2, "default": 2, "meta": 1}
    assert page1["diagnostic_counts"] == {
        "total": 5,
        "empty_description": 1,
        "meta": 1,
        "ok": 3,
    }


def test_tools_facet_counts_respect_the_other_dimension(tools_client: TestClient):
    """分类计数排除分类自身，插件计数排除插件自身。"""
    data = _get_tools(tools_client, category="self")
    assert data["plugin_counts"] == {"core": 1, "PluginA": 1}
    assert data["category_counts"] == {"self": 2, "default": 2, "meta": 1}


def test_tools_facet_counts_respect_search(tools_client: TestClient):
    data = _get_tools(tools_client, search="alpha")
    assert data["plugin_counts"] == {"core": 1}
    assert data["category_counts"] == {"self": 1}


def test_tools_count_is_filtered_total_not_page_size(tools_client: TestClient):
    data = _get_tools(tools_client, search="alpha", limit=1)
    assert data["count"] == 1
    assert data["total_count"] == 5
    assert len(data["tools"]) == 1


def test_tools_offset_beyond_end_returns_empty_page(tools_client: TestClient):
    data = _get_tools(tools_client, limit=10, offset=99)
    assert data["tools"] == []
    assert data["count"] == 5
    assert data["has_more"] is False


# ── 技能列表 ────────────────────────────────────────────────────────────


def test_skills_legacy_contract_returns_everything(skills_client: TestClient):
    data = _get_skills(skills_client)
    assert data["limit"] is None
    assert len(data["skills"]) == 3
    assert data["count"] == 3
    assert data["has_more"] is False


def test_skills_pagination_slices_the_sorted_set(skills_client: TestClient):
    first = _get_skills(skills_client, limit=2, offset=0)
    second = _get_skills(skills_client, limit=2, offset=2)

    assert [s["name"] for s in first["skills"]] == ["skill-a", "skill-b"]
    assert [s["name"] for s in second["skills"]] == ["skill-c"]
    assert first["count"] == second["count"] == 3
    assert first["has_more"] is True
    assert second["has_more"] is False


def test_skills_offset_beyond_end_returns_empty_page(skills_client: TestClient):
    data = _get_skills(skills_client, limit=10, offset=99)
    assert data["skills"] == []
    assert data["count"] == 3
    assert data["has_more"] is False
