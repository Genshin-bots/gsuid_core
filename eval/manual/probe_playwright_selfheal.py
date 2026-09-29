"""端到端自愈验证：把浏览器目录指到空目录，真实跑一次自动下载。

会真的下载约 650 MiB（chromium + headless shell + ffmpeg），别在生产机器上随手跑。
实测参考：playwright 1.58.0 / Windows，空目录 → 下载完成约 32s。

用法::

    python eval/manual/probe_playwright_selfheal.py
    GSUID_PLAYWRIGHT_AUTOINSTALL=0 python eval/manual/probe_playwright_selfheal.py  # 关闭分支
"""

import os
import asyncio
import tempfile
from pathlib import Path

from gsuid_core.utils.playwright_autofix import ensure_chromium


def _report(root: Path) -> None:
    for child in sorted(root.iterdir()):
        size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
        print(f"  {child.name}: {size / 1024 / 1024:.1f} MiB")


def main() -> None:
    fresh = Path(tempfile.mkdtemp(prefix="pw-browsers-"))
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(fresh)
    print(f"PLAYWRIGHT_BROWSERS_PATH={fresh}")
    print(f"before: {sorted(p.name for p in fresh.iterdir())}")

    asyncio.run(ensure_chromium())

    print(f"after:  {sorted(p.name for p in fresh.iterdir())}")
    _report(fresh)
    print(f"browsers path kept at: {fresh}")


main()
