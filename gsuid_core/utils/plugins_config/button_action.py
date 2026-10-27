"""插件配置按钮的回调。

插件用 ``register_config_button`` 声明 ``GsButtonConfig.endpoint``。
框架把 POST（开始）和 GET（看进度）挂到这个地址上，两个方法都要管理员登录。
"""

from __future__ import annotations

import asyncio
from typing import Protocol, TypedDict
from typing_extensions import NotRequired

from fastapi import Depends

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_admin
from gsuid_core.webconsole.session_store import SessionRecord
from gsuid_core.utils.plugins_config.models import (
    ButtonState,
    GsButtonConfig,
    ButtonStateView,
    button_state_view,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig, all_config_list

_PHASES = frozenset({"idle", "running", "success", "error"})


class ButtonActionResult(TypedDict):
    status: int
    msg: str
    data: NotRequired[ButtonStateView]


class ButtonReport:
    """回调用来汇报进度。每次 update 都写回配置，刷新页面还能看到。"""

    def __init__(self, cfg: StringConfig, key: str) -> None:
        self._cfg = cfg
        self._key = key

    @property
    def state(self) -> ButtonState:
        item = self._cfg.config[self._key]
        if not isinstance(item, GsButtonConfig):
            return ButtonState()
        return item.data

    def update(
        self,
        *,
        phase: str,
        progress: int,
        message: str,
        fields: dict[str, str] | None = None,
    ) -> None:
        item = self._cfg.config[self._key]
        if not isinstance(item, GsButtonConfig):
            return
        if phase not in _PHASES:
            phase = "error"
        bounded = max(0, min(100, progress))
        next_fields = dict(item.data.fields) if fields is None else dict(fields)
        self._cfg.set_button_state(
            self._key,
            ButtonState(phase=phase, progress=bounded, message=message, fields=next_fields),
        )


class ButtonHandler(Protocol):
    async def __call__(self, report: ButtonReport) -> None: ...


_handlers: dict[str, ButtonHandler] = {}
_tasks: dict[str, asyncio.Task[None]] = {}


def running_task(endpoint: str) -> asyncio.Task[None] | None:
    task = _tasks.get(endpoint)
    if task is None or task.done():
        return None
    return task


def _find(endpoint: str) -> tuple[StringConfig, str, GsButtonConfig] | None:
    for cfg in all_config_list.values():
        for key, item in cfg.config.items():
            if isinstance(item, GsButtonConfig) and item.endpoint == endpoint:
                return cfg, key, item
    return None


async def start_button(endpoint: str) -> ButtonActionResult:
    found = _find(endpoint)
    handler = _handlers.get(endpoint)
    if found is None or handler is None:
        return {"status": 1, "msg": "未注册该按钮回调"}
    cfg, key, item = found
    if running_task(endpoint) is not None:
        return {"status": 0, "msg": "进行中", "data": button_state_view(item.data)}
    report = ButtonReport(cfg, key)
    report.update(phase="running", progress=0, message="已开始")

    async def _run() -> None:
        try:
            await handler(report)
        except Exception as exc:
            # 插件回调失败只记到按钮状态，不能把配置接口打崩。
            logger.exception(t("log.config.button_callback_failed", endpoint=endpoint, e=exc))
            report.update(phase="error", progress=report.state.progress, message=str(exc))
        finally:
            _tasks.pop(endpoint, None)

    _tasks[endpoint] = asyncio.create_task(_run())
    return {"status": 0, "msg": "已开始", "data": button_state_view(report.state)}


async def read_button(endpoint: str) -> ButtonActionResult:
    found = _find(endpoint)
    if found is None:
        return {"status": 1, "msg": "未找到按钮配置"}
    cfg, key, item = found
    if item.data.phase == "running" and running_task(endpoint) is None:
        report = ButtonReport(cfg, key)
        report.update(phase="error", progress=item.data.progress, message="任务已中断")
        stored = cfg.config[key]
        if isinstance(stored, GsButtonConfig):
            return {"status": 0, "msg": "ok", "data": button_state_view(stored.data)}
    return {"status": 0, "msg": "ok", "data": button_state_view(item.data)}


def register_config_button(endpoint: str, handler: ButtonHandler) -> None:
    """把回调挂到 endpoint。POST 开始，GET 读进度。两者都要求管理员。"""
    if not endpoint.startswith("/api/") or endpoint != endpoint.strip() or " " in endpoint:
        raise ValueError("按钮回调地址必须以 /api/ 开头")
    existing = _handlers.get(endpoint)
    if existing is handler:
        return
    if existing is not None:
        raise ValueError(f"按钮回调地址已注册: {endpoint}")
    _handlers[endpoint] = handler
    _mount(endpoint)


def _mount(endpoint: str) -> None:
    slug = endpoint.strip("/").replace("/", "_")

    @app.post(endpoint, operation_id=f"config_button_post_{slug}")
    async def post_button(_user: SessionRecord = Depends(require_admin)) -> ButtonActionResult:
        return await start_button(endpoint)

    @app.get(endpoint, operation_id=f"config_button_get_{slug}")
    async def get_button(_user: SessionRecord = Depends(require_admin)) -> ButtonActionResult:
        return await read_button(endpoint)
