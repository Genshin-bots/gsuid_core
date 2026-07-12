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

# agent 评测靠请求 history 做多轮上下文；端点默认 max_history=0 会清空它（extract_history），
# 故显式传正值让端点把 history 真正喂进模型上下文（case 可用 max_history 覆盖）。
AGENT_EVAL_MAX_HISTORY = 30


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
    """驱动一次 run，返回结构化 Trace（失败/超时返回带 error 的 Trace，计为该 run 失败）。

    agent 评测**必须**带 ``enable_tools=True`` 才走真实工具装配（否则跑的是无工具的
    记忆评测 agent）；case 可用 ``persona`` 覆盖人格（默认早柚），``enable_tools`` 显式关掉。
    端到端 latency（HTTP 往返墙钟）填进 Trace，供 ``max_latency`` verifier 抓死循环/挂起。
    """
    uid = f"eval_{case['id']}_{run_idx}_{uuid.uuid4().hex[:6]}"
    since = time.time()
    # persona 默认早柚（全局默认人格会暴露 AI 身份，非角色，评测须显式指定）；
    # 允许 case 传 persona: null 显式关人格（judge/通用助手场景）。
    persona = case["persona"] if "persona" in case else "早柚"
    enable_tools = case.get("enable_tools", True)
    resp = await call_chat_with_history(
        client,
        base_url=base_url,
        user_id=uid,
        message=case["message"],
        history=case.get("history", []),
        persona_name=persona,
        enable_observer=False,
        enable_tools=enable_tools,
        max_history=int(case.get("max_history", AGENT_EVAL_MAX_HISTORY)),
        timeout=timeout,  # 评测隔离：默认不写记忆
    )
    latency = time.time() - since
    delivered = resp.get("data") if isinstance(resp.get("data"), str) else ""
    if resp.get("error"):
        return Trace(error=f"api:{resp.get('error')}", latency=latency)

    # A 模式：端点已返回 trace / session_id
    if isinstance(resp.get("trace"), dict):
        tr = parse_session_log(resp["trace"])
        tr.latency = latency
        tr.returned_text = delivered or ""
        return tr
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
        # 拿不到轨迹但拿到了文本 data：退化成"纯文本 Trace"，让 final_* / judge 类断言仍可判，
        # 只有工具类断言会因无轨迹而失败（比整条判 error 更能反映真实回复）。
        if delivered:
            return Trace(final_text=delivered, returned_text=delivered, latency=latency)
        return Trace(error="session_log_not_found（建议按 README 让端点返回 session_id/trace）", latency=latency)
    tr = parse_session_log(doc)
    tr.latency = latency
    tr.returned_text = delivered or ""
    return tr


async def run_case(client: httpx.AsyncClient, base_url: str, case: dict, k: int, wait: float = 75.0) -> list[Trace]:
    # 同一 case 的 k 次 run 串行（pass^k 要独立采样；避免并发抢同一 user 的日志关联）
    return [await run_once(client, base_url, case, i, wait) for i in range(k)]


# ───────────────────────── 批量 B 模式（快得多） ─────────────────────────
# 交接文档第 2 节：session_log 空闲≥60s（POLL 15s）才落盘；逐条 run 各等一次 ≈ 1min/run，
# 100+ 例 × k 会拖到数小时。批量模式：一次性 fire 全部 run（并发≤3）→ 全部返回后**只等一次**
# flush → 一趟扫盘按唯一 user_id 关联。每 run user_id 唯一 → session 文件天然不冲突。


async def _fire_run(client, base_url, case, run_idx, sem, timeout) -> dict:
    uid = f"eval_{case['id']}_{run_idx}_{uuid.uuid4().hex[:6]}"
    queued = time.time()
    persona = case["persona"] if "persona" in case else "早柚"
    enable_tools = case.get("enable_tools", True)
    async with sem:
        # setup（可选）：跨轮 modify/cancel 类用例需要**真实的既有任务**才能被"定位并修改"。
        # 合成 history 里写"已设好"却从未真调工具落库 → 评测里根本无任务可改（假失败）。
        # 这里先按 setup 里的消息真跑一遍（同 uid，工具落 DB），主消息再借状态池定位到它，
        # 与生产"先建后改"完全一致。setup 结果不参与打分。
        for _su in case.get("setup", []) or []:
            await call_chat_with_history(
                client,
                base_url=base_url,
                user_id=uid,
                message=_su["message"] if isinstance(_su, dict) else str(_su),
                history=[],
                persona_name=persona,
                enable_observer=False,
                enable_tools=enable_tools,
                max_history=0,
                timeout=timeout,
            )
        # ⚠️ latency 从**拿到并发槽后**起算——端点同步阻塞到 agent 跑完，这段才是单次 agent
        # 运行的真实耗时（供 max_latency 抓死循环/挂起）。若从 queued 起算会把"等信号量排队"
        # 的时间算进去（426 run / concurrency 3 时队尾能等几分钟），令 max_latency 全线误判。
        call_start = time.time()
        resp = await call_chat_with_history(
            client,
            base_url=base_url,
            user_id=uid,
            message=case["message"],
            history=case.get("history", []),
            persona_name=persona,
            enable_observer=False,
            enable_tools=enable_tools,
            max_history=int(case.get("max_history", AGENT_EVAL_MAX_HISTORY)),
            timeout=timeout,
        )
        latency = time.time() - call_start
    return {"case_id": case["id"], "run_idx": run_idx, "uid": uid, "since": queued, "resp": resp, "latency": latency}


def _scan_all_logs(uids: set, since: float) -> dict:
    """一趟扫 session_logs，返回 {uid: doc}（优先含 result/run_end 的完整轨迹）。"""
    out: dict = {}
    for p in glob.glob(str(SESSION_LOG_DIR / "*.json")):
        pp = Path(p)
        try:
            if pp.stat().st_mtime < since - 2:
                continue
            doc = json.loads(pp.read_text(encoding="utf-8"))
        except Exception:
            continue
        blob = json.dumps(doc, ensure_ascii=False)
        types = {e.get("type") for e in doc.get("entries", [])}
        complete = ("result" in types) or ("run_end" in types)
        for uid in uids:
            if uid in blob:
                if uid not in out or complete:
                    out[uid] = doc
                break
    return out


def _trace_from_fired(f: dict, doc) -> Trace:
    resp = f["resp"]
    # HTTP data = 出戏防火墙 scrub 之后、用户真正看到的交付文本（内容断言判它）
    delivered = resp.get("data") if isinstance(resp.get("data"), str) else ""
    if doc is not None:
        tr = parse_session_log(doc)  # session_log = 工具轨迹 + 原始(pre-scrub) final_text
        tr.latency = f["latency"]
        tr.returned_text = delivered or ""
        return tr
    if resp.get("error"):
        return Trace(error=f"api:{resp.get('error')}", latency=f["latency"])
    if delivered:
        # 拿到交付文本但没扫到轨迹：退化成纯文本 Trace（final_*/judge 可判；工具类断言必失败）
        return Trace(final_text=delivered, returned_text=delivered, latency=f["latency"])
    return Trace(error="session_log_not_found", latency=f["latency"])


async def run_suite_batch(
    client: httpx.AsyncClient,
    base_url: str,
    cases: list[dict],
    default_k: int,
    wait: float = 85.0,
    concurrency: int = 3,
    timeout: float = 220.0,
    rescans: int = 4,
    rescan_gap: float = 15.0,
    force_k: bool = False,
) -> dict:
    """批量跑整套 → {case_id: [Trace, ...]}（按 run_idx 有序）。

    per-case ``k`` 覆盖 default_k（除非 ``force_k`` — 冒烟时 CLI --k 硬覆盖全部）。fire 全部 run
    （并发受 ``concurrency`` 限）→ 只等一次 ``wait`` 让日志落盘 → 一趟扫盘；仍缺的 uid 再补扫
    ``rescans`` 次（每次隔 ``rescan_gap``）。
    """
    sem = asyncio.Semaphore(concurrency)
    specs: list[tuple[dict, int]] = []
    for c in cases:
        ck = default_k if force_k else int(c.get("k", default_k))
        for i in range(ck):
            specs.append((c, i))

    earliest = time.time()
    total = len(specs)
    print(f"[batch] firing {total} runs (concurrency={concurrency})…", flush=True)

    done = 0

    async def _fire_and_tick(c, i):
        nonlocal done
        f = await _fire_run(client, base_url, c, i, sem, timeout)
        done += 1
        err = f["resp"].get("error")
        if done % 10 == 0 or err:
            tag = f"ERR({err})" if err else "ok"
            print(
                f"[batch] fired {done}/{total}  last={f['case_id']}#{f['run_idx']} {f['latency']:.0f}s {tag}",
                flush=True,
            )
        return f

    fired = await asyncio.gather(*[_fire_and_tick(c, i) for c, i in specs])
    print(f"[batch] all {total} fired; waiting {wait:.0f}s for session_log flush…", flush=True)

    # 只等一次让日志 flush（空闲≥60s 才落盘），再一趟扫盘；缺失的补扫
    await asyncio.sleep(wait)
    all_uids = {f["uid"] for f in fired}
    docs = await asyncio.to_thread(_scan_all_logs, all_uids, earliest)
    for _ in range(rescans):
        missing = {u for u in all_uids if u not in docs}
        if not missing:
            break
        await asyncio.sleep(rescan_gap)
        docs.update(await asyncio.to_thread(_scan_all_logs, missing, earliest))

    per_case: dict = {}
    for f in fired:
        tr = _trace_from_fired(f, docs.get(f["uid"]))
        per_case.setdefault(f["case_id"], []).append((f["run_idx"], tr))
    return {cid: [t for _, t in sorted(runs)] for cid, runs in per_case.items()}
