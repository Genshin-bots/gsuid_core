"""通用评测 runner：probe / judge 的并发 + 断点续跑 + 坏答卷修复（repair）骨架。

BEAM_10M 与 LongMemEval（以及未来新增基准）共用的执行层：
- **resume**：answers/judge 文件按 ``question_id`` 增量跳过；
- **repair**：加载已有结果时剔除失败记录（``[ERROR]`` / ``评判请求失败`` / 超时空答），
  这些题会被重新执行——替代以前会话 scratchpad 里的 repair_full.py 手工流程；
- **并发**：Semaphore 限流 + 落盘串行锁，答卷文件随做随存，任意中断安全续跑。

用法（各基准脚本只需提供"单题执行"协程）::

    async def answer_one(item) -> dict: ...  # 返回含 question_id 的记录


    await run_items(items, answer_one, out_file, concurrency=12, resume=True)
"""

from __future__ import annotations

import os
import json
import asyncio
from typing import Any, Dict, List, Callable, Iterable, Optional, Awaitable

from eval.common.io import dump_json

# 判定"这条记录是坏的、需要重跑"的默认标记（出现在 agent_answer / judge.reason 里）
# 含 agent 管线把 LLM 连接/限流错误当正文返回的情形（判分侧 parse 失败会写进 judge.reason）。
FAILURE_MARKERS = (
    "[ERROR]",
    "评判请求失败",
    "评判超时",
    "request failed",
    "Connection error",
    "执行出错",
    "无法解析评判回复",
)


def is_failed_record(rec: Dict[str, Any], failure_markers: Iterable[str] = FAILURE_MARKERS) -> bool:
    """答卷/评判记录是否属于执行失败（应剔除重跑），而非真实的错误答案。"""
    texts: List[str] = []
    for key in ("agent_answer", "answer"):
        v = rec.get(key)
        if isinstance(v, str):
            texts.append(v)
    judge = rec.get("judge")
    if isinstance(judge, dict):
        reason = judge.get("reason")
        if isinstance(reason, str):
            texts.append(reason)
        if judge.get("error"):
            return True
    if rec.get("status_code") not in (None, 200):
        return True
    return any(m in t for t in texts for m in failure_markers)


def load_resume(
    out_file: str,
    *,
    id_field: str = "question_id",
    repair: bool = True,
    failure_markers: Iterable[str] = FAILURE_MARKERS,
) -> tuple[List[Dict[str, Any]], set]:
    """加载已有结果用于断点续跑。

    返回 (保留的记录列表, 已完成 id 集合)。``repair=True`` 时失败记录被剔除
    （不进入已完成集合 → 会被重跑）。
    """
    if not os.path.isfile(out_file):
        return [], set()
    try:
        with open(out_file, "r", encoding="utf-8") as f:
            records = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[resume] 读取 {out_file} 失败（从头开始）: {e}")
        return [], set()
    if not isinstance(records, list):
        return [], set()
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for rec in records:
        if not isinstance(rec, dict) or not rec.get(id_field):
            continue
        if repair and is_failed_record(rec, failure_markers):
            dropped += 1
            continue
        kept.append(rec)
    if dropped:
        print(f"[resume] 剔除 {dropped} 条失败记录（将重跑），保留 {len(kept)} 条")
    return kept, {r[id_field] for r in kept}


async def run_items(
    items: List[Dict[str, Any]],
    one: Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]],
    out_file: str,
    *,
    id_field: str = "question_id",
    concurrency: int = 1,
    resume: bool = True,
    repair: bool = True,
    label: str = "run",
) -> List[Dict[str, Any]]:
    """通用并发执行：对每个未完成 item 调 ``one(item)``，结果随做随存到 out_file。

    ``one`` 返回 None 表示该题跳过（不落盘、下次续跑仍会执行）；抛异常则记录错误占位。
    """
    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    results, done_ids = load_resume(out_file, id_field=id_field, repair=repair) if resume else ([], set())
    todo = [it for it in items if it.get(id_field) not in done_ids]
    print(f"[{label}] 共 {len(items)} 题，已完成 {len(done_ids)}，本次执行 {len(todo)}，并发 {concurrency}")
    if not todo:
        return results

    sem = asyncio.Semaphore(max(concurrency, 1))
    lock = asyncio.Lock()
    counter = {"done": 0}

    async def _wrapped(item: Dict[str, Any]) -> None:
        async with sem:
            qid = item.get(id_field, "?")
            try:
                rec = await one(item)
            except Exception as e:  # noqa: BLE001 —— 单题失败不拖垮整轮
                rec = {id_field: qid, "agent_answer": f"[ERROR] {e}", "status_code": -1}
                print(f"[{label}] {qid} 异常: {e}")
            if rec is None:
                return
            async with lock:
                results.append(rec)
                counter["done"] += 1
                dump_json(out_file, results)
                print(f"[{label}] {qid} done ({counter['done']}/{len(todo)})")

    await asyncio.gather(*[_wrapped(it) for it in todo])
    return results


def summarize_by(
    records: List[Dict[str, Any]],
    *,
    type_field: str = "category",
    passed_fn: Callable[[Dict[str, Any]], bool] = lambda r: bool((r.get("judge") or {}).get("passed")),
) -> Dict[str, Dict[str, int]]:
    """按类别汇总 pass/total，返回 {category: {passed, total}}（附 __all__ 总计）。"""
    stats: Dict[str, Dict[str, int]] = {}
    total = {"passed": 0, "total": 0}
    for r in records:
        cat = str(r.get(type_field, "unknown"))
        s = stats.setdefault(cat, {"passed": 0, "total": 0})
        s["total"] += 1
        total["total"] += 1
        if passed_fn(r):
            s["passed"] += 1
            total["passed"] += 1
    stats["__all__"] = total
    return stats
