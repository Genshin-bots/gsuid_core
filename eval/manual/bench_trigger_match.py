"""触发器匹配开销基准：量出当前线性扫描的真实成本。

不重建世界、不连数据库：只从插件源码里抽出真实命令串，构造等量的 Trigger 对象，
再按 handler.py 的循环形状计时。目的是回答"值不值得上 trie"，不是回归测试。

用法（在仓库根目录）：
    uv run python eval/manual/bench_trigger_match.py
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

# 逐轮换的入参：命中 / 首字命中但词不匹配 / 完全不相关
CASES: list[tuple[str, str]] = [
    ("命中命令", "原神帮助"),
    ("带参数", "原神抽卡记录 90 温迪"),
    ("群聊闲聊(首字都对不上)", "今天天气真不错啊大家吃了吗"),
    ("英文不相关", "hello everyone how are you doing today"),
    ("纯前缀无正文", "原神"),
]


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


def collect_real_specs() -> list[tuple[str, str]]:
    """从插件源码抽出 (type, keyword)，抽不到就丢弃。"""
    specs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in _iter_py_files():
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(src)
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                kind = DECORATOR_RE.search(dec.func.attr)
                if not kind:
                    continue
                tname = kind.group(1)
                if tname == "message" or tname == "meta":
                    continue
                if not dec.args:
                    continue
                first = dec.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value:
                    spec = (tname, first.value)
                    if spec not in seen:
                        seen.add(spec)
                        specs.append(spec)
    return specs


async def _noop(bot, ev):  # noqa: ANN001, ANN201
    return None


def build_triggers(specs: list[tuple[str, str]], prefixes: tuple[str, ...]) -> list[Trigger]:
    triggers: list[Trigger] = []
    for tname, kw in specs:
        for p in prefixes:
            triggers.append(Trigger(tname, kw, _noop, p, False, False))  # type: ignore[arg-type]
    return triggers


def make_event(text: str) -> Event:
    ev = Event("bench", "bench", "1", "group", "10000", "20000", {"nickname": "bench"})
    ev.raw_text = text
    ev.text = text
    return ev


def scan(triggers: list[Trigger], ev: Event) -> int:
    """复刻 handler.py 的内层循环形状。"""
    hit = 0
    for tr in triggers:
        if tr.check_command(ev):
            hit += 1
    return hit


def timeit(fn, rounds: int) -> tuple[float, float]:
    """返回 (总秒数, 每条消息微秒)。"""
    start = time.perf_counter()
    for _ in range(rounds):
        fn()
    elapsed = time.perf_counter() - start
    return elapsed, elapsed / rounds * 1e6


def main() -> None:
    specs = collect_real_specs()
    print(f"从插件源码抽到的真实命令: {len(specs)} 条")
    print("类型分布:", dict(Counter(t for t, _ in specs).most_common()))

    prefixes = ("", "原神", "崩铁", "鸣潮")
    triggers = build_triggers(specs, prefixes)
    print(f"按 4 个前缀展开后的 Trigger 数: {len(triggers)}\n")

    rounds = 2000
    print(f"{'场景':<28}{'命中数':>7}{'每条消息(µs)':>16}")
    for label, text in CASES:
        ev = make_event(text)
        hits = scan(triggers, ev)
        _, per = timeit(lambda: scan(triggers, ev), rounds)
        print(f"{label:<28}{hits:>7}{per:>16.1f}")

    # 分解：授权判定 vs check_command 的占比
    print("\n--- 成本分解 ---")
    ev = make_event("今天天气真不错啊大家吃了吗")
    _, per = timeit(lambda: scan(triggers, ev), rounds)
    print(f"{'纯 check_command 线性扫描':<28}{'':>7}{per:>16.1f}")

    first_chars = {t._probe[:1] for t in triggers if t._probe}
    _, per = timeit(lambda: ev.raw_text[:1] in first_chars, rounds)
    print(f"{'单次 dict 查表(参照)':<28}{'':>7}{per:>16.1f}")

    print(f"\n内存: {len(triggers)} 个 Trigger 对象")


if __name__ == "__main__":
    main()
