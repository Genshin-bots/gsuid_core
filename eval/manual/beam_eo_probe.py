"""EO-only 快速 A/B：只跑 event_ordering 题（20 conv × 2 = 40 题）。

答卷/判分写到 ``_eo_answers_{conv}.json`` / ``_eo_judge_{conv}.json``，不动主结果文件。
用法（.venv python）：python eval/manual/beam_eo_probe.py [--conv 0,1,2] [--keep]
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


async def _run(convs: list[int], base_url: str, timeout: float, keep: bool) -> int:
    spec = ro._spec("100k")
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    if not await ro._wait_core(host, port, timeout=600.0):
        print("[eo] core 未就绪", flush=True)
        return 2
    total = 0
    for conv in convs:
        row = ro.load_beam_row(conv, ro._data_glob(spec), columns=["probing_questions", "chat"])
        probes = [p for p in ro.iter_probing_questions(row) if p[0] == "event_ordering"]
        if not probes:
            print(f"[eo] conv={conv} 无 EO 题", flush=True)
            continue
        answers = str(Path(ro._out_dir(spec)) / f"_eo_answers_{conv}.json")
        judge = str(Path(ro._out_dir(spec)) / f"_eo_judge_{conv}.json")
        if not keep:
            for path in (answers, judge):
                if os.path.isfile(path):
                    os.remove(path)
        fallback = ro._fallback_clock_from_chat(row["chat"] if "chat" in row else [])
        await ro.cmd_probe(
            base_url=base_url,
            user_id=ro._user_id(spec, conv),
            probes=probes,
            answers_file=answers,
            timeout=timeout,
            resume=True,
            fallback_clock=fallback,
        )
        await ro.cmd_judge(base_url=base_url, answers_file=answers, judge_file=judge, timeout=timeout, resume=True)
        recs = ro.load_json(judge)
        got = 0
        for rec in recs if isinstance(recs, list) else []:
            if not isinstance(rec, dict):
                continue
            jd = rec.get("judge")
            if isinstance(jd, dict) and bool(jd.get("passed")):
                got += 1
        total += got
        print(f"[eo] conv={conv} EO {got}/{len(probes)}", flush=True)
    print(f"[eo] EO TOTAL {total}/{len(convs) * 2}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="EO-only 快速评测")
    parser.add_argument("--conv", default="", help="逗号分隔 conv；留空=0..19")
    parser.add_argument("--base-url", default=ro.DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=ro.DEFAULT_TIMEOUT)
    parser.add_argument("--keep", action="store_true", help="保留已有答卷（续跑）")
    args = parser.parse_args()
    raw = str(args.conv).strip()
    convs = [int(x) for x in raw.split(",") if x.strip()] if raw else list(range(20))
    return asyncio.run(_run(convs, str(args.base_url).rstrip("/"), float(args.timeout), bool(args.keep)))


if __name__ == "__main__":
    raise SystemExit(main())
