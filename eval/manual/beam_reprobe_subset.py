"""BEAM official 子集复测：只重跑指定 conv 的 probe+judge（不 clear、不 ingest）。

用法（必须用 .venv 的 python）：
  .venv\\Scripts\\python.exe -u eval\\manual\\beam_reprobe_subset.py --scale 100k --conv 12,19

口径与 run_official.py 的 reprobe 完全一致（同一 cmd_probe_conv / cmd_judge_conv /
_unmark_probe / write_scale_report），只是把 conv 循环收窄成子集，用来快速迭代。
正式终值仍走 `run_official.py reprobe --scale 100k`。
"""

from __future__ import annotations

import os
import sys
import asyncio
import argparse
from pathlib import Path
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

from eval.BEAM_official import run_official as ro  # noqa: E402


def _parse_convs(raw: str) -> list[int]:
    convs: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        conv = int(part)
        if conv not in convs:
            convs.append(conv)
    return convs


async def _run(spec: ro.ScaleSpec, convs: list[int], base_url: str, timeout: float) -> int:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    ingested = set(ro._progress(spec)["ingest"])
    print(f"[subset] {spec.paper_name} convs={convs} ingested={sorted(ingested)}", flush=True)
    if not await ro._wait_core(host, port, timeout=600.0):
        print("[subset] core 未就绪，退出", flush=True)
        return 2
    if await ro.cmd_ping(base_url):
        print("[subset] ping 失败，退出", flush=True)
        return 2
    for conv in convs:
        if conv not in ingested:
            print(f"[subset] {spec.key} conv={conv} 未摄入，跳过", flush=True)
            continue
        print(f"\n========== subset {spec.paper_name} conv {conv} ==========", flush=True)
        ro._unmark_probe(spec, conv)
        rc = await ro.cmd_probe_conv(spec, base_url, conv, timeout)
        if rc:
            print(f"[subset] probe 停在 conv={conv} rc={rc}", flush=True)
            ro.write_scale_report(spec)
            ro.write_ladder_report()
            return rc
        rc = await ro.cmd_judge_conv(spec, base_url, conv, timeout)
        if rc:
            print(f"[subset] judge 停在 conv={conv} rc={rc}", flush=True)
            ro.write_scale_report(spec)
            ro.write_ladder_report()
            return rc
        ro._mark(spec, "finish", conv)
        ro.write_scale_report(spec)
    ro.write_ladder_report()
    print(f"[subset] {spec.paper_name} 子集完成", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BEAM official 子集复测（不 clear、不 ingest）")
    parser.add_argument("--scale", default="100k", choices=ro.SCALE_ORDER)
    parser.add_argument("--conv", default="", help="逗号分隔的 conv，如 12,19；留空=全部已摄入 conv")
    parser.add_argument("--base-url", default=ro.DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=ro.DEFAULT_TIMEOUT)
    args = parser.parse_args()
    spec = ro._spec(str(args.scale))
    raw = str(args.conv).strip()
    convs = _parse_convs(raw) if raw else list(range(spec.n_conv))
    if not convs or min(convs) < 0 or max(convs) >= spec.n_conv:
        print(f"[subset] conv 越界: {convs} (0..{spec.n_conv - 1})", flush=True)
        return 2
    return asyncio.run(_run(spec, convs, str(args.base_url).rstrip("/"), float(args.timeout)))


if __name__ == "__main__":
    raise SystemExit(main())
