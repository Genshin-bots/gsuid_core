"""Official BEAM 100k reprobe watchdog.

Keeps Core and ``run_official.py reprobe --scale 100k`` alive until 20 convs finish.
Does not clear or re-ingest. Probe/judge concurrency defaults to 11 (MiniMax-M3 only).
"""

from __future__ import annotations

import os
import json
import time
import socket
import subprocess
from pathlib import Path
from datetime import datetime

_ROOT = Path(__file__).resolve().parents[2]
_OUT = _ROOT / "eval" / "BEAM_official" / "results" / "_ladder"
_RES = _ROOT / "eval" / "BEAM_official" / "results" / "100k"
_PY = _ROOT / ".venv" / "Scripts" / "python.exe"
_LOG = _OUT / "watchdog_100k.log"
_CAMPAIGN_OUT = _OUT / "run100k.out"
_CAMPAIGN_ERR = _OUT / "run100k.err"
_INTERVAL_S = 90
_STALL_S = 45 * 60
_HOST = "127.0.0.1"
_PORT = 8765
_CONCURRENCY = "11"
_STAMP = _OUT / "run100k.stamp"

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_BREAKAWAY_FROM_JOB = 0x01000000
_SPAWN_FLAGS = _CREATE_NEW_PROCESS_GROUP | _BREAKAWAY_FROM_JOB


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    line = f"{_now()} {msg}"
    print(line, flush=True)
    with _LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _tcp_up() -> bool:
    try:
        with socket.create_connection((_HOST, _PORT), timeout=2.0):
            return True
    except OSError:
        return False


def _token() -> str:
    tok = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    if tok:
        return tok
    cmd = _OUT / "start_core.cmd"
    if cmd.is_file():
        for line in cmd.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("set GSUID_LOCAL_TEST_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def _api_up(tok: str) -> bool:
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if tok:
        headers["X-Local-Test-Token"] = tok
    try:
        req = urllib.request.Request(
            f"http://{_HOST}:{_PORT}/api/ai/memory/batch_observe",
            data=b"{}",
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return int(resp.status) != 404
    except urllib.error.HTTPError as e:
        return int(e.code) != 404
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return False


def _pids(needle: str) -> list[int]:
    import psutil

    out: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.Error, OSError):
            continue
        if needle in cmd:
            out.append(int(proc.info["pid"]))
    return out


def _kill_pids(pids: list[int]) -> None:
    import psutil

    for pid in pids:
        try:
            psutil.Process(pid).terminate()
        except (psutil.Error, OSError):
            continue
    procs = [psutil.Process(pid) for pid in pids if psutil.pid_exists(pid)]
    _gone, alive = psutil.wait_procs(procs, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.Error, OSError):
            continue


def _progress() -> dict[str, list[int]]:
    path = _RES / "progress.json"
    if not path.is_file():
        return {"ingest": [], "probe": [], "judge": [], "finish": []}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {"ingest": [], "probe": [], "judge": [], "finish": []}
    out: dict[str, list[int]] = {}
    for key in ("ingest", "probe", "judge", "finish"):
        ids = raw[key] if key in raw and isinstance(raw[key], list) else []
        out[key] = [int(x) for x in ids if isinstance(x, int)]
    return out


def _campaign_done() -> bool:
    prog = _progress()
    if len(prog["finish"]) < 20:
        return False
    if not _STAMP.is_file():
        return False
    report = _RES / "report.md"
    if not report.is_file() or report.stat().st_mtime + 1 < _STAMP.stat().st_mtime:
        return False
    if _CAMPAIGN_OUT.is_file():
        text = _CAMPAIGN_OUT.read_text(encoding="utf-8", errors="replace")
        if "[reprobe] 128K 完成" in text or "[all] 128K 20 conv × 20 完成" in text:
            return True
    for ln in report.read_text(encoding="utf-8", errors="replace").splitlines():
        if ln.startswith("**总分：") and "/400" in ln:
            return True
    return False


def _start_core() -> None:
    cmd = _OUT / "start_core.cmd"
    if not cmd.is_file():
        _log("start_core.cmd missing")
        return
    quoted = str(cmd).replace("'", "''")
    cwd = str(_ROOT).replace("'", "''")
    ps = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -Namespace root\\cimv2 "
        "-MethodName Create -Arguments @{ CommandLine = 'cmd.exe /c "
        + quoted
        + "'; CurrentDirectory = '"
        + cwd
        + "' }; Write-Output ($r.ReturnValue.ToString() + ' ' + $r.ProcessId.ToString())"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)


def _spawn_campaign(tok: str) -> None:
    _CAMPAIGN_OUT.parent.mkdir(parents=True, exist_ok=True)
    out_f = _CAMPAIGN_OUT.open("ab")
    err_f = _CAMPAIGN_ERR.open("ab")
    env = os.environ.copy()
    env["GSUID_LOCAL_TEST_MODE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_PROXY"] = "localhost,127.0.0.1"
    env["no_proxy"] = "localhost,127.0.0.1"
    if tok:
        env["GSUID_LOCAL_TEST_TOKEN"] = tok
    args = [
        str(_PY),
        "-u",
        str(_ROOT / "eval" / "BEAM_official" / "run_official.py"),
        "--concurrency",
        _CONCURRENCY,
        "reprobe",
        "--scale",
        "100k",
    ]
    subprocess.Popen(
        args,
        cwd=str(_ROOT),
        env=env,
        stdout=out_f,
        stderr=err_f,
        stdin=subprocess.DEVNULL,
        creationflags=_SPAWN_FLAGS,
    )


def _log_age_s() -> float | None:
    newest: float | None = None
    for path in (_CAMPAIGN_OUT, _CAMPAIGN_ERR):
        if path.is_file():
            age = time.time() - path.stat().st_mtime
            newest = age if newest is None else min(newest, age)
    return newest


def _eval_running() -> bool:
    return bool(_pids("run_official.py"))


def main() -> int:
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    tok = _token()
    _log(f"100k watchdog start token_set={bool(tok)} concurrency={_CONCURRENCY}")
    idle = 0
    while True:
        if _campaign_done():
            _log("100k all 20 convs finished; watchdog exit")
            return 0
        prog = _progress()
        if not _api_up(tok):
            cores = _pids("gsuid_core.core")
            if cores:
                _log(f"core pid={cores} still starting; wait api finish={prog['finish']}")
            else:
                _log(f"core/api down; restarting core finish={prog['finish']}")
                _start_core()
            time.sleep(15)
            continue
        if _eval_running():
            age = _log_age_s()
            if age is not None and age > _STALL_S:
                _log(f"campaign stall age={age:.0f}s; restart run_official")
                _kill_pids(_pids("run_official.py"))
                time.sleep(3)
                _spawn_campaign(tok)
                idle = 0
            else:
                idle += 1
                if idle % 10 == 0:
                    _log(
                        f"alive ingest={len(prog['ingest'])} probe={len(prog['probe'])} "
                        f"finish={len(prog['finish'])} log_age={None if age is None else round(age)}"
                    )
            time.sleep(_INTERVAL_S)
            continue
        _log(f"campaign not running; spawning reprobe --scale 100k finish={prog['finish']} ingest={prog['ingest']}")
        _spawn_campaign(tok)
        idle = 0
        time.sleep(_INTERVAL_S)


if __name__ == "__main__":
    raise SystemExit(main())
