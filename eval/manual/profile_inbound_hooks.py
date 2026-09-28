"""拆开 ON_INBOUND 扇出：到底是哪个 hook、hook 里的哪一步吃掉了 11ms。

用法：
    uv run python eval/manual/profile_inbound_hooks.py
"""

from __future__ import annotations

import time
import asyncio

from gsuid_core.models import Event, Message

ROUNDS = 200
COLD = "今天天气真不错啊大家吃了吗"


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

    regs = hooks_for(AgentHookPoint.ON_INBOUND)
    print("=" * 84)
    print(f"ON_INBOUND 订阅者（{len(regs)} 个，按 priority 升序串行执行）")
    print("=" * 84)
    for r in regs:
        print(f"  priority={r.priority:<6} kit={r.kit_id:<18} label={r.label:<16} timeout={r.timeout_ms}ms")

    print()
    print("=" * 84)
    print("逐个 hook 实测（ms/条）")
    print("=" * 84)
    print(f"{'hook':<34}{'ms/条':>10}{'占比':>8}")
    total = 0.0
    times: list[tuple[str | None, float]] = []
    for r in regs:
        ctx.current_kit_id = r.kit_id
        start = time.perf_counter()
        for _ in range(ROUNDS):
            from gsuid_core.ai_core.hooks.dispatch import _invoke

            await _invoke(r.func, ctx, r.timeout_ms, r.label, AgentHookPoint.ON_INBOUND)
        per = (time.perf_counter() - start) / ROUNDS * 1000
        total += per
        times.append((r.label or r.kit_id, per))
    ctx.current_kit_id = None
    for label, per in times:
        share = per / total * 100 if total else 0
        print(f"{label:<34}{per:>10.4f}{share:>7.1f}%")
    print(f"{'合计':<34}{total:>10.4f}")

    # memory hook 内部再拆一层
    mem = next((r for r in regs if r.kit_id is not None and r.kit_id.endswith("memory")), None)
    if mem is not None:
        print()
        print("=" * 84)
        print("记忆 hook 内部拆解")
        print("=" * 84)
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.configs.ai_config import ai_config

        print(f"  enable_memory        = {ai_config.get_config('enable_memory').data}")
        print(f"  observer_enabled     = {memory_config.observer_enabled}")
        print(f"  memory_mode          = {memory_config.memory_mode}")
        print(f"  memory_session       = {memory_config.memory_session}")

        if not (
            ai_config.get_config("enable_memory").data
            and memory_config.observer_enabled
            and "被动感知" in memory_config.memory_mode
        ):
            print("  → observe 应当直接 return，本轮 11ms 不该来自记忆摄取")
        else:
            from gsuid_core.ai_core.memory import observe

            start = time.perf_counter()
            for _ in range(ROUNDS):
                await observe(
                    content=COLD,
                    speaker_id=ctx.user_id,
                    group_id=ctx.group_id,
                    bot_self_id="99999",
                    observer_blacklist=memory_config.observer_blacklist,
                    message_type="group_msg",
                    bot_id="ProfBot",
                )
            per = (time.perf_counter() - start) / ROUNDS * 1000
            print(f"  observe() 直接调用    = {per:.4f} ms/条")

    # meme hook 是否真的在干活（无图片应早退）
    print()
    print("=" * 84)
    print("表情 hook（纯文本消息应在提取 image 后早退）")
    print("=" * 84)
    meme = next((r for r in regs if r.kit_id is not None and r.kit_id.endswith("meme")), None)
    if meme is not None:
        from gsuid_core.ai_core.meme.config import meme_config

        print(f"  meme_enable          = {meme_config.get_config('meme_enable').data}")
        print(f"  meme_auto_collect    = {meme_config.get_config('meme_auto_collect').data}")
        print(f"  消息里图片数          = {len(ev.image_list or [])}")


if __name__ == "__main__":
    asyncio.run(main())
