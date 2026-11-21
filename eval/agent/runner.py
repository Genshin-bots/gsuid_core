"""把 case 打到运行中的 core，收集**工具轨迹**。

复用既有评测底座 `eval/common/http_client.call_chat_with_history` 驱动 `/api/chat_with_history`
（继承其鉴权头/超时/错误约定，与 BEAM 官方 ladder / longmemeval 一致）。区别在于：记忆评测只看返回的
**文本** `data`，而 agent 评测要的是**工具轨迹**——优先从 session_log 捞；端点也会返回
``tool_calls``（仅工具名），扫不到日志时用它填 HTTP-only Trace，避免 ``must_call_any`` 空打。

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

from eval.agent.harness import Trace, ToolCall, parse_session_log, pick_user_visible, trace_awaits_delivery
from eval.common.http_client import call_eval_inbox_turn, call_chat_with_history

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
    safe_sid = re.sub(r"[^0-9A-Za-z_.-]+", "_", session_id).strip("_")
    fast_cands: list[Path] = []
    f = SESSION_LOG_DIR / f"{session_id}.json"
    if f.exists():
        fast_cands.append(f)
    if safe_sid:
        for p in SESSION_LOG_DIR.glob(f"*{safe_sid}*.json"):
            if p not in fast_cands:
                fast_cands.append(p)
    for p in fast_cands:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "session_id" in doc and doc["session_id"] == session_id:
            best, best_key = _prefer_log(best, doc, _doc_sort_key(doc, p.stat().st_mtime))
    if best is not None:
        return best
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

    resp, skip, injected_images = await _execute_case_http(
        client,
        base_url,
        case,
        uid,
        persona,
        enable_tools,
        timeout,
    )
    latency = time.time() - since
    _raw_data: object = resp["data"] if "data" in resp else None
    delivered = _raw_data if isinstance(_raw_data, str) else ""
    if "error" in resp and resp["error"]:
        return Trace(error=f"api:{resp['error']}", latency=latency, injected_image_ids=injected_images)

    # A 模式：端点已返回 trace / session_id
    if "trace" in resp and isinstance(resp["trace"], dict):
        tr = parse_session_log(resp["trace"], skip_runs=skip)
        tr.latency = latency
        tr.injected_image_ids = injected_images
        log_last = tr.visible_texts[-1] if tr.visible_texts else tr.final_text
        tr.returned_text = pick_user_visible(delivered, log_last)
        return tr
    doc = None
    session_id = str(resp["session_id"]) if "session_id" in resp and resp["session_id"] else ""
    if session_id:
        for _ in range(10):
            doc = _find_log_by_session_id(session_id)
            if doc and _log_complete(doc):
                break
            await asyncio.sleep(1.5)
    if doc is None:  # B 模式兜底（阻塞轮询放线程池，避免卡事件循环）
        doc = await asyncio.to_thread(_scan_log_by_user, uid, since, wait)
    if doc is None:
        # 拿不到日志但拿到了文本：退化成 HTTP-only Trace。端点 tool_calls 是工具名列表。
        if delivered:
            tr = _http_text_trace(resp, delivered, latency)
            tr.injected_image_ids = injected_images
            return tr
        return Trace(
            error="session_log_not_found（建议按 README 让端点返回 session_id/trace）",
            latency=latency,
            injected_image_ids=injected_images,
        )
    tr = parse_session_log(doc, skip_runs=skip)
    tr.latency = latency
    tr.injected_image_ids = injected_images
    log_last = tr.visible_texts[-1] if tr.visible_texts else tr.final_text
    tr.returned_text = pick_user_visible(delivered, log_last)
    return tr


async def run_case(client: httpx.AsyncClient, base_url: str, case: dict, k: int, wait: float = 75.0) -> list[Trace]:
    # 同一 case 的 k 次 run 串行（pass^k 要独立采样；避免并发抢同一 user 的日志关联）
    return [await run_once(client, base_url, case, i, wait) for i in range(k)]


# 批量执行模式：并发执行全部运行并统一刷新日志，按唯一 user_id 检索会话文件。


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


def _sub_run_tag(val: str, run_tag: str) -> str:
    return val.replace("{run}", run_tag)


def _format_step_group(raw_gid: object, run_tag: str) -> Optional[str]:
    if raw_gid is None or raw_gid == "":
        return None
    s = _sub_run_tag(str(raw_gid), run_tag)
    return s if run_tag in s else f"{s}_{run_tag}"


async def _execute_case_http(
    client: httpx.AsyncClient,
    base_url: str,
    case: dict,
    uid: str,
    persona: str | None,
    enable_tools: bool,
    timeout: float,
) -> tuple[dict, int, list[str]]:
    """统一执行 single / multi_turn_session / inbox_burst / multi_session，返回 (resp, skip_runs, image_ids)。"""
    mode = str(case["mode"] if "mode" in case and case["mode"] else "single")
    max_hist = int(case["max_history"] if "max_history" in case else AGENT_EVAL_MAX_HISTORY)
    group_id = _case_group_id(case, run_tag=uid)
    injected_images: list[str] = []

    if mode == "multi_turn_session":
        sid = f"eval_sess_{case['id']}_{uuid.uuid4().hex[:8]}"
        turns = list(case["turns"] if "turns" in case and case["turns"] else [])
        if "message" in case and case["message"]:
            turns.append(
                {
                    "message": case["message"],
                    "user_id": case["user_id"] if "user_id" in case else "",
                    "images": case["images"] if "images" in case else [],
                }
            )
        last_resp: dict = {"status_code": -1, "data": None, "error": "empty_turns"}
        for t_item in turns:
            raw_msg = t_item if isinstance(t_item, str) else str(t_item["message"] if "message" in t_item else "")
            t_msg = _sub_run_tag(raw_msg, uid)
            t_uid = (
                _sub_run_tag(str(t_item["user_id"]), uid)
                if isinstance(t_item, dict) and "user_id" in t_item and t_item["user_id"]
                else uid
            )
            t_imgs = (
                list(t_item["images"]) if isinstance(t_item, dict) and "images" in t_item and t_item["images"] else None
            )
            last_resp = await call_chat_with_history(
                client,
                base_url=base_url,
                user_id=t_uid,
                message=t_msg,
                history=[],
                persona_name=persona,
                enable_observer=bool(case["enable_observer"]) if "enable_observer" in case else False,
                enable_tools=enable_tools,
                max_history=max_hist,
                group_id=group_id,
                session_id=sid,
                images=t_imgs,
                timeout=timeout,
            )
            if isinstance(last_resp.get("image_ids"), list):
                injected_images.extend(str(x) for x in last_resp["image_ids"] if x)
        return last_resp, 0, injected_images

    if mode == "inbox_burst":
        sid = f"eval_sess_ib_{case['id']}_{uuid.uuid4().hex[:8]}"
        gid = group_id or f"eval_grp_{case['id']}_{uid}"
        bursts: list[list[dict]] = []
        if "bursts" in case and case["bursts"]:
            bursts = [list(b) for b in case["bursts"]]
        elif "burst" in case and case["burst"]:
            bursts = [list(case["burst"])]
        last_resp = {"status_code": -1, "data": None, "error": "empty_burst"}
        for b_msgs in bursts:
            norm_msgs: list[dict] = []
            for m in b_msgs:
                entry = dict(m)
                if "user_id" in entry and entry["user_id"]:
                    entry["user_id"] = _sub_run_tag(str(entry["user_id"]), uid)
                if "text" in entry and entry["text"]:
                    entry["text"] = _sub_run_tag(str(entry["text"]), uid)
                norm_msgs.append(entry)
            last_resp = await call_eval_inbox_turn(
                client,
                base_url=base_url,
                group_id=gid,
                messages=norm_msgs,
                persona_name=persona,
                session_id=sid,
                enable_tools=enable_tools,
                enable_observer=bool(case["enable_observer"]) if "enable_observer" in case else False,
                max_history=max_hist,
                timeout=timeout,
            )
            if isinstance(last_resp.get("image_ids"), list):
                injected_images.extend(str(x) for x in last_resp["image_ids"] if x)
        skip_n = (
            0
            if len(bursts) <= 1 or bool(case["score_all_turns"] if "score_all_turns" in case else False)
            else len(bursts) - 1
        )
        return last_resp, skip_n, injected_images

    if mode == "multi_session":
        steps = list(case["steps"] if "steps" in case and case["steps"] else [])
        last_resp = {"status_code": -1, "data": None, "error": "empty_steps"}
        for st in steps:
            s_uid = _sub_run_tag(str(st["user_id"] if "user_id" in st and st["user_id"] else uid), uid)
            s_gid = _format_step_group(st["group_id"] if "group_id" in st else None, uid)
            s_msg = _sub_run_tag(str(st["message"] if "message" in st else ""), uid)
            s_role = str(st["role"] if "role" in st else "probe")
            s_obs = bool(st["enable_observer"]) if "enable_observer" in st else (s_role == "seed")
            s_tools = (
                bool(st["enable_tools"]) if "enable_tools" in st else (enable_tools if s_role == "probe" else False)
            )
            s_imgs = list(st["images"]) if "images" in st and st["images"] else None
            s_hist: list[dict[str, str]] = []
            if "history" in st and st["history"]:
                for h in st["history"]:
                    if isinstance(h, dict):
                        s_hist.append(
                            {
                                "role": str(h["role"] if "role" in h else "user"),
                                "content": _sub_run_tag(str(h["content"] if "content" in h else ""), uid),
                            }
                        )
            last_resp = await call_chat_with_history(
                client,
                base_url=base_url,
                user_id=s_uid,
                message=s_msg,
                history=s_hist,
                persona_name=persona,
                enable_observer=s_obs,
                enable_tools=s_tools,
                max_history=max_hist if s_role == "probe" else 0,
                group_id=s_gid,
                images=s_imgs,
                timeout=timeout,
            )
            if s_role == "probe" and isinstance(last_resp.get("image_ids"), list):
                injected_images.extend(str(x) for x in last_resp["image_ids"] if x)
        return last_resp, 0, injected_images

    # single 模式
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
    case_imgs = list(case["images"]) if "images" in case and case["images"] else None
    resp = await call_chat_with_history(
        client,
        base_url=base_url,
        user_id=uid,
        message=case["message"],
        history=history,
        persona_name=persona,
        enable_observer=bool(case["enable_observer"]) if "enable_observer" in case else False,
        enable_tools=enable_tools,
        max_history=max_hist,
        group_id=group_id,
        images=case_imgs,
        timeout=timeout,
    )
    if isinstance(resp.get("image_ids"), list):
        injected_images.extend(str(x) for x in resp["image_ids"] if x)
    return resp, _prefix_run_count(case), injected_images


async def _fire_run(client, base_url, case, run_idx, sem, timeout) -> dict:
    uid = f"eval_{case['id']}_{run_idx}_{uuid.uuid4().hex[:6]}"
    queued = time.time()
    persona = case["persona"] if "persona" in case else "早柚"
    enable_tools = case["enable_tools"] if "enable_tools" in case else True
    async with sem:
        call_start = time.time()
        resp, skip_runs, injected_images = await _execute_case_http(
            client,
            base_url,
            case,
            uid,
            persona,
            enable_tools,
            timeout,
        )
        latency = time.time() - call_start
    return {
        "case_id": case["id"],
        "run_idx": run_idx,
        "uid": uid,
        "session_id": str(resp["session_id"])
        if isinstance(resp, dict) and "session_id" in resp and resp["session_id"]
        else "",
        "image_ids": injected_images,
        "since": queued,
        "resp": resp,
        "latency": latency,
        "skip_runs": skip_runs,
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
    img_ids = [str(x) for x in (f["image_ids"] if "image_ids" in f and f["image_ids"] else [])]
    if doc is not None:
        tr = parse_session_log(doc, skip_runs=skip)
        tr.latency = f["latency"]
        tr.injected_image_ids = img_ids
        log_last = tr.visible_texts[-1] if tr.visible_texts else tr.final_text
        tr.returned_text = pick_user_visible(delivered, log_last)
        return tr
    if resp.get("error"):
        return Trace(error=f"api:{resp.get('error')}", latency=f["latency"], injected_image_ids=img_ids)
    if delivered:
        tr = _http_text_trace(resp, delivered, f["latency"])
        tr.injected_image_ids = img_ids
        return tr
    return Trace(error="session_log_not_found", latency=f["latency"], injected_image_ids=img_ids)


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
    # A 模式快路径：端点已同步落盘并返回 session_id，直接按 session_id 秒级读取；缺失再走 B 模式等待
    docs: dict = {}
    missing_uids: set[str] = set()
    for f in fired:
        sid = str(f["session_id"] if "session_id" in f else "")
        if sid:
            doc = await asyncio.to_thread(_find_log_by_session_id, sid)
            if doc is not None and _log_complete(doc):
                docs[f["uid"]] = doc
                continue
        missing_uids.add(f["uid"])

    if missing_uids:
        print(
            f"[batch] {len(docs)}/{total} loaded by session_id; waiting {wait:.0f}s for {len(missing_uids)} remaining…",
            flush=True,
        )
        await asyncio.sleep(wait)
        for i in range(1 + rescans):
            found = await asyncio.to_thread(_scan_all_logs, missing_uids, earliest)
            for uid, doc in found.items():
                if uid not in docs:
                    docs[uid] = doc
                    continue
                chosen, _ = _prefer_log(docs[uid], doc, _doc_sort_key(doc, 0.0))
                docs[uid] = chosen
            if all(u in docs for u in missing_uids):
                break
            if i < rescans:
                await asyncio.sleep(rescan_gap)
    else:
        print(f"[batch] all {total} session_logs loaded immediately via session_id", flush=True)

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


def _tool_calls_from_http(resp: dict) -> list[ToolCall]:
    raw = resp.get("tool_calls") if isinstance(resp, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[ToolCall] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(ToolCall(name=item.strip(), args={}, raw_args=""))
    return out


def _http_text_trace(resp: dict, delivered: str, latency: float) -> Trace:
    return Trace(
        final_text=delivered,
        returned_text=delivered,
        latency=latency,
        tool_calls=_tool_calls_from_http(resp),
    )


def _is_eval_silence_or_ack(tr: Trace) -> bool:
    """短应/沉默：回灌尚未变成终局结论。"""
    text = (tr.content_text or "").strip()
    if not text:
        return True
    if text in {"<SILENCE>", "[SILENCE]", "SILENCE", "<沉默>", "[沉默]"}:
        return True
    return len(text) <= 40
