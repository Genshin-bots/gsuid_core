"""**旧前端 + 新后端**的兼容契约。

分页改造只能「加字段 + 加可选参数」，不能改旧字段的语义——否则只升级后端、
不升级前端的用户会看到列表变空、计数对不上。

这里逐条钉住**改造前前端实际读取的字段**（均为不带新参数的老请求）：
- `/api/ai/entity_index` → `data.entries` + `data.count`（评测脚本也读 `entries`）
- `/api/ai/tools/list` → `data.tools` / `categories` / `plugins` / `count` / `total_count`
- `/api/ai/skills/list` → `data.skills` + `data.count`
- `/api/theme/presets` → `data.presets` + 两个目录名字段
"""

from __future__ import annotations

from typing import Any, Dict, List, Iterator

import pytest
from fastapi.testclient import TestClient

from gsuid_core.ai_core import entity_index as entity_index_mod
from gsuid_core.webconsole import (
    theme_api,
    plugins_api,
    ai_tools_api,
    ai_skills_api,
    scheduler_api,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.webconsole._local_test_gate import require_auth_or_local_test


class _StubTool:
    def __init__(self, name: str, description: str, plugin: str) -> None:
        self.name = name
        self.description = description
        self.plugin = plugin


class _StubSkill:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description
        self.content = f"# {name}"
        self.license = None
        self.compatibility = None
        self.uri = ""
        self.metadata: Dict[str, str] = {}


@pytest.fixture
def legacy_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """模拟旧前端：注册表固定，不带任何新参数。"""
    registry: Dict[str, Dict[str, _StubTool]] = {
        "self": {
            "alpha": _StubTool("alpha", "第一个工具", "core"),
            "beta": _StubTool("beta", "第二个工具", "PluginA"),
        },
        "default": {
            "gamma": _StubTool("gamma", "第三个工具", "PluginA"),
        },
    }
    monkeypatch.setattr(ai_tools_api, "get_registered_tools", lambda: registry)

    fake_skills = {
        "skill-a": _StubSkill("skill-a", "甲"),
        "skill-b": _StubSkill("skill-b", "乙"),
    }
    monkeypatch.setattr(ai_skills_api, "skills", fake_skills)
    monkeypatch.setattr(ai_skills_api, "get_skill_source", lambda name: ("data", None))

    fake_presets: List[Dict[str, Any]] = [
        {
            "name": "黑夜街道",
            "filename": "黑夜街道.json",
            "source": "builtin",
            "size_bytes": 248,
            "mtime": 1718952200.0,
            "is_active": True,
            "valid": True,
            "is_builtin": True,
            "config": {"mode": "dark", "color": "blue"},
        }
    ]
    monkeypatch.setattr(theme_api, "list_theme_presets", lambda: fake_presets)

    saved = dict(entity_index_mod.get_entity_index())
    entity_index_mod.clear_entity_index()
    entity_index_mod.register_entity_surface("刻晴", "刻晴", "GenshinUID")
    entity_index_mod.register_entity_surface("秧秧", "秧秧", "WutheringWaves")

    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[require_auth_or_local_test] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_auth, None)
        app.dependency_overrides.pop(require_auth_or_local_test, None)
        entity_index_mod.clear_entity_index()
        entity_index_mod.get_entity_index().update(saved)


def _data(client: TestClient, url: str, **params: str | int | bool) -> dict:
    resp = client.get(url, params=params)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 0
    return body["data"]


def test_entity_index_keeps_the_legacy_shape(legacy_client: TestClient):
    """旧前端读 `entries` + `count`；评测脚本也读 `entries`。"""
    data = _data(legacy_client, "/api/ai/entity_index")
    assert isinstance(data["entries"], list)
    assert data["count"] == len(data["entries"])
    for entry in data["entries"]:
        assert isinstance(entry["surface"], str)
        assert isinstance(entry["plugins"], list)
        assert isinstance(entry["ambiguous"], bool)


def test_entity_index_legacy_call_sees_every_surface(legacy_client: TestClient):
    """不带 limit → 全量（旧前端要靠这个做本地筛选）。"""
    data = _data(legacy_client, "/api/ai/entity_index")
    assert {e["surface"] for e in data["entries"]} == {"刻晴", "秧秧"}


def test_tools_list_keeps_the_legacy_shape(legacy_client: TestClient):
    data = _data(legacy_client, "/api/ai/tools/list")
    assert isinstance(data["tools"], list)
    assert isinstance(data["categories"], list)
    assert isinstance(data["plugins"], list)
    assert isinstance(data["by_category"], dict)
    assert isinstance(data["by_plugin"], dict)
    assert isinstance(data["total_count"], int)
    # 旧语义：count == 本次返回的条数（不分页时就是总数）
    assert data["count"] == len(data["tools"])
    assert data["total_count"] == 3
    for tool in data["tools"]:
        assert isinstance(tool["name"], str)
        assert isinstance(tool["description"], str)
        assert isinstance(tool["plugin"], str)
        assert isinstance(tool["category"], str)


def test_tools_list_legacy_category_filter_still_works(legacy_client: TestClient):
    """旧前端用 category / plugin 参数做筛选，语义不能变。"""
    data = _data(legacy_client, "/api/ai/tools/list", category="self")
    assert data["count"] == len(data["tools"]) == 2
    assert {t["category"] for t in data["tools"]} == {"self"}

    by_plugin = _data(legacy_client, "/api/ai/tools/list", plugin="PluginA")
    assert by_plugin["count"] == len(by_plugin["tools"]) == 2
    assert {t["plugin"] for t in by_plugin["tools"]} == {"PluginA"}


def test_skills_list_keeps_the_legacy_shape(legacy_client: TestClient):
    data = _data(legacy_client, "/api/ai/skills/list")
    assert isinstance(data["skills"], list)
    assert data["count"] == len(data["skills"]) == 2


def test_theme_presets_keep_the_legacy_shape(legacy_client: TestClient):
    data = _data(legacy_client, "/api/theme/presets")
    assert isinstance(data["presets"], list)
    assert isinstance(data["user_presets_path"], str)
    assert isinstance(data["builtin_presets_path"], str)
    for preset in data["presets"]:
        assert isinstance(preset["name"], str)
        assert isinstance(preset["filename"], str)
        assert isinstance(preset["size_bytes"], int)
        assert isinstance(preset["mtime"], (int, float))
        assert isinstance(preset["is_active"], bool)
        assert isinstance(preset["valid"], bool)


def test_eval_script_contract_still_holds(legacy_client: TestClient):
    """`eval/tool_selection/run.py` 按 `entries[].plugins[0]` 取插件归属。"""
    data = _data(legacy_client, "/api/ai/entity_index")
    for entry in data["entries"]:
        if entry["ambiguous"]:
            continue
        assert isinstance(entry["plugins"][0], str)


class _FakeStoreResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        return self._payload


class _FakeStoreClient:
    payload: Dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeStoreClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def get(self, url: str, timeout: int = 0) -> _FakeStoreResponse:
        return _FakeStoreResponse(self.payload)


def test_plugin_store_keeps_the_legacy_shape(monkeypatch: pytest.MonkeyPatch):
    """旧前端读 `plugins` / `fun_plugins` / `tool_plugins`，且不传新参数时应拿到全量。"""
    _FakeStoreClient.payload = {
        "plugins": {
            "AlphaPlugin": {"info": "甲插件", "alias": ["a"], "type": "tip", "content": "普通"},
            "BetaPlugin": {"info": "乙插件", "alias": ["b"], "type": "tip", "content": "普通"},
        },
        "fun_plugins": ["AlphaPlugin"],
        "tool_plugins": [],
    }
    monkeypatch.setattr("httpx.AsyncClient", _FakeStoreClient)

    async def _fake_installed() -> Dict[str, Any]:
        return {"alphaplugin": {"name": "AlphaPlugin"}}

    monkeypatch.setattr(plugins_api, "get_local_plugins_list", _fake_installed)
    app.dependency_overrides[require_auth] = lambda: None
    try:
        client = TestClient(app)
        data = _data(client, "/api/plugin-store/list")
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert isinstance(data["plugins"], list)
    assert isinstance(data["fun_plugins"], list)
    assert isinstance(data["tool_plugins"], list)
    # 不传 limit → 全量（旧前端要在本地做筛选与页签切换）
    assert len(data["plugins"]) == 2
    assert data["count"] == data["total_count"] == 2
    for plugin in data["plugins"]:
        assert isinstance(plugin["id"], str)
        assert isinstance(plugin["tags"], list)
        assert isinstance(plugin["installed"], bool)
        assert isinstance(plugin["hasUpdate"], bool)


def test_scheduler_jobs_keep_the_legacy_shape(monkeypatch: pytest.MonkeyPatch):
    """旧前端读 `data`（数组）直接遍历——`data` 不能改成对象。"""
    jobs = [
        {
            "id": "job_a",
            "name": "任务 A",
            "description": "甲任务",
            "next_run_time": None,
            "trigger": "cron[hour=3]",
            "trigger_description": "每天 3 点",
            "paused": False,
        },
        {
            "id": "job_b",
            "name": "任务 B",
            "description": "乙任务",
            "next_run_time": None,
            "trigger": "cron[hour=4]",
            "trigger_description": "每天 4 点",
            "paused": True,
        },
    ]
    monkeypatch.setattr(scheduler_api, "collect_scheduler_jobs", lambda: jobs)
    app.dependency_overrides[require_auth] = lambda: None
    try:
        client = TestClient(app)
        resp = client.get("/api/scheduler/jobs")
    finally:
        app.dependency_overrides.pop(require_auth, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 0
    # 旧前端：`jobs.map(...)` —— data 必须仍是数组
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 2
    for job in body["data"]:
        assert isinstance(job["id"], str)
        assert isinstance(job["name"], str)
        assert isinstance(job["paused"], bool)
