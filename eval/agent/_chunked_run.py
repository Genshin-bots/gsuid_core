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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS = Path(__file__).parent / "results"
CHUNK = int(os.environ.get("EVAL_CHUNK", "35"))
CONCURRENCY = int(os.environ.get("EVAL_CONCURRENCY", "2"))
MAX_RETRY = int(os.environ.get("EVAL_MAX_RETRY", "2"))
RESTART_EVERY = int(os.environ.get("EVAL_RESTART_EVERY", "0"))
RETRY_CONTAMINATED = os.environ.get("EVAL_RETRY_CONTAMINATED", "0") == "1"
OUT = RESULTS / Path(os.environ.get("EVAL_OUT_NAME", "_optimized_v2_full.json"))
BASE = "http://127.0.0.1:8765"
_TRANSPORT_MARKERS = (
    "All connection",
    "connection attempts",
    "session_log_not_found",
    "api:",
    "ConnectError",
    "Connection refused",
    "WinError 10061",
)


def _eval_token() -> str:
    token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    if not token:
        raise SystemExit("GSUID_LOCAL_TEST_TOKEN is required (no fallback)")
    return token


def core_up() -> bool:
    try:
        r = httpx.post(
            f"{BASE}/api/chat_with_history",
            headers={"X-Local-Test-Token": _eval_token()},
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


def _pids_on_port(port: int) -> list[int]:
    r = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True,
        check=False,
    )
    raw = r.stdout or b""
    text = raw.decode("utf-8", "replace")
    if "LISTENING" not in text.upper():
        text = raw.decode("gbk", "replace")
    pids: list[int] = []
    needle = f":{port}"
    for line in text.splitlines():
        if needle not in line or "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def kill_core() -> None:
    for pid in _pids_on_port(8765):
        print(f"[chunked] kill pid {pid} on :8765")
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], check=False)
    time.sleep(2)


def _core_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GSUID_LOCAL_TEST_MODE": "1",
            "GSUID_LOCAL_TEST_TOKEN": _eval_token(),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "NO_PROXY": "localhost,127.0.0.1",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
        }
    )
    return env


def _spawn_core() -> int:
    subprocess.run([sys.executable, str(Path(__file__).parent / "_cancel_pending.py")], cwd=ROOT, check=False)
    log_out = RESULTS / "_core_chunk_stdout.log"
    log_err = RESULTS / "_core_chunk_stderr.log"
    RESULTS.mkdir(parents=True, exist_ok=True)
    off = log_out.stat().st_size if log_out.exists() else 0
    with open(log_out, "ab") as so, open(log_err, "ab") as se:
        subprocess.Popen(
            ["uv", "run", "core", "--port", "8765"],
            cwd=str(ROOT),
            env=_core_env(),
            stdout=so,
            stderr=se,
        )
    return off


def _wait_core(log_off: int) -> None:
    log_out = RESULTS / "_core_chunk_stdout.log"
    synced = False
    for i in range(90):
        time.sleep(2)
        if not synced and log_out.exists():
            chunk = log_out.read_bytes()[log_off:].decode("utf-8", "ignore")
            if "工具同步完成" in chunk:
                print("[chunked] tools synced")
                synced = True
        if core_up():
            print(f"[chunked] core up after ~{i * 2}s synced={synced}")
            return
    raise RuntimeError("core failed to start")


def restart_core() -> None:
    print("[chunked] restarting core...")
    kill_core()
    off = _spawn_core()
    _wait_core(off)


def ensure_core() -> None:
    if core_up():
        return
    print("[chunked] core down, starting...")
    off = _spawn_core()
    _wait_core(off)


def flatten_fails(r: dict) -> list[str]:
    out: list[str] = []
    for item in r.get("fails") or []:
        if isinstance(item, list):
            out.extend(str(x) for x in item)
        else:
            out.append(str(item))
    return out


def is_transport_fail(r: dict) -> bool:
    # 只扫 fails：sample 是用户可见回复，里面出现 "api:" 不算传输故障。
    blob = " ".join(flatten_fails(r))
    return any(x in blob for x in _TRANSPORT_MARKERS)


def is_contaminated(r: dict) -> bool:
    if r.get("status") == "judge_error":
        return True
    if r.get("case_pass"):
        return False
    if is_transport_fail(r):
        return True
    fs = flatten_fails(r)
    if fs and all(x.strip() == "judge: judge" for x in fs):
        return True
    return False


def aggregate(results: list[dict]) -> dict:
    from eval.agent.harness import aggregate as harness_agg

    base = harness_agg(results)
    tools = [float(r.get("avg_tools") or 0) for r in results]
    lats = [float(r["avg_latency"]) for r in results if r.get("avg_latency")]
    base["avg_tools_per_case"] = round(sum(tools) / len(tools), 2) if tools else 0.0
    base["avg_latency_s"] = round(sum(lats) / len(lats), 1) if lats else 0.0
    return base


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
    scored = int(s["scored_cases"]) if "scored_cases" in s else int(s["total_cases"])
    print(f"[chunked] merged {s['passed_cases']}/{scored} = {float(s['pass_rate']) * 100:.1f}%")


def main() -> int:
    os.environ["GSUID_LOCAL_TEST_TOKEN"] = _eval_token()
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
    print(f"[chunked] active={len(active)} chunk={CHUNK} restart_every={RESTART_EVERY}")

    by_id = load_existing()
    dropped = 0
    for cid, r in list(by_id.items()):
        drop = is_transport_fail(r)
        if RETRY_CONTAMINATED and is_contaminated(r):
            drop = True
        if (not r.get("case_pass")) and (not r.get("fails")) and (not r.get("sample")):
            drop = True
        if drop:
            by_id.pop(cid, None)
            dropped += 1
    pending = [c for c in active if c["id"] not in by_id]
    print(f"[chunked] pending={len(pending)} kept={len(by_id)} dropped={dropped}")

    retry_counts: dict[str, int] = {}
    chunk_i = 0
    while pending:
        chunk_i += 1
        if chunk_i == 1 or (RESTART_EVERY > 0 and (chunk_i - 1) % RESTART_EVERY == 0):
            restart_core()
        else:
            ensure_core()
        batch = pending[:CHUNK]
        pending = pending[CHUNK:]
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
            "--no-reset-state",
            "--only",
            only,
            "--out",
            str(out_p),
        ]
        env = os.environ.copy()
        env["GSUID_LOCAL_TEST_TOKEN"] = _eval_token()
        env["NO_PROXY"] = "localhost,127.0.0.1"
        env["PYTHONUTF8"] = "1"
        rc = subprocess.call(cmd, cwd=str(ROOT), env=env)
        print(f"[chunked] chunk {chunk_i} rc={rc} core_up={core_up()}")
        if not out_p.exists():
            print("[chunked] missing chunk out, requeue whole batch")
            pending = batch + pending
            restart_core()
            continue
        doc = json.loads(out_p.read_text(encoding="utf-8"))
        requeue_ids: set[str] = set()
        for r in doc.get("results") or []:
            rid = str(r["id"])
            if not is_transport_fail(r):
                by_id[rid] = r
                continue
            n = retry_counts[rid] if rid in retry_counts else 0
            if n >= MAX_RETRY:
                by_id[rid] = r
                print(f"[chunked] keep transport fail {rid} (retries exhausted)")
                continue
            retry_counts[rid] = n + 1
            requeue_ids.add(rid)
        save_merged(by_id)
        if requeue_ids:
            print(f"[chunked] requeue transport fails: {len(requeue_ids)}")
            pending = [c for c in active if c["id"] in requeue_ids] + pending
            restart_core()

    s = aggregate(list(by_id.values()))
    scored = int(s["scored_cases"]) if "scored_cases" in s else int(s["total_cases"])
    je = int(s["judge_error_cases"]) if "judge_error_cases" in s else 0
    print("\n===== FINAL =====")
    print(
        f"总通过率: {s['passed_cases']}/{scored} = {float(s['pass_rate']) * 100:.1f}%"
        f"  （judge_error {je} 不进分母；total={s['total_cases']}）"
    )
    print(f"平均工具数/例: {s['avg_tools_per_case']}   平均延迟: {s['avg_latency_s']}s")
    print(
        f"token input={s.get('input_tokens', 0)}  output={s.get('output_tokens', 0)}  "
        f"cache_read={s.get('cache_read_tokens', 0)}  cache_rate={float(s.get('cache_rate') or 0) * 100:.1f}%"
    )
    for d, v in s["by_domain"].items():
        print(f"  {d:24s} {v['pass']}/{v['total']}  ({float(v['rate']) * 100:.0f}%)")
    print(f"报告: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
