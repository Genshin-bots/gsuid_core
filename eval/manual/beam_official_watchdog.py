"""Official BEAM ladder watchdog: keep Core + 128K→10M campaign alive."""

from __future__ import annotations

import os
import sys
import time
import socket
import subprocess
from pathlib import Path
from datetime import datetime

_ROOT = Path(__file__).resolve().parents[2]
_OUT = _ROOT / "eval" / "BEAM_official" / "results" / "_ladder"
_LOG = _OUT / "watchdog.log"
_CAMPAIGN_OUT = _OUT / "campaign.out"
_CAMPAIGN_ERR = _OUT / "campaign.err"
_CORE_OUT = _OUT / "core.out"
_CORE_ERR = _OUT / "core.err"
_INTERVAL_S = 180
_STALL_S = 45 * 60
_STALL_REBUILD_S = 45 * 60
_HOST = "127.0.0.1"
_PORT = 8765
_VENV_PY = str(_ROOT / ".venv" / "Scripts" / "python.exe")
_NEEDLE = "run_official.py ladder"

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


def _eval_api_up() -> bool:
    if not _tcp_up():
        return False
    try:
        import urllib.error
        import urllib.request

        headers = {"Content-Type": "application/json"}
        token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
        if token:
            headers["X-Local-Test-Token"] = token
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


def _inherit_token() -> None:
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1")
    if os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip():
        return
    try:
        import psutil
    except ImportError:
        return
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.Error, OSError):
            continue
        if "gsuid_core.core" not in cmd:
            continue
        try:
            env = proc.environ()
        except (psutil.Error, OSError):
            continue
        tok = env.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
        if tok:
            os.environ["GSUID_LOCAL_TEST_TOKEN"] = tok
            return


def _pids_matching(needle: str) -> list[int] | None:
    try:
        import psutil
    except ImportError:
        _log("psutil missing")
        return None
    out: list[int] = []
    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            cmd = " ".join(proc.info["cmdline"] or [])
            if needle in cmd:
                out.append(int(proc.info["pid"]))
    except (psutil.Error, OSError) as e:
        _log(f"process query fail {e!r}")
        return None
    return out


def _kill_pids(pids: list[int]) -> None:
    try:
        import psutil
    except ImportError:
        for pid in pids:
            cmd = f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True)
        return
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


def _spawn(args: list[str], stdout_path: Path, stderr_path: Path) -> None:
    env = os.environ.copy()
    env["GSUID_LOCAL_TEST_MODE"] = "1"
    env["GSUID_EO_STRATEGY"] = "ledger"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_PROXY"] = "localhost,127.0.0.1"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    out_f = open(stdout_path, "ab")
    err_f = open(stderr_path, "ab")
    flags = _CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            args,
            cwd=str(_ROOT),
            env=env,
            stdout=out_f,
            stderr=err_f,
            stdin=subprocess.DEVNULL,
            creationflags=_SPAWN_FLAGS,
        )
    except OSError:
        subprocess.Popen(
            args,
            cwd=str(_ROOT),
            env=env,
            stdout=out_f,
            stderr=err_f,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )


def _tail(path: Path, n: int) -> list[str]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


def _campaign_done() -> bool:
    return any("[ladder] 128K → 500K → 1M → 10M 完成" in ln for ln in _tail(_CAMPAIGN_OUT, 80))


def _campaign_stopped() -> bool:
    blob = _tail(_CAMPAIGN_OUT, 80)
    return any("[ladder] 停在" in ln or "[all] 停在" in ln for ln in blob)


def _stall_limit_s(last_lines: list[str]) -> int:
    blob = "\n".join(last_lines)
    if "last=True" in blob or "'rebuild': True" in blob or "rebuild=True" in blob or "[Clear]" in blob:
        return _STALL_REBUILD_S
    if "[Probe]" in blob or "[Judge]" in blob or "[judge]" in blob:
        return 40 * 60
    return _STALL_S


def _log_age_s() -> float | None:
    if not _CAMPAIGN_OUT.is_file():
        return None
    return time.time() - _CAMPAIGN_OUT.stat().st_mtime


def _ensure_core() -> None:
    if _tcp_up():
        return
    existing = _pids_matching("gsuid_core.core")
    if existing is None:
        _log("skip core spawn: process query failed")
        return
    if not existing:
        _log("core down; starting full plugins")
        py = _VENV_PY if Path(_VENV_PY).is_file() else sys.executable
        _spawn([py, "-m", "gsuid_core.core", "--port", "8765"], _CORE_OUT, _CORE_ERR)
    deadline = time.time() + 600
    while time.time() < deadline:
        if _eval_api_up():
            _log("core eval api ready")
            return
        time.sleep(2)
    _log("core failed to listen")


def _ensure_campaign(*, restart: bool) -> None:
    pids = _pids_matching(_NEEDLE)
    if pids is None:
        _log("skip spawn: process query failed")
        return
    if restart and pids:
        _log(f"killing stalled campaign pids={pids}")
        _kill_pids(pids)
        time.sleep(2)
        pids = _pids_matching(_NEEDLE)
        if pids is None:
            _log("skip spawn after kill: process query failed")
            return
    if pids:
        return
    _log("starting official ladder")
    py = _VENV_PY if Path(_VENV_PY).is_file() else sys.executable
    _spawn(
        [py, "-u", str(_ROOT / "eval" / "BEAM_official" / "run_official.py"), "ladder"],
        _CAMPAIGN_OUT,
        _CAMPAIGN_ERR,
    )


def tick() -> str:
    if _campaign_done():
        return "done"
    if _campaign_stopped():
        pids = _pids_matching(_NEEDLE)
        if pids:
            return "running_after_stop"
        return "stopped"
    _ensure_core()
    if not _tcp_up():
        _log("skip campaign start: core still down")
        return "core_down"
    pids = _pids_matching(_NEEDLE)
    last = _tail(_CAMPAIGN_OUT, 12)
    age = _log_age_s()
    limit = _stall_limit_s(last)
    stalled = age is not None and age > limit
    if pids is None:
        _log("skip: cannot list campaign processes")
        return "query_fail"
    if not pids:
        _log("campaign missing")
        _ensure_campaign(restart=False)
        return "restarted_missing"
    if stalled:
        _log(f"campaign stalled age={age:.0f}s limit={limit}s last={last[-1] if last else ''}")
        if not _tcp_up():
            core_pids = _pids_matching("gsuid_core.core") or []
            if core_pids:
                _log(f"restart core pids={core_pids}")
                _kill_pids(core_pids)
                time.sleep(3)
            _ensure_core()
        else:
            _log("core listen ok; not killing core on ingest stall")
        _ensure_campaign(restart=True)
        return "restarted_stall"
    _log(f"ok campaign_pids={pids} age={0 if age is None else int(age)}s last={last[-1] if last else ''}")
    return "ok"


def main() -> int:
    _inherit_token()
    _log("watchdog start official ladder")
    while True:
        try:
            status = tick()
            if status == "done":
                _log("campaign finished; watchdog exit")
                return 0
            if status == "stopped":
                _log("campaign stopped with failure; watchdog exit")
                return 2
        except Exception as e:
            _log(f"tick error {e!r}")
        time.sleep(_INTERVAL_S)


if __name__ == "__main__":
    raise SystemExit(main())
