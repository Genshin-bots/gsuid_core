"""LongMemEval-S 清库重灌战役：按域 ingest + probe + judge。

口径对齐 docs/BENCHMARK.md 生产 Chat（评测助手 + 目录卡 + search_cognition + clock_at），
但 **不** skip-ingest：每题 clear 后再 batch_observe。不覆盖历史 prod7 答卷。

  uv run python eval/manual/lme_fresh.py ping
  uv run python eval/manual/lme_fresh.py reset
  uv run python eval/manual/lme_fresh.py smoke
  uv run python eval/manual/lme_fresh.py domain --qtype single-session-preference
  uv run python eval/manual/lme_fresh.py all
"""

from __future__ import annotations

import os
import sys
import json
import asyncio
import argparse
from typing import Any
from argparse import Namespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import httpx  # noqa: E402

from eval.common import DEFAULT_BASE_URL, load_json, load_eval_data, call_clear_user_global  # noqa: E402
from eval.run_eval import (  # noqa: E402
    LM_DOMAIN_ORDER,
    _lm_load,
    _lm_judge,
    _lm_probe,
    _lm_report,
    _domain_files,
)

TAG = "fresh"
OUT_DIR = os.path.join(_ROOT, "eval", "longmemeval", "results")
PROGRESS = os.path.join(OUT_DIR, "fresh_progress.json")
REPORT = os.path.join(OUT_DIR, "fresh_report.md")

# 2026-09-03 prod7（skip-ingest）对照，不是本战役门槛。
PROD7 = {
    "single-session-preference": (27, 30),
    "single-session-user": (69, 70),
    "single-session-assistant": (54, 56),
    "knowledge-update": (76, 78),
    "multi-session": (118, 133),
    "temporal-reasoning": (118, 133),
}

SMOKE_N = 3
INGEST_TIMEOUT = 4000.0
JUDGE_TIMEOUT = 240.0
CONCURRENCY = 1


def _progress() -> dict[str, Any]:
    if os.path.isfile(PROGRESS):
        raw = load_json(PROGRESS)
        if isinstance(raw, dict):
            return raw
    return {"reset": False, "smoke": {}, "domains": {}}


def _save_progress(doc: dict[str, Any]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = PROGRESS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS)


def _make_args(
    *,
    base_url: str,
    qtype: str | None,
    start: int | None,
    end: int | None,
    skip_ingest: bool,
) -> Namespace:
    answers, judge = (
        _domain_files(qtype or "smoke", TAG)
        if qtype
        else (
            os.path.join(OUT_DIR, f"answers_smoke_{TAG}.json"),
            os.path.join(OUT_DIR, f"judge_smoke_{TAG}.json"),
        )
    )
    if qtype is None:
        answers = os.path.join(OUT_DIR, f"answers_smoke_{TAG}.json")
        judge = os.path.join(OUT_DIR, f"judge_smoke_{TAG}.json")
    return Namespace(
        extract=False,
        system2=False,
        inject_date=True,
        clear_first=True,
        skip_ingest=skip_ingest,
        question_type=qtype,
        enable_tools=True,
        no_memory_eval=True,
        persona_name="评测助手",
        concurrency=CONCURRENCY,
        answers_file=answers,
        judge_file=judge,
        timeout=INGEST_TIMEOUT,
        base_url=base_url,
        eval_data=None,
        start=start,
        end=end,
        tag=TAG,
    )


async def cmd_ping(base_url: str) -> int:
    url = f"{base_url.rstrip('/')}/api/chat_with_history"
    payload = {
        "user_id": "lme_fresh_ping",
        "message": "ping",
        "history": [],
        "persona_name": "评测助手",
        "enable_observer": False,
        "enable_tools": False,
    }
    headers: dict[str, str] = {}
    token = os.getenv("GSUID_LOCAL_TEST_TOKEN", "")
    if token:
        headers["X-Local-Test-Token"] = token
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.post(url, json=payload, headers=headers)
    print(f"[ping] chat_with_history={resp.status_code} body={resp.text[:240]}")
    if resp.status_code != 200:
        return 2
    print("[ping] OK")
    return 0


async def cmd_reset(base_url: str) -> int:
    """按题 HTTP DELETE。前缀批量 IN 会撞 SQLite 变量上限（500 个 eval_ scope）。"""
    from eval.longmemeval.run_longmem_eval import resolve_eval_data_path

    data = load_eval_data(resolve_eval_data_path())
    qids: list[str] = []
    for row in data:
        if isinstance(row, dict) and "question_id" in row:
            qids.append(str(row["question_id"]))
    print(f"[reset] HTTP clear {len(qids)} eval_* scopes", flush=True)
    ok = 0
    fail = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
        for i, qid in enumerate(qids, start=1):
            uid = f"eval_{qid}"
            resp = await call_clear_user_global(client, base_url, uid, timeout=180.0)
            st = resp["status"] if isinstance(resp, dict) and "status" in resp else 1
            if st == 0:
                ok += 1
            else:
                fail += 1
                print(f"[reset] fail {uid} {resp}", flush=True)
            if i % 25 == 0 or i == len(qids):
                print(f"[reset] {i}/{len(qids)} ok={ok} fail={fail}", flush=True)
    print(f"[reset] done ok={ok} fail={fail}", flush=True)
    if fail:
        return 2
    doc = _progress()
    doc["reset"] = True
    _save_progress(doc)
    if not await asyncio.to_thread(os.path.isfile, REPORT):
        _append_report(
            [
                "# LongMemEval-S fresh（清库重灌）",
                "",
                "口径：评测助手 + `--enable-tools` + `--no-memory-eval` + `--inject-date` + `--clear-first`。",
                "不 skip-ingest。答卷后缀 `_fresh`，不覆盖 prod7。",
                "",
            ]
        )
    return 0


def _summarize_judge(path: str) -> tuple[int, int]:
    recs = load_json(path)
    if not isinstance(recs, list):
        return 0, 0
    passed = 0
    total = 0
    for r in recs:
        if not isinstance(r, dict):
            continue
        j = r["judge"] if "judge" in r else {}
        if not isinstance(j, dict):
            continue
        total += 1
        if bool(j["passed"]) if "passed" in j else False:
            passed += 1
    return passed, total


def _append_report(lines: list[str]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(REPORT, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _domain_verdict(qtype: str, passed: int, total: int) -> str:
    base = PROD7[qtype] if qtype in PROD7 else (0, 1)
    rate = passed / total if total else 0.0
    base_rate = base[0] / base[1] if base[1] else 0.0
    if total < 1:
        return "empty"
    if rate + 0.08 < base_rate or rate < 0.70:
        return "weak"
    return "ok"


async def cmd_smoke(base_url: str) -> int:
    args = _make_args(
        base_url=base_url,
        qtype="single-session-preference",
        start=0,
        end=SMOKE_N,
        skip_ingest=False,
    )
    args.answers_file = os.path.join(OUT_DIR, f"answers_smoke_{TAG}.json")
    args.judge_file = os.path.join(OUT_DIR, f"judge_smoke_{TAG}.json")
    print(f"[smoke] {SMOKE_N} 道 SSP，clear+ingest+评测助手+tools")
    await _lm_probe(args)
    if not await asyncio.to_thread(os.path.isfile, args.answers_file):
        print("[smoke] 没有答卷，停下。")
        return 2
    args.timeout = JUDGE_TIMEOUT
    await _lm_judge(args)
    passed, total = _summarize_judge(args.judge_file)
    print(f"[smoke] {passed}/{total}")
    _lm_report(args.judge_file)
    doc = _progress()
    doc["smoke"] = {"passed": passed, "total": total}
    _save_progress(doc)
    _append_report(
        [
            "## smoke (SSP 前 3 题)",
            f"- **{passed}/{total}**",
            "",
        ]
    )
    if total < SMOKE_N or passed < 1:
        print("[smoke] 未通过，停下查原因，不要开全量。")
        return 2
    return 0


async def cmd_domain(base_url: str, qtype: str) -> int:
    if qtype not in LM_DOMAIN_ORDER:
        print(f"[domain] 未知类别 {qtype}，可选 {LM_DOMAIN_ORDER}")
        return 2
    args = _make_args(base_url=base_url, qtype=qtype, start=None, end=None, skip_ingest=False)
    n = len(_lm_load(args))
    print(f"[domain] {qtype} n={n} clear+ingest")
    await _lm_probe(args)
    args.timeout = JUDGE_TIMEOUT
    await _lm_judge(args)
    passed, total = _summarize_judge(args.judge_file)
    _lm_report(args.judge_file)
    verdict = _domain_verdict(qtype, passed, total)
    base = PROD7[qtype] if qtype in PROD7 else (0, 0)
    print(f"[domain] {qtype} {passed}/{total} verdict={verdict} prod7={base[0]}/{base[1]}")
    doc = _progress()
    domains = doc["domains"] if "domains" in doc and isinstance(doc["domains"], dict) else {}
    domains[qtype] = {
        "passed": passed,
        "total": total,
        "verdict": verdict,
        "answers": args.answers_file,
        "judge": args.judge_file,
    }
    doc["domains"] = domains
    _save_progress(doc)
    _append_report(
        [
            f"## {qtype}",
            f"- 本战役（清库重灌）: **{passed}/{total}**",
            f"- prod7 skip-ingest 对照: {base[0]}/{base[1]}",
            f"- verdict: {verdict}",
            "",
        ]
    )
    if verdict == "weak":
        print("[domain] 低于对照，跑 diagnose 抽失败题。")
        from eval.run_eval import _lm_diagnose

        dargs = Namespace(
            answers_file=args.answers_file,
            judge_file=args.judge_file,
            eval_data=None,
        )
        _lm_diagnose(dargs)
        return 2
    return 0


async def cmd_all(base_url: str) -> int:
    doc = _progress()
    if not bool(doc["reset"]) if "reset" in doc else True:
        rc = await cmd_reset(base_url)
        if rc:
            return rc
        doc = _progress()
    smoke = doc["smoke"] if "smoke" in doc and isinstance(doc["smoke"], dict) else {}
    if not smoke:
        rc = await cmd_smoke(base_url)
        if rc:
            return rc
        doc = _progress()
    domains = doc["domains"] if "domains" in doc and isinstance(doc["domains"], dict) else {}
    for qtype in LM_DOMAIN_ORDER:
        prev = domains[qtype] if qtype in domains and isinstance(domains[qtype], dict) else {}
        done_ok = bool(prev) and prev["verdict"] == "ok" if "verdict" in prev else False
        prev_total = int(prev["total"]) if "total" in prev else 0
        prev_passed = int(prev["passed"]) if "passed" in prev else 0
        if done_ok and prev_total > 0:
            print(f"[all] skip done {qtype} {prev_passed}/{prev_total}")
            continue
        rc = await cmd_domain(base_url, qtype)
        if rc:
            print(f"[all] 停在 {qtype} rc={rc}")
            return rc
        doc = _progress()
        domains = doc["domains"] if "domains" in doc and isinstance(doc["domains"], dict) else {}
    print("[all] 6 域完成")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LongMemEval-S 清库重灌（按域）")
    p.add_argument("stage", choices=("ping", "reset", "smoke", "domain", "all"))
    p.add_argument("--qtype", default=None, choices=LM_DOMAIN_ORDER)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    return p


async def main_async(args: argparse.Namespace) -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    base = args.base_url.rstrip("/")
    if args.stage == "ping":
        return await cmd_ping(base)
    if args.stage == "reset":
        return await cmd_reset(base)
    if args.stage == "smoke":
        return await cmd_smoke(base)
    if args.stage == "domain":
        if not args.qtype:
            print("[domain] 需要 --qtype")
            return 2
        return await cmd_domain(base, args.qtype)
    return await cmd_all(base)


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
