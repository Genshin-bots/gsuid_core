"""
LongMemEval-S 评估脚本

使用 gsuid_core 框架的 HTTP 接口完成 LongMemEval-S 基准测试。

=============================================================================
longmemeval_s_cleaned.json 数据结构说明
=============================================================================

顶层结构: List[Question]，共 500 道题

每个 Question 是一个字典，包含以下 key:

  question_id          : str   - 题目唯一标识符，如 "e47becba"
  question_type        : str   - 题目类型，共 6 种:
                                  - "single-session-user"       : 单会话-用户信息记忆
                                  - "single-session-assistant"  : 单会话-助手信息记忆
                                  - "single-session-preference" : 单会话-用户偏好记忆
                                  - "multi-session"             : 跨会话记忆
                                  - "knowledge-update"          : 知识更新追踪
                                  - "temporal-reasoning"        : 时序推理
  question             : str   - 需要Agent回答的问题，如 "What degree did I graduate with?"
  question_date        : str   - 问题提出的时间，格式 "2023/05/30 (Tue) 23:40"
  answer               : str   - 标准答案，如 "Business Administration"
  answer_session_ids   : List[str] - 标准答案所在的会话ID列表，
                                     这些ID也出现在 haystack_session_ids 中，
                                     标识了包含答案信息的那个/些会话
  haystack_dates       : List[str] - 每个会话对应的日期，与 haystack_session_ids 一一对应，
                                     长度 = haystack_session_ids 的长度
  haystack_session_ids : List[str] - 所有会话的ID列表（含答案会话和干扰会话），
                                     与 haystack_sessions 一一对应，
                                     长度 = haystack_sessions 的长度
  haystack_sessions    : List[Session] - 所有会话列表，按时间顺序排列，
                                         包含答案会话和干扰会话，
                                         长度 = haystack_session_ids 的长度

Session 结构: List[Turn]，每个会话是一个多轮对话

每个 Turn 是一个字典:
  role    : str - "user" 或 "assistant"
  content : str - 该轮对话的文本内容

示例:
  [
    {"role": "user", "content": "I bought a Fitbit on February 15th..."},
    {"role": "assistant", "content": "Congratulations on your new Fitbit..."},
    {"role": "user", "content": "I also got a foam roller on March 2nd..."},
    {"role": "assistant", "content": "Foam rolling is an excellent addition..."}
  ]

关键关系:
  - haystack_session_ids[i] 对应 haystack_sessions[i] 对应 haystack_dates[i]
  - answer_session_ids 是 haystack_session_ids 的子集，标识包含答案信息的会话
  - 评估时需要将 haystack_sessions 全部注入Agent，然后询问 question

=============================================================================

评估流程:
  第一部分 (Phase 1 - run):
    1. 读取 longmemeval_s_cleaned.json
    2. 对每道题:
       a. 将 haystack_sessions 中的所有对话展平为 history 列表
       b. 通过 /api/chat_with_history 接口一次性传入 history + question
       c. 收集 Agent 的回答
    3. 将所有回答保存为 JSON 文件

  第二部分 (Phase 2 - judge):
    1. 读取标准答案和 Phase 1 的回答
    2. 启动一个 LLM Agent 作为评判
    3. 对比每道题的回答与标准答案，判定正确/错误
    4. 输出准确率和错题集

用法:
  # 运行 Phase 1（需要 gsuid_core 服务已启动且 ENABLE_HTTP=True）
  python eval/longmemeval/run_longmem_eval.py run --base-url http://127.0.0.1:8765

  # 运行 Phase 2（评判，需要 gsuid_core 服务已启动）
  python eval/longmemeval/run_longmem_eval.py judge \
      --answers-file eval/longmemeval/results/answers.json

  # 一键运行全部
  python eval/longmemeval/run_longmem_eval.py all --base-url http://127.0.0.1:8765

  # 指定题目范围
  python eval/longmemeval/run_longmem_eval.py run --start 0 --end 5 \
      --base-url http://127.0.0.1:8765

  # 指定输出目录
  python eval/longmemeval/run_longmem_eval.py run --output-dir eval/longmemeval/results

  # 不使用 LLM 评判，改用字符串匹配
  python eval/longmemeval/run_longmem_eval.py judge \
      --answers-file eval/longmemeval/results/answers.json --no-llm-judge

  # Phase A: 仅 System-1 检索（摄入+检索，不重建分层图）
  python eval/longmemeval/run_longmem_eval.py run_s1 --base-url http://127.0.0.1:8765

  # Phase B: 手动触发分层图重建
  python eval/longmemeval/run_longmem_eval.py rebuild --base-url http://127.0.0.1:8765

  # Phase C: 使用已有记忆检索+回答，不摄入新数据
  python eval/longmemeval/run_longmem_eval.py run_full --base-url http://127.0.0.1:8765
"""

import os
import sys
import json
import time
import asyncio
import argparse
from typing import Any, Set, Dict, List, Optional
from pathlib import Path

import httpx

# 允许以 ``python eval/longmemeval/run_longmem_eval.py`` 直接运行
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from eval.common import (  # noqa: E402
    DEFAULT_TIMEOUT,
    DEFAULT_BASE_URL,
    dump_json,
    load_json,
    load_eval_data,
    read_existing_ids,
    call_batch_observe,
    judge_single_answer,
    simple_string_match,
    load_existing_answers,
    call_chat_with_history,
    call_rebuild_hiergraph,
    extract_text_from_response,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 默认配置（已迁移到 eval/common/http_client.py，向后兼容保留同名引用）
DEFAULT_SEND_API = "/api/send_msg"

# 评判用的 System Prompt
JUDGE_SYSTEM_PROMPT = """你是一个严格的答案评判助手。你的任务是判断 Agent 的回答是否与标准答案语义一致。

评判规则:
1. 如果 Agent 的回答中包含了标准答案的核心信息，则判定为正确
2. 不要求完全一致，只要语义等价即可
3. 如果 Agent 回答了不同的内容，或者回答"不知道"/"无法回答"，则判定为错误
4. 对于人名、地名、数字等事实性信息，必须精确匹配（允许轻微的格式差异）
5. 如果标准答案是多个选项之一，Agent 回答了其中正确的那个即可

你必须只输出 JSON 格式，不要输出其他内容:
{"correct": true/false, "reason": "判断理由"}"""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


# ``load_eval_data`` / ``load_existing_answers`` 已迁移到 ``eval.common.io``，
# 顶部 import 即用。


def flatten_haystack_sessions(
    haystack_sessions: List[List[Dict[str, str]]],
) -> List[Dict[str, str]]:
    """
    将 haystack_sessions 展平为单一的 history 列表

    haystack_sessions 是 List[Session]，每个 Session 是 List[Turn]
    展平后得到一个连续的对话历史: [{"role": "user", "content": "..."}, ...]

    这与 /api/chat_with_history 接口的 history 参数格式一致。
    """
    history: List[Dict[str, str]] = []
    for session in haystack_sessions:
        if not isinstance(session, list):
            continue
        for turn in session:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role", "")
            content = turn.get("content", "")
            if not content or not isinstance(content, str):
                continue
            if role in ("user", "assistant"):
                history.append({"role": role, "content": content})
    return history


def flatten_haystack_with_dates(
    haystack_sessions: List[List[Dict[str, str]]],
    haystack_dates: List[str],
) -> List[Dict[str, str]]:
    """展平 haystack 并给每个 turn 附上其会话日期（ISO8601 timestamp 字段）。

    LongMemEval 的 haystack_dates 形如 ``"2023/05/30 (Tue) 23:40"``，与
    haystack_sessions 一一对应；temporal-reasoning / knowledge-update 两类题
    依赖记忆片段的真实时间戳，摄入时必须带上。
    """
    from datetime import datetime, timedelta

    turns: List[Dict[str, str]] = []
    for i, session in enumerate(haystack_sessions):
        if not isinstance(session, list):
            continue
        ts_iso = ""
        if i < len(haystack_dates):
            raw = str(haystack_dates[i])
            # 去掉 "(Tue)" 这类星期注记后按 "YYYY/MM/DD HH:MM" 解析
            cleaned = " ".join(p for p in raw.split() if not p.startswith("("))
            for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
                try:
                    ts_iso = datetime.strptime(cleaned, fmt).isoformat()
                    break
                except ValueError:
                    continue
        base_dt = datetime.fromisoformat(ts_iso) if ts_iso else None
        for j, turn in enumerate(session):
            if not isinstance(turn, dict):
                continue
            role = turn.get("role", "")
            content = turn.get("content", "")
            if not content or not isinstance(content, str) or role not in ("user", "assistant"):
                continue
            item = {"role": role, "content": content}
            if base_dt is not None:
                # 同一会话内 turn 依次 +1s，保持会话内顺序可排序
                item["timestamp"] = (base_dt + timedelta(seconds=j)).isoformat()
            turns.append(item)
    return turns


# ---------------------------------------------------------------------------
# Phase A: 仅 System-1 检索 - 摄入+检索，不重建分层图
# ---------------------------------------------------------------------------


async def run_phase_s1_only(
    eval_data_path: str,
    base_url: str,
    output_dir: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
    timeout: float = DEFAULT_TIMEOUT,
    resume: bool = False,
    concurrency: int = 1,
) -> str:
    """
    Phase A: 仅 System-1 检索

    流程:
    1. 将 haystack_sessions 摄入记忆系统
    2. 仅使用 System-1 向量检索（enable_system2=False）
    3. 保存到 answer_a.json
    4. 不触发分层图重建

    并发安全性：每道题使用独立 user_id（eval_{question_id}），记忆 scope 相互隔离，
    可安全并发；结果追加与落盘由 asyncio.Lock 串行化。

    Args:
        eval_data_path: 评估数据 JSON 文件路径
        base_url: gsuid_core 服务基础 URL
        output_dir: 输出目录
        start: 起始题目索引（含）
        end: 结束题目索引（不含）
        timeout: 请求超时
        resume: 是否启用增量更新

    Returns:
        回答文件路径
    """
    eval_data = load_eval_data(eval_data_path)

    if start is not None or end is not None:
        s = start or 0
        e = end or len(eval_data)
        eval_data = eval_data[s:e]
        print(f"[PhaseA] 题目范围: [{s}, {e})，共 {len(eval_data)} 道")

    os.makedirs(output_dir, exist_ok=True)

    # 使用 answer_a.json
    answers_file = os.path.join(output_dir, "answer_a.json")
    existing_results: List[Dict[str, Any]] = []
    existing_ids: Set[str] = set()

    if resume and os.path.isfile(answers_file):
        try:
            existing_results = load_json(answers_file)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[PhaseA] 读取已有结果失败: {e}")
            existing_results = []
        if isinstance(existing_results, list):
            existing_ids = read_existing_ids(answers_file, id_field="question_id")
            eval_data = [q for q in eval_data if q.get("question_id") not in existing_ids]
            print(f"[PhaseA] 增量模式: 跳过 {len(existing_ids)} 道已处理题目，剩余 {len(eval_data)} 道")
        else:
            print(f"[PhaseA] 已有文件格式异常，忽略: {answers_file}")
            existing_results = []

    results: List[Dict[str, Any]] = list(existing_results)
    save_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(concurrency, 1))

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:

        async def _one(idx: int, question_data: Dict[str, Any]) -> None:
            async with sem:
                try:
                    question_id = question_data["question_id"]
                    question = question_data["question"]
                    haystack_sessions = question_data.get("haystack_sessions", [])
                    haystack_dates = question_data.get("haystack_dates", [])
                    turns = flatten_haystack_with_dates(haystack_sessions, haystack_dates)

                    print(f"\n[PhaseA Question {idx}] ID: {question_id} ({len(turns)} turns)")

                    # 摄入走 batch_observe（同步 flush、按 turn 切块直写 Episode，带时间戳）；
                    # chat_with_history 的 observer 队列路径在 eval_mode 下不产 Episode（检索恒空）。
                    obs = await call_batch_observe(
                        client=client,
                        base_url=base_url,
                        user_id=f"eval_{question_id}",
                        turns=turns,
                        flush=True,
                        timeout=timeout,
                    )
                    if obs.get("status") != 0:
                        raise RuntimeError(f"batch_observe failed: {obs.get('msg')}")

                    # enable_observer=False（已摄入），enable_system2=False (仅System-1)
                    resp = await call_chat_with_history(
                        client=client,
                        base_url=base_url,
                        user_id=f"eval_{question_id}",
                        message=question,
                        history=[],
                        timeout=timeout,
                        enable_observer=False,
                        enable_system2=False,
                    )

                    status_code = resp.get("status_code", -1)
                    agent_answer = ""
                    if status_code == 200:
                        agent_answer = extract_text_from_response(resp.get("data"))
                    else:
                        agent_answer = f"[ERROR] status_code={status_code}"

                    result = {
                        "question_id": question_id,
                        "question": question,
                        "standard_answer": question_data.get("answer", ""),
                        "agent_answer": agent_answer,
                        "status_code": status_code,
                    }
                    # 追加与落盘串行化（断点续跑友好）
                    async with save_lock:
                        results.append(result)
                        dump_json(answers_file, results)
                    print(f"  [PhaseA {idx}] {question_id} done (status={status_code})")

                except Exception as e:
                    print(f"  [ERROR] idx={idx} {e}")

        await asyncio.gather(*[_one(i, q) for i, q in enumerate(eval_data)])

    print(f"\n[PhaseA] 完成! 回答已保存至: {answers_file}")
    return answers_file


# ---------------------------------------------------------------------------
# Phase B: 手动触发分层图重建
# ---------------------------------------------------------------------------


async def run_trigger_rebuild(
    base_url: str,
    scope_key: Optional[str] = None,
) -> None:
    """
    Phase B: 手动触发分层图重建

    调用 /api/ai/memory/hiergraph/rebuild 触发分层图重建

    Args:
        base_url: gsuid_core 服务基础 URL
        scope_key: 指定 scope_key；不指定时打印提示并跳过（core 端点强制必填）
    """
    if not scope_key:
        print("[PhaseB] 未指定 --scope-key；core 端点强制必填，请用 --scope-key 显式传入")
        return

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await call_rebuild_hiergraph(client, base_url, scope_key)
        except (httpx.HTTPError, OSError) as e:
            print(f"[PhaseB] 重建触发失败: {e}")
            return
    print(f"[PhaseB] 重建触发: {resp}")


# ---------------------------------------------------------------------------
# Phase C: 使用已有记忆检索+回答 - 不摄入新数据
# ---------------------------------------------------------------------------


async def run_phase_full_retrieval(
    eval_data_path: str,
    base_url: str,
    output_dir: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
    timeout: float = DEFAULT_TIMEOUT,
    resume: bool = False,
) -> str:
    """
    Phase C: 使用已有记忆检索+回答

    流程:
    1. 不摄入任何数据（enable_observer=False）
    2. 使用 System-1 + System-2 检索（enable_system2=True）
    3. 保存到 answer_b.json

    Args:
        eval_data_path: 评估数据 JSON 文件路径
        base_url: gsuid_core 服务基础 URL
        output_dir: 输出目录
        start: 起始题目索引（含）
        end: 结束题目索引（不含）
        timeout: 请求超时
        resume: 是否启用增量更新

    Returns:
        回答文件路径
    """
    eval_data = load_eval_data(eval_data_path)

    if start is not None or end is not None:
        s = start or 0
        e = end or len(eval_data)
        eval_data = eval_data[s:e]
        print(f"[PhaseC] 题目范围: [{s}, {e})，共 {len(eval_data)} 道")

    os.makedirs(output_dir, exist_ok=True)

    # 使用 answer_b.json
    answers_file = os.path.join(output_dir, "answer_b.json")
    existing_results: List[Dict[str, Any]] = []
    existing_ids: Set[str] = set()

    if resume and os.path.isfile(answers_file):
        try:
            existing_results = load_json(answers_file)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[PhaseC] 读取已有结果失败: {e}")
            existing_results = []
        if isinstance(existing_results, list):
            existing_ids = read_existing_ids(answers_file, id_field="question_id")
            eval_data = [q for q in eval_data if q.get("question_id") not in existing_ids]
            print(f"[PhaseC] 增量模式: 跳过 {len(existing_ids)} 道已处理题目，剩余 {len(eval_data)} 道")
        else:
            print(f"[PhaseC] 已有文件格式异常，忽略: {answers_file}")
            existing_results = []

    results: List[Dict[str, Any]] = list(existing_results)

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        for idx, question_data in enumerate(eval_data):
            try:
                question_id = question_data["question_id"]
                question = question_data["question"]
                haystack_sessions = question_data.get("haystack_sessions", [])
                history = flatten_haystack_sessions(haystack_sessions)

                print(f"\n[PhaseC Question {idx}] ID: {question_id}")

                # enable_observer=False (不摄入), enable_system2=True (使用System-2)
                resp = await call_chat_with_history(
                    client=client,
                    base_url=base_url,
                    user_id=f"eval_{question_id}",
                    message=question,
                    history=history,
                    timeout=timeout,
                    enable_observer=False,
                    enable_system2=True,
                )

                status_code = resp.get("status_code", -1)
                agent_answer = ""
                if status_code == 200:
                    agent_answer = extract_text_from_response(resp.get("data"))
                else:
                    agent_answer = f"[ERROR] status_code={status_code}"

                result = {
                    "question_id": question_id,
                    "question": question,
                    "standard_answer": question_data.get("answer", ""),
                    "agent_answer": agent_answer,
                    "status_code": status_code,
                }
                results.append(result)

                # 每次保存（断点续跑友好）
                dump_json(answers_file, results)

            except Exception as e:
                print(f"  [ERROR] {e}")

    print(f"\n[PhaseC] 完成! 回答已保存至: {answers_file}")
    return answers_file


# ---------------------------------------------------------------------------
# Phase 1: 运行评估 - 通过 /api/chat_with_history 传入 history + question
# ---------------------------------------------------------------------------


# ``call_chat_with_history`` / ``call_send_msg`` / ``extract_text_from_response``
# 已迁移到 ``eval.common.http_client``，顶部 import 即用。


async def run_single_question(
    client: httpx.AsyncClient,
    base_url: str,
    question_data: Dict[str, Any],
    question_index: int,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    运行单道题目的评估

    流程:
    1. 将 haystack_sessions 展平为 history 列表
    2. 通过 /api/chat_with_history 接口一次性传入 history + question
    3. 收集 Agent 的回答

    Args:
        client: httpx 异步客户端
        base_url: 服务基础 URL
        question_data: 题目数据（参见模块头部数据结构说明）
        question_index: 题目索引
        timeout: 请求超时

    Returns:
        评估结果字典
    """
    question_id = question_data["question_id"]
    question_type = question_data.get("question_type", "unknown")
    question = question_data["question"]
    answer = question_data["answer"]
    haystack_sessions = question_data.get("haystack_sessions", [])

    # 展平 haystack_sessions 为 history
    history = flatten_haystack_sessions(haystack_sessions)

    print(f"\n{'=' * 60}")
    print(f"[Question {question_index}] ID: {question_id}")
    print(f"  Type: {question_type}")
    print(f"  Question: {question}")
    print(f"  Standard Answer: {answer}")
    print(f"  Haystack sessions: {len(haystack_sessions)} -> history turns: {len(history)}")

    # 使用 /api/chat_with_history 一次性传入 history + question
    user_id = f"eval_{question_id}"

    resp = await call_chat_with_history(
        client=client,
        base_url=base_url,
        user_id=user_id,
        message=question,
        history=history,
        timeout=timeout,
    )

    # 提取回答文本和 memory 字段
    agent_answer = ""
    memory = None
    status_code = resp.get("status_code", -1)
    if status_code == 200:
        raw_data = resp.get("data")
        agent_answer = extract_text_from_response(raw_data)
        memory = resp.get("memory")
    else:
        error_msg = resp.get("error", "unknown")
        agent_answer = f"[ERROR] 请求失败: status_code={status_code}, error={error_msg}"

    print(f"  Agent 回答: {agent_answer[:200]}{'...' if len(agent_answer) > 200 else ''}")

    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": question,
        "standard_answer": answer,
        "agent_answer": agent_answer,
        "memory": memory,
        "status_code": status_code,
        "history_turns": len(history),
    }


async def run_phase1(
    eval_data_path: str,
    base_url: str,
    output_dir: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
    timeout: float = DEFAULT_TIMEOUT,
    resume: bool = False,
) -> str:
    """
    Phase 1: 运行评估，收集 Agent 回答

    Args:
        eval_data_path: 评估数据 JSON 文件路径
        base_url: gsuid_core 服务基础 URL
        output_dir: 输出目录
        start: 起始题目索引（含）
        end: 结束题目索引（不含）
        timeout: 请求超时
        resume: 是否启用增量更新，跳过已存在于 results/answers.json 中的 question_id

    Returns:
        回答文件路径
    """
    # 加载数据
    eval_data = load_eval_data(eval_data_path)

    # 截取范围
    if start is not None or end is not None:
        s = start or 0
        e = end or len(eval_data)
        eval_data = eval_data[s:e]
        print(f"[Phase1] 题目范围: [{s}, {e})，共 {len(eval_data)} 道")

    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)

    # 增量更新：加载已有结果
    existing_results: List[Dict[str, Any]] = []
    existing_ids: Set[str] = set()
    answers_file: Optional[str] = None

    if resume:
        existing_results, existing_ids, answers_file = load_existing_answers(output_dir)
        if existing_ids:
            skipped_count = sum(1 for q in eval_data if q.get("question_id") in existing_ids)
            eval_data = [q for q in eval_data if q.get("question_id") not in existing_ids]
            print(f"[Phase1] 增量模式: 跳过 {skipped_count} 道已处理题目，剩余 {len(eval_data)} 道")
        else:
            print("[Phase1] 增量模式: 未检测到已有结果，将从头开始")

    # 如果没有检测到已有文件，则固定使用 answers.json
    if answers_file is None:
        answers_file = os.path.join(output_dir, "answers.json")

    # 运行评估（已有结果 + 新结果）
    results: List[Dict[str, Any]] = list(existing_results)

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        # 先测试连接
        try:
            test_resp = await client.get(f"{base_url}/docs", timeout=5.0)
            print(f"[Phase1] 服务连接测试: OK (status={test_resp.status_code})")
        except Exception as e:
            print(f"[Phase1] 服务连接测试失败: {e}")
            print("[Phase1] 请确保 gsuid_core 服务已启动且 ENABLE_HTTP=True")
            return ""

        for idx, question_data in enumerate(eval_data):
            try:
                result = await run_single_question(
                    client=client,
                    base_url=base_url,
                    question_data=question_data,
                    question_index=idx,
                    timeout=timeout,
                )
                results.append(result)
            except Exception as e:
                print(f"  [ERROR] 题目 {idx} 执行异常: {e}")
                results.append(
                    {
                        "question_id": question_data.get("question_id", f"unknown_{idx}"),
                        "question_type": question_data.get("question_type", "unknown"),
                        "question": question_data.get("question", ""),
                        "standard_answer": question_data.get("answer", ""),
                        "agent_answer": f"[ERROR] {e}",
                        "status_code": -1,
                        "history_turns": 0,
                    }
                )

            # 每完成一道题就保存一次（防止中途崩溃丢失数据）
            dump_json(answers_file, results)

    # 最终统计（基于全部结果）
    total_count = len(results)
    new_count = total_count - len(existing_results)
    success_count = sum(1 for r in results if r.get("status_code") == 200)
    error_count = total_count - success_count
    print(f"\n{'=' * 60}")
    print("[Phase1] 完成!")
    print(f"  总题数: {total_count} (本次新增: {new_count})")
    print(f"  成功: {success_count}")
    print(f"  失败: {error_count}")
    print(f"  回答已保存至: {answers_file}")

    return answers_file


# ---------------------------------------------------------------------------
# Phase 2: 评判 - 对比答案，计算准确率
# ---------------------------------------------------------------------------


# ``judge_single_answer`` / ``parse_judge_response`` / ``simple_string_match``
# 已迁移到 ``eval.common.judge``，顶部 import 即用。


async def run_phase2(
    answers_file: str,
    base_url: str,
    output_dir: str,
    eval_data_path: Optional[str] = None,
    use_llm_judge: bool = True,
    timeout: float = 60.0,
    concurrency: int = 1,
) -> str:
    """
    Phase 2: 评判回答，计算准确率

    Args:
        answers_file: Phase 1 输出的回答文件路径
        base_url: gsuid_core 服务基础 URL
        output_dir: 输出目录
        eval_data_path: 原始评估数据路径（可选，用于交叉验证）
        use_llm_judge: 是否使用 LLM 评判（False 则使用简单字符串匹配）
        timeout: 评判请求超时

    Returns:
        评判结果文件路径
    """
    # 加载回答数据
    answers = load_json(answers_file)
    print(f"[Phase2] 已加载 {len(answers)} 条回答，来自 {answers_file}")

    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)

    # 评判结果
    judge_results: List[Dict[str, Any]] = []
    correct_count = 0
    wrong_count = 0
    error_count = 0

    # 按题目类型统计
    type_stats: Dict[str, Dict[str, int]] = {}

    client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    try:
        # 并发评判：每题一次独立 LLM 调用，互不依赖；先并发收集 judge_result（保持原顺序），
        # 统计与落盘在收集完成后串行执行，保证计数/打印与串行版一致。
        sem = asyncio.Semaphore(max(concurrency, 1))

        async def _judge_one(idx: int, answer_data: Dict[str, Any]) -> Dict[str, Any]:
            agent_answer = answer_data.get("agent_answer", "")
            if agent_answer.startswith("[ERROR]"):
                return {"correct": False, "reason": "Agent 执行失败"}
            if not use_llm_judge:
                return {
                    "correct": simple_string_match(answer_data.get("standard_answer", ""), agent_answer),
                    "reason": "简单字符串匹配",
                }
            async with sem:
                print(f"[Judge {idx}/{len(answers)}] ID: {answer_data.get('question_id', '')}")
                return await judge_single_answer(
                    client=client,
                    base_url=base_url,
                    question=answer_data.get("question", ""),
                    standard_answer=answer_data.get("standard_answer", ""),
                    agent_answer=agent_answer,
                    timeout=timeout,
                )

        all_judges = await asyncio.gather(*[_judge_one(i, a) for i, a in enumerate(answers)])

        for idx, answer_data in enumerate(answers):
            question_id = answer_data.get("question_id", f"unknown_{idx}")
            question_type = answer_data.get("question_type", "unknown")
            question = answer_data.get("question", "")
            standard_answer = answer_data.get("standard_answer", "")
            agent_answer = answer_data.get("agent_answer", "")
            judge_result = all_judges[idx]

            # 执行失败的题目直接记为 error，不进入 wrong_count
            is_execution_error = agent_answer.startswith("[ERROR]")
            if is_execution_error:
                error_count += 1

            is_correct = judge_result.get("correct", False)
            # error 题不计入 wrong_count（已记入 error_count），保证 correct+wrong+error == total
            if is_correct:
                correct_count += 1
                status_icon = "✅"
            elif not is_execution_error:
                wrong_count += 1
                status_icon = "❌"
            else:
                status_icon = "❌"

            print(f"  {status_icon} {'正确' if is_correct else '错误'} - {judge_result.get('reason', '')}")

            # 按类型统计：error 与 wrong 二选一，保证 correct+wrong+error == total
            if question_type not in type_stats:
                type_stats[question_type] = {"correct": 0, "wrong": 0, "error": 0}
            if is_correct:
                type_stats[question_type]["correct"] += 1
            elif is_execution_error:
                type_stats[question_type]["error"] += 1
            else:
                type_stats[question_type]["wrong"] += 1

            # 保存评判结果
            judge_results.append(
                {
                    "question_id": question_id,
                    "question_type": question_type,
                    "question": question,
                    "standard_answer": standard_answer,
                    "agent_answer": agent_answer,
                    "judge_correct": is_correct,
                    "judge_reason": judge_result.get("reason", ""),
                }
            )

    finally:
        await client.aclose()

    # 计算准确率
    total = len(answers)
    accuracy = correct_count / total * 100 if total > 0 else 0

    # 构建错题集
    wrong_questions = [r for r in judge_results if not r["judge_correct"]]

    # 保存结果
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    result_file = os.path.join(output_dir, f"judge_result_{timestamp}.json")

    output_data = {
        "summary": {
            "total": total,
            "correct": correct_count,
            "wrong": wrong_count,
            "error": error_count,
            "accuracy": round(accuracy, 2),
            "answers_file": answers_file,
            "use_llm_judge": use_llm_judge,
            "timestamp": timestamp,
        },
        "type_stats": type_stats,
        "results": judge_results,
        "wrong_questions": wrong_questions,
    }

    dump_json(result_file, output_data)

    # 打印汇总
    print(f"\n{'=' * 60}")
    print("[Phase2] 评判完成!")
    print(f"  总题数: {total}")
    print(f"  正确: {correct_count}")
    print(f"  错误: {wrong_count}")
    print(f"  执行失败: {error_count}")
    print(f"  准确率: {accuracy:.2f}%")
    print("\n  按类型统计:")
    for qtype, stats in type_stats.items():
        type_total = stats["correct"] + stats["wrong"] + stats["error"]
        type_acc = stats["correct"] / type_total * 100 if type_total > 0 else 0
        print(f"    {qtype}: {stats['correct']}/{type_total} ({type_acc:.1f}%)")
    print(f"\n  错题数: {len(wrong_questions)}")
    print(f"  结果已保存至: {result_file}")

    return result_file


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def resolve_eval_data_path() -> str:
    """解析评估数据文件路径"""
    # 优先使用同级目录下的文件
    script_dir = Path(__file__).resolve().parent
    local_path = script_dir / "longmemeval_s_cleaned.json"
    if local_path.exists():
        return str(local_path)

    # 回退到项目根目录下的 eval 目录
    project_root = script_dir.parent
    root_path = project_root / "eval" / "longmemeval_s_cleaned.json"
    if root_path.exists():
        return str(root_path)

    # 默认返回同级目录路径（后续会报错）
    return str(local_path)


async def main():
    parser = argparse.ArgumentParser(
        description="LongMemEval-S 评估脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # ---- run 子命令 ----
    run_parser = subparsers.add_parser("run", help="Phase 1: 运行评估，收集 Agent 回答")
    run_parser.add_argument(
        "--base-url",
        type=str,
        default=DEFAULT_BASE_URL,
        help=f"gsuid_core 服务基础 URL (默认: {DEFAULT_BASE_URL})",
    )
    run_parser.add_argument(
        "--eval-data",
        type=str,
        default=None,
        help="评估数据 JSON 文件路径 (默认: eval/longmemeval_s_cleaned.json)",
    )
    run_parser.add_argument(
        "--output-dir",
        type=str,
        default="eval/longmemeval/results",
        help="输出目录 (默认: eval/longmemeval/results)",
    )
    run_parser.add_argument(
        "--start",
        type=int,
        default=None,
        help="起始题目索引（含）",
    )
    run_parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="结束题目索引（不含）",
    )
    run_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"单次请求超时秒数 (默认: {DEFAULT_TIMEOUT})",
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="增量更新模式：跳过 output_dir 下已有 answers_*.json 中已处理的 question_id",
    )

    # ---- judge 子命令 ----
    judge_parser = subparsers.add_parser("judge", help="Phase 2: 评判回答，计算准确率")
    judge_parser.add_argument(
        "--base-url",
        type=str,
        default=DEFAULT_BASE_URL,
        help=f"gsuid_core 服务基础 URL (默认: {DEFAULT_BASE_URL})",
    )
    judge_parser.add_argument(
        "--answers-file",
        type=str,
        required=True,
        help="Phase 1 输出的回答文件路径",
    )
    judge_parser.add_argument(
        "--output-dir",
        type=str,
        default="eval/longmemeval/results",
        help="输出目录 (默认: eval/longmemeval/results)",
    )
    judge_parser.add_argument(
        "--no-llm-judge",
        action="store_true",
        help="不使用 LLM 评判，改用简单字符串匹配",
    )
    judge_parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="评判请求超时秒数 (默认: 60)",
    )
    judge_parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="并发评判数（每题独立 LLM 调用；默认 1 串行）",
    )

    # ---- all 子命令 ----
    all_parser = subparsers.add_parser("all", help="一键运行 Phase 1 + Phase 2")
    all_parser.add_argument(
        "--base-url",
        type=str,
        default=DEFAULT_BASE_URL,
        help=f"gsuid_core 服务基础 URL (默认: {DEFAULT_BASE_URL})",
    )
    all_parser.add_argument(
        "--eval-data",
        type=str,
        default=None,
        help="评估数据 JSON 文件路径 (默认: eval/longmemeval_s_cleaned.json)",
    )
    all_parser.add_argument(
        "--output-dir",
        type=str,
        default="eval/longmemeval/results",
        help="输出目录 (默认: eval/longmemeval/results)",
    )
    all_parser.add_argument(
        "--start",
        type=int,
        default=None,
        help="起始题目索引（含）",
    )
    all_parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="结束题目索引（不含）",
    )
    all_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"单次请求超时秒数 (默认: {DEFAULT_TIMEOUT})",
    )
    all_parser.add_argument(
        "--no-llm-judge",
        action="store_true",
        help="不使用 LLM 评判，改用简单字符串匹配",
    )
    all_parser.add_argument(
        "--resume",
        action="store_true",
        help="增量更新模式：跳过 output_dir 下已有 answers_*.json 中已处理的 question_id",
    )

    # ---- run_s1 子命令 ----
    run_s1_parser = subparsers.add_parser("run_s1", help="Phase A: 仅 System-1 检索，摄入+检索，不重建分层图")
    run_s1_parser.add_argument(
        "--base-url",
        type=str,
        default=DEFAULT_BASE_URL,
        help=f"gsuid_core 服务基础 URL (默认: {DEFAULT_BASE_URL})",
    )
    run_s1_parser.add_argument(
        "--eval-data",
        type=str,
        default=None,
        help="评估数据 JSON 文件路径",
    )
    run_s1_parser.add_argument(
        "--output-dir",
        type=str,
        default="eval/longmemeval/results",
        help="输出目录 (默认: eval/longmemeval/results)",
    )
    run_s1_parser.add_argument(
        "--start",
        type=int,
        default=None,
        help="起始题目索引（含）",
    )
    run_s1_parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="结束题目索引（不含）",
    )
    run_s1_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"单次请求超时秒数 (默认: {DEFAULT_TIMEOUT})",
    )
    run_s1_parser.add_argument(
        "--resume",
        action="store_true",
        help="增量更新模式",
    )
    run_s1_parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="并发处理题目数（每题独立 user_id/scope，可安全并发；默认 1 串行）",
    )

    # ---- rebuild 子命令 ----
    rebuild_parser = subparsers.add_parser("rebuild", help="Phase B: 手动触发分层图重建")
    rebuild_parser.add_argument(
        "--base-url",
        type=str,
        default=DEFAULT_BASE_URL,
        help=f"gsuid_core 服务基础 URL (默认: {DEFAULT_BASE_URL})",
    )
    rebuild_parser.add_argument(
        "--scope-key",
        type=str,
        default=None,
        help="指定 scope_key，不指定则触发所有 scope",
    )

    # ---- run_full 子命令 ----
    run_full_parser = subparsers.add_parser("run_full", help="Phase C: 使用已有记忆检索+回答，不摄入新数据")
    run_full_parser.add_argument(
        "--base-url",
        type=str,
        default=DEFAULT_BASE_URL,
        help=f"gsuid_core 服务基础 URL (默认: {DEFAULT_BASE_URL})",
    )
    run_full_parser.add_argument(
        "--eval-data",
        type=str,
        default=None,
        help="评估数据 JSON 文件路径",
    )
    run_full_parser.add_argument(
        "--output-dir",
        type=str,
        default="eval/longmemeval/results",
        help="输出目录 (默认: eval/longmemeval/results)",
    )
    run_full_parser.add_argument(
        "--start",
        type=int,
        default=None,
        help="起始题目索引（含）",
    )
    run_full_parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="结束题目索引（不含）",
    )
    run_full_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"单次请求超时秒数 (默认: {DEFAULT_TIMEOUT})",
    )
    run_full_parser.add_argument(
        "--resume",
        action="store_true",
        help="增量更新模式",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "run":
        eval_data_path = args.eval_data or resolve_eval_data_path()
        if not os.path.exists(eval_data_path):
            print(f"[ERROR] 评估数据文件不存在: {eval_data_path}")
            return

        await run_phase1(
            eval_data_path=eval_data_path,
            base_url=args.base_url,
            output_dir=args.output_dir,
            start=args.start,
            end=args.end,
            timeout=args.timeout,
            resume=args.resume,
        )

    elif args.command == "run_s1":
        eval_data_path = args.eval_data or resolve_eval_data_path()
        if not os.path.exists(eval_data_path):
            print(f"[ERROR] 评估数据文件不存在: {eval_data_path}")
            return

        await run_phase_s1_only(
            eval_data_path=eval_data_path,
            base_url=args.base_url,
            output_dir=args.output_dir,
            start=args.start,
            end=args.end,
            timeout=args.timeout,
            resume=args.resume,
            concurrency=getattr(args, "concurrency", 1),
        )

    elif args.command == "rebuild":
        await run_trigger_rebuild(
            base_url=args.base_url,
            scope_key=args.scope_key,
        )

    elif args.command == "run_full":
        eval_data_path = args.eval_data or resolve_eval_data_path()
        if not os.path.exists(eval_data_path):
            print(f"[ERROR] 评估数据文件不存在: {eval_data_path}")
            return

        await run_phase_full_retrieval(
            eval_data_path=eval_data_path,
            base_url=args.base_url,
            output_dir=args.output_dir,
            start=args.start,
            end=args.end,
            timeout=args.timeout,
            resume=args.resume,
        )

    elif args.command == "judge":
        if not os.path.exists(args.answers_file):
            print(f"[ERROR] 回答文件不存在: {args.answers_file}")
            return

        await run_phase2(
            answers_file=args.answers_file,
            base_url=args.base_url,
            output_dir=args.output_dir,
            use_llm_judge=not args.no_llm_judge,
            timeout=args.timeout,
            concurrency=getattr(args, "concurrency", 1),
        )

    elif args.command == "all":
        eval_data_path = args.eval_data or resolve_eval_data_path()
        if not os.path.exists(eval_data_path):
            print(f"[ERROR] 评估数据文件不存在: {eval_data_path}")
            return

        # Phase 1
        answers_file = await run_phase1(
            eval_data_path=eval_data_path,
            base_url=args.base_url,
            output_dir=args.output_dir,
            start=args.start,
            end=args.end,
            timeout=args.timeout,
            resume=args.resume,
        )

        if not answers_file:
            print("[ERROR] Phase 1 未生成回答文件，跳过 Phase 2")
            return

        # Phase 2
        await run_phase2(
            answers_file=answers_file,
            base_url=args.base_url,
            output_dir=args.output_dir,
            use_llm_judge=not args.no_llm_judge,
            timeout=60.0,
        )


if __name__ == "__main__":
    asyncio.run(main())
