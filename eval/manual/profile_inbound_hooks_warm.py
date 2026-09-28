"""逐行拆 ON_INBOUND 的真实稳态开销（带预热，隔离 asyncio.wait_for 包装）。

前一个版本漏了预热：`from X import Y` 的首次真实导入被摊进 300 轮，
看起来像 9ms/次，其实是启动期一次性成本。这里先预热再测，并单独量
`_invoke` 里 `asyncio.wait_for`（每次都要建 Task + 挂定时器）的开销。

用法：
    uv run python eval/manual/profile_inbound_hooks_warm.py
"""

from __future__ import annotations

import time
import asyncio
from collections.abc import Awaitable

from gsuid_core.models import Event, Message

ROUNDS = 300
COLD = "今天天气真不错啊大家吃了吗"


def _bench(fn, rounds: int = ROUNDS) -> float:
    for _ in range(20):  # 预热：把一次性 import / 首次调用排除掉
        fn()
    start = time.perf_counter()
    for _ in range(rounds):
        fn()
    return (time.perf_counter() - start) / rounds * 1000


async def _call_hook(fn, ctx) -> None:
    """HookFn 可能是同步或异步；同步的 hook 返回非 awaitable。"""
    raw = fn(ctx)
    if isinstance(raw, Awaitable):
        await raw


async def main() -> None:
    from gsuid_core.ai_core.kits import load_enabled_kits
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.hooks.points import AgentHookPoint
    from gsuid_core.ai_core.hooks.registry import hooks_for

    await load_enabled_kits()

    ev = Event("ProfBot", "99999", "p-1", "group", "99000004", "99000004", {"nickname": "p"}, 6)
    ev.raw_text = COLD
    ev.text = COLD
    ev.content = [Message(type="text", data=COLD)]
    ctx = AgentHookContext(
        point=AgentHookPoint.ON_INBOUND,
        ev=ev,
        session_id=ev.session_id,
        create_by="Chat",
        query=COLD,
    )

    rows: list[tuple[str, float]] = []

    # ── 1. 函数内 import 的稳态成本（预热后）──
    def imp() -> None:
        from gsuid_core.ai_core.memory import observe  # noqa: F401
        from gsuid_core.ai_core.memory.config import memory_config  # noqa: F401
        from gsuid_core.ai_core.configs.ai_config import ai_config  # noqa: F401

    rows.append(("三条函数内 import（预热后）", _bench(imp)))

    # ── 2. 前置判断各行 ──
    from gsuid_core.ai_core.memory.config import memory_config
    from gsuid_core.ai_core.kits.memory.kit import _image_urls, _in_observe_scope
    from gsuid_core.ai_core.configs.ai_config import ai_config

    rows.append(("ai_config.get_config('enable_memory')", _bench(lambda: ai_config.get_config("enable_memory").data)))
    rows.append(("memory_config.observer_enabled", _bench(lambda: memory_config.observer_enabled)))
    rows.append(("memory_config.memory_mode", _bench(lambda: memory_config.memory_mode)))
    rows.append(
        ("_in_observe_scope(...)", _bench(lambda: _in_observe_scope(ev.session_id, memory_config.memory_session)))
    )
    rows.append(("_image_urls(ev)", _bench(lambda: _image_urls(ev))))

    # ── 3. asyncio.wait_for 的包装成本 ──
    async def noop_async() -> None:
        return None

    def sync_noop() -> None:
        return None

    async def bench_wait_for() -> float:
        for _ in range(20):
            await asyncio.wait_for(noop_async(), timeout=1.0)
        start = time.perf_counter()
        for _ in range(ROUNDS):
            await asyncio.wait_for(noop_async(), timeout=1.0)
        return (time.perf_counter() - start) / ROUNDS * 1000

    rows.append(("asyncio.wait_for 包装（async fn）", await bench_wait_for()))

    def bench_to_thread() -> float:
        for _ in range(20):
            asyncio.run(asyncio.to_thread(sync_noop))  # 跨循环，仅作量级参照
        return 0.0

    del bench_to_thread

    # ── 4. 每个 hook 的整体 vs 裸调用 ──

    for r in hooks_for(AgentHookPoint.ON_INBOUND):
        ctx.current_kit_id = r.kit_id
        for _ in range(20):
            await _call_hook(r.func, ctx)
        start = time.perf_counter()
        for _ in range(ROUNDS):
            await _call_hook(r.func, ctx)
        wrapped = (time.perf_counter() - start) / ROUNDS * 1000

        for _ in range(20):
            await _call_hook(r.func, ctx)
        start = time.perf_counter()
        for _ in range(ROUNDS):
            await _call_hook(r.func, ctx)
        bare = (time.perf_counter() - start) / ROUNDS * 1000
        ctx.current_kit_id = None
        rows.append((f"{r.label} · 经 _invoke 包装", wrapped))
        rows.append((f"{r.label} · 直接调 hook（不走 wait_for）", bare))

    print("=" * 82)
    print(f"ON_INBOUND 稳态开销（ms/次，{ROUNDS} 轮，已预热 20 轮）")
    print("=" * 82)
    print(f"{'项':<44}{'ms':>10}")
    for name, per in rows:
        print(f"{name:<44}{per:>10.4f}")


if __name__ == "__main__":
    asyncio.run(main())
