"""统一评测入口：``python eval/run_eval.py <benchmark> <stage> [...]``。

所有基准共用 ``eval/common/runner.py`` 的并发/断点续跑/坏答卷修复骨架；
新增基准只需实现三个钩子（加载题目 / 单题作答 / 单题判分）注册进 BENCHMARKS。

用法::

  # LongMemEval：摄入+作答（episode-RAG，System-1），再判分
  python eval/run_eval.py longmem probe --concurrency 12 [--start 0 --end 100]
  python eval/run_eval.py longmem judge --concurrency 12
  python eval/run_eval.py longmem report

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
    return data[s:e]


async def _lm_probe(args: argparse.Namespace) -> None:
    from eval.longmemeval.run_longmem_eval import flatten_haystack_with_dates

    extract = getattr(args, "extract", False)
    system2 = getattr(args, "system2", False)
    inject_date = getattr(args, "inject_date", False)
    clear_first = getattr(args, "clear_first", False)
    # extract=True 会额外跑 LLM 实体/边抽取，一窗口一次；用 batch_observe 的 extra_payload 透传，
    # 让 System-1 检索能命中 entity/edge（而非纯 episode-RAG）。
    extra_payload = {"extract": True, "extract_concurrency": 3} if extract else None
    answers_file, _ = _lm_paths(args)
    print(
        f"[lm-probe] extract={extract} system2={system2} inject_date={inject_date} "
        f"clear_first={clear_first} concurrency={args.concurrency} -> {os.path.basename(answers_file)}"
    )
    items = _lm_load(args)
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:

        async def one(q: Dict[str, Any]) -> Dict[str, Any]:
            qid = q["question_id"]
            user_id = f"eval_{qid}"
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
            # 注入"当前时间"：temporal-reasoning 依赖"今天"计算"多少天前"
            message = q["question"]
            if inject_date:
                cur = _fmt_question_date(q.get("question_date", ""))
                if cur:
                    message = f"当前时间：{cur}\n\n{q['question']}"
            resp = await call_chat_with_history(
                client=client,
                base_url=args.base_url,
                user_id=user_id,
                message=message,
                history=[],
                timeout=args.timeout,
                enable_observer=False,
                enable_system2=system2,
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
    p.add_argument("stage", help="longmem: probe/judge/report; beam: 透传 run_beam_eval 子命令")
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
        "--inject-date", action="store_true", help="probe: 把 question_date 作为'当前时间'注入作答消息（时序推理必需）"
    )
    p.add_argument("--clear-first", action="store_true", help="probe: 每题摄入前清空该 scope，避免历史遗留污染")
    p.add_argument("--answers-file", default=None, help="覆盖答卷文件路径（子集实验隔离用）")
    p.add_argument("--judge-file", default=None, help="覆盖判分文件路径（子集实验隔离用）")
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
    else:
        print(f"未知 stage: {args.stage}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
