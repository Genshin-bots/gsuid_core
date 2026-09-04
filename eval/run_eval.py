"""统一评测入口：``python eval/run_eval.py <benchmark> <stage> [...]``。

所有基准共用 ``eval/common/runner.py`` 的并发/断点续跑/坏答卷修复骨架；
新增基准只需实现三个钩子（加载题目 / 单题作答 / 单题判分）注册进 BENCHMARKS。

用法::

  # LongMemEval：摄入+作答（episode-RAG，System-1），再判分
  python eval/run_eval.py longmem probe --concurrency 12 [--start 0 --end 100]
  python eval/run_eval.py longmem judge --concurrency 12
  python eval/run_eval.py longmem report
  python eval/run_eval.py longmem diagnose --answers-file eval/longmemeval/results/answers_ssp_v3.json

  # BEAM-10M：委托既有 run_beam_eval.py（保持其 CLI 与状态文件不变）
  python eval/run_eval.py beam probe --conv 0
  python eval/run_eval.py beam judge --conv 0
"""

from __future__ import annotations

import os
import sys
import asyncio
import argparse
import subprocess
from typing import Any, Dict, List

import httpx

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from eval.common import (  # noqa: E402
    DEFAULT_BASE_URL,
    dump_json,
    load_json,
    load_eval_data,
    call_batch_observe,
    judge_single_answer,
    call_chat_with_history,
    call_clear_user_global,
    extract_text_from_response,
)
from eval.common.runner import run_items, summarize_by  # noqa: E402


def _fmt_question_date(raw: str) -> str:
    """把 LongMemEval 的 ``question_date``（``2023/05/30 (Tue) 23:40``）规整为可读当前时间串。

    temporal-reasoning 类问题（"多少天前/几周前"）必须让作答模型知道"今天"，否则无从计算——
    标准 LongMemEval 协议本就把提问时间作为输入。此前未注入，是系统性低估时序推理的根因。
    """
    raw = str(raw or "").strip()
    if not raw:
        return ""
    # 去掉 "(Tue)" 这类星期注记
    return " ".join(p for p in raw.split() if not p.startswith("("))


LM_DIR = os.path.join(_PROJECT_ROOT, "eval", "longmemeval")
LM_RESULTS = os.path.join(LM_DIR, "results")
LM_ANSWERS = os.path.join(LM_RESULTS, "answers_runner.json")
LM_JUDGE = os.path.join(LM_RESULTS, "judge_runner.json")
LM_DOMAIN_ORDER = (
    "single-session-preference",
    "single-session-user",
    "single-session-assistant",
    "knowledge-update",
    "multi-session",
    "temporal-reasoning",
)


def _lm_paths(args: argparse.Namespace) -> tuple[str, str]:
    """答卷 / 判分文件路径，可用 --answers-file / --judge-file 覆盖（子集实验隔离用）。"""
    ans = getattr(args, "answers_file", None) or LM_ANSWERS
    jdg = getattr(args, "judge_file", None) or LM_JUDGE
    return ans, jdg


# ─────────────────────────────────────────────
# LongMemEval
# ─────────────────────────────────────────────


def _lm_load(args: argparse.Namespace) -> List[Dict[str, Any]]:
    from eval.longmemeval.run_longmem_eval import resolve_eval_data_path

    data = load_eval_data(args.eval_data or resolve_eval_data_path())
    s, e = args.start or 0, args.end or len(data)
    items = data[s:e]
    qtype = args.question_type
    if qtype:
        items = [q for q in items if q.get("question_type") == qtype]
    return items


async def _lm_probe(args: argparse.Namespace) -> None:
    from eval.longmemeval.run_longmem_eval import flatten_haystack_with_dates

    extract = args.extract
    system2 = args.system2
    inject_date = args.inject_date
    clear_first = args.clear_first
    skip_ingest = args.skip_ingest
    qtype = args.question_type or "*"
    # extract=True 会额外跑 LLM 实体/边抽取，一窗口一次；用 batch_observe 的 extra_payload 透传，
    # 让 System-1 检索能命中 entity/edge（而非纯 episode-RAG）。
    extra_payload = {"extract": True, "extract_concurrency": 10} if extract else None
    answers_file, _ = _lm_paths(args)
    print(
        f"[lm-probe] extract={extract} system2={system2} inject_date={inject_date} "
        f"clear_first={clear_first} skip_ingest={skip_ingest} "
        f"enable_tools={args.enable_tools} memory_eval={not args.no_memory_eval} "
        f"type={qtype} persona={args.persona_name or '评测助手'} "
        f"concurrency={args.concurrency} -> {os.path.basename(answers_file)}"
    )
    items = _lm_load(args)
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:

        async def one(q: Dict[str, Any]) -> Dict[str, Any]:
            qid = q["question_id"]
            user_id = f"eval_{qid}"
            if not skip_ingest:
                turns = flatten_haystack_with_dates(q.get("haystack_sessions", []), q.get("haystack_dates", []))
                # 清库再灌，避免历史遗留 / 部分 scope 污染检索（评测态每题独立 scope）
                if clear_first:
                    await call_clear_user_global(client, args.base_url, user_id, timeout=args.timeout)
                obs = await call_batch_observe(
                    client=client,
                    base_url=args.base_url,
                    user_id=user_id,
                    turns=turns,
                    flush=True,
                    timeout=args.timeout,
                    extra_payload=extra_payload,
                )
                if obs.get("status") != 0:
                    raise RuntimeError(f"batch_observe: {obs.get('msg')}")
            message = q["question"]
            clock_at = None
            if inject_date:
                clock_at = _fmt_question_date(q.get("question_date", "")) or None
            resp = await call_chat_with_history(
                client=client,
                base_url=args.base_url,
                user_id=user_id,
                message=message,
                history=[],
                timeout=args.timeout,
                enable_observer=False,
                enable_system2=system2,
                enable_tools=args.enable_tools,
                memory_eval=not args.no_memory_eval,
                persona_name=args.persona_name or "评测助手",
                clock_at=clock_at,
            )
            status = resp.get("status_code", -1)
            answer = extract_text_from_response(resp.get("data")) if status == 200 else f"[ERROR] status_code={status}"
            return {
                "question_id": qid,
                "question_type": q.get("question_type", "unknown"),
                "question": q["question"],
                "standard_answer": q.get("answer", ""),
                "agent_answer": answer,
                "memory": resp.get("memory"),
                "status_code": status,
            }

        await run_items(
            items,
            one,
            answers_file,
            concurrency=args.concurrency,
            resume=True,
            repair=True,
            label="lm-probe",
        )


async def _lm_judge(args: argparse.Namespace) -> None:
    answers_file, judge_file = _lm_paths(args)
    answers = load_json(answers_file)
    ok = [a for a in answers if a.get("status_code") == 200]
    print(f"[lm-judge] 有效答卷 {len(ok)}/{len(answers)} <- {os.path.basename(answers_file)}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:

        async def one(a: Dict[str, Any]) -> Dict[str, Any]:
            j = await judge_single_answer(
                client=client,
                base_url=args.base_url,
                question=a.get("question", ""),
                standard_answer=a.get("standard_answer", ""),
                agent_answer=a.get("agent_answer", ""),
                timeout=args.timeout,
            )
            return {
                "question_id": a["question_id"],
                "question_type": a.get("question_type", "unknown"),
                "judge": {"passed": bool(j.get("correct")), "reason": j.get("reason", "")},
            }

        await run_items(
            ok,
            one,
            judge_file,
            concurrency=args.concurrency,
            resume=True,
            repair=True,
            label="lm-judge",
        )
    _lm_report(judge_file)


def _lm_report(judge_file: str = LM_JUDGE) -> None:
    if not os.path.isfile(judge_file):
        print("[lm-report] 无判分文件")
        return
    records = load_json(judge_file)
    stats = summarize_by(records, type_field="question_type")
    total = stats.pop("__all__")
    print("\n===== LongMemEval 结果 =====")
    for cat, s in sorted(stats.items()):
        print(f"  {cat:28s} {s['passed']}/{s['total']} ({s['passed'] / max(s['total'], 1) * 100:.1f}%)")
    print(f"  TOTAL: {total['passed']}/{total['total']} ({total['passed'] / max(total['total'], 1) * 100:.2f}%)")
    summary_name = "summary_runner.json"
    if judge_file != LM_JUDGE:
        summary_name = "summary_" + os.path.splitext(os.path.basename(judge_file))[0] + ".json"
    dump_json(os.path.join(LM_RESULTS, summary_name), {"stats": stats, "total": total})


def _lm_diagnose(args: argparse.Namespace) -> None:
    """对照答卷/判分，并可选查库里金标词是否落在该题 scope。"""
    import re
    import sqlite3

    answers_file, judge_file = _lm_paths(args)
    if not os.path.isfile(answers_file):
        print(f"[lm-diagnose] 无答卷 {answers_file}")
        return
    answers = load_json(answers_file)
    judges = {r["question_id"]: r for r in (load_json(judge_file) if os.path.isfile(judge_file) else [])}
    passed = 0
    total = 0
    db = os.path.join(_PROJECT_ROOT, "data", "GsData.db")
    conn: sqlite3.Connection | None = sqlite3.connect(db) if os.path.isfile(db) else None
    tok_re = re.compile(r"[A-Za-z]{4,}|[0-9]{3,}|[一-鿿]{2,}")
    print(f"[lm-diagnose] answers={os.path.basename(answers_file)} judge={os.path.basename(judge_file)}")
    for a in answers:
        qid = str(a.get("question_id") or "")
        rec = judges[qid] if qid in judges else {}
        judge = rec["judge"] if "judge" in rec and isinstance(rec["judge"], dict) else {}
        ok = bool(judge["passed"]) if "passed" in judge else False
        total += 1
        if ok:
            passed += 1
        gold = str(a.get("standard_answer") or "")
        ans = str(a.get("agent_answer") or "").replace("\n", " / ")
        mark = "PASS" if ok else "FAIL"
        print(f"\n{mark} {qid}  {(a.get('question') or '')[:80]}")
        print(f"  GOLD {gold[:140].replace(chr(10), ' / ')}")
        print(f"  ANS  {ans[:160]}")
        if conn is None or not qid:
            continue
        scope = f"user_global:eval_{qid}"
        n_ep = conn.execute("SELECT COUNT(*) FROM aimemepisode WHERE scope_key=?", (scope,)).fetchone()
        ep_n = int(n_ep[0]) if n_ep else 0
        kws = []
        seen: set[str] = set()
        for m in tok_re.finditer(gold):
            w = m.group(0)
            key = w.lower()
            if key in seen or len(w) < 4:
                continue
            seen.add(key)
            kws.append(w)
            if len(kws) >= 6:
                break
        hits = []
        for kw in kws:
            row = conn.execute(
                "SELECT COUNT(*) FROM aimemepisode WHERE scope_key=? AND content LIKE ?",
                (scope, f"%{kw}%"),
            ).fetchone()
            hits.append(f"{kw}={int(row[0]) if row else 0}")
        print(f"  DB   episodes={ep_n}  {', '.join(hits)}")
    if conn is not None:
        conn.close()
    print(f"\n===== diagnose {passed}/{total} ({passed / max(total, 1) * 100:.1f}%) =====")


def _domain_files(qtype: str, tag: str | None) -> tuple[str, str]:
    slug = qtype.replace("-", "_")
    suffix = f"_{tag}" if tag else ""
    return (
        os.path.join(LM_RESULTS, f"answers_{slug}{suffix}.json"),
        os.path.join(LM_RESULTS, f"judge_{slug}{suffix}.json"),
    )


async def _lm_run_domains(args: argparse.Namespace) -> None:
    """按域 probe+judge。生产 Chat：tools + 目录卡 + clock_at + skip-ingest。"""
    types = (args.question_type,) if args.question_type else LM_DOMAIN_ORDER
    tag = args.tag if args.tag else None
    args.skip_ingest = True
    args.inject_date = True
    args.enable_tools = True
    args.no_memory_eval = True
    args.concurrency = 1
    for qtype in types:
        args.question_type = qtype
        args.answers_file, args.judge_file = _domain_files(qtype, tag)
        print(f"\n===== domain {qtype} =====", flush=True)
        args.timeout = 4000.0
        await _lm_probe(args)
        args.timeout = 180.0
        await _lm_judge(args)
    print("\n===== run-domains done =====", flush=True)


def _lm_answer_is_infra(answer: dict[str, object]) -> bool:
    """传输/进程故障，不是内容 FAIL。内容错题不得被 mark-fails 重跑。"""
    status = answer["status_code"] if "status_code" in answer else 200
    if isinstance(status, int) and status not in (200, 0):
        return True
    raw = answer["agent_answer"] if "agent_answer" in answer else ""
    text = raw if isinstance(raw, str) else str(raw)
    if not text.strip():
        return True
    if text.startswith("[ERROR]"):
        return True
    low = text.lower()
    if "connecterror" in low or "all connection" in low:
        return True
    return "session_log_not_found" in low


def _lm_mark_fails(args: argparse.Namespace) -> None:
    """只把传输/空答失败改成 [ERROR] rerun，内容 FAIL 保留。"""
    tag = args.tag if args.tag else None
    if args.answers_file or args.judge_file:
        pairs = [_lm_paths(args)]
    else:
        types = (args.question_type,) if args.question_type else LM_DOMAIN_ORDER
        pairs = [_domain_files(qtype, tag) for qtype in types]
    total_fail = 0
    for ans_path, jdg_path in pairs:
        if not os.path.isfile(ans_path) or not os.path.isfile(jdg_path):
            print(f"[mark-fails] skip missing {os.path.basename(ans_path)}")
            continue
        answers = load_json(ans_path)
        judges = load_json(jdg_path)
        answers_by_id: dict[str, dict[str, object]] = {}
        for a in answers:
            if isinstance(a, dict) and "question_id" in a:
                answers_by_id[str(a["question_id"])] = a
        fail_ids: set[str] = set()
        for it in judges:
            if not isinstance(it, dict) or "question_id" not in it:
                continue
            judge = it["judge"] if "judge" in it else None
            if not isinstance(judge, dict):
                continue
            passed = bool(judge["passed"]) if "passed" in judge else False
            if passed:
                continue
            qid = str(it["question_id"])
            ans = answers_by_id[qid] if qid in answers_by_id else None
            if ans is None or _lm_answer_is_infra(ans):
                fail_ids.add(qid)
        n_ans = 0
        for a in answers:
            if not isinstance(a, dict) or "question_id" not in a:
                continue
            if str(a["question_id"]) not in fail_ids:
                continue
            a["agent_answer"] = "[ERROR] rerun"
            a["status_code"] = -1
            n_ans += 1
        dump_json(ans_path, answers)
        kept = [
            j for j in judges if isinstance(j, dict) and "question_id" in j and str(j["question_id"]) not in fail_ids
        ]
        dump_json(jdg_path, kept)
        total_fail += len(fail_ids)
        print(f"[mark-fails] {os.path.basename(ans_path)}: marked {n_ans} infra fails, kept {len(kept)} judges")
    print(f"[mark-fails] total infra fails marked {total_fail}")


# ─────────────────────────────────────────────
# BEAM（委托既有脚本，保持状态文件/CLI 兼容）
# ─────────────────────────────────────────────


def _beam_delegate(stage: str, extra: List[str]) -> int:
    script = os.path.join(_PROJECT_ROOT, "eval", "BEAM_10M", "run_beam_eval.py")
    cmd = [sys.executable, script, stage, *extra]
    print("[beam] delegate:", " ".join(cmd))
    return subprocess.call(cmd, cwd=_PROJECT_ROOT)


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description="统一评测入口")
    p.add_argument("benchmark", choices=["longmem", "beam"])
    p.add_argument("stage", help="longmem: probe/judge/report/diagnose/run-domains/mark-fails; beam: 透传")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--eval-data", default=None)
    p.add_argument("--start", type=int, default=None)
    p.add_argument("--end", type=int, default=None)
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument(
        "--extract", action="store_true", help="probe: 摄入时跑 LLM 实体/边抽取（System-1 图检索，非纯 episode-RAG）"
    )
    p.add_argument("--system2", action="store_true", help="probe: 作答时启用 System-2 分层图遍历")
    p.add_argument(
        "--inject-date", action="store_true", help="probe: 把 question_date 作为 clock_at 传入（时序推理必需）"
    )
    p.add_argument("--clear-first", action="store_true", help="probe: 每题摄入前清空该 scope，避免历史遗留污染")
    p.add_argument("--answers-file", default=None, help="覆盖答卷文件路径（子集实验隔离用）")
    p.add_argument("--judge-file", default=None, help="覆盖判分文件路径（子集实验隔离用）")
    p.add_argument(
        "--question-type",
        default=None,
        help="probe/load: 只跑该 question_type（如 single-session-preference）",
    )
    p.add_argument(
        "--skip-ingest",
        action="store_true",
        help="probe: 跳过 clear/batch_observe，复用库里已摄入记忆只重作答",
    )
    p.add_argument(
        "--persona-name",
        default="评测助手",
        help="probe: 作答人格（默认评测助手：必须作答、禁止静音）",
    )
    p.add_argument(
        "--enable-tools",
        action="store_true",
        help="probe: 装配真实工具（含 search_cognition），对齐生产 Chat",
    )
    p.add_argument(
        "--no-memory-eval",
        action="store_true",
        help="probe: 不灌评测证据块，走生产目录卡 + 模型自己 search_cognition",
    )
    p.add_argument("--tag", default=None, help="run-domains/mark-fails: 答卷文件后缀，如 prod7")
    args, extra = p.parse_known_args()

    if args.benchmark == "beam":
        return _beam_delegate(args.stage, extra)
    if args.stage == "probe":
        asyncio.run(_lm_probe(args))
    elif args.stage == "judge":
        asyncio.run(_lm_judge(args))
    elif args.stage == "report":
        _, judge_file = _lm_paths(args)
        _lm_report(judge_file)
    elif args.stage == "diagnose":
        _lm_diagnose(args)
    elif args.stage == "run-domains":
        asyncio.run(_lm_run_domains(args))
    elif args.stage == "mark-fails":
        _lm_mark_fails(args)
    else:
        print(f"未知 stage: {args.stage}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
