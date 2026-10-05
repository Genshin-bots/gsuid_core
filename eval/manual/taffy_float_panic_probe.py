"""探测 pytakumi/taffy float 布局 panic（taffy-0.11.0 compute/float.rs:213）。

taffy 的 ``FloatContext::subdivide_segment`` 带一条 debug 断言::

    assert!(old_segment.y.contains(&divide_at_y) && old_segment.y.start != divide_at_y)

只在 **CSS float** 布局里被调用，且是 f32 浮点比较：能否触发取决于
「某个 float 的下边缘是否恰好落在另一分段的起始边界上」，
属于精度敏感退化情形，手算推不出触发值 —— 用随机化穷举探针。

用法::

    uv run python eval/manual/taffy_float_panic_probe.py                 # 扫描
    uv run python eval/manual/taffy_float_panic_probe.py --seed 7 -n 3000 # 换种子/预算
    uv run python eval/manual/taffy_float_panic_probe.py --html           # 打印首个命中 HTML

每个候选在独立子进程中渲染：panic 若是 abort 型会直接打死整个进程，
父进程据此判定「该候选触发崩溃」并从下一个继续（一次崩溃只损失一个子进程）。
"""

from __future__ import annotations

import os
import sys
import random
import argparse
import tempfile
import subprocess
from pathlib import Path

# 半分/分数高度是重点嫌疑：line-height 1.5 @15px = 22.5px 一行，
# 文本撑出的高度天然是 x.5，与 float 分段边界做 f32 比较最容易差 1 ULP。
FRACS = (0, 10, 20, 22.5, 25, 30, 33.33, 40, 45, 50, 60, 66.67, 70, 75, 80, 100, 112.5, 120, 133.33, 150)
WIDTHS = (60, 80, 100, 120, 150, 180, 220)
SIDES = ("left", "right")
CLEARS = ("none", "left", "right", "both")


def build_html(seed: int, idx: int) -> str:
    # 每个 idx 独立随机源。崩溃后续跑若共用顺序 RNG，同一 idx 会生成另一份 HTML。
    # random.Random 不接受 tuple 种子，3.11 起会 TypeError。
    rnd = random.Random(f"{seed}:{idx}")
    n = rnd.randint(2, 5)
    parts: list[str] = []
    for _ in range(n):
        side = rnd.choice(SIDES)
        w = rnd.choice(WIDTHS)
        h = rnd.choice(FRACS)
        mt = rnd.choice(FRACS)
        clear = rnd.choice(CLEARS)
        style = f"float:{side};width:{w}px;height:{h}px;margin-top:{mt}px;clear:{clear};background:#345"
        parts.append(f'<div style="{style}"></div>')
    npara = rnd.randint(1, 3)
    body = "\n".join(parts)
    for i in range(npara):
        body += f'\n<p style="font-size:15px;line-height:1.5">第{i}段文字环绕 content {"填充" * rnd.randint(3, 40)}</p>'
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>\n'
        f"*{{margin:0;padding:0;box-sizing:border-box}}\n"
        f"body{{background:#fff;width:800px}}\n"
        f'</style></head><body>\n<div style="width:600px">\n{body}\n</div>\n</body></html>'
    )


def child(start: int, seed: int, n: int) -> int:
    """渲染 [start, start+n)，每个成功打一行 OK <idx>；panic 则进程直接死。"""
    import pytakumi

    renderer = pytakumi.Renderer()
    for i in range(start, start + n):
        pytakumi.html_to_pic(build_html(seed, i), width=800, height=None, format="png", renderer=renderer)
        print(f"OK {i}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=int, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("-n", "--budget", type=int, default=2000)
    parser.add_argument("--html", action="store_true", help="打印命中用例的 HTML")
    args = parser.parse_args()

    if args.child is not None:
        return child(args.child, args.seed, args.budget)

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    crashes: list[int] = []
    start = 0
    while start < args.budget:
        # stderr 落临时文件：避免和 stdout 抢管道导致死锁
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
            proc = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child",
                    str(start),
                    "--seed",
                    str(args.seed),
                    "-n",
                    str(args.budget),
                ],
                stdout=subprocess.PIPE,
                stderr=errf,
                text=True,
                encoding="utf-8",
                env=env,
            )
            last_ok = start - 1
            assert proc.stdout is not None
            for line in proc.stdout:
                if line.startswith("OK "):
                    last_ok = int(line.split()[1])
            proc.wait()
            errf.seek(0)
            err = errf.read()

        if proc.returncode == 0:
            print(f"[done] {args.budget} 个候选全部渲染完成，无崩溃")
            break

        crashed = last_ok + 1
        panicked = "panicked" in err or "PanicException" in err
        # 非 panic 的退出（探针自身 bug / 渲染报错）要和真崩溃区分开，否则会把
        # 「每个用例都失败」误读成「每个用例都触发 taffy bug」
        kind = "panic/abort" if panicked else f"exit={proc.returncode} 非panic"
        print(f"[crash] seed={args.seed} idx={crashed} ({kind})")
        tail = err.strip().splitlines()
        for line in tail[-6:] if panicked else tail[-4:]:
            print(f"        {line}")
        crashes.append(crashed)
        if args.html:
            print("--- HTML ---")
            print(build_html(args.seed, crashed))
            break
        if not panicked:
            print("        ^ 非 panic：探针或渲染侧报错，不是 taffy bug，停止扫描")
            break
        start = crashed + 1

    print(f"\n结论：{len(crashes)}/{args.budget} 个候选触发崩溃，命中 idx={crashes}")
    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(main())
