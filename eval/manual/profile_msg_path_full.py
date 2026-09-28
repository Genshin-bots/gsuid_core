"""整条消息路径逐环节实测：把 handle_event 的每一步单独拎出来计时。

为什么不用整进程 cProfile：core 空转就有 ~1 核的定时任务/心跳，绝对排名会被
启动 import 和后台作业淹没，而 Windows 上又难做优雅停机让 profile 落盘。
这里改成"逐环节直接调真实函数"，每个数字都可复现、可单独复跑。

用法：
    uv run python eval/manual/profile_msg_path_full.py
"""

from __future__ import annotations

import time
import asyncio
from collections.abc import Callable

from gsuid_core.models import Event, Message

ROUNDS = 200
COLD = "今天天气真不错啊大家吃了吗"


def _ev(text: str = COLD) -> Event:
    e = Event("ProfBot", "99999", "p-1", "group", "99000004", "99000004", {"nickname": "p"}, 6)
    e.raw_text = text
    e.text = text
    e.content = [Message(type="text", data=text)]
    return e


def _bench(fn: Callable[[], object], rounds: int = ROUNDS) -> float:
    """返回每次的毫秒。"""
    start = time.perf_counter()
    for _ in range(rounds):
        fn()
    return (time.perf_counter() - start) / rounds * 1000


async def main() -> None:
    rows: list[tuple[str, float, str]] = []

    # ── 0. 基线：空循环的计时噪声 ──
    ev = _ev()
    rows.append(("计时器自身噪声（空循环）", _bench(lambda: None, 2000), "参照"))

    # ── 1. Event 构建（msg_process 的核心）──
    def build() -> int:
        e = Event("ProfBot", "99999", "p-1", "group", "99000004", "99000004", {"nickname": "p"}, 6)
        e.raw_text += COLD
        return len(e.raw_text)

    rows.append(("① 构建 Event / msg_process", _bench(build), "纯内存"))

    # ── 2. 候选索引（本次优化项）──
    from gsuid_core.sv import SL, SV
    from gsuid_core.trigger import Trigger
    from gsuid_core.trigger_index import TriggerIndex
    from eval.manual.bench_trigger_match import collect_real_specs

    async def _noop(bot, e):  # noqa: ANN001, ANN202
        return None

    specs = collect_real_specs()
    tl: dict[str, dict[str, Trigger]] = {}
    trigs: list[Trigger] = []
    for tname, kw in specs:
        b = tl.setdefault(tname, {})
        for p in ("", "原神", "崩铁", "鸣潮"):
            tr = Trigger(tname, kw, _noop, p, False, False)  # type: ignore[arg-type]
            b[p + kw] = tr
            trigs.append(tr)
    sv = SV.__new__(SV, "__prof__")
    sv.name = "__prof__"
    sv.TL = tl
    saved = dict(SL.lst)
    SL.lst.clear()
    SL.lst[sv.name] = sv
    index = TriggerIndex()
    rows.append(("② 命令匹配 · 索引（改后）", _bench(lambda: index.candidates(ev)), f"{len(trigs)} 触发器"))
    rows.append(("② 命令匹配 · 线性（改前）", _bench(lambda: [t for t in trigs if t.check_command(ev)]), "对照"))

    # ── 3. 用户/群记账（改为缓冲后应为纯内存）──
    import gsuid_core.handler as handler

    handler._user_buffer.clear()
    handler._group_buffer.clear()
    handler._ensure_flush_task_started = lambda: None  # type: ignore[assignment]
    t = time.perf_counter()
    for _ in range(ROUNDS):
        handler._user_buffer.clear()
        handler._group_buffer.clear()
    rows.append(
        (
            "③ 用户/群记账（缓冲后）",
            _bench(lambda: handler._schedule_user_group_write("b", "u", "g", "n", "i")),
            "纯内存，零 IO",
        )
    )
    del t

    # ── 4. 历史入库 ──
    try:
        from gsuid_core.message_history import get_history_manager

        hm = get_history_manager()

        def hist() -> None:
            hm.add_message(
                event=ev,
                role="user",
                content=COLD,
                user_name="p",
                user_avatar=None,
                metadata={"msg_id": "p-1", "bot_id": "ProfBot", "user_type": "group"},
            )

        rows.append(("④ 历史入库 add_message", _bench(hist), "含 DB"))
    except Exception as e:  # noqa: BLE001 - 诊断脚本，取不到就标注跳过
        rows.append(("④ 历史入库 add_message", -1.0, f"跳过: {type(e).__name__}"))

    # ── 5. AI 入站 hook 扇出 ──
    try:
        from gsuid_core.ai_core.kits import load_enabled_kits
        from gsuid_core.ai_core.hooks.models import AgentHookContext
        from gsuid_core.ai_core.hooks.points import AgentHookPoint
        from gsuid_core.ai_core.hooks.dispatch import fire_hooks, should_fire

        await load_enabled_kits()
        fires = should_fire(AgentHookPoint.ON_INBOUND)
        ctx = AgentHookContext(
            point=AgentHookPoint.ON_INBOUND,
            ev=ev,
            session_id=ev.session_id,
            create_by="Chat",
            query=COLD,
        )

        def hooks() -> None:
            asyncio.get_event_loop()

        # fire_hooks 是协程，用事件循环实测
        loop = asyncio.get_event_loop()
        start = time.perf_counter()
        for _ in range(ROUNDS):
            await fire_hooks(AgentHookPoint.ON_INBOUND, ctx)
        per = (time.perf_counter() - start) / ROUNDS * 1000
        del hooks, loop
        rows.append(("⑤ AI 入站 hook 扇出", per, f"should_fire={fires}"))
    except Exception as e:  # noqa: BLE001
        rows.append(("⑤ AI 入站 hook 扇出", -1.0, f"跳过: {type(e).__name__}: {e}"[:60]))

    SL.lst.clear()
    SL.lst.update(saved)

    print("=" * 88)
    print("单条消息各环节耗时（ms/条，2026-09-28 实机配置：30+ 插件 / AI 开 / 记忆开）")
    print("=" * 88)
    print(f"{'环节':<34}{'ms/条':>10}{'备注':<28}")
    for name, per, note in rows:
        shown = "跳过" if per < 0 else f"{per:.4f}"
        print(f"{name:<34}{shown:>10}  {note[:26]}")

    print()
    print("=" * 88)
    print("不在上表、但同样吃时间的（来自实机日志与进程采样）")
    print("=" * 88)
    print("  ⑥ 命令体本身（渲染/查图库）    日志实测 单条 help = 11~14 秒")
    print("  ⑦ core 空闲后台作业            进程采样 ~1.0~1.6 核（心跳/定时/TTL/自检）")
    print("  ⑧ 启动期模块 import             整进程 profile 里 import 占绝对大头")


if __name__ == "__main__":
    asyncio.run(main())
