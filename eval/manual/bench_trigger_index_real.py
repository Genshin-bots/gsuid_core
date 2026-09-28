"""实测：索引化前后，真实插件命令分布下的匹配耗时与命中一致性。

用的是**线上真正的 TriggerIndex**，不是原型；命令分布从插件源码 AST 抽取。
每个场景同时跑「现行线性扫描」与「索引候选 + check_command」，并断言命中集合完全相等。

用法（仓库根目录）：
    uv run python eval/manual/bench_trigger_index_real.py
"""

from __future__ import annotations

import time
from collections.abc import Callable

from gsuid_core.sv import SL, SV
from gsuid_core.models import Event
from gsuid_core.trigger import Trigger
from gsuid_core.trigger_index import TriggerIndex
from eval.manual.bench_trigger_match import CASES, make_event, collect_real_specs

PREFIXES = ("", "原神", "崩铁", "鸣潮")
ROUNDS = 2000


class _StubPlugins:
    """只提供 index 遍历用到的 TL；不参与鉴权与执行。"""

    def __init__(self, tl: dict[str, dict[str, Trigger]]) -> None:
        self.TL = tl


async def _noop(bot, ev):  # noqa: ANN001, ANN202
    return None


def build_registry() -> tuple[list[Trigger], SV]:
    """把真实命令灌进一个临时 SV，让 TriggerIndex 能从 SL.lst 建索引。"""
    specs = collect_real_specs()
    tl: dict[str, dict[str, Trigger]] = {}
    all_triggers: list[Trigger] = []
    for tname, kw in specs:
        bucket = tl.setdefault(tname, {})
        for p in PREFIXES:
            if not tname.isidentifier() and tname in ("file", "message", "meta"):
                continue
            key = p + kw
            tr = Trigger(tname, kw, _noop, p, False, False)  # type: ignore[arg-type]
            bucket[key] = tr
            all_triggers.append(tr)

    sv = SV.__new__(SV, "__bench__")
    sv.name = "__bench__"
    sv.TL = tl
    sv.plugins = _StubPlugins(tl)  # type: ignore[assignment]
    return all_triggers, sv


def main() -> None:
    triggers, sv = build_registry()
    saved = dict(SL.lst)
    SL.lst.clear()
    SL.lst[sv.name] = sv
    try:
        index = TriggerIndex()
        total = len(triggers)
        print(f"真实命令展开后的 Trigger 数: {total}\n")

        # ---- 命中一致性：索引化后不能多命中也不能少命中 ----
        print("=== 命中一致性（索引 vs 现行线性扫描）===")
        all_equal = True
        for label, text in CASES:
            ev = make_event(text)
            linear = {id(t) for t in triggers if t.check_command(ev)}
            indexed = {id(t) for t in index.candidates(ev) if t.check_command(ev)}
            ok = linear == indexed
            all_equal = all_equal and ok
            print(f"  {label:<26}{'一致 ✓' if ok else f'不一致 ✗ 多{len(indexed - linear)} 少{len(linear - indexed)}'}")
        print(f"  → {'全部一致，行为无变化' if all_equal else '存在行为差异，必须排查'}\n")

        # ---- 耗时 ----
        def linear_loop(ev: Event) -> int:
            n = 0
            for tr in triggers:
                if tr.check_command(ev):
                    n += 1
            return n

        def index_loop(ev: Event) -> int:
            n = 0
            for tr in index.candidates(ev):
                if tr.check_command(ev):
                    n += 1
            return n

        print("=== 耗时（每条消息，µs）===")
        print(f"{'场景':<26}{'线性扫描':>12}{'索引化':>12}{'加速':>10}{'候选/全量':>12}")
        for label, text in CASES:
            ev = make_event(text)
            a, b = _time(linear_loop, ev), _time(index_loop, ev)
            cands = len(index.candidates(ev))
            print(f"{label:<26}{a:>12.1f}{b:>12.1f}{a / b:>9.1f}x{cands:>7}/{total:<5}")

        cold_ev = make_event("今天天气真不错啊大家吃了吗")
        a, b = _time(linear_loop, cold_ev), _time(index_loop, cold_ev)
        print(f"{'冷路径(闲聊)':<26}{a:>12.1f}{b:>12.1f}{a / b:>9.1f}x{len(index.candidates(cold_ev)):>7}/{total:<5}")
    finally:
        SL.lst.clear()
        SL.lst.update(saved)


def _time(fn: Callable[[Event], int], ev: Event) -> float:
    start = time.perf_counter()
    for _ in range(ROUNDS):
        fn(ev)
    return (time.perf_counter() - start) / ROUNDS * 1e6


if __name__ == "__main__":
    main()
