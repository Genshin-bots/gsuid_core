"""分块跑 agent 评测：core 挂了可续跑，结果增量合并到 OUT。"""

from __future__ import annotations

import os
import sys
import json
import time
import subprocess
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).parent / "results"
_token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
if not _token:
    raise SystemExit("GSUID_LOCAL_TEST_TOKEN is required (no fallback)")
TOKEN = _token
BASE = "http://127.0.0.1:8765"
CHUNK = int(os.environ.get("EVAL_CHUNK", "35"))
CONCURRENCY = int(os.environ.get("EVAL_CONCURRENCY", "2"))
OUT = RESULTS / Path(os.environ.get("EVAL_OUT_NAME", "_optimized_v2_full.json"))


def core_up() -> bool:
    try:
        r = httpx.post(
            f"{BASE}/api/chat_with_history",
            headers={"X-Local-Test-Token": TOKEN},
            json={
                "user_id": "health",
                "message": "ping",
                "history": [],
                "enable_tools": False,
                "enable_observer": False,
            },
            timeout=30,
        )
        return r.status_code == 200
    except Exception:
        return False


def ensure_core() -> None:
    if core_up():
        return
    print("[chunked] core down, restarting...")
    env = os.environ.copy()
    env.update(
        {
            "GSUID_LOCAL_TEST_MODE": "1",
            "GSUID_LOCAL_TEST_TOKEN": TOKEN,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "NO_PROXY": "localhost,127.0.0.1",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
        }
    )
    subprocess.run([sys.executable, str(Path(__file__).parent / "_cancel_pending.py")], cwd=ROOT, check=False)
    log_out = RESULTS / "_core_chunk_stdout.log"
    log_err = RESULTS / "_core_chunk_stderr.log"
    with open(log_out, "ab") as so, open(log_err, "ab") as se:
        subprocess.Popen(
            ["uv", "run", "core", "--dev"],
            cwd=str(ROOT),
            env=env,
            stdout=so,
            stderr=se,
        )
    for i in range(90):
        time.sleep(2)
        if core_up():
            print(f"[chunked] core up after ~{i * 2}s")
            return
    raise RuntimeError("core failed to start")


def is_transport_fail(r: dict) -> bool:
    fails = str(r.get("fails", ""))
    sample = str(r.get("sample", ""))
    blob = fails + sample
    return any(
        x in blob
        for x in (
            "All connection",
            "connection attempts",
            "session_log_not_found",
            "api:",
            "ConnectError",
        )
    )


def aggregate(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r.get("case_pass"))
    by_dom: dict[str, dict[str, int]] = {}
    for r in results:
        d = str(r.get("domain", "?"))
        slot = by_dom.setdefault(d, {"pass": 0, "total": 0})
        slot["total"] += 1
        if r.get("case_pass"):
            slot["pass"] += 1
    tools = [float(r.get("avg_tools") or 0) for r in results]
    lats = [float(r["avg_latency"]) for r in results if r.get("avg_latency")]
    return {
        "total_cases": total,
        "passed_cases": passed,
        "pass_rate": (passed / total) if total else 0.0,
        "by_domain": {
            d: {
                "pass": v["pass"],
                "total": v["total"],
                "rate": (v["pass"] / v["total"]) if v["total"] else 0.0,
            }
            for d, v in sorted(by_dom.items())
        },
        "avg_tools_per_case": round(sum(tools) / len(tools), 2) if tools else 0.0,
        "avg_latency_s": round(sum(lats) / len(lats), 1) if lats else 0.0,
    }


def load_existing() -> dict[str, dict]:
    if not OUT.exists():
        return {}
    doc = json.loads(OUT.read_text(encoding="utf-8"))
    return {r["id"]: r for r in doc.get("results") or [] if "id" in r}


def save_merged(by_id: dict[str, dict]) -> None:
    results = list(by_id.values())
    doc = {"summary": aggregate(results), "results": results}
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    s = doc["summary"]
    print(f"[chunked] merged {s['passed_cases']}/{s['total_cases']} = {s['pass_rate'] * 100:.1f}%")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    os.environ["GSUID_LOCAL_TEST_TOKEN"] = TOKEN
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["PYTHONUTF8"] = "1"

    from eval.agent.run import load_cases

    cases_dir = Path(__file__).parent / "cases"
    _, cases = load_cases(
        cases_dir / "agent_hard_suite.yaml",
        extra=[
            cases_dir / "group_chat_expansion.yaml",
            cases_dir / "group_chat_prod_patterns.yaml",
            cases_dir / "cognition_hub_mixed.yaml",
            cases_dir / "speaker_slot_recall.yaml",
        ],
    )
    active = [c for c in cases if not c.get("needs_fixture")]
    print(f"[chunked] active={len(active)} chunk={CHUNK}")

    by_id = load_existing()
    # retry transport failures
    for cid, r in list(by_id.items()):
        if is_transport_fail(r) or (not r.get("case_pass") and not r.get("fails") and not r.get("sample")):
            by_id.pop(cid, None)

    pending = [c for c in active if c["id"] not in by_id]
    print(f"[chunked] pending={len(pending)} already_ok={len(by_id)}")

    chunk_i = 0
    while pending:
        ensure_core()
        batch = pending[:CHUNK]
        pending = pending[CHUNK:]
        chunk_i += 1
        out_p = RESULTS / f"_opt_chunk_{chunk_i}.json"
        only = ",".join(c["id"] for c in batch)
        print(f"[chunked] chunk {chunk_i}: n={len(batch)} -> {out_p.name}")
        cmd = [
            sys.executable,
            "-m",
            "eval.agent.run",
            "--k",
            "1",
            "--judge",
            "bot",
            "--concurrency",
            str(CONCURRENCY),
            "--wait",
            "90",
            "--only",
            only,
            "--out",
            str(out_p),
        ]
        env = os.environ.copy()
        env["GSUID_LOCAL_TEST_TOKEN"] = TOKEN
        env["NO_PROXY"] = "localhost,127.0.0.1"
        env["PYTHONUTF8"] = "1"
        rc = subprocess.call(cmd, cwd=str(ROOT), env=env)
        print(f"[chunked] chunk {chunk_i} rc={rc} core_up={core_up()}")
        if out_p.exists():
            doc = json.loads(out_p.read_text(encoding="utf-8"))
            for r in doc.get("results") or []:
                if is_transport_fail(r):
                    # leave for retry
                    continue
                by_id[r["id"]] = r
            save_merged(by_id)
            # re-queue transport fails
            failed_ids = {r["id"] for r in doc.get("results") or [] if is_transport_fail(r)}
            if failed_ids:
                print(f"[chunked] requeue transport fails: {len(failed_ids)}")
                pending = [c for c in active if c["id"] in failed_ids] + pending

    s = aggregate(list(by_id.values()))
    print("\n===== FINAL =====")
    print(f"总通过率: {s['passed_cases']}/{s['total_cases']} = {s['pass_rate'] * 100:.1f}%")
    print(f"平均工具数/例: {s['avg_tools_per_case']}   平均延迟: {s['avg_latency_s']}s")
    for d, v in s["by_domain"].items():
        print(f"  {d:24s} {v['pass']}/{v['total']}  ({v['rate'] * 100:.0f}%)")
    print(f"报告: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
