"""HTTP Agent 单测夹具。文件名无 test_ 前缀，不进默认收集。"""

from __future__ import annotations

from typing import List

from pytest import MonkeyPatch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gsuid_core.ai_core.http_agent.auth import reset_auth_bans_for_tests
from gsuid_core.ai_core.http_agent.config import (
    DEFAULT_RPM,
    DEFAULT_QUEUE_MAX,
    DEFAULT_MAX_IMAGES,
    DEFAULT_WALL_CLOCK,
    DEFAULT_AUTH_BAN_SEC,
    DEFAULT_HARD_TIMEOUT,
    DEFAULT_AUTH_FAIL_MAX,
    DEFAULT_HEARTBEAT_SEC,
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_IDEMPOTENCY_CAP,
    DEFAULT_IDEMPOTENCY_TTL,
    DEFAULT_PER_KEY_CONCURRENT,
    HttpAgentSettings,
)
from gsuid_core.ai_core.http_agent.limiter import limiter
from gsuid_core.ai_core.http_agent.runtime import reset_runtime_for_tests
from gsuid_core.ai_core.http_agent.idempotency import idempotency_store


def sample_settings(
    *,
    enable: bool = True,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    per_key_concurrent: int = DEFAULT_PER_KEY_CONCURRENT,
    rate_limit_rpm: int = DEFAULT_RPM,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_images: int = DEFAULT_MAX_IMAGES,
    wall_clock: int = DEFAULT_WALL_CLOCK,
    hard_timeout: int = DEFAULT_HARD_TIMEOUT,
    heartbeat_sec: int = DEFAULT_HEARTBEAT_SEC,
    queue_max: int = DEFAULT_QUEUE_MAX,
    idempotency_ttl: int = DEFAULT_IDEMPOTENCY_TTL,
    idempotency_cap: int = DEFAULT_IDEMPOTENCY_CAP,
    cors_origins: List[str] | None = None,
    default_persona: str = "",
    auth_fail_max: int = DEFAULT_AUTH_FAIL_MAX,
    auth_ban_sec: int = DEFAULT_AUTH_BAN_SEC,
) -> HttpAgentSettings:
    return HttpAgentSettings(
        enable=enable,
        max_concurrent=max_concurrent,
        per_key_concurrent=per_key_concurrent,
        rate_limit_rpm=rate_limit_rpm,
        max_body_bytes=max_body_bytes,
        max_images=max_images,
        wall_clock=wall_clock,
        hard_timeout=hard_timeout,
        heartbeat_sec=heartbeat_sec,
        queue_max=queue_max,
        idempotency_ttl=idempotency_ttl,
        idempotency_cap=idempotency_cap,
        cors_origins=list(cors_origins) if cors_origins is not None else [],
        default_persona=default_persona,
        auth_fail_max=auth_fail_max,
        auth_ban_sec=auth_ban_sec,
    )


class _Flag:
    data: bool

    def __init__(self, data: bool) -> None:
        self.data = data


class _AiConfigStub:
    def __init__(self, enable: bool = True) -> None:
        self._enable = enable

    def get_config(self, key: str) -> _Flag:
        if key == "enable":
            return _Flag(self._enable)
        return _Flag(True)


def patch_ai_enable(monkeypatch: MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(
        "gsuid_core.ai_core.configs.ai_config.ai_config",
        _AiConfigStub(enabled),
    )


def patch_settings(
    monkeypatch: MonkeyPatch,
    settings: HttpAgentSettings,
    *,
    ai_enable: bool = True,
) -> None:
    def _load() -> HttpAgentSettings:
        return settings

    def _enabled() -> bool:
        return settings.enable

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.config.load_http_agent_settings", _load, raising=False)
    monkeypatch.setattr("gsuid_core.ai_core.http_agent.routes.load_http_agent_settings", _load, raising=False)
    monkeypatch.setattr("gsuid_core.ai_core.http_agent.limiter.load_http_agent_settings", _load, raising=False)
    monkeypatch.setattr("gsuid_core.ai_core.http_agent.auth.load_http_agent_settings", _load, raising=False)
    monkeypatch.setattr("gsuid_core.ai_core.http_agent.idempotency.load_http_agent_settings", _load, raising=False)
    monkeypatch.setattr("gsuid_core.ai_core.http_agent.persona.load_http_agent_settings", _load, raising=False)
    monkeypatch.setattr("gsuid_core.ai_core.http_agent.config.is_http_agent_enabled", _enabled, raising=False)
    patch_ai_enable(monkeypatch, ai_enable)


def make_agent_app() -> FastAPI:
    from gsuid_core.ai_core.http_agent.routes import agent_router
    from gsuid_core.webconsole.http_agent_keys_api import admin_router

    app = FastAPI()
    app.include_router(agent_router)
    app.include_router(admin_router)
    return app


def make_client() -> TestClient:
    return TestClient(make_agent_app())


def reset_http_agent_runtime() -> None:
    limiter.reset_for_tests()
    reset_auth_bans_for_tests()
    reset_runtime_for_tests()
    idempotency_store.reset_for_tests()


def install_chat_mocks(monkeypatch: MonkeyPatch, *, send_text: str = "hello") -> None:
    """让 /chat/stream 在无 LLM 下跑通：人格/预算/就绪/turn 全 mock。"""

    def _persona(**_kwargs: object) -> str:
        return "test-persona"

    async def _budget(_event: object) -> None:
        return None

    async def _turn(*, bot: object, event: object, wall_clock: int, run_id: str) -> object:
        from gsuid_core.ai_core.handle_ai import PassiveChatResult

        send = getattr(bot, "send")
        if send_text:
            await send(send_text)
        return PassiveChatResult("ok")

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.persona.resolve_http_persona", _persona)
    monkeypatch.setattr("gsuid_core.ai_core.turn_pipeline.evaluate_budget", _budget)
    monkeypatch.setattr("gsuid_core.ai_core.startup.is_ai_core_ready", lambda: True)
    patch_ai_enable(monkeypatch, True)
    monkeypatch.setattr("gsuid_core.ai_core.http_agent.bridge.run_http_agent_turn", _turn)
