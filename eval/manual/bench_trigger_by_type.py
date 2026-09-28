"""按触发器类型量化：现行线性扫描 vs 各自可用的索引，为方案文档取数。

产出三组数：
1. 真实插件命令的类型分布（AST 抽取 + 前缀展开后的 Trigger 数）
2. 每种类型在「未命中」这条冷路径上每次 check_command 的成本（线性扫描的固定税）
3. 各自索引方案能把这条税降到什么量级

用法（仓库根目录）：
    uv run python eval/manual/bench_trigger_by_type.py
"""

from __future__ import annotations

import re
import ast
import time
from pathlib import Path
from collections import Counter

from gsuid_core.models import Event
from gsuid_core.trigger import Trigger

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = [ROOT / "gsuid_core" / "plugins", ROOT / "gsuid_core" / "buildin_plugins"]
DECORATOR_RE = re.compile(r"on_(command|prefix|suffix|keyword|fullmatch|regex|file|message|meta)\b")
_GAP = " \t　\xa0"

# 每种类型「该用什么索引」以及「索引后每次消息还剩多少活」
PLAN: dict[str, tuple[str, str]] = {
    "command": ("字符 trie（走前缀）", "O(len(msg))，共享前缀越深收益越大"),
    "prefix": ("字符 trie（走前缀）", "同上"),
    "fullmatch": ("字符 trie（走前缀）", "整句相等最常见的形态，仍按前缀走即可"),
    "suffix": ("末字分桶 dict", "O(1) 定位到同末字的一小组"),
    "keyword": ("不可预判，兜底桶", "子串在任意位置，只能全量；好在 str.__contains__ 是 C 级"),
    "regex": ("不可预判，兜底桶", "只能抽字面量前缀做首字预筛，无法保证不漏"),
    "file": ("不可预判，兜底桶", "ext 集合可精确查，但触发器数量个位数"),
    "message": ("不可预判，兜底桶", "语义就是「每条消息都听」，天然 O(n)"),
    "meta": ("独立分发路径", "不走文本匹配，handler 已单独遍历"),
}

TYPES = list(PLAN)


async def _noop(bot, ev):  # noqa: ANN001, ANN201
    return None


def _iter_py_files() -> list[Path]:
    out: list[Path] = []
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            s = str(p)
            if "__pycache__" in s or ".venv" in s or "test_output" in s:
                continue
            out.append(p)
    return out


def collect_specs() -> list[tuple[str, str]]:
    """抽出 (type, keyword)。message/meta 的 keyword 是 uuid/事件名，单独计数。"""
    specs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                kind = DECORATOR_RE.search(dec.func.attr)
                if not kind or not dec.args:
                    continue
                tname = kind.group(1)
                if tname in ("message", "meta"):
                    specs.append((tname, ""))
                    continue
                first = dec.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value:
                    spec = (tname, first.value)
                    if spec not in seen:
                        seen.add(spec)
                        specs.append(spec)
    return specs


def make_event(text: str) -> Event:
    ev = Event("bench", "bench", "1", "group", "10000", "20000", {"nickname": "bench"})
    ev.raw_text = text
    ev.text = text
    return ev


def bench(fn, rounds: int) -> float:
    start = time.perf_counter()
    for _ in range(rounds):
        fn()
    return (time.perf_counter() - start) / rounds * 1e6


def main() -> None:
    specs = collect_specs()
    counts = Counter(t for t, _ in specs)
    # 典型插件前缀展开倍数（实测 _on 里 prefix 列表平均长度量级）
    PREFIX_EXPAND = 2
    print("=" * 92)
    print("1. 真实命令的类型分布（AST 扫描 gsuid_core/plugins + buildin_plugins）")
    print("=" * 92)
    total = sum(counts.values())
    for t in TYPES:
        n = counts.get(t, 0)
        print(f"  {t:<10}{n:>5} 条   展开后约 {n * PREFIX_EXPAND:>5} Trigger   {n / total * 100:>5.1f}%")
    print(f"  {'合计':<10}{total:>5} 条   展开后约 {total * PREFIX_EXPAND:>5} Trigger")

    # ── 每种类型单独造 200 个触发器，测冷路径单次 check_command 成本 ──
    print()
    print("=" * 92)
    print("2. 冷路径（消息完全不命中）单次 check_command 成本 —— 线性扫描的固定税")
    print("=" * 92)
    sample = {
        "command": [("原神", "帮助"), ("原神", "抽卡记录"), ("原神", "角色一览")],
        "prefix": [("原神", "查询")],
        "fullmatch": [("原神", "帮助")],
        "suffix": [("", "card图")],
        "keyword": [("", "关键词")],
        "regex": [("", r"^(\d+)?(练度)$"), ("", r"^(\d+)?(攻略)$")],
        "file": [("", "png")],
        "message": [("", "")],
    }
    cold = "今天天气真不错啊大家吃了吗"
    ev = make_event(cold)
    rows: list[tuple[str, float, float, float]] = []
    for t in TYPES:
        if t == "meta":
            continue
        seeds = sample.get(t, [("", "x")])
        pool: list[Trigger] = []
        i = 0
        while len(pool) < 200:
            pfx, kw = seeds[i % len(seeds)]
            suffix = "" if not kw else (kw if i // len(seeds) < 2 else f"{kw}{i}")
            pool.append(Trigger(t, suffix or "u", _noop, pfx, False, False))  # type: ignore[arg-type]
            i += 1
        per = bench(lambda: [x for x in pool if x.check_command(ev)], 400) / len(pool)
        rows.append((t, per, per * 200, per * 200 * PREFIX_EXPAND))
    print(f"  {'类型':<10}{'单次(µs)':>11}{'200条(µs)':>12}{'展开后(µs)':>13}")
    for t, per, tot, exp in rows:
        print(f"  {t:<10}{per:>11.3f}{tot:>12.1f}{exp:>13.1f}")

    # ── regex: findall vs search ──
    print()
    print("=" * 92)
    print("3. _check_regex 用 re.findall 只为取 bool —— 换 re.search 的差")
    print("=" * 92)
    body = "原神帮助今天天气真不错啊大家吃了吗"
    pats = [r"^(\d+)?(练度)$", r"帮助", r"[0-9]+", r"^(?P<a>\w+)?(查询)(?P<b>\d+)$"]
    fa = bench(lambda: [bool(re.findall(p, body)) for p in pats], 20000)
    se = bench(lambda: [re.search(p, body) is not None for p in pats], 20000)
    print(f"  {len(pats)} 个已编译缓存的正则：findall {fa:.2f}µs   search {se:.2f}µs   省 {fa - se:.2f}µs")
    print("  （正则数量少时绝对值小，但它是兜底桶里唯一不可省的 C 级调用）")

    # ── 索引后残留的兜底桶成本（按真实分布，不按合成 200 条）──
    print()
    print("=" * 92)
    print("4. 索引化后每条消息的残留成本（按真实分布）")
    print("=" * 92)
    unit = {t: per for t, per, _tot, _exp in rows}
    always_kinds = ("keyword", "regex", "file", "message")
    always_breakdown = []
    always_total = 0.0
    for k in always_kinds:
        n = counts.get(k, 0) * (1 if k in ("keyword", "message") else PREFIX_EXPAND)
        c = unit[k] * n
        always_total += c
        always_breakdown.append((k, n, c))
    print(f"  {'兜底类型':<10}{'条目':>7}{'单次(µs)':>12}{'小计(µs)':>12}")
    for k, n, c in always_breakdown:
        print(f"  {k:<10}{n:>7}{unit[k]:>12.3f}{c:>12.1f}")
    print(f"  {'合计':<10}{'':>7}{'':>12}{always_total:>12.1f}")
    print(f"  trie 走一遍 len(msg)={len(cold)} 约 2~5µs")
    print(f"  → 索引化后冷路径约 {always_total + 4:.1f}µs，其中 regex 占 {always_breakdown[1][2]:.1f}µs")

    print()
    print("  —— regex 若改为注册时 re.compile 预编译（实测 1.56x）——")
    rx_now = always_breakdown[1][2]
    rx_pre = rx_now / 1.56
    print(f"  regex 兜底 {rx_now:.1f}µs -> {rx_pre:.1f}µs，冷路径约 {always_total - rx_now + rx_pre + 4:.1f}µs")

    print()
    print("=" * 92)
    print("5. 每种类型该用什么索引")
    print("=" * 92)
    for t in TYPES:
        idx, gain = PLAN[t]
        print(f"  {t:<10}{idx:<26}{gain}")


if __name__ == "__main__":
    main()
