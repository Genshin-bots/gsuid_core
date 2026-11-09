"""BEAM per-conversation CLI: probe / judge / report.

Usage:
  python eval/BEAM_official/run_beam.py reprobe --split 100k --conv-idx 0
  python eval/BEAM_official/run_beam.py judge --split 100k --conv-idx 0
  python eval/BEAM_official/run_beam.py report --split 100k
"""

from __future__ import annotations

import sys
import asyncio
import argparse
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.BEAM_official import run_official as ro  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BEAM probe / judge / report")
    parser.add_argument("command", choices=("reprobe", "judge", "report"))
    parser.add_argument("--split", default="100k", choices=ro.SCALE_ORDER)
    parser.add_argument("--conv-idx", type=int, default=None)
    parser.add_argument("--base-url", default=ro.DEFAULT_BASE_URL)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=ro.DEFAULT_TIMEOUT)
    return parser


async def main_async(args: argparse.Namespace) -> int:
    spec = ro._spec(str(args.split))
    if args.command == "report":
        with ro.results_writer(spec):
            ro.write_scale_report(spec)
            ro.write_ladder_report()
        return 0

    conv = args.conv_idx
    if conv is None or conv < 0 or conv >= spec.n_conv:
        print(f"[run_beam] --conv-idx must be 0..{spec.n_conv - 1}", flush=True)
        return 2

    with ro.results_writer(spec):
        if args.command == "reprobe":
            ro._unmark_probe(spec, conv)
            rc = await ro.cmd_probe_conv(
                spec,
                str(args.base_url).rstrip("/"),
                conv,
                float(args.timeout),
                concurrency=int(args.concurrency),
            )
            if rc == 0:
                ro.write_scale_report(spec)
            return rc

        rc = await ro.cmd_judge_conv(
            spec,
            str(args.base_url).rstrip("/"),
            conv,
            float(args.timeout),
            concurrency=int(args.concurrency),
        )
        if rc == 0:
            ro._mark(spec, "finish", conv)
            ro.write_scale_report(spec)
        return rc


def main() -> int:
    ro.force_utf8_stdio()
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
