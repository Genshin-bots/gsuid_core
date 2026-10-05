"""确认 taffy float panic 的两个关键性质：确定性复现 + 是否打死进程。

用法::

    uv run python eval/manual/taffy_float_panic_verify.py
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taffy_float_panic_probe import build_html  # noqa: E402

HIT_IDX = 623
HIT_SEED = 0

CHILD = """
import sys, pathlib
sys.path.insert(0, r"{d}")
from taffy_float_panic_probe import build_html
import pytakumi
html = build_html({seed}, {idx})
try:
    pytakumi.html_to_pic(html, width=800, height=None, format="png",
                         renderer=pytakumi.Renderer())
except BaseException as e:
    print("PYTHON_EXC", type(e).__module__ + "." + type(e).__name__)
    sys.exit(9)
print("RENDER_OK")
sys.exit(0)
"""


def main() -> int:
    html = build_html(HIT_SEED, HIT_IDX)
    print("=" * 70)
    print("命中用例 HTML（seed=%d idx=%d）:" % (HIT_SEED, HIT_IDX))
    print("=" * 70)
    print(html)
    print("=" * 70)

    code = CHILD.format(d=str(Path(__file__).resolve().parent), seed=HIT_SEED, idx=HIT_IDX)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(f"returncode = {proc.returncode}")
    print(f"stdout     = {proc.stdout.strip()!r}")
    print("stderr:")
    for line in proc.stderr.strip().splitlines():
        print(f"  {line}")

    killed = proc.returncode not in (0, 9)
    print()
    if killed and "PYTHON_EXC" not in proc.stdout:
        print(">>> 结论：panic 未被转成 Python 异常，进程被直接 abort（returncode=%d）" % proc.returncode)
        print(">>> 这意味着一次 render_html_to_image 就能带走整个 core 进程。")
    elif "PYTHON_EXC" in proc.stdout:
        print(">>> 结论：panic 被转成 Python 异常，理论上可被 try/except 捕获")
    else:
        print(">>> 结论：本次未复现（不稳定？）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
