"""带周期落盘的 cProfile 包装器：跑 core 并每隔几秒把 profile 写到文件。

为什么不用 `python -m cProfile -m gsuid_core.core`：那样 profile 只在**正常退出**时落盘，
而 Windows 上优雅停机很难保证。本包装器在后台线程里定期 dump，压测期间随时可读。

注意：cProfile 只 profile 调用 enable() 的那个线程（这里是主线程 = uvicorn 事件循环），
`asyncio.to_thread` 里跑的同步函数不在其中。

用法：
    uv run python eval/manual/run_core_profiled.py <输出路径> [间隔秒] -- <core 参数...>
"""

from __future__ import annotations

import sys
import cProfile
import threading
from pathlib import Path


def main() -> None:
    argv = sys.argv[1:]
    if "--" not in argv:
        print(__doc__)
        raise SystemExit(1)
    split = argv.index("--")
    out = Path(argv[0])
    every = float(argv[1]) if len(argv) > 1 and not argv[1].startswith("--") else 5.0
    sys.argv = ["core", *argv[split + 1 :]]

    out.parent.mkdir(parents=True, exist_ok=True)
    prof = cProfile.Profile()
    stop = threading.Event()

    def dumper() -> None:
        while not stop.wait(every):
            try:
                prof.dump_stats(str(out))
            except Exception:  # noqa: BLE001 - dump 失败不能拖垮主进程
                pass

    threading.Thread(target=dumper, daemon=True).start()
    prof.enable()
    try:
        import asyncio

        from gsuid_core.core import main as core_main

        asyncio.run(core_main())
    finally:
        prof.disable()
        stop.set()
        prof.dump_stats(str(out))
        print(f"profile -> {out}")


if __name__ == "__main__":
    main()
