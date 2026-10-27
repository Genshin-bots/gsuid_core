"""配置按钮：管理员才能打回调，进度写回配置，刷新后还在。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from gsuid_core.webconsole.plugins_api import _build_config_item
from gsuid_core.utils.plugins_config.models import (
    GSC,
    ButtonState,
    GsStrConfig,
    GsButtonConfig,
    ButtonStatusField,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig, reconcile_config
from gsuid_core.utils.plugins_config.button_action import (
    ButtonReport,
    read_button,
    running_task,
    start_button,
    register_config_button,
)

_ENDPOINT = "/api/gs_test_config_button"


def _calls(route: APIRoute) -> list[object]:
    found: list[object] = []
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        found.append(dep.call)
        stack.extend(dep.dependencies)
    return found


def _button(endpoint: str = _ENDPOINT, message: str = "") -> GsButtonConfig:
    return GsButtonConfig(
        title="安装",
        desc="下载运行时",
        data=ButtonState(message=message),
        endpoint=endpoint,
        label="下载",
        confirm="确认下载？",
        status_fields=[ButtonStatusField(key="version", label="版本")],
    )


def _cfg(tmp_path: Path, name: str) -> StringConfig:
    return StringConfig(name, tmp_path / "config.json", {"install": _button()})


async def _ok(report: ButtonReport) -> None:
    report.update(phase="running", progress=40, message="下载中", fields={"version": "v0.2.5"})
    report.update(
        phase="success",
        progress=100,
        message="已安装",
        fields={"version": "v0.2.5", "path": "/opt/obscura"},
    )


def test_button_routes_require_admin() -> None:
    from gsuid_core.webconsole.app_app import app
    from gsuid_core.webconsole.web_api import require_admin

    register_config_button(_ENDPOINT, _ok)
    matched = [route for route in app.routes if isinstance(route, APIRoute) and route.path == _ENDPOINT]
    methods: set[str] = set()
    for route in matched:
        if route.methods is not None:
            methods.update(route.methods)
    assert methods == {"GET", "POST"}
    for route in matched:
        assert require_admin in _calls(route)


def test_button_without_token_is_401() -> None:
    from gsuid_core.webconsole.app_app import app

    register_config_button(_ENDPOINT, _ok)
    client = TestClient(app)
    assert client.post(_ENDPOINT).status_code == 401
    assert client.get(_ENDPOINT).status_code == 401


def test_button_persists_progress_and_ignores_form_save(tmp_path: Path) -> None:
    register_config_button(_ENDPOINT, _ok)
    cfg = _cfg(tmp_path, "gs-test-config-button-persist")

    async def _run() -> dict[str, object]:
        started = await start_button(_ENDPOINT)
        task = running_task(_ENDPOINT)
        if task is not None:
            await task
        return dict(started)

    started = asyncio.run(_run())
    assert started["status"] == 0
    stored = cfg.get_config("install")
    assert isinstance(stored, GsButtonConfig)
    assert stored.data.phase == "success"
    assert stored.data.progress == 100
    assert stored.data.fields["path"] == "/opt/obscura"
    assert cfg.set_config("install", {"phase": "idle"}) is True
    kept = cfg.get_config("install")
    assert isinstance(kept, GsButtonConfig)
    assert kept.data.phase == "success"
    payload = _build_config_item(kept, "install")
    assert payload["type"] == "gsbutton"
    assert payload["endpoint"] == _ENDPOINT
    assert payload["status_fields"] == [{"key": "version", "label": "版本"}]
    assert payload["value"]["phase"] == "success"


def test_second_click_does_not_start_another_run(tmp_path: Path) -> None:
    gate = asyncio.Event()
    entered = 0

    async def _slow(report: ButtonReport) -> None:
        nonlocal entered
        entered += 1
        report.update(phase="running", progress=10, message="进行中")
        await gate.wait()
        report.update(phase="success", progress=100, message="完成", fields={"version": "v1"})

    endpoint = "/api/gs_test_config_button_slow"
    register_config_button(endpoint, _slow)
    StringConfig(
        "gs-test-config-button-slow",
        tmp_path / "slow.json",
        {"install": _button(endpoint)},
    )

    async def _run() -> None:
        first = await start_button(endpoint)
        await asyncio.sleep(0)
        assert first["status"] == 0
        assert entered == 1
        second = await start_button(endpoint)
        assert second["msg"] == "进行中"
        gate.set()
        task = running_task(endpoint)
        if task is not None:
            await task
        done = await read_button(endpoint)
        view = done["data"] if "data" in done else None
        assert view is not None
        assert view["phase"] == "success"

    asyncio.run(_run())


def test_reconcile_keeps_button_result_and_refreshes_label() -> None:
    stored: dict[str, GSC] = {
        "install": GsButtonConfig(
            title="旧标题",
            desc="旧说明",
            data=ButtonState(phase="success", progress=100, message="已安装", fields={"version": "v0.2.5"}),
            endpoint="/api/old",
            label="旧按钮",
            status_fields=[ButtonStatusField(key="old", label="旧")],
        )
    }
    default: dict[str, GSC] = {
        "install": GsButtonConfig(
            title="安装 Obscura",
            desc="下载浏览器",
            data=ButtonState(),
            endpoint="/api/obscura_browser/install",
            label="下载并安装",
            status_fields=[ButtonStatusField(key="version", label="版本")],
        ),
        "note": GsStrConfig(title="备注", desc="", data=""),
    }
    merged = reconcile_config(stored, default, config_name="button-test")
    item = merged["install"]
    assert isinstance(item, GsButtonConfig)
    assert item.data.phase == "success"
    assert item.data.fields["version"] == "v0.2.5"
    assert item.endpoint == "/api/obscura_browser/install"
    assert item.label == "下载并安装"
    assert item.status_fields[0].key == "version"
    assert "note" in merged


def test_failed_callback_is_visible_on_next_read(tmp_path: Path) -> None:
    endpoint = "/api/gs_test_config_button_fail"

    async def _boom(report: ButtonReport) -> None:
        raise RuntimeError("磁盘满了")

    register_config_button(endpoint, _boom)
    cfg = StringConfig(
        "gs-test-config-button-fail",
        tmp_path / "fail.json",
        {
            "install": GsButtonConfig(
                title="安装",
                desc="",
                data=ButtonState(),
                endpoint=endpoint,
                label="下载",
            )
        },
    )

    async def _run() -> dict[str, object]:
        await start_button(endpoint)
        task = running_task(endpoint)
        if task is not None:
            await task
        return dict(await read_button(endpoint))

    body = asyncio.run(_run())
    data = body["data"]
    assert isinstance(data, dict)
    assert data["phase"] == "error"
    assert data["message"] == "磁盘满了"
    stored = cfg.get_config("install")
    assert isinstance(stored, GsButtonConfig)
    assert stored.data.phase == "error"
