"""入站处理不能占住 WebSocket 读循环。"""

from __future__ import annotations

import asyncio

import pytest

import gsuid_core.handler as handler
from gsuid_core.bot import _Bot
from gsuid_core.models import MessageReceive


async def _free_permits(slots: asyncio.Semaphore) -> int:
    free = 0
    while free < 8:
        probe: asyncio.Task[bool] = asyncio.create_task(slots.acquire())
        done, _pending = await asyncio.wait({probe}, timeout=0.01)
        if probe not in done:
            probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)
            break
        free += 1
    for _ in range(free):
        slots.release()
    return free


def test_slow_inbound_returns_on_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_slow_inbound_returns_on_budget(monkeypatch))


async def _slow_inbound_returns_on_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_INBOUND_BUDGET_S", 0.15)

    async def slow(_ws: _Bot, _msg: MessageReceive, is_http: bool = False) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(handler, "handle_event", slow)
    slots = asyncio.Semaphore(2)
    started = asyncio.get_running_loop().time()
    await handler.run_inbound_event(_Bot("inbound"), MessageReceive(), slots)
    assert asyncio.get_running_loop().time() - started < 2
    assert await _free_permits(slots) == 2


def test_full_inbound_slots_drop_the_extra_message(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_full_inbound_slots_drop_the_extra_message(monkeypatch))


async def _full_inbound_slots_drop_the_extra_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_INBOUND_SLOT_WAIT_S", 0.15)
    monkeypatch.setattr(handler, "_INBOUND_BUDGET_S", 5.0)
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold(_ws: _Bot, _msg: MessageReceive, is_http: bool = False) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(handler, "handle_event", hold)
    slots = asyncio.Semaphore(1)
    first = asyncio.create_task(handler.run_inbound_event(_Bot("inbound"), MessageReceive(), slots))
    await asyncio.wait_for(started.wait(), 2)
    t0 = asyncio.get_running_loop().time()
    await handler.run_inbound_event(_Bot("inbound"), MessageReceive(), slots)
    assert asyncio.get_running_loop().time() - t0 < 1
    release.set()
    await asyncio.wait_for(first, 2)
    assert await _free_permits(slots) == 1


def test_inbound_failure_does_not_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_inbound_failure_does_not_escape(monkeypatch))


async def _inbound_failure_does_not_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(_ws: _Bot, _msg: MessageReceive, is_http: bool = False) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(handler, "handle_event", boom)
    slots = asyncio.Semaphore(1)
    await handler.run_inbound_event(_Bot("inbound"), MessageReceive(), slots)
    assert await _free_permits(slots) == 1


def test_cancel_waits_until_handle_event_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_cancel_waits_until_handle_event_finishes(monkeypatch))


async def _cancel_waits_until_handle_event_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    finished = asyncio.Event()

    async def slow(_ws: _Bot, _msg: MessageReceive, is_http: bool = False) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finished.set()

    monkeypatch.setattr(handler, "handle_event", slow)
    slots = asyncio.Semaphore(1)
    inbound = asyncio.create_task(handler.run_inbound_event(_Bot("inbound"), MessageReceive(), slots))
    await asyncio.wait_for(started.wait(), 2)
    inbound.cancel()
    await asyncio.gather(inbound, return_exceptions=True)
    assert finished.is_set()
    assert await _free_permits(slots) == 1
