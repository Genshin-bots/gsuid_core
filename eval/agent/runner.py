"""把 case 打到运行中的 core，收集**工具轨迹**。

复用既有评测底座 `eval/common/http_client.call_chat_with_history` 驱动 `/api/chat_with_history`
（继承其鉴权头/超时/错误约定，与 BEAM_10M / longmemeval 一致）。区别在于：记忆评测只看返回的
**文本** `data`，而 agent 评测要的是**工具轨迹**——只能从 session_log 捞（该端点当前不返回轨迹）。

轨迹关联（自动择优）：
  A. 端点若按 README「3 行增强」返回 `session_id`（并在 run 结束 flush 会话）→ 精确、秒级。
  B. 未增强 → 用每 run 唯一 user_id + 轮询 session_logs 兜底（慢：默认空闲≥~1 分钟才落盘）。
"""

from __future__ import annotations

import glob
import json
import time
import uuid
import asyncio
from typing import Optional
from pathlib import Path

import httpx

from eval.agent.harness import Trace, parse_session_log
from eval.common.http_client import call_chat_with_history

SESSION_LOG_DIR = Path("data/ai_core/session_logs")


def _find_log_by_session_id(session_id: str) -> Optional[dict]:
    f = SESSION_LOG_DIR / f"{session_id}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
    for p in SESSION_LOG_DIR.glob("*.json"):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if doc.get("session_id") == session_id:
            return doc
    return None


def _scan_log_by_user(user_id: str, since: float, wait: float) -> Optional[dict]:
    """B 模式兜底：轮询等含 user_id 且已落到有 result/run_end 的日志。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        best: Optional[dict] = None
        best_mtime = 0.0
        for p in glob.glob(str(SESSION_LOG_DIR / "*.json")):
            pp = Path(p)
            try:
                if pp.stat().st_mtime < since - 2:
                    continue
                doc = json.loads(pp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if user_id not in json.dumps(doc, ensure_ascii=False):
                continue
            if pp.stat().st_mtime > best_mtime:
                best, best_mtime = doc, pp.stat().st_mtime
        if best is not None:
            types = {e.get("type") for e in best.get("entries", [])}
            if "result" in types or "run_end" in types:
                return best
        time.sleep(3)
    return None


async def run_once(
    client: httpx.AsyncClient, base_url: str, case: dict, run_idx: int, wait: float = 75.0, timeout: float = 200.0
) -> Trace:
    """驱动一次 run，返回结构化 Trace（失败/超时返回带 error 的 Trace，计为该 run 失败）。"""
    uid = f"eval_{case['id']}_{run_idx}_{uuid.uuid4().hex[:6]}"
    since = time.time()
    resp = await call_chat_with_history(
        client,
        base_url=base_url,
        user_id=uid,
        message=case["message"],
        history=case.get("history", []),
        persona_name=case.get("persona"),
        enable_observer=False,
        timeout=timeout,  # 评测隔离：默认不写记忆
    )
    if resp.get("error"):
        return Trace(error=f"api:{resp.get('error')}")

    # A 模式：端点已返回 trace / session_id
    if isinstance(resp.get("trace"), dict):
        return parse_session_log(resp["trace"])
    doc = None
    session_id = resp.get("session_id")
    if session_id:
        for _ in range(10):
            doc = _find_log_by_session_id(session_id)
            if doc and any(e.get("type") in ("result", "run_end") for e in doc.get("entries", [])):
                break
            await asyncio.sleep(1.5)
    if doc is None:  # B 模式兜底（阻塞轮询放线程池，避免卡事件循环）
        doc = await asyncio.to_thread(_scan_log_by_user, uid, since, wait)
    if doc is None:
        return Trace(error="session_log_not_found（建议按 README 让端点返回 session_id/trace）")
    return parse_session_log(doc)


async def run_case(client: httpx.AsyncClient, base_url: str, case: dict, k: int, wait: float = 75.0) -> list[Trace]:
    # 同一 case 的 k 次 run 串行（pass^k 要独立采样；避免并发抢同一 user 的日志关联）
    return [await run_once(client, base_url, case, i, wait) for i in range(k)]
