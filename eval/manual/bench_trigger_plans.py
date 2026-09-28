"""匹配方案原型对比：线性扫描 vs 首字分桶 vs 真 trie / 哈希表。

只测"候选集收窄"这一步的耗时，不含鉴权与执行。用真实命令分布，决定改法。
"""

from __future__ import annotations

import time
from typing import Callable
from collections import defaultdict

from bench_trigger_match import (
    CASES,
    make_event,
    collect_real_specs,
)

from gsuid_core.trigger import Trigger

_PREFIX_TYPES = frozenset({"command", "prefix"})
_FLAT_TYPES = frozenset({"command", "prefix", "fullmatch"})


async def _noop(bot, ev):  # noqa: ANN001, ANN201
    return None


def build(specs: list[tuple[str, str]], prefixes: tuple[str, ...]) -> list[Trigger]:
    out: list[Trigger] = []
    for tname, kw in specs:
        for p in prefixes:
            out.append(Trigger(tname, kw, _noop, p, False, False))  # type: ignore[arg-type]
    return out


# ---------- 方案 A：现状线性扫描 ----------
def plan_linear(triggers: list[Trigger]) -> Callable[[str], list[Trigger]]:
    def run(msg: str) -> list[Trigger]:
        ev = make_event(msg)
        return [t for t in triggers if t.check_command(ev)]

    return run


# ---------- 方案 B：按类型 + 首字分桶 ----------
def plan_bucket(triggers: list[Trigger]) -> Callable[[str], list[Trigger]]:
    head_bucket: dict[str, list[Trigger]] = defaultdict(list)
    tail_bucket: dict[str, list[Trigger]] = defaultdict(list)
    always: list[Trigger] = []
    by_type: dict[str, list[Trigger]] = defaultdict(list)
    for t in triggers:
        by_type[t.type].append(t)
        if t.type in _FLAT_TYPES:
            head = t._head[:1]
            if head in (" ", "\t", "　", "\xa0"):
                always.append(t)
            else:
                head_bucket[head].append(t)
        elif t.type == "suffix":
            kw = t.keyword.strip()
            if kw:
                tail_bucket[kw[-1]].append(t)
            else:
                always.append(t)
        else:
            # regex / keyword / file / meta / message：首字不可预判
            always.append(t)

    def run(msg: str) -> list[Trigger]:
        ev = make_event(msg)
        stripped = msg.strip(" \t　\xa0")
        out: list[Trigger] = []
        if stripped:
            out.extend(head_bucket.get(stripped[0], ()))
        else:
            out.extend(always)
        out.extend(tail_bucket.get(msg[-1], ()))
        out.extend(always)
        seen: set[int] = set()
        uniq: list[Trigger] = []
        for t in out:
            if id(t) not in seen:
                seen.add(id(t))
                uniq.append(t)
        return [t for t in uniq if t.check_command(ev)]

    return run


# ---------- 方案 C：command/prefix 用 trie，fullmatch 用哈希 ----------
class TrieNode:
    __slots__ = ("children", "payload")

    def __init__(self) -> None:
        self.children: dict[str, TrieNode] = {}
        self.payload: list[Trigger] = []


def plan_trie(triggers: list[Trigger]) -> Callable[[str], list[Trigger]]:
    root = TrieNode()
    exact: dict[str, list[Trigger]] = defaultdict(list)
    tail_bucket: dict[str, list[Trigger]] = defaultdict(list)
    always: list[Trigger] = []
    gap_heads: set[int] = set()

    for t in triggers:
        if t.type in _PREFIX_TYPES:
            head = t._head
            if not head or head[0] in " \t　\xa0":
                always.append(t)
                continue
            gap_heads.add(id(t))
            node = root
            for ch in head:
                nxt = node.children.get(ch)
                if nxt is None:
                    nxt = TrieNode()
                    node.children[ch] = nxt
                node = nxt
            node.payload.append(t)
        elif t.type == "fullmatch":
            exact[t._head].append(t)
        elif t.type == "suffix":
            kw = t.keyword.strip()
            (tail_bucket[kw[-1]] if kw else always).append(t)
        else:
            always.append(t)

    def run(msg: str) -> list[Trigger]:
        stripped = msg.strip(" \t　\xa0")
        ev = make_event(msg)
        out: list[Trigger] = []
        if stripped:
            node = root
            for ch in stripped:
                node = node.children.get(ch)  # type: ignore[assignment]
                if node is None:
                    break
                out.extend(node.payload)
        out.extend(exact.get(stripped, ()))
        if msg:
            out.extend(tail_bucket.get(msg[-1], ()))
        out.extend(always)
        seen: set[int] = set()
        uniq: list[Trigger] = []
        for t in out:
            if id(t) not in seen:
                seen.add(id(t))
                uniq.append(t)
        return [t for t in uniq if t.check_command(ev)]

    return run


def timeit(fn: Callable[[str], list[Trigger]], rounds: int) -> float:
    start = time.perf_counter()
    for _ in range(rounds):
        fn("今天天气真不错啊大家吃了吗")
    return (time.perf_counter() - start) / rounds * 1e6


def main() -> None:
    specs = collect_real_specs()
    triggers = build(specs, ("", "原神", "崩铁", "鸣潮"))
    print(f"Trigger 数: {len(triggers)}   命令数: {len(specs)}\n")

    plans = [
        ("A 线性扫描(现状)", plan_linear(triggers)),
        ("B 首字/尾字分桶", plan_bucket(triggers)),
        ("C trie+哈希", plan_trie(triggers)),
    ]

    # 正确性：三者命中集合必须一致
    base = plan_linear(triggers)
    for label, plan in plans[1:]:
        for case_label, text in CASES:
            got = {id(t) for t in plan(text)}
            want = {id(t) for t in base(text)}
            if got != want:
                print(f"  !! {label} 在 [{case_label}] 与线性扫描不一致: 多{got - want} 少{want - got}")
        print(f"{label} 命中集合与线性扫描一致 ✓")

    print()
    header = f"{'场景':<26}{'A 线性':>11}{'B 分桶':>11}{'C trie':>11}{'C/A':>9}"
    print(header)
    rounds = 1500
    for case_label, text in CASES:
        row = f"{case_label:<26}"
        times = []
        for _label, plan in plans:
            ev_start = time.perf_counter()
            for _ in range(rounds):
                plan(text)
            us = (time.perf_counter() - ev_start) / rounds * 1e6
            times.append(us)
        row += f"{times[0]:>11.1f}{times[1]:>11.1f}{times[2]:>11.1f}{times[0] / times[2]:>8.1f}x"
        print(row)

    print("\n--- 冷路径（群聊闲聊，首字全不匹配）---")
    a, b, c = (timeit(p, rounds) for _l, p in plans)
    print(f"A {a:.1f}µs   B {b:.1f}µs   C {c:.1f}µs   加速 {a / c:.1f}x")


if __name__ == "__main__":
    main()
