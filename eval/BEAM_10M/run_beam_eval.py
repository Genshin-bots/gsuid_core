"""BEAM-10M 评测脚本

针对 gsuid_core 框架的 AI Memory 模块，对 BEAM-10M（10 conversations × 10 plans ×
100+ turns，单对话最高 1000 万 Token）做长上下文记忆评测。

=============================================================================
数据集布局（参考 ``eval/BEAM_10M/README.md``）
=============================================================================

每条 row（1 条 conversation）字段：

  conversation_id       : str / int
  conversation_seed     : dict  {category, id, mode, subtopics, theme, timeline, title}
  user_profile          : dict  {user_info, user_relationships}
  narratives            : str   整段叙事情节背景
  conversation_plan     : str
  user_questions        : array  期间穿插的用户问题
  chat                  : dict  {plan-1: [batch, ...], ..., plan-10: [batch, ...]}
                          每个 batch = {batch_number, time_anchor, turns}
                          每个 turn  = {role, content, id, index, question_type, time_anchor}
  plans                 : array of plan dicts（与 chat 同构，按 1..10 编号）
                          每个 plan 含独立 user_questions / conversation_seed / chat
  probing_questions     : str（Python repr，可 ast.literal_eval）
                          含 10 个类别，每类 2 题；每题字段随类别变化
                          (ideal_response / ideal_answer / answer /
                           expected_compliance / ideal_summary)

=============================================================================
评测流程
=============================================================================

每条 conversation 分配唯一 ``user_id = beam_eval_<conv_idx>``，scope=user_global：

  1. clear          —— 清空该 user 的全部记忆（``/api/ai/memory/users/{user}/global/clear``）
  2. ingest-plan    —— 把指定 plan 的全部 turn 通过 ``/api/ai/memory/batch_observe``
                        灌入记忆，turn.time_anchor 用作观测时间戳
  3. probe          —— 对 20 道探针题逐条调 ``/api/chat_with_history``（enable_observer=False），
                        收集 ``agent_answer`` 与 ``memory`` 字段
  4. judge          —— 用 ``eval.common.judge.judge_beam_single`` 按 rubric 给分

``all`` 子命令串联 1→4 跑一个会话；多个 plan 累计测试可用 ``ingest-batch`` 单独跑摄入，
再 ``probe`` 一次拿到对应的长度-准确率曲线。

=============================================================================
用法示例
=============================================================================

  # 单 plan 一站式
  python eval/BEAM_10M/run_beam_eval.py all --conv 0 --plans 1

  # 多 plan 累计
  python eval/BEAM_10M/run_beam_eval.py ingest-batch --conv 0 --plans 1,2,3
  python eval/BEAM_10M/run_beam_eval.py probe --conv 0

  # 仅评判已有答卷
  python eval/BEAM_10M/run_beam_eval.py judge --answers eval/BEAM_10M/results/answers_0.json

  # 全部 10 条对话 × 单 plan 批量跑
  for i in $(seq 0 9); do
    python eval/BEAM_10M/run_beam_eval.py all --conv $i --plans 1
  done
"""

from __future__ import annotations

import os
import ast
import sys
import time
import asyncio
import argparse
from typing import Any, Set, Dict, List, Tuple, Optional

import httpx

# 允许以 ``python eval/BEAM_10M/run_beam_eval.py`` 直接运行
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from eval.common import (  # noqa: E402
    DEFAULT_TIMEOUT,
    DEFAULT_BASE_URL,
    dump_json,
    load_json,
    judge_beam_single,
    read_existing_ids,
    call_batch_observe,
    call_chat_with_history,
    call_clear_user_global,
    extract_text_from_response,
)

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

DEFAULT_PARQUET_GLOB = "eval/BEAM_10M/data/10M-*.parquet"
DEFAULT_OUTPUT_DIR = "eval/BEAM_10M/results"

# BEAM-10M 标准答案字段在不同类别下的命名
_STANDARD_ANSWER_FIELD = {
    "abstention": "ideal_response",
    "contradiction_resolution": "ideal_answer",
    "event_ordering": "answer",
    "information_extraction": "answer",
    "instruction_following": "expected_compliance",
    "knowledge_update": "answer",
    "multi_session_reasoning": "answer",
    "preference_following": "expected_compliance",
    "summarization": "ideal_summary",
    "temporal_reasoning": "answer",
}

# user_id 模板（每条 conversation 唯一）
USER_ID_TEMPLATE = "beam_eval_{conv_id}"


# ─────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────


def _read_parquet_rows(
    parquet_paths: List[str],
    columns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """读取 parquet 文件，concat 成 List[Dict]。

    优先用 pyarrow（更快）；不可用时回退到 pandas。
    ``columns`` 可做列裁剪：probe/judge 只需 ``probing_questions``，
    跳过 ~12M token 的 ``chat``/``plans`` 列可把客户端峰值内存从 ~3.7GB 降到 ~百MB。
    """
    if not parquet_paths:
        raise FileNotFoundError(f"未找到 BEAM-10M parquet 文件，请检查路径: {parquet_paths}")

    try:
        import pyarrow.parquet as pq  # type: ignore

        rows: List[Dict[str, Any]] = []
        for p in parquet_paths:
            tbl = pq.read_table(p, columns=columns)
            rows.extend(tbl.to_pylist())
        return rows
    except ImportError:
        pass

    import pandas as pd  # type: ignore

    frames = [pd.read_parquet(p, columns=columns) for p in parquet_paths]
    df = pd.concat(frames, ignore_index=True)
    raw_records: List[Dict[str, Any]] = [{str(k): v for k, v in row.items()} for row in df.to_dict(orient="records")]
    return raw_records


def load_beam_dataset(
    parquet_glob: str = DEFAULT_PARQUET_GLOB,
    columns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """加载 BEAM-10M 数据集。

    Args:
        parquet_glob: 支持 glob 模式（如 ``eval/BEAM_10M/data/10M-*.parquet``）；
                      也支持单文件路径。

    Returns:
        List[Dict]，每项是一条 conversation。
    """
    import glob as _glob

    candidates = _glob.glob(parquet_glob) if any(c in parquet_glob for c in "*?[") else [parquet_glob]
    candidates = [c for c in candidates if c.endswith(".parquet") and os.path.isfile(c)]
    rows = _read_parquet_rows(sorted(candidates), columns=columns)

    # 标准化：把 probing_questions 解析回 dict
    for idx, row in enumerate(rows):
        pq_raw = row.get("probing_questions")
        if isinstance(pq_raw, str):
            try:
                row["probing_questions"] = ast.literal_eval(pq_raw)
            except Exception as e:
                print(f"[Loader] conv {idx} probing_questions 解析失败: {e}")
                row["probing_questions"] = {}
        elif pq_raw is None:
            row["probing_questions"] = {}

    print(f"[Loader] 已加载 {len(rows)} 条 conversation")
    return rows


def normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """把单个 plan dict 标准化为 ``{"plan_id": int, "batches": [...]}``。

    BEAM-10M 数据集中 plan 的 ``chat`` 字段存在多种表示：

    - 标准结构：``chat = [batch, ...]``，每个 ``batch = {batch_number, time_anchor, turns}``；
    - 嵌套 numpy 数组（pandas 读取后）：``chat = np.array([np.array([turn, ...]), ...])``，
      此时每个 batch 本身就是 turn dict 的数组，不再含 ``batch_number``；
    - 扁平数组：``chat = [turn, ...]``。

    本函数统一把每条 turn 抽出来并保留 batch 级元信息（缺失则填空）。
    """
    plan_id_raw = plan.get("plan_id")
    if plan_id_raw is None:
        plan_id_raw = plan.get("id") or 0
    try:
        plan_id = int(plan_id_raw)
    except (TypeError, ValueError):
        plan_id = 0

    chat = plan.get("chat") or []
    batches_out: List[Dict[str, Any]] = []

    def _to_list(x: Any) -> list:
        if hasattr(x, "tolist"):
            return x.tolist()
        return list(x) if x is not None else []

    for batch in chat:
        # case A: dict with batch_number / time_anchor / turns
        if isinstance(batch, dict):
            turn_arr = batch.get("turns") or []
            batch_meta = {
                "batch_number": batch.get("batch_number"),
                "time_anchor": batch.get("time_anchor"),
                "turns": _to_list(turn_arr),
            }
            # turns 自身可能仍是 2D 数组：外层是 batch 中各"轮次组"
            if (
                batch_meta["turns"]
                and isinstance(batch_meta["turns"][0], (list, tuple))
                or (batch_meta["turns"] and hasattr(batch_meta["turns"][0], "tolist"))
            ):
                flat: List[Dict[str, Any]] = []
                for sub in batch_meta["turns"]:
                    flat.extend(_to_list(sub))
                batch_meta["turns"] = flat
            batches_out.append(batch_meta)
            continue

        # case B: numpy array / list of turn dicts
        items = _to_list(batch)
        if items and isinstance(items[0], dict):
            batches_out.append(
                {
                    "batch_number": None,
                    "time_anchor": None,
                    "turns": items,
                }
            )
        # case C: 嵌套 2D 数组（外层 batch，内层 turn 列表）
        elif items and isinstance(items[0], (list, tuple)) or (items and hasattr(items[0], "tolist")):
            flat2: List[Dict[str, Any]] = []
            for sub in items:
                flat2.extend(_to_list(sub))
            batches_out.append({"batch_number": None, "time_anchor": None, "turns": flat2})

    return {"plan_id": plan_id, "batches": batches_out}


def extract_turns_from_plan(plan_norm: Dict[str, Any]) -> List[Dict[str, str]]:
    """从标准化 plan 中抽出 ``[{"role","content","time_anchor"?}, ...]`` turn 列表。

    每条 turn 同时保留 ``plan_id`` 与 ``batch_number``，便于回溯。
    BEAM-10M 中一个 batch 内通常仅首条 turn 带 ``time_anchor``，本函数对后续
    同 batch 的空 anchor 做"向前回填"——继承本 batch 内最近一条非空 anchor。
    """
    turns: List[Dict[str, str]] = []
    for batch in plan_norm["batches"]:
        batch_anchor = batch.get("time_anchor")
        last_anchor: Optional[str] = batch_anchor if batch_anchor else None
        for turn in batch.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role") or "").lower()
            content = str(turn.get("content") or "").strip()
            if not content or role not in {"user", "assistant"}:
                continue
            ta = turn.get("time_anchor") or last_anchor or ""
            if ta:
                last_anchor = str(ta)
            turns.append(
                {
                    "role": role,
                    "content": content,
                    "time_anchor": str(ta),
                    "plan_id": str(plan_norm["plan_id"]),
                    "batch_number": str(batch.get("batch_number") or ""),
                }
            )
    return turns


def parse_time_anchor(time_anchor: str, fallback: Optional[float] = None) -> Optional[str]:
    """把 BEAM-10M turn.time_anchor 解析为 ISO8601 字符串。

    BEAM-10M 中常见的几种 time_anchor 写法：

    - ``"July-01-2024"``         → 2024-07-01T00:00:00Z
    - ``"2024-07-01"``           → 2024-07-01T00:00:00Z
    - ``"2024-07-01 10:00:00"``  → 2024-07-01T10:00:00Z
    - ``"2024-07-01T10:00:00"``  → 2024-07-01T10:00:00Z
    - ``"2024/07/01 10:00:00"``  → 2024-07-01T10:00:00Z

    一律按 UTC 解析。无法解析时返回 ``None``，调用方退到当前时间。
    """
    if not time_anchor or not isinstance(time_anchor, str):
        return None
    from datetime import datetime as _dt, timezone as _tz

    s = time_anchor.strip()
    if not s:
        return None

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%B-%d-%Y",
        "%b-%d-%Y",
        "%d-%B-%Y",
        "%d-%b-%Y",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
    )
    for fmt in formats:
        try:
            return _dt.strptime(s, fmt).replace(tzinfo=_tz.utc).isoformat()
        except ValueError:
            continue
    return None


def iter_probing_questions(row: Dict[str, Any]) -> List[Tuple[str, int, Dict[str, Any]]]:
    """遍历一行 conversation 的全部 20 道探针题。

    Returns:
        List of ``(category, index_in_category, probe_dict)``。
    """
    out: List[Tuple[str, int, Dict[str, Any]]] = []
    for cat, items in (row.get("probing_questions") or {}).items():
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if isinstance(item, dict):
                out.append((cat, i, item))
    return out


def extract_standard_answer(probe: Dict[str, Any], category: str) -> str:
    """从单道探针题中抽取标准答案（按类别取不同字段）。"""
    field = _STANDARD_ANSWER_FIELD.get(category, "answer")
    val = probe.get(field)
    if val is None:
        # 兜底：所有类别都尝试 answer
        val = probe.get("answer")
    return str(val).strip() if val is not None else ""


# ─────────────────────────────────────────────
# 子命令实现
# ─────────────────────────────────────────────


async def cmd_clear(
    base_url: str,
    user_id: str,
    timeout: float,
) -> None:
    """清空 ``user_global:<user_id>`` 范围内的全部记忆。"""
    print(f"[Clear] user_id={user_id}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        resp = await call_clear_user_global(client, base_url, user_id)
    print(f"[Clear] -> {resp}")


async def cmd_ingest_plan(
    base_url: str,
    user_id: str,
    plan: Dict[str, Any],
    *,
    flush: bool = True,
    trigger_rebuild: bool = False,
    timeout: float = 300.0,
) -> Dict[str, Any]:
    """把单个 plan 的所有 turn 通过 ``batch_observe`` 灌入。"""
    plan_id = plan["plan_id"]
    turns = extract_turns_from_plan(plan)
    print(f"[Ingest] user_id={user_id}, plan_id={plan_id}, turns={len(turns)}")

    # 把 time_anchor 转成 ISO8601
    payload_turns: List[Dict[str, Any]] = []
    for t in turns:
        item: Dict[str, Any] = {"role": t["role"], "content": t["content"]}
        iso = parse_time_anchor(t.get("time_anchor", ""))
        if iso:
            item["timestamp"] = iso
        payload_turns.append(item)

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        resp = await call_batch_observe(
            client=client,
            base_url=base_url,
            user_id=user_id,
            turns=payload_turns,
            scope_type="user_global",
            flush=flush,
            trigger_rebuild=trigger_rebuild,
        )

    print(f"[Ingest] plan_id={plan_id} -> {resp.get('status')}: {resp.get('msg')} data={resp.get('data')}")
    return {"plan_id": plan_id, "turns": len(turns), "response": resp}


async def cmd_ingest_batch(
    base_url: str,
    user_id: str,
    plans: List[Dict[str, Any]],
    *,
    flush: bool = True,
    trigger_rebuild: bool = False,
    timeout: float = 300.0,
) -> List[Dict[str, Any]]:
    """按 plan 顺序累计摄入多个 plan，每个 plan 一次 batch_observe。"""
    results: List[Dict[str, Any]] = []
    for plan in plans:
        r = await cmd_ingest_plan(
            base_url=base_url,
            user_id=user_id,
            plan=plan,
            flush=flush,
            trigger_rebuild=trigger_rebuild,
            timeout=timeout,
        )
        results.append(r)
    return results


async def cmd_probe(
    base_url: str,
    user_id: str,
    probes: List[Tuple[str, int, Dict[str, Any]]],
    *,
    answers_file: str,
    timeout: float = DEFAULT_TIMEOUT,
    enable_observer: bool = False,
    enable_system2: bool = True,
    resume: bool = True,
) -> str:
    """遍历 20 道探针题，逐条调 ``chat_with_history`` 收集回答。

    Args:
        probes: :func:`iter_probing_questions` 输出。
        answers_file: 答卷落盘路径；每答完一题立即写入（断点续跑友好）。
        resume: 启用增量更新（跳过已有 ``question_id`` 的题目）。
    """
    existing_ids: Set[str] = set()
    existing_results: List[Dict[str, Any]] = []
    if resume and os.path.isfile(answers_file):
        try:
            existing_results = load_json(answers_file)
            if not isinstance(existing_results, list):
                existing_results = []
            existing_ids = read_existing_ids(answers_file, id_field="question_id")
        except Exception as e:
            print(f"[Probe] 读取已有答卷失败: {e}")

    print(f"[Probe] user_id={user_id}，共 {len(probes)} 题，已存在 {len(existing_ids)} 条")

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        # 探活用 openapi.json：能同时验证「服务在线」与「local-test gate 已开」——
        # chat_with_history 仅在 GSUID_LOCAL_TEST_MODE=1 时才注册进 schema
        try:
            spec = await client.get(f"{base_url}/openapi.json", timeout=5.0)
            paths = spec.json().get("paths", {}) if spec.status_code == 200 else {}
            if "/api/chat_with_history" in paths:
                print(f"[Probe] 服务在线且 local-test gate 已开 (status={spec.status_code})")
            else:
                print(
                    "[Probe] ⚠️ 服务在线但未见 /api/chat_with_history；请确认服务进程设了 "
                    "GSUID_LOCAL_TEST_MODE=1（否则评测端点全 404）"
                )
        except Exception as e:
            print(f"[Probe] 服务连接失败: {e}（请确认 gsuid_core 服务已启动）")
            return answers_file

        results: List[Dict[str, Any]] = list(existing_results)

        for category, idx_in_cat, probe in probes:
            qid = f"{user_id}__{category}__{idx_in_cat}"
            if qid in existing_ids:
                continue

            question = probe.get("question") or ""
            standard_answer = extract_standard_answer(probe, category)
            time_anchor = probe.get("time_anchor", "")
            rubric = probe.get("rubric") or []
            if isinstance(rubric, str):
                rubric = [rubric]

            print(f"\n[Probe] ({category} #{idx_in_cat}) {question[:80]}")

            resp = await call_chat_with_history(
                client=client,
                base_url=base_url,
                user_id=user_id,
                message=question,
                history=[],
                enable_observer=enable_observer,
                # 评测已在摄入收尾触发分层图重建，探针显式开 System-2 以利用它：
                # 事件排序/摘要/跨会话等聚合题靠类目自顶向下遍历召回，纯 System-1 向量召回不足。
                enable_system2=enable_system2,
            )

            status_code = resp.get("status_code", -1)
            if status_code == 200:
                agent_answer = extract_text_from_response(resp.get("data"))
                memory = resp.get("memory")
            else:
                error_msg = resp.get("error", "unknown")
                agent_answer = f"[ERROR] status={status_code}, error={error_msg}"
                memory = None

            record = {
                "question_id": qid,
                "category": category,
                "question": question,
                "standard_answer": standard_answer,
                "agent_answer": agent_answer,
                "memory": memory,
                "rubric": list(rubric),
                "time_anchor": str(time_anchor),
                "status_code": status_code,
                "user_id": user_id,
            }
            results.append(record)
            dump_json(answers_file, results)

            preview = agent_answer[:200].replace("\n", " ")
            print(f"  -> {preview}{'...' if len(agent_answer) > 200 else ''}")

    print(f"\n[Probe] 完成，答卷: {answers_file}")
    return answers_file


async def cmd_judge(
    base_url: str,
    answers_file: str,
    judge_file: str,
    *,
    timeout: float = 240.0,
    resume: bool = True,
) -> str:
    """用 rubric-based judge 给分，支持断点续跑。"""
    answers = load_json(answers_file)
    if not isinstance(answers, list):
        raise ValueError(f"answers_file 格式异常: {answers_file}")

    existing_ids: Set[str] = set()
    existing_results: List[Dict[str, Any]] = []
    if resume and os.path.isfile(judge_file):
        try:
            existing_results = load_json(judge_file)
            if not isinstance(existing_results, list):
                existing_results = []
            existing_ids = read_existing_ids(judge_file, id_field="question_id")
        except Exception as e:
            print(f"[Judge] 读取已有评判失败: {e}")

    print(f"[Judge] 共 {len(answers)} 条答卷，已评判 {len(existing_ids)} 条")

    results: List[Dict[str, Any]] = list(existing_results)

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        for ans in answers:
            qid = ans.get("question_id", "")
            if not qid or qid in existing_ids:
                continue
            category = ans.get("category", "")
            question = ans.get("question", "")
            std = ans.get("standard_answer", "")
            agent_answer = ans.get("agent_answer", "")
            rubric = ans.get("rubric") or []
            if not isinstance(rubric, list):
                rubric = [str(rubric)]

            if agent_answer.startswith("[ERROR]"):
                judge_result = {
                    "rubric_scores": [0] * len(rubric),
                    "passed": False,
                    "reason": "Agent 执行失败",
                }
            else:
                judge_result = await judge_beam_single(
                    client=client,
                    base_url=base_url,
                    question=question,
                    standard_answer=std,
                    agent_answer=agent_answer,
                    rubric=rubric,
                    category=category,
                    timeout=timeout,
                )

            record = {
                "question_id": qid,
                "category": category,
                "judge": judge_result,
            }
            results.append(record)
            dump_json(judge_file, results)
            print(
                f"  [{category}] {qid} -> passed={judge_result.get('passed')} "
                f"scores={judge_result.get('rubric_scores')}"
            )

    print(f"\n[Judge] 完成，结果: {judge_file}")
    return judge_file


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────


def _resolve_plans(row: Dict[str, Any], plan_ids: List[int]) -> List[Dict[str, Any]]:
    """从 conversation row 中挑选指定 plan 序号（1-indexed）的标准化 plan 列表。

    BEAM-10M 内部 plan_id 是 0-indexed 字符串（'0'..'9'），但 CLI 上对外采用
    1-indexed（用户说 ``--plan 1`` 即 plans 数组里的第一个）。

    Args:
        plan_ids: 用户传入的 1-indexed plan 序号。
    """
    plans = [normalize_plan(p) for p in (row.get("plans") or [])]
    by_data_id = {p["plan_id"]: p for p in plans}
    selected: List[Dict[str, Any]] = []
    for user_idx in plan_ids:
        data_id = user_idx - 1  # 1-indexed CLI → 0-indexed data
        if data_id in by_data_id:
            selected.append(by_data_id[data_id])
            continue
        # 回退到 row['chat']['plan-<n>']
        chat_dict = row.get("chat") or {}
        key = f"plan-{user_idx}"
        if key in chat_dict and chat_dict[key] is not None:
            selected.append(normalize_plan({"plan_id": data_id, "chat": chat_dict[key]}))
    return selected


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BEAM-10M 评测脚本")
    parser.add_argument(
        "--data",
        default=DEFAULT_PARQUET_GLOB,
        help="parquet 文件路径或 glob（默认 eval/BEAM_10M/data/10M-*.parquet）",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="输出目录",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="gsuid_core HTTP 服务地址",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="单次请求超时（秒）",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    # clear
    p_clear = sub.add_parser("clear", help="清空指定 user 的 user_global 记忆")
    p_clear.add_argument("--conv", type=int, required=True, help="conversation 索引")

    # ingest-plan
    p_ingest_p = sub.add_parser("ingest-plan", help="摄入单个 plan 的 turn")
    p_ingest_p.add_argument("--conv", type=int, required=True)
    p_ingest_p.add_argument("--plan", type=int, required=True, help="plan 序号 1..10")
    p_ingest_p.add_argument("--flush", action="store_true", default=True)
    p_ingest_p.add_argument("--no-flush", dest="flush", action="store_false")
    p_ingest_p.add_argument("--rebuild", action="store_true", default=False)

    # ingest-batch
    p_ingest_b = sub.add_parser("ingest-batch", help="累计摄入多个 plan")
    p_ingest_b.add_argument("--conv", type=int, required=True)
    p_ingest_b.add_argument("--plans", required=True, help="逗号分隔，如 1,2,3")
    p_ingest_b.add_argument("--flush", action="store_true", default=True)
    p_ingest_b.add_argument("--no-flush", dest="flush", action="store_false")
    p_ingest_b.add_argument("--rebuild", action="store_true", default=False)

    # probe
    p_probe = sub.add_parser("probe", help="对 20 道探针题收集 Agent 回答")
    p_probe.add_argument("--conv", type=int, required=True)
    p_probe.add_argument(
        "--scope",
        default=None,
        help="覆盖默认 user_id（默认 beam_eval_<conv>）",
    )
    p_probe.add_argument("--no-resume", dest="resume", action="store_false")

    # judge
    p_judge = sub.add_parser("judge", help="对答卷按 rubric 给分")
    p_judge.add_argument("--answers", required=True, help="probe 输出的答卷 JSON")
    p_judge.add_argument("--no-resume", dest="resume", action="store_false")

    # all
    p_all = sub.add_parser("all", help="clear → ingest → probe → judge 一站式")
    p_all.add_argument("--conv", type=int, required=True)
    p_all.add_argument("--plans", required=True, help="逗号分隔 plan 序号")
    p_all.add_argument(
        "--scope",
        default=None,
        help="覆盖默认 user_id（默认 beam_eval_<conv>）",
    )
    p_all.add_argument("--no-judge", dest="do_judge", action="store_false")
    # 默认重建：eval_mode 下 flush_all 跳过自动重建，不重建则检索缺 category 路
    p_all.add_argument("--rebuild", dest="rebuild", action="store_true", default=True)
    p_all.add_argument("--no-rebuild", dest="rebuild", action="store_false")

    return parser


async def main_async(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    if args.cmd in {"probe", "judge"}:
        # probe/judge 只读探针题，列裁剪跳过 ~12M token 的 chat/plans（省 ~3.5GB 客户端内存）
        rows = load_beam_dataset(args.data, columns=["probing_questions"])
    elif args.cmd in {"all", "ingest-plan", "ingest-batch", "clear"}:
        rows = load_beam_dataset(args.data)
    else:
        rows = []

    if args.cmd == "clear":
        user_id = USER_ID_TEMPLATE.format(conv_id=args.conv)
        await cmd_clear(base_url, user_id, timeout=args.timeout)
        return 0

    # 预绑定避免下游各 cmd 分支里 row 被判 possibly-unbound（judge 分支不读 row）
    row: Dict[str, Any] = {}
    if args.cmd in {"ingest-plan", "ingest-batch", "probe", "all"}:
        if args.conv < 0 or args.conv >= len(rows):
            print(f"[Error] conv={args.conv} 越界，共 {len(rows)} 条")
            return 2
        row = rows[args.conv]

    if args.cmd == "ingest-plan":
        user_id = USER_ID_TEMPLATE.format(conv_id=args.conv)
        plans = _resolve_plans(row, [args.plan])
        if not plans:
            print(f"[Error] conv {args.conv} 找不到 plan {args.plan}")
            return 2
        await cmd_ingest_plan(
            base_url=base_url,
            user_id=user_id,
            plan=plans[0],
            flush=args.flush,
            trigger_rebuild=args.rebuild,
            timeout=args.timeout,
        )
        return 0

    if args.cmd == "ingest-batch":
        user_id = USER_ID_TEMPLATE.format(conv_id=args.conv)
        plan_ids = [int(x) for x in args.plans.split(",") if x.strip()]
        plans = _resolve_plans(row, plan_ids)
        await cmd_ingest_batch(
            base_url=base_url,
            user_id=user_id,
            plans=plans,
            flush=args.flush,
            trigger_rebuild=args.rebuild,
            timeout=args.timeout,
        )
        return 0

    if args.cmd == "probe":
        user_id = args.scope or USER_ID_TEMPLATE.format(conv_id=args.conv)
        probes = iter_probing_questions(row)
        answers_file = os.path.join(output_dir, f"answers_{args.conv}.json")
        await cmd_probe(
            base_url=base_url,
            user_id=user_id,
            probes=probes,
            answers_file=answers_file,
            timeout=args.timeout,
            resume=args.resume,
        )
        return 0

    if args.cmd == "judge":
        answers_file = args.answers
        base = os.path.splitext(os.path.basename(answers_file))[0]
        judge_file = os.path.join(output_dir, f"judge_{base}.json")
        await cmd_judge(
            base_url=base_url,
            answers_file=answers_file,
            judge_file=judge_file,
            timeout=args.timeout,
            resume=args.resume,
        )
        return 0

    if args.cmd == "all":
        user_id = args.scope or USER_ID_TEMPLATE.format(conv_id=args.conv)
        plan_ids = [int(x) for x in args.plans.split(",") if x.strip()]
        plans = _resolve_plans(row, plan_ids)

        # 1) clear
        await cmd_clear(base_url, user_id, timeout=args.timeout)
        # 2) ingest
        await cmd_ingest_batch(
            base_url=base_url,
            user_id=user_id,
            plans=plans,
            flush=True,
            trigger_rebuild=args.rebuild,
            timeout=args.timeout,
        )
        # 3) probe
        answers_file = os.path.join(output_dir, f"answers_{args.conv}.json")
        probes = iter_probing_questions(row)
        await cmd_probe(
            base_url=base_url,
            user_id=user_id,
            probes=probes,
            answers_file=answers_file,
            timeout=args.timeout,
            resume=False,
        )
        # 4) judge
        if args.do_judge:
            judge_file = os.path.join(output_dir, f"judge_{args.conv}.json")
            await cmd_judge(
                base_url=base_url,
                answers_file=answers_file,
                judge_file=judge_file,
                timeout=args.timeout,
                resume=False,
            )
        return 0

    print(f"[Error] 未知子命令: {args.cmd}")
    return 1


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    t0 = time.time()
    rc = asyncio.run(main_async(args))
    print(f"\n[Done] elapsed = {time.time() - t0:.1f}s, rc = {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
