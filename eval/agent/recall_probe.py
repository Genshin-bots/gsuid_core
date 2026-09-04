"""离线召回探针：打运行中 core 的 assemble_preview，确认关键问句能召回目标工具。

用法（core 已开 GSUID_LOCAL_TEST_MODE）：
  uv run python -m eval.agent.recall_probe --token $env:GSUID_LOCAL_TEST_TOKEN
"""

from __future__ import annotations

import os
import sys
import json
import argparse
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ENDPOINT = "/api/ai/tools/assemble_preview"

# 问句来自 eval/agent 用例，期望是「种子或展开池」里至少命中一个。
GOLD: list[tuple[str, frozenset[str]]] = [
    ("明天下午3点提醒我开会，就一次就行", frozenset({"add_once_task"})),
    ("3分钟后提醒我看一下锅里的汤", frozenset({"add_once_task"})),
    ("帮我设置每天早上8点提醒我喝水", frozenset({"add_interval_task"})),
    ("工作的时候每隔30分钟提醒我站起来活动一下", frozenset({"add_interval_task"})),
    ("以后每周一早上9点提醒我交周报", frozenset({"add_interval_task"})),
    ("我现在都设了哪些定时提醒？列给我看看", frozenset({"list_scheduled_tasks"})),
    (
        "把我那个每天吃药的提醒取消掉",
        frozenset({"cancel_scheduled_task", "list_scheduled_tasks"}),
    ),
    (
        "我那个喝水提醒，时间从8点改到9点",
        frozenset({"modify_scheduled_task", "list_scheduled_tasks"}),
    ),
    ("先把我的周报提醒暂停一段时间，别删", frozenset({"pause_scheduled_task", "list_scheduled_tasks"})),
    ("帮我查下今天天气怎么样", frozenset({"web_search_tool", "weather_handler", "get_weather"})),
]


def _hit_names(data: dict) -> set[str]:
    names: set[str] = set()
    for key in ("core", "seeds", "pool"):
        rows = data[key] if key in data else []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and "name" in row:
                names.add(str(row["name"]))
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--token", default=os.getenv("GSUID_LOCAL_TEST_TOKEN", ""))
    ap.add_argument("--out", default=str(Path(__file__).parent / "results" / "_recall_probe.json"))
    args = ap.parse_args()
    headers = {"X-Local-Test-Token": args.token} if args.token else {}
    rows: list[dict[str, object]] = []
    failed = 0
    with httpx.Client(timeout=60.0) as client:
        for query, want in GOLD:
            resp = client.post(
                f"{args.base_url.rstrip('/')}{_ENDPOINT}",
                headers=headers,
                json={"query": query},
            )
            if resp.status_code != 200:
                failed += 1
                rows.append({"query": query, "ok": False, "error": f"http {resp.status_code}"})
                print(f"FAIL  http {resp.status_code}  {query}")
                continue
            payload = resp.json()
            data = payload["data"] if isinstance(payload, dict) and "data" in payload else {}
            if not isinstance(data, dict):
                failed += 1
                rows.append({"query": query, "ok": False, "error": "bad payload"})
                print(f"FAIL  bad payload  {query}")
                continue
            hit = _hit_names(data)
            ok = bool(hit & want)
            if not ok:
                failed += 1
            core_n = data["core_pool_size"] if "core_pool_size" in data else None
            print(
                f"{'PASS' if ok else 'FAIL'}  want={sorted(want)}  hit={sorted(hit)[:12]}  core={core_n}  {query[:40]}"
            )
            rows.append(
                {
                    "query": query,
                    "want": sorted(want),
                    "hit": sorted(hit),
                    "ok": ok,
                    "core_pool_size": core_n,
                }
            )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"failed": failed, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"recall {len(GOLD) - failed}/{len(GOLD)}  wrote {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
