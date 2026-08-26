"""HTTP Agent SSE 实机 e2e。不要 test_ 前缀。需已启动 core（勿 --dev）。

用法::

    $env:PYTHONUTF8="1"
    $env:NO_PROXY="localhost,127.0.0.1"
    uv run python eval/manual/http_agent_stream.py --base-url http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import time
import argparse
from typing import List
from pathlib import Path

import httpx

from gsuid_core.data_store import get_res_path
from gsuid_core.ai_core.http_agent.keys import HttpAgentKeyStore
from gsuid_core.ai_core.http_agent.protocol import SseFrame, parse_sse_chunk


def _config_path() -> Path:
    return get_res_path("ai_core") / "http_agent_api.json"


def _set_enable(value: bool) -> None:
    path = _config_path()
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "enable_http_agent_api" not in raw:
        raise SystemExit(f"missing enable_http_agent_api in {path}")
    entry = raw["enable_http_agent_api"]
    if not isinstance(entry, dict):
        raise SystemExit("enable_http_agent_api is not a GSC object")
    entry["data"] = value
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=4), encoding="utf-8")
    time.sleep(0.2)


def _first_persona() -> str | None:
    persona_root = get_res_path("ai_core") / "persona"
    if not persona_root.is_dir():
        return None
    for p in sorted(persona_root.iterdir()):
        if p.is_dir() and (p / "persona.md").exists():
            return p.name
    return None


def _parse_events(text: str) -> List[SseFrame]:
    return parse_sse_chunk(text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    base = str(args.base_url).rstrip("/")
    timeout = float(args.timeout)

    try:
        httpx.get(f"{base}/api/v1/agent/health", timeout=5.0)
    except httpx.ConnectError as e:
        raise SystemExit(f"core 未启动: {base} ({e})") from e

    print("== 1. enable=false → 404")
    _set_enable(False)
    r = httpx.post(f"{base}/api/v1/agent/chat/stream", json={"text": "hi", "client_msg_id": "e2e-off"}, timeout=10.0)
    print("   ", r.status_code, r.text[:120])
    if r.status_code != 404:
        raise SystemExit("expected 404 when disabled")

    print("== 2. Admin/钥：本进程写钥文件（enable=false 可建）")
    store = HttpAgentKeyStore()
    token, rec = store.create(user_id="http-e2e-user", bot_id="HTTP_AGENT_E2E", label="e2e")
    print("   key_id=", rec["key_id"])

    print("== 3. 错钥 401（先打开 enable）")
    _set_enable(True)
    health = httpx.get(f"{base}/api/v1/agent/health", timeout=10.0)
    print("   health", health.status_code, health.text)
    if health.status_code != 200:
        raise SystemExit("health should be 200 after enable")
    bad = httpx.post(
        f"{base}/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "e2e-bad"},
        headers={"Authorization": "Bearer gsk_deadbeef_nope"},
        timeout=10.0,
    )
    print("   ", bad.status_code, bad.text[:120])
    if bad.status_code != 401:
        raise SystemExit("expected 401 for bad key")

    persona = _first_persona()
    print("== 4. 正确钥流式", "persona=", persona)
    body = {
        "text": "你好，请用一句话打个招呼。",
        "client_msg_id": f"e2e-ok-{int(time.time())}",
        "session_id": "e2e-default",
    }
    if persona:
        body["persona"] = persona
    with httpx.stream(
        "POST",
        f"{base}/api/v1/agent/chat/stream",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    ) as resp:
        print("   status", resp.status_code)
        if resp.status_code != 200:
            print("   body", resp.read().decode("utf-8", errors="replace")[:500])
            raise SystemExit("stream failed")
        raw = resp.read().decode("utf-8", errors="replace")
    frames = _parse_events(raw)
    events = [f.event for f in frames]
    print("   events", events)
    if not events or events[0] != "run.start":
        raise SystemExit("missing run.start")
    terminals = [e for e in events if e in ("run.done", "run.error")]
    if len(terminals) != 1:
        raise SystemExit(f"expected exactly one terminal, got {terminals}")
    has_text = "text" in events
    silence = any(f.event == "run.done" and f.data.get("status") == "silence" for f in frames)
    if not has_text and not silence:
        raise SystemExit("expected gated text or run.done silence")
    print("   ok: text=", has_text, "silence=", silence, "terminal=", terminals[0])

    print("== 5. 断连还槽")
    with httpx.stream(
        "POST",
        f"{base}/api/v1/agent/chat/stream",
        json={"text": "请慢慢说。", "client_msg_id": f"e2e-disc-{int(time.time())}", "session_id": "e2e-default"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    ) as resp:
        if resp.status_code != 200:
            raise SystemExit(f"disconnect probe failed {resp.status_code}")
        # 读到第一块即关闭，模拟断连
        for _chunk in resp.iter_bytes():
            break
    time.sleep(0.5)
    again = httpx.post(
        f"{base}/api/v1/agent/chat/stream",
        json={"text": "还在吗", "client_msg_id": f"e2e-after-{int(time.time())}", "session_id": "e2e-default"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    print("   after disconnect", again.status_code)
    if again.status_code == 429:
        raise SystemExit("slot leaked: 429 after disconnect")

    print("== 6. sessions/reset")
    rst = httpx.post(
        f"{base}/api/v1/agent/sessions/reset",
        json={"session_id": "e2e-default"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    print("   reset", rst.status_code, rst.text)
    if rst.status_code != 200:
        raise SystemExit("reset failed")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
