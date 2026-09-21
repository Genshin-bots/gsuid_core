"""对已摄入 scope 只补抽取（write_episodes=false）：产出 entities/edges/events，不重灌、不重复嵌入。

用法：.venv\\Scripts\\python.exe eval\\manual\\beam_extract_events.py --conv 0,12 [--scale 100k]
"""

from __future__ import annotations

import os
import sys
import asyncio
import argparse
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

import httpx  # noqa: E402

from eval.BEAM_official import run_official as ro  # noqa: E402
from eval.common.beam_runner import parse_time_anchor  # noqa: E402
from eval.common.http_client import call_batch_observe  # noqa: E402


async def _extract_one(spec: ro.ScaleSpec, base_url: str, conv: int, timeout: float, chunk_size: int) -> int:
    user_id = ro._user_id(spec, conv)
    row = ro.load_beam_row(conv, ro._data_glob(spec), columns=["chat"])
    plan = ro.chat_to_plan(row["chat"] if "chat" in row else [])
    turns = ro.extract_turns_from_plan(plan)
    payload: list[dict] = []
    for t in turns:
        item: dict = {"role": t["role"], "content": t["content"]}
        iso = parse_time_anchor(t["time_anchor"] if "time_anchor" in t else "")
        if iso:
            item["timestamp"] = iso
        payload.append(item)
    from eval.common.timestamps import spread_payload_timestamps

    payload = spread_payload_timestamps(payload)
    print(f"[extract] conv={conv} user_id={user_id} turns={len(payload)}", flush=True)
    added = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        n = len(payload)
        for start in range(0, n, chunk_size):
            chunk = payload[start : start + chunk_size]
            last = start + chunk_size >= n
            resp = await call_batch_observe(
                client=client,
                base_url=base_url,
                user_id=user_id,
                turns=chunk,
                scope_type="user_global",
                flush=last,
                trigger_rebuild=last,
                timeout=timeout,
                extra_payload={"extract": True, "write_episodes": False},
            )
            data = resp.get("data") if isinstance(resp, dict) else None
            ex = data.get("extract") if isinstance(data, dict) else None
            prog = f"{start // chunk_size + 1}/{(n + chunk_size - 1) // chunk_size}"
            print(f"[extract] conv={conv} chunk {prog} status={resp.get('status')} extract={ex}", flush=True)
            if not isinstance(resp, dict) or resp.get("status") != 0:
                return 2
            if isinstance(ex, dict) and "events_added" in ex:
                added += int(ex["events_added"])
    print(f"[extract] conv={conv} done", flush=True)
    return 0 if added >= 0 else 0


async def _run(convs: list[int], base_url: str, timeout: float, chunk_size: int) -> int:
    spec = ro._spec("100k")
    for conv in convs:
        rc = await _extract_one(spec, base_url, conv, timeout, chunk_size)
        if rc:
            print(f"[extract] 停在 conv={conv} rc={rc}", flush=True)
            return rc
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="只补抽取（不重灌）")
    parser.add_argument("--conv", required=True, help="逗号分隔 conv")
    parser.add_argument("--base-url", default=ro.DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=ro.DEFAULT_TIMEOUT)
    parser.add_argument("--chunk-size", type=int, default=200)
    args = parser.parse_args()
    convs = [int(x) for x in str(args.conv).replace(" ", "").split(",") if x.strip()]
    return asyncio.run(_run(convs, str(args.base_url).rstrip("/"), float(args.timeout), int(args.chunk_size)))


if __name__ == "__main__":
    raise SystemExit(main())
