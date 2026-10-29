"""调度任务列表 WebAPI：服务端搜索 / 分页 + 状态统计（总数）。

``data`` 仍是数组（旧前端直接遍历），分页与统计信息放在信封的 ``meta`` 上——
这是保住旧契约的关键：**不能**把 ``data`` 改成对象。
"""

from __future__ import annotations

from typing import Any, Dict, List, Iterator

import pytest
from fastapi.testclient import TestClient

from gsuid_core.webconsole import scheduler_api
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


def _make_jobs(count: int = 45) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    for i in range(count):
        jobs.append(
            {
                "id": f"job_{i:03d}",
                "name": f"任务 {i:03d}",
                "description": f"第 {i:03d} 个定时任务的说明",
                "next_run_time": "2026-01-01T00:00:00+08:00",
                "trigger": "cron[hour=3]",
                "trigger_description": "每天 3 点",
                "paused": i % 5 == 0,
            }
        )
    return jobs


@pytest.fixture
def jobs_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    jobs = _make_jobs()
    monkeypatch.setattr(scheduler_api, "collect_scheduler_jobs", lambda: jobs)
    app.dependency_overrides[require_auth] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_auth, None)


def _get(client: TestClient, **params: str | int | bool) -> Dict[str, Any]:
    resp = client.get("/api/scheduler/jobs", params=params)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 0
    return body


def test_jobs_legacy_contract_returns_everything(jobs_client: TestClient):
    """不传 limit → 全量；``data`` 仍是数组（旧前端直接 map）。"""
    body = _get(jobs_client)
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 45
    meta = body["meta"]
    assert meta["total"] == 45
    assert meta["limit"] is None
    assert meta["has_more"] is False


def test_jobs_pagination_slices_the_list(jobs_client: TestClient):
    first = _get(jobs_client, limit=20, offset=0)
    last = _get(jobs_client, limit=20, offset=40)

    assert len(first["data"]) == 20
    assert len(last["data"]) == 5
    assert first["meta"]["total"] == last["meta"]["total"] == 45
    assert first["meta"]["has_more"] is True
    assert last["meta"]["has_more"] is False

    ids = [j["id"] for j in first["data"]] + [j["id"] for j in last["data"]]
    assert len(set(ids)) == 25


def test_jobs_search_filters_before_paging(jobs_client: TestClient):
    """先筛后切：搜索后分页，第二页不能为空。"""
    page1 = _get(jobs_client, search="任务 01", limit=2, offset=0)
    page2 = _get(jobs_client, search="任务 01", limit=2, offset=2)

    # 「任务 01」命中 任务 010 ~ 任务 019，共 10 条
    assert page1["meta"]["total"] == page2["meta"]["total"] == 10
    assert len(page1["data"]) == 2
    assert len(page2["data"]) == 2


def test_jobs_counts_are_totals_regardless_of_paging(jobs_client: TestClient):
    """状态统计对全量任务做，不随搜索/翻页变化。"""
    page1 = _get(jobs_client, limit=20, offset=0)
    searched = _get(jobs_client, search="任务 01")

    for body in (page1, searched):
        counts = body["meta"]["counts"]
        assert counts["total"] == 45
        assert counts["paused"] == 9
        assert counts["running"] == 36
        assert counts["error"] == 0


def test_jobs_offset_beyond_end_returns_empty_page(jobs_client: TestClient):
    body = _get(jobs_client, limit=10, offset=999)
    assert body["data"] == []
    assert body["meta"]["total"] == 45
    assert body["meta"]["has_more"] is False
