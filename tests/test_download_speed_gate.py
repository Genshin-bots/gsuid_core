"""资源测速门闩：领跑失败或被取消时，等待方不能拿走启动时的空地址。"""

from __future__ import annotations

import asyncio

import pytest

from gsuid_core.utils.download_resource import download_core


def _reset_speed_gate() -> None:
    download_core.global_tag = ""
    download_core.global_url = ""
    download_core.NOW_SPEED_TEST = False
    download_core._SPEED_TEST_DONE = False
    download_core._SPEED_TEST_EVENT = asyncio.Event()


@pytest.fixture(autouse=True)
def _clean_speed_gate():
    _reset_speed_gate()
    yield
    _reset_speed_gate()


def test_waiter_retries_when_leader_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """领跑测速抛错后，已经在等的调用方要拿到下一轮的地址，而不是空字符串。"""
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def flaky(_urls: dict[str, str]) -> tuple[str, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            await release.wait()
            raise RuntimeError("boom")
        return "[OK]", "https://example.test"

    monkeypatch.setattr(download_core, "find_fastest_url", flaky)

    async def scenario() -> None:
        leader = asyncio.create_task(download_core.check_speed())
        await started.wait()
        waiter = asyncio.create_task(download_core.check_speed())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(RuntimeError, match="boom"):
            await leader
        assert await asyncio.wait_for(waiter, timeout=2) == ("[OK]", "https://example.test")

    asyncio.run(scenario())


def test_waiter_retries_when_leader_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """领跑任务被取消时，等待方同样不能把空地址当成测速结果。"""
    calls = {"n": 0}
    started = asyncio.Event()

    async def flaky(_urls: dict[str, str]) -> tuple[str, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            await asyncio.Event().wait()
        return "[OK]", "https://example.test"

    monkeypatch.setattr(download_core, "find_fastest_url", flaky)

    async def scenario() -> None:
        leader = asyncio.create_task(download_core.check_speed())
        await started.wait()
        waiter = asyncio.create_task(download_core.check_speed())
        await asyncio.sleep(0)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        assert await asyncio.wait_for(waiter, timeout=2) == ("[OK]", "https://example.test")

    asyncio.run(scenario())
