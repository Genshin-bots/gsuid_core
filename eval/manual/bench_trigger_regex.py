"""正则路径微基准：索引化之后 regex 成了新瓶颈，这里量三种改法。

现行 _check_regex 用 re.findall(pattern, rest) 只为取一个 bool，且每次都过
re._compile 的缓存查找。测三种写法在"每条消息都要跑一遍"这个场景下的差。
"""

from __future__ import annotations

import re
import time

PATTERNS = [
    r"^(\d+)?(练度)$",
    r"^(\d+)?(攻略)$",
    r"帮助",
    r"[0-9]+",
    r"^(?P<a>\w+)?(查询)(?P<b>\d+)$",
    r"^原神(帮助|抽卡)",
    r"\d{3,}",
    r"^(武器|角色)一览$",
]
BODY = "今天天气真不错啊大家吃了吗大家都在聊游戏的事情"
COMPILED = [re.compile(p) for p in PATTERNS]
ROUNDS = 30000


def bench(fn) -> float:
    start = time.perf_counter()
    for _ in range(ROUNDS):
        fn()
    return (time.perf_counter() - start) / ROUNDS * 1e6


def main() -> None:
    n = len(PATTERNS)
    print(f"{n} 个正则，body {len(BODY)} 字，测 {ROUNDS} 轮\n")

    a = bench(lambda: [bool(re.findall(p, BODY)) for p in PATTERNS])
    b = bench(lambda: [re.search(p, BODY) is not None for p in PATTERNS])
    c = bench(lambda: [rx.search(BODY) is not None for rx in COMPILED])
    d = bench(lambda: [rx.findall(BODY) != [] for rx in COMPILED])

    print(f"{'写法':<46}{'每次(µs)':>10}{'相对现状':>10}")
    print(f"{'re.findall(pattern, s) -> bool  [现状]':<46}{a:>10.2f}{'1.00x':>10}")
    print(f"{'re.search(pattern, s) is not None':<46}{b:>10.2f}{a / b:>9.2f}x")
    print(f"{'precompiled.search(s) is not None':<46}{c:>10.2f}{a / c:>9.2f}x")
    print(f"{'precompiled.findall(s) != []':<46}{d:>10.2f}{a / d:>9.2f}x")

    print("\n含义：")
    print(f"  现状每条消息 {a:.2f}µs；改 precompiled.search 后 {c:.2f}µs")
    print("  真实部署 regex 触发器约 40 个（20 条命令 x 前缀展开），")
    print(f"  即每条消息 {a * 40 / n:.1f}µs -> {c * 40 / n:.1f}µs，省 {(a - c) * 40 / n:.1f}µs")
    print("  注：findall/search 语义等价（都只用于取 bool），但 precompile")
    print("  省掉的是每次调用都要走的 re._cache 查找与 _compile 分派。")

    # 字面量首字可预判性：能安全收进首字桶的比例
    print("\n各正则能否安全抽出「必为首字」的字面量（用于首字预筛）：")
    prefilterable = 0
    for p in PATTERNS:
        m = re.match(r"\^([^\^\$\\\[\(\?\)\*\|\+\{\}]{1,})", p)
        lit = m.group(1) if m else None
        if lit:
            prefilterable += 1
        print(f"  {p:<40}字面前缀 = {lit!r}")
    print(f"  可预筛 {prefilterable}/{n} —— 覆盖率低且规则脆弱，抽错了会漏匹配，不建议作为主路径。")


if __name__ == "__main__":
    main()
