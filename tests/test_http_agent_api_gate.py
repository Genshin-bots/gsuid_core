"""关闸 404；AI 关不挂路由；Admin 非 404；OpenAPI 不出现 Agent 面。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from http_agent_support import (
    make_agent_app,
    patch_settings,
    patch_ai_enable,
    sample_settings,
    reset_http_agent_runtime,
)


def setup_function() -> None:
    reset_http_agent_runtime()


def _agent_paths(app: FastAPI) -> list[str]:
    out: list[str] = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/agent"):
            out.append(route.path)
    return out


def test_agent_surface_404_when_disabled(monkeypatch) -> None:
    patch_settings(monkeypatch, sample_settings(enable=False))
    client = TestClient(make_agent_app())
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "m1"},
        headers={"Authorization": "Bearer gsk_xxxxxxxx_secret"},
    )
    assert r.status_code == 404
    h = client.get("/api/v1/agent/health")
    assert h.status_code == 404


def test_disabled_invalid_json_is_404_not_422(monkeypatch) -> None:
    patch_settings(monkeypatch, sample_settings(enable=False))
    client = TestClient(make_agent_app())
    r = client.post(
        "/api/v1/agent/chat/stream",
        content=b"{not-json",
        headers={"Content-Type": "application/json", "Authorization": "Bearer gsk_xxxxxxxx_secret"},
    )
    assert r.status_code == 404


def test_agent_surface_404_when_ai_disabled(monkeypatch) -> None:
    patch_settings(monkeypatch, sample_settings(enable=True), ai_enable=False)
    client = TestClient(make_agent_app())
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "m1"},
        headers={"Authorization": "Bearer gsk_xxxxxxxx_secret"},
    )
    assert r.status_code == 404
    h = client.get("/api/v1/agent/health")
    assert h.status_code == 404


def test_register_skips_when_ai_disabled(monkeypatch) -> None:
    from gsuid_core.ai_core.http_agent.register import register_http_agent_routes

    patch_ai_enable(monkeypatch, False)
    app = FastAPI()
    register_http_agent_routes(app)
    assert _agent_paths(app) == []


def test_register_mounts_when_ai_enabled(monkeypatch) -> None:
    from gsuid_core.ai_core.http_agent.register import register_http_agent_routes

    patch_ai_enable(monkeypatch, True)
    app = FastAPI()
    register_http_agent_routes(app)
    assert "/api/v1/agent/health" in _agent_paths(app)


def test_admin_keys_not_404_when_disabled(monkeypatch) -> None:
    patch_settings(monkeypatch, sample_settings(enable=False))
    client = TestClient(make_agent_app())
    r = client.post("/api/http-agent/admin/keys", json={"user_id": "u1", "bot_id": "b1"})
    assert r.status_code != 404
    assert r.status_code == 401


def test_admin_keys_not_404_when_ai_disabled(monkeypatch) -> None:
    patch_settings(monkeypatch, sample_settings(enable=True), ai_enable=False)
    client = TestClient(make_agent_app())
    r = client.post("/api/http-agent/admin/keys", json={"user_id": "u1", "bot_id": "b1"})
    assert r.status_code == 401


def test_openapi_excludes_agent_routes() -> None:
    from gsuid_core.ai_core.http_agent.routes import agent_router

    app = FastAPI()
    app.include_router(agent_router)
    spec = app.openapi()
    paths = spec["paths"] if "paths" in spec else {}
    for path in paths:
        assert not str(path).startswith("/api/v1/agent")
