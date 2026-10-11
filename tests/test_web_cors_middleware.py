"""web_cors_origins：只回显名单内的源，并带 credentials。"""

from __future__ import annotations

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from gsuid_core.web_cors_middleware import (
    WebCorsMiddleware,
    normalize_cors_origin,
    parse_web_cors_origins,
)


def test_normalize_cors_origin_strips_path_and_default_port() -> None:
    assert normalize_cors_origin("  http://localhost:8080/app/ ") == "http://localhost:8080"
    assert normalize_cors_origin("http://localhost:80") == "http://localhost"
    assert normalize_cors_origin("https://Example.com:443/x") == "https://example.com"
    assert normalize_cors_origin("http://[::1]:8080") == "http://[::1]:8080"
    assert normalize_cors_origin("*") is None
    assert normalize_cors_origin("http://evil.com\r\nX: y") is None
    assert normalize_cors_origin("localhost:8080") is None


def test_parse_web_cors_origins_drops_invalid_items() -> None:
    parsed = parse_web_cors_origins(
        ["http://localhost:8080/", "*", 1, "", "https://console.example:8443/hub"],
    )
    assert parsed == frozenset({"http://localhost:8080", "https://console.example:8443"})


def _client(origins: frozenset[str], monkeypatch) -> TestClient:
    monkeypatch.setattr(
        "gsuid_core.web_cors_middleware.load_web_cors_origins",
        lambda: origins,
    )
    app = FastAPI()
    app.add_middleware(WebCorsMiddleware)

    @app.get("/api/auth/admin/exists")
    async def exists(response: Response) -> dict:
        response.headers["Vary"] = "Accept-Encoding"
        return {"ok": True}

    return TestClient(app)


def test_preflight_echoes_allowed_origin(monkeypatch) -> None:
    client = _client(frozenset({"http://localhost:8080"}), monkeypatch)
    response = client.options(
        "/api/auth/admin/exists",
        headers={
            "Origin": "http://localhost:8080",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Content-Type, Authorization",
        },
    )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "http://localhost:8080"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "authorization" in response.headers["access-control-allow-headers"]
    assert "content-type" in response.headers["access-control-allow-headers"]
    assert response.headers["access-control-allow-origin"] != "*"


def test_get_keeps_existing_vary(monkeypatch) -> None:
    client = _client(frozenset({"http://localhost:8080"}), monkeypatch)
    response = client.get("/api/auth/admin/exists", headers={"Origin": "http://localhost:8080"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["access-control-allow-origin"] == "http://localhost:8080"
    assert response.headers["access-control-allow-credentials"] == "true"
    vary = response.headers["vary"].lower()
    assert "origin" in vary
    assert "accept-encoding" in vary


def test_unknown_origin_and_empty_list_add_no_cors_headers(monkeypatch) -> None:
    client = _client(frozenset({"http://localhost:8080"}), monkeypatch)
    denied = client.get("/api/auth/admin/exists", headers={"Origin": "http://evil.example"})
    assert denied.status_code == 200
    assert "access-control-allow-origin" not in denied.headers

    closed = _client(frozenset(), monkeypatch)
    silent = closed.get("/api/auth/admin/exists", headers={"Origin": "http://localhost:8080"})
    assert silent.status_code == 200
    assert "access-control-allow-origin" not in silent.headers
