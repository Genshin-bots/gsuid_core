"""把 case 打到运行中的 core，收集**工具轨迹**。

复用既有评测底座 `eval/common/http_client.call_chat_with_history` 驱动 `/api/chat_with_history`
（继承其鉴权头/超时/错误约定，与 BEAM_10M / longmemeval 一致）。区别在于：记忆评测只看返回的
**文本** `data`，而 agent 评测要的是**工具轨迹**——只能从 session_log 捞（该端点当前不返回轨迹）。

轨迹关联（自动择优）：
  A. 端点若按 README「3 行增强」返回 `session_id`（并在 run 结束 flush 会话）→ 精确、秒级。
  B. 未增强 → 用每 run 唯一 user_id + 轮询 session_logs 兜底（慢：默认空闲≥~1 分钟才落盘）。
"""

from __future__ import annotations

import re
import glob
import json
import time
import uuid
import asyncio
from typing import Optional
from pathlib import Path

import httpx

from eval.agent.harness import Trace, parse_session_log, pick_user_visible, trace_awaits_delivery
from eval.common.http_client import call_chat_with_history

# 与 interaction_scaffold 说话人前缀同形，避免 runner 去 import 生产脚手架。
_SPEAKER_HEAD_RE = re.compile(r"^[^：:（()）\n]{1,16}\(用户ID:[^)]{1,24}\)[：:]\s*")

SESSION_LOG_DIR = Path("data/ai_core/session_logs")


def _owner_prefix(case: dict) -> str:
    """从 probe / history 抽出「昵称(用户ID:x)：」，setup 必须挂同一说话人。"""
    texts: list[str] = [str(case["message"] if "message" in case else "")]
    for turn in reversed(list(case.get("history") or [])):
        if not isinstance(turn, dict):
            continue
        if turn.get("role") == "user":
            texts.append(str(turn.get("content") or ""))
    for text in texts:
        m = _SPEAKER_HEAD_RE.match((text or "").strip())
        if m:
            return m.group(0)
    return ""


def _with_owner_prefix(message: str, prefix: str) -> str:
    raw = (message or "").strip()
    if not prefix or not raw:
        return message
    if _SPEAKER_HEAD_RE.match(raw):
        return message
    return f"{prefix}{raw}"


def _prefix_run_count(case: dict) -> int:
    n = len(case.get("setup") or [])
    n += len(case.get("warmup_turns") or [])
    return n


def _setup_messages(case: dict) -> list[str]:
    prefix = _owner_prefix(case)
    out: list[str] = []
    for su in case.get("setup") or []:
        raw = su["message"] if isinstance(su, dict) else str(su)
        out.append(_with_owner_prefix(raw, prefix))
    return out


# agent 评测靠请求 history 做多轮上下文；端点默认 max_history=0 会清空它（extract_history），
# 故显式传正值让端点把 history 真正喂进模型上下文（case 可用 max_history 覆盖）。
AGENT_EVAL_MAX_HISTORY = 30


def _doc_sort_key(doc: dict, mtime: float) -> float:
    """段文件越新越大。setup 与打分枪同 uid 时用它选打分那一段。"""
    for field in ("updated_at", "created_at"):
        if field in doc:
            v = doc[field]
            if isinstance(v, (int, float)):
                return float(v)
    return mtime


def _log_complete(doc: dict) -> bool:
    types = {e.get("type") for e in doc.get("entries") or []}
    return ("result" in types) or ("run_end" in types)


def _prefer_log(current: dict | None, candidate: dict, cand_key: float) -> tuple[dict, float]:
    """完整段优先，其次时间戳更新的（避开 setup 枪盖住打分枪）。"""
    if current is None:
        return candidate, cand_key
    cur_key = _doc_sort_key(current, 0.0)
    cur_ok = _log_complete(current)
    cand_ok = _log_complete(candidate)
    if cand_ok and not cur_ok:
        return candidate, cand_key
    if cur_ok and not cand_ok:
        return current, cur_key
    if cand_key >= cur_key:
        return candidate, cand_key
    return current, cur_key


def _find_log_by_session_id(session_id: str) -> Optional[dict]:
    best: dict | None = None
    best_key = -1.0
    f = SESSION_LOG_DIR / f"{session_id}.json"
    if f.exists():
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            best, best_key = _prefer_log(best, doc, _doc_sort_key(doc, f.stat().st_mtime))
        except Exception:
            pass
    for p in SESSION_LOG_DIR.glob("*.json"):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "session_id" not in doc or doc["session_id"] != session_id:
            continue
        best, best_key = _prefer_log(best, doc, _doc_sort_key(doc, p.stat().st_mtime))
    return best


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


async def _run_warmup_turns(
    client: httpx.AsyncClient,
    base_url: str,
    uid: str,
    case: dict,
    persona: str | None,
    timeout: float,
    group_id: Optional[str] = None,
) -> list[dict[str, str]]:
    """执行 warmup_turns：逐轮发消息并积累模型真实回复为 history。

    用于长对话 OOC 评测——让模型自己的输出逐轮回灌上下文，
    模拟真实多轮漂移（纯合成 history 测不到这个）。
    warmup 阶段默认关工具（warmup_tools 可覆盖），加速且避免外部依赖。
    """
    history: list[dict[str, str]] = list(case.get("history", []))
    wt_enable = case.get("warmup_tools", False)
    max_hist = int(case.get("max_history", AGENT_EVAL_MAX_HISTORY))
    for wt in case.get("warmup_turns", []):
        msg = wt if isinstance(wt, str) else str(wt.get("message", ""))
        if not msg:
            continue
        wt_resp = await call_chat_with_history(
            client,
            base_url=base_url,
            user_id=uid,
            message=msg,
            history=history,
            persona_name=persona,
            enable_observer=False,
            enable_tools=wt_enable,
            max_history=max_hist,
            group_id=group_id,
            timeout=timeout,
        )
        wt_text = wt_resp.get("data") if isinstance(wt_resp.get("data"), str) else ""
        history.append({"role": "user", "content": msg})
        if wt_text:
            history.append({"role": "assistant", "content": wt_text})
    return history


async def run_once(
    client: httpx.AsyncClient, base_url: str, case: dict, run_idx: int, wait: float = 75.0, timeout: float = 200.0
) -> Trace:
    """驱动一次 run，返回结构化 Trace（失败/超时返回带 error 的 Trace，计为该 run 失败）。

    agent 评测**必须**带 ``enable_tools=True`` 才走真实工具装配（否则跑的是无工具的
    记忆评测 agent）；case 可用 ``persona`` 覆盖人格（默认早柚），``enable_tools`` 显式关掉。
    端到端 latency（HTTP 往返墙钟）填进 Trace，供 ``max_latency`` verifier 抓死循环/挂起。
    支持 ``warmup_turns``：先逐轮发真实对话积累上下文，再发 probe message 打分。
    """
    uid = f"eval_{case['id']}_{run_idx}_{uuid.uuid4().hex[:6]}"
    since = time.time()
    # persona 默认早柚（全局默认人格会暴露 AI 身份，非角色，评测须显式指定）；
    # 允许 case 传 persona: null 显式关人格（judge/通用助手场景）。
    persona = case["persona"] if "persona" in case else "早柚"
    enable_tools = case["enable_tools"] if "enable_tools" in case else True
    group_id = _case_group_id(case, run_tag=uid)

    for setup_msg in _setup_messages(case):
        await call_chat_with_history(
            client,
            base_url=base_url,
            user_id=uid,
            message=setup_msg,
            history=[],
            persona_name=persona,
            enable_observer=False,
            enable_tools=enable_tools,
            max_history=0,
            group_id=group_id,
            timeout=timeout,
        )

    # warmup_turns：逐轮真实对话积累上下文（长对话 OOC 评测用）
    if case.get("warmup_turns"):
        history = await _run_warmup_turns(
            client,
            base_url,
            uid,
            case,
            persona,
            timeout,
            group_id=group_id,
        )
    else:
        history = case.get("history", [])

    resp = await call_chat_with_history(
        client,
        base_url=base_url,
        user_id=uid,
        message=case["message"],
        history=history,
        persona_name=persona,
        enable_observer=False,
        enable_tools=enable_tools,
        max_history=int(case.get("max_history", AGENT_EVAL_MAX_HISTORY)),
        group_id=group_id,
        timeout=timeout,  # 评测隔离：默认不写记忆
    )
    latency = time.time() - since
    _raw_data: object = resp["data"] if "data" in resp else None
    delivered = _raw_data if isinstance(_raw_data, str) else ""
    if resp.get("error"):
        return Trace(error=f"api:{resp.get('error')}", latency=latency)

    # A 模式：端点已返回 trace / session_id
    skip = _prefix_run_count(case)
    if isinstance(resp.get("trace"), dict):
        tr = parse_session_log(resp["trace"], skip_runs=skip)
        tr.latency = latency
        log_last = tr.visible_texts[-1] if tr.visible_texts else tr.final_text
        tr.returned_text = pick_user_visible(delivered, log_last)
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
    tr = parse_session_log(doc, skip_runs=skip)
    tr.latency = latency
    log_last = tr.visible_texts[-1] if tr.visible_texts else tr.final_text
    tr.returned_text = pick_user_visible(delivered, log_last)
    return tr


async def run_case(client: httpx.AsyncClient, base_url: str, case: dict, k: int, wait: float = 75.0) -> list[Trace]:
    # 同一 case 的 k 次 run 串行（pass^k 要独立采样；避免并发抢同一 user 的日志关联）
    return [await run_once(client, base_url, case, i, wait) for i in range(k)]


# ───────────────────────── 批量 B 模式（快得多） ─────────────────────────
# session_log 空闲≥60s 才落盘；逐条各等一次 ≈1min/run。批量：并发 fire 全部 run
# → 只等一次 flush → 按唯一 user_id 扫盘。每 run user_id 唯一，session 文件不冲突。


def _case_group_id(case: dict, run_tag: str = "") -> Optional[str]:
    """群聊向用例注入合成 group_id，让端点走群会话语义（沉默/is_tome/多人）。

    case 可显式 ``group_id``；或 targets 含 group-chat/multi-user 时自动生成。
    ``run_tag`` 把同 case 的 k 次 run 隔开，避免并发抢同一群会话。
    """
    gid = ""
    if "group_id" in case and case["group_id"]:
        gid = str(case["group_id"])
    else:
        targets = case["targets"] if "targets" in case and case["targets"] else []
        domain = str(case["domain"]) if "domain" in case and case["domain"] else ""
        flags = set(targets) | {domain}
        if flags & {
            "group-chat",
            "multi-user",
            "multi_user_session",
            "implicit_addressing",
            "silence_judgment",
            "multi_speaker",
        }:
            cid = str(case["id"]) if "id" in case else "x"
            gid = f"eval_grp_{cid}"
    if not gid:
        return None
    return f"{gid}_{run_tag}" if run_tag else gid


async def _fire_run(client, base_url, case, run_idx, sem, timeout) -> dict:
    uid = f"eval_{case['id']}_{run_idx}_{uuid.uuid4().hex[:6]}"
    queued = time.time()
    persona = case["persona"] if "persona" in case else "早柚"
    enable_tools = case["enable_tools"] if "enable_tools" in case else True
    group_id = _case_group_id(case, run_tag=uid)
    async with sem:
        # setup（可选）：跨轮 modify/cancel 类用例需要**真实的既有任务**才能被"定位并修改"。
        # 合成 history 里写"已设好"却从未真调工具落库 → 评测里根本无任务可改（假失败）。
        # 这里先按 setup 里的消息真跑一遍（同 uid，工具落 DB），主消息再借状态池定位到它，
        # 与生产"先建后改"完全一致。setup 结果不参与打分。
        for setup_msg in _setup_messages(case):
            await call_chat_with_history(
                client,
                base_url=base_url,
                user_id=uid,
                message=setup_msg,
                history=[],
                persona_name=persona,
                enable_observer=False,
                enable_tools=enable_tools,
                max_history=0,
                group_id=group_id,
                timeout=timeout,
            )
        # warmup_turns：逐轮真实对话积累上下文（长对话 OOC 评测）；
        # 不计入 probe latency，仅为主消息构建多轮上下文。
        if case.get("warmup_turns"):
            history = await _run_warmup_turns(
                client,
                base_url,
                uid,
                case,
                persona,
                timeout,
                group_id=group_id,
            )
        else:
            history = case.get("history", [])
        # ⚠️ latency 从**拿到并发槽后**起算——端点同步阻塞到 agent 跑完，这段才是单次 agent
        # 运行的真实耗时（供 max_latency 抓死循环/挂起）。若从 queued 起算会把"等信号量排队"
        # 的时间算进去（426 run / concurrency 3 时队尾能等几分钟），令 max_latency 全线误判。
        call_start = time.time()
        resp = await call_chat_with_history(
            client,
            base_url=base_url,
            user_id=uid,
            message=case["message"],
            history=history,
            persona_name=persona,
            enable_observer=False,
            enable_tools=enable_tools,
            max_history=int(case.get("max_history", AGENT_EVAL_MAX_HISTORY)),
            group_id=group_id,
            timeout=timeout,
        )
        latency = time.time() - call_start
    return {
        "case_id": case["id"],
        "run_idx": run_idx,
        "uid": uid,
        "since": queued,
        "resp": resp,
        "latency": latency,
        "skip_runs": _prefix_run_count(case),
    }


def _scan_all_logs(uids: set, since: float) -> dict:
    """一趟扫 session_logs，返回 {uid: doc}。

    setup 与打分枪共用 uid、各写一段完整 log。必须取 **updated_at 最晚的完整段**，
    不能按 glob 顺序后者覆盖——否则工具断言打在「建任务」那一轮上。
    """
    out: dict = {}
    for p in glob.glob(str(SESSION_LOG_DIR / "*.json")):
        pp = Path(p)
        try:
            mtime = pp.stat().st_mtime
            if mtime < since - 2:
                continue
            doc = json.loads(pp.read_text(encoding="utf-8"))
        except Exception:
            continue
        blob = json.dumps(doc, ensure_ascii=False)
        cand_key = _doc_sort_key(doc, mtime)
        for uid in uids:
            if uid not in blob:
                continue
            prev = out[uid] if uid in out else None
            chosen, _ = _prefer_log(prev, doc, cand_key)
            out[uid] = chosen
            break
    return out


def _trace_from_fired(f: dict, doc) -> Trace:
    resp = f["resp"]
    delivered = resp.get("data") if isinstance(resp.get("data"), str) else ""
    skip = int(f["skip_runs"] if "skip_runs" in f else 0)
    if doc is not None:
        tr = parse_session_log(doc, skip_runs=skip)
        tr.latency = f["latency"]
        log_last = tr.visible_texts[-1] if tr.visible_texts else tr.final_text
        tr.returned_text = pick_user_visible(delivered, log_last)
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
    delivery_wait: float = 90.0,
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
    print(
        f"[batch] firing {total} runs in parallel (concurrency={concurrency}; "
        f"same-case setup/warmup/probe stay serial)…",
        flush=True,
    )

    done = 0

    async def _fire_and_tick(c, i):
        nonlocal done
        f = await _fire_run(client, base_url, c, i, sem, timeout)
        done += 1
        err = f["resp"]["error"] if "error" in f["resp"] and f["resp"]["error"] else None
        if done % 10 == 0 or err:
            tag = f"ERR({err})" if err else "ok"
            print(
                f"[batch] fired {done}/{total} left={total - done}  "
                f"last={f['case_id']}#{f['run_idx']} {f['latency']:.0f}s {tag}",
                flush=True,
            )
        return f

    fired = await asyncio.gather(*[_fire_and_tick(c, i) for c, i in specs])
    print(f"[batch] all {total} fired; waiting {wait:.0f}s for session_log flush…", flush=True)

    # 只等一次让日志 flush（空闲≥60s 才落盘），再一趟扫盘；缺失的补扫
    await asyncio.sleep(wait)
    all_uids = {f["uid"] for f in fired}
    docs: dict = {}
    for i in range(1 + rescans):
        found = await asyncio.to_thread(_scan_all_logs, all_uids, earliest)
        for uid, doc in found.items():
            if uid not in docs:
                docs[uid] = doc
                continue
            chosen, _ = _prefer_log(docs[uid], doc, _doc_sort_key(doc, 0.0))
            docs[uid] = chosen
        if all(u in docs for u in all_uids):
            break
        if i < rescans:
            await asyncio.sleep(rescan_gap)

    per_case: dict = {}
    pending_delivery: list[dict] = []
    for f in fired:
        tr = _trace_from_fired(f, docs.get(f["uid"]))
        if trace_awaits_delivery(tr) and _is_eval_silence_or_ack(tr):
            pending_delivery.append(f)
        per_case.setdefault(f["case_id"], []).append((f["run_idx"], tr))

    if pending_delivery and delivery_wait > 0:
        uids = {f["uid"] for f in pending_delivery}
        print(
            f"[batch] {len(pending_delivery)} runs await deferred delivery; wait {delivery_wait:.0f}s…",
            flush=True,
        )
        deadline = time.time() + delivery_wait
        while time.time() < deadline:
            await asyncio.sleep(min(8.0, max(2.0, delivery_wait / 8)))
            found = await asyncio.to_thread(_scan_all_logs, uids, earliest)
            still: list[dict] = []
            for f in pending_delivery:
                uid = f["uid"]
                if uid in found:
                    docs[uid] = found[uid]
                tr = _trace_from_fired(f, docs.get(uid))
                if trace_awaits_delivery(tr) and _is_eval_silence_or_ack(tr):
                    still.append(f)
            pending_delivery = still
            if not pending_delivery:
                break
        # 回灌可能已进 log，按最新 doc 重填
        rebuilt: dict = {}
        for f in fired:
            tr = _trace_from_fired(f, docs.get(f["uid"]))
            rebuilt.setdefault(f["case_id"], []).append((f["run_idx"], tr))
        per_case = rebuilt

    return {cid: [t for _, t in sorted(runs)] for cid, runs in per_case.items()}


def _is_eval_silence_or_ack(tr: Trace) -> bool:
    """短应/沉默：回灌尚未变成终局结论。"""
    text = (tr.content_text or "").strip()
    if not text:
        return True
    if text in {"<SILENCE>", "[SILENCE]", "SILENCE"}:
        return True
    return len(text) <= 40
