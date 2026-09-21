"""LLM 评判 + 简单字符串匹配。

包含三个核心入口：

- :func:`judge_single_answer` —— LongMemEval 风格，给定 (question, standard_answer,
  agent_answer) 直接让 LLM 判 PASS / FAIL，返回 ``{"correct": bool, "reason": str}``；
- :func:`judge_beam_single` —— BEAM-10M 风格，按 rubric 列表逐条给分，返回
  ``{"rubric_scores": [...], "passed": bool, "reason": str}``；
- :func:`judge_beam_order` —— event_ordering：对齐矩阵 + 本地 coverage / Kendall τ-b。
"""

from __future__ import annotations

import re
import json
import math
import asyncio
from typing import Any, Dict, List

import httpx

from .http_client import (
    call_chat_with_history,
    extract_text_from_response,
)


def _judge_text(value: str | int | float | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


# 判定阶段的瞬时故障标记：agent 管线捕获 LLM 连接/限流错误后会把它当作正文文本返回
# （HTTP 仍是 200），parse 不到 JSON 就默认判错——必须识别并重试，否则大批答卷被误判为 FAIL。
_TRANSIENT_JUDGE_MARKERS = (
    "connection error",
    "执行出错",
    "无法解析评判回复",
    "rate limit",
    "rate_limit",
    "too many request",
    "timed out",
    "timeout",
    "服务器繁忙",
    "503",
    "502",
    "connection aborted",
    "connection reset",
    "<silence>",
    "[silence]",
    "不太想说",
    "额…出错了",
    "额...出错了",
)


_JUDGE_MAX_RETRIES = 5
_JUDGE_BACKOFF_BASE = 1.5


def _is_transient_judge_failure(status_code: int, judge_text: str) -> bool:
    """判定这次评判是否命中瞬时故障（应退避重试），而非模型给出的真实判决。"""
    if status_code != 200:
        return True
    body = (judge_text or "").strip()
    if not body:
        return True
    low = body.lower()
    if low in {"<silence>", "[silence]", "silence", "</silence>", "<silence/>"}:
        return True
    return any(m in low for m in _TRANSIENT_JUDGE_MARKERS)


# ─────────────────────────────────────────────
# LongMemEval 风格：单一 PASS / FAIL
# ─────────────────────────────────────────────


async def judge_single_answer(
    client: httpx.AsyncClient,
    base_url: str,
    question: str,
    standard_answer: str | int | float | None,
    agent_answer: str | int | float | None,
    timeout: float = 60.0,
    user_id: str = "judge_user",
) -> Dict[str, Any]:
    """使用 LLM Agent 评判单道题的回答。

    通过 ``/api/chat_with_history`` 接口发送评判请求，让 LLM 判断回答是否正确。
    使用独立的 ``user_id`` 避免与其他会话冲突。
    """
    standard_answer = _judge_text(standard_answer)
    agent_answer = _judge_text(agent_answer)
    judge_prompt = f"""请判断 Agent 的回答是否与标准答案语义一致。

问题: {question}

标准答案: {standard_answer}

Agent 的回答: {agent_answer}

只输出单独一行：PASS 或 FAIL。"""

    # 判分走 provider，高并发命中连接/限流时 agent 管线把错误当正文返回（HTTP 200），不重试
    # 会被 parse 成 FAIL 大批误判——指数退避仅对瞬时故障重试，真实判决直接返回。
    last_text = ""
    last_status = -1
    for attempt in range(_JUDGE_MAX_RETRIES):
        resp = await call_chat_with_history(
            client=client,
            base_url=base_url,
            user_id=user_id,
            message=judge_prompt,
            history=[],
            timeout=timeout,
            as_judge=True,
        )
        last_status = resp.get("status_code", -1)
        last_text = extract_text_from_response(resp.get("data")) if last_status == 200 else ""
        if not _is_transient_judge_failure(last_status, last_text):
            return parse_judge_response(last_text)
        if attempt < _JUDGE_MAX_RETRIES - 1:
            await asyncio.sleep(_JUDGE_BACKOFF_BASE * (2**attempt))

    detail = last_text.strip() or resp.get("error", "unknown")
    return {"correct": False, "reason": f"评判请求失败(瞬时故障, 重试耗尽): status={last_status}, {detail[:120]}"}


def parse_judge_response(text: str) -> Dict[str, Any]:
    """解析评判 Agent 的回复，提取 ``correct`` 和 ``reason``。

    支持多种格式：
      - 纯 JSON：``{"correct": true, "reason": "..."}``
      - Markdown 代码块包裹的 JSON
      - 包含 JSON 片段的混合文本
    """
    if not text:
        return {"correct": False, "reason": "评判回复为空"}

    head = text.strip().splitlines()[0].strip()
    if re.fullmatch(r"PASS|FAIL", head, flags=re.IGNORECASE):
        return {"correct": head.upper() == "PASS", "reason": text.strip()[:500]}

    # 1) 直接解析
    try:
        result = json.loads(text.strip())
        if isinstance(result, dict) and "correct" in result:
            return {
                "correct": bool(result["correct"]),
                "reason": str(result.get("reason", "")),
            }
    except json.JSONDecodeError:
        pass

    # 2) Markdown 代码块
    json_pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
    matches = re.findall(json_pattern, text, re.DOTALL)
    for match in matches:
        try:
            result = json.loads(match.strip())
            if isinstance(result, dict) and "correct" in result:
                return {
                    "correct": bool(result["correct"]),
                    "reason": str(result.get("reason", "")),
                }
        except json.JSONDecodeError:
            continue

    # 3) 大括号内 JSON
    brace_pattern = r'\{[^{}]*"correct"[^{}]*\}'
    matches = re.findall(brace_pattern, text)
    for match in matches:
        try:
            result = json.loads(match)
            if isinstance(result, dict) and "correct" in result:
                return {
                    "correct": bool(result["correct"]),
                    "reason": str(result.get("reason", "")),
                }
        except json.JSONDecodeError:
            continue

    # 4) 关键词兜底
    text_lower = text.lower()
    if '"correct": true' in text_lower or "'correct': true" in text_lower:
        return {"correct": True, "reason": f"基于关键词判断: {text[:200]}"}
    if '"correct": false' in text_lower or "'correct': false" in text_lower:
        return {"correct": False, "reason": f"基于关键词判断: {text[:200]}"}

    return {"correct": False, "reason": f"无法解析评判回复: {text[:200]}"}


def simple_string_match(standard_answer: str | int | float | None, agent_answer: str | int | float | None) -> bool:
    """简单字符串匹配评判（作为 LLM 评判的备选）。

    判断标准答案的核心词是否出现在 Agent 回答中（80% 分词命中率）。
    """
    agent_answer = _judge_text(agent_answer)
    standard_answer = _judge_text(standard_answer)
    if not agent_answer or agent_answer.startswith("[ERROR]"):
        return False

    std_lower = standard_answer.lower().strip()
    agent_lower = agent_answer.lower().strip()

    # 纯数字金标必须整词，避免 3 命中 13 workshops。
    if std_lower.isdigit() or (std_lower.startswith("-") and std_lower[1:].isdigit()):
        return re.search(rf"(?<!\d){re.escape(std_lower)}(?!\d)", agent_lower) is not None

    if std_lower in agent_lower:
        return True

    std_words = set(re.findall(r"\w+", std_lower))
    agent_words = set(re.findall(r"\w+", agent_lower))
    if not std_words:
        return False

    overlap = std_words & agent_words
    ratio = len(overlap) / len(std_words) if std_words else 0
    return ratio >= 0.8


# ─────────────────────────────────────────────
# BEAM-10M 风格：rubric-based
# ─────────────────────────────────────────────


_BEAM_JUDGE_PROMPT = """你是一名长对话记忆评测裁判。基于【类别】【标准答案】和【rubric 检查点】判断 Agent 输出是否达标。

请按 rubric 逐条判断是否命中（1 表示命中，0 表示未命中），并给出整体 PASS/FAIL。
整体 PASS 定义：rubric 检查点全部命中，**或** Agent 答案的核心事实/语义与标准答案一致。

【类别】{category}
【问题】{question}
【标准答案】
{standard_answer}

【rubric 检查点】
{rubric_block}

【Agent 答案】
{agent_answer}

请严格输出以下 JSON（不要任何额外文字）：
{{
  "rubric_scores": [1, 0, 1, ...],
  "passed": true,
  "reason": "逐条说明命中 / 未命中原因"
}}
"""


# BEAM 官方 Listing 21 / compute_metrics.llm_equivalence 原文（逐对 YES/NO）。
_BEAM_EQ_SYSTEM = (
    "You are a binary classifier.\n"
    "If the TWO snippets describe the SAME event/fact, reply **YES**\n"
    "Otherwise reply **NO**. No extra words.\n"
    "DO NOT provide any exaplanation."
)

_BEAM_EQ_BATCH_PROMPT = """{system}

Pairs below. For each pair output one line: PAIR i-j YES|NO

{pairs}

Output only those PAIR lines.
"""

_DOUBLE_JUDGE_PREFIXES = (
    "beam_off_100k_0__",
    "beam_off_100k_10__",
    "beam_off_100k_13__",
    "beam_off_100k_17__",
)


def kendall_tau_b(xs: List[int], ys: List[int]) -> float | None:
    """Kendall τ-b。长度不同或不足 2 对时返回 None。"""
    n = len(xs)
    if n != len(ys) or n < 2:
        return None
    conc = 0
    disc = 0
    ties_x = 0
    ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0) == (dy > 0):
                conc += 1
            else:
                disc += 1
    denom = math.sqrt((conc + disc + ties_x) * (conc + disc + ties_y))
    if denom == 0.0:
        return None
    return (conc - disc) / denom


def parse_align_list(raw: object, n: int) -> List[int | None]:
    """把裁判输出的 align 收成长度 n 的 1-based 编号或 None。"""
    out: List[int | None] = [None] * n
    if not isinstance(raw, list) or n <= 0:
        return out
    for i, item in enumerate(raw[:n]):
        if item is None:
            continue
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            out[i] = item if item >= 1 else None
            continue
        if isinstance(item, float) and item.is_integer():
            val = int(item)
            out[i] = val if val >= 1 else None
            continue
        if isinstance(item, str) and item.strip().isdigit():
            val = int(item.strip())
            out[i] = val if val >= 1 else None
    return out


def official_tau_from_align(align: List[int | None]) -> float | None:
    """官方式 τ-b：全部对齐才算 Kendall，否则 0（Kimi / 参考实现）。"""
    if any(item is None for item in align):
        return 0.0
    n = len(align)
    if n < 2:
        return None
    pred = [int(item) for item in align if item is not None]
    if len(pred) != n:
        return 0.0
    return kendall_tau_b(list(range(n)), pred)


def order_metrics_from_align(align: List[int | None]) -> tuple[float, float | None]:
    """coverage = 对齐条数 / N；τ-b 仅在全部对齐时计算。"""
    n = len(align)
    if n == 0:
        return 0.0, None
    ranks: List[int] = []
    missing = 0
    for item in align:
        if item is None:
            missing += 1
        else:
            ranks.append(item)
    coverage = (n - missing) / n
    if missing > 0:
        return coverage, None
    return coverage, kendall_tau_b(list(range(n)), ranks)


_NUMBERED_ITEM = re.compile(r"^\d+[\.\)]\s+\S")


def split_agent_items(agent_answer: str) -> List[str]:
    """只收编号清单行，丢掉散文噪声。"""
    items: List[str] = []
    for line in (agent_answer or "").splitlines():
        s = line.strip()
        if _NUMBERED_ITEM.match(s):
            items.append(s)
    return items


def pair_line_stats(text: str) -> tuple[int, int]:
    """解析到的 PAIR 行数、YES 数。"""
    n_pair = 0
    n_yes = 0
    for raw in (text or "").splitlines():
        m = re.search(r"PAIR\s+(\d+)\s*[-:]\s*(\d+)\s+(YES|NO)", raw.strip().upper())
        if m is None:
            continue
        n_pair += 1
        if m.group(3) == "YES":
            n_yes += 1
    return n_pair, n_yes


def parse_eq_pair_lines(text: str, n_r: int, n_a: int) -> List[List[bool]]:
    """解析 PAIR i-j YES|NO 成 n_r × n_a 矩阵。"""
    mat: List[List[bool]] = [[False] * n_a for _ in range(n_r)]
    if not text:
        return mat
    for raw in text.splitlines():
        line = raw.strip().upper()
        m = re.search(r"PAIR\s+(\d+)\s*[-:]\s*(\d+)\s+(YES|NO)", line)
        if m is None:
            continue
        i = int(m.group(1)) - 1
        j = int(m.group(2)) - 1
        if 0 <= i < n_r and 0 <= j < n_a:
            mat[i][j] = m.group(3) == "YES"
    return mat


def align_from_eq_matrix(mat: List[List[bool]]) -> List[int | None]:
    """每个 rubric 取第一个未占用的 YES agent 项（1-based）。"""
    n_r = len(mat)
    used: set[int] = set()
    align: List[int | None] = [None] * n_r
    for i, row in enumerate(mat):
        for j, hit in enumerate(row):
            if hit and (j + 1) not in used:
                align[i] = j + 1
                used.add(j + 1)
                break
    return align


def intersect_align(a: List[int | None], b: List[int | None]) -> List[int | None]:
    n = max(len(a), len(b))
    out: List[int | None] = []
    for i in range(n):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        out.append(x if x == y else None)
    return out


def majority_align(first: List[int | None], second: List[int | None]) -> List[int | None]:
    """两次 align 按位多数；不一致留第一次，禁止交集直接变 null。"""
    n = max(len(first), len(second))
    out: List[int | None] = []
    for i in range(n):
        x = first[i] if i < len(first) else None
        y = second[i] if i < len(second) else None
        out.append(x if x == y else x)
    return out


def should_double_judge(question_id: str) -> bool:
    if "event_ordering" not in (question_id or ""):
        return False
    return any(question_id.startswith(p) for p in _DOUBLE_JUDGE_PREFIXES)


def attach_order_metrics(parsed: Dict[str, Any], rubric: List[str]) -> Dict[str, Any]:
    """给已解析的 BEAM judge 对象补 align / coverage / tau。"""
    raw_align = parsed["align"] if "align" in parsed else None
    align = parse_align_list(raw_align, len(rubric))
    coverage, tau = order_metrics_from_align(align)
    parsed["align"] = align
    parsed["coverage"] = coverage
    parsed["tau"] = tau
    parsed["tau_official"] = official_tau_from_align(align)
    raw_scores = parsed["rubric_scores"] if "rubric_scores" in parsed else []
    rubric_all = False
    if isinstance(raw_scores, list) and len(raw_scores) == len(rubric):
        rubric_all = True
        for v in raw_scores:
            if not isinstance(v, (int, float)) or int(v) < 1:
                rubric_all = False
                break
    parsed["passed"] = coverage >= 1.0 and tau is not None and tau >= 0.999 and rubric_all
    return parsed


async def judge_beam_single(
    client: httpx.AsyncClient,
    base_url: str,
    question: str,
    standard_answer: str,
    agent_answer: str,
    rubric: List[str],
    category: str,
    timeout: float = 60.0,
    user_id: str = "judge_beam_user",
) -> Dict[str, Any]:
    """BEAM-10M 风格的 rubric-based 评判。

    Args:
        rubric: 该题的标准 rubric 检查点列表，每项 1 个短句。
        category: BEAM-10M 类别名（abstention / contradiction_resolution / ...）。

    Returns:
        ``{"rubric_scores": List[int], "passed": bool, "reason": str}``
    """
    rubric_block = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rubric)) or "（无 rubric）"
    prompt = _BEAM_JUDGE_PROMPT.format(
        category=category,
        question=question,
        standard_answer=standard_answer,
        rubric_block=rubric_block,
        agent_answer=agent_answer,
    )

    last_text = ""
    last_status = -1
    last_error = "unknown"
    for attempt in range(_JUDGE_MAX_RETRIES):
        resp = await call_chat_with_history(
            client=client,
            base_url=base_url,
            user_id=user_id,
            message=prompt,
            history=[],
            timeout=timeout,
            as_judge=True,
        )
        last_status = resp.get("status_code", -1)
        last_error = str(resp.get("error", "unknown"))
        last_text = extract_text_from_response(resp.get("data")) if last_status == 200 else ""
        parsed = parse_beam_judge_response(last_text, rubric)
        reason = str(parsed.get("reason", ""))
        if not _is_transient_judge_failure(last_status, last_text) and not reason.startswith("无法解析"):
            return parsed
        if attempt < _JUDGE_MAX_RETRIES - 1:
            await asyncio.sleep(_JUDGE_BACKOFF_BASE * (2**attempt))

    if last_status != 200:
        return {
            "rubric_scores": [0] * len(rubric),
            "passed": False,
            "reason": f"评判请求失败(瞬时故障, 重试耗尽): status={last_status}, error={last_error}",
        }
    return parse_beam_judge_response(last_text, rubric)


async def _eq_matrix_once(
    client: httpx.AsyncClient,
    base_url: str,
    rubric: List[str],
    agent_items: List[str],
    timeout: float,
    user_id: str,
) -> tuple[List[List[bool]], int, str, str]:
    pair_lines: list[str] = []
    for i, ref in enumerate(rubric, 1):
        for j, sys in enumerate(agent_items, 1):
            pair_lines.append(f"PAIR {i}-{j}\nFirst snippet: {ref}\nSecond snippet: {sys}")
    prompt = _BEAM_EQ_BATCH_PROMPT.format(system=_BEAM_EQ_SYSTEM, pairs="\n\n".join(pair_lines))
    last_text = ""
    last_status = -1
    last_error = "unknown"
    for attempt in range(_JUDGE_MAX_RETRIES):
        resp = await call_chat_with_history(
            client=client,
            base_url=base_url,
            user_id=user_id,
            message=prompt,
            history=[],
            timeout=timeout,
            as_judge=True,
        )
        last_status = resp["status_code"] if "status_code" in resp else -1
        last_error = str(resp["error"] if "error" in resp else "unknown")
        last_text = extract_text_from_response(resp["data"]) if last_status == 200 and "data" in resp else ""
        if not _is_transient_judge_failure(last_status, last_text):
            mat = parse_eq_pair_lines(last_text, len(rubric), len(agent_items))
            return mat, last_status, last_error, last_text
        if attempt < _JUDGE_MAX_RETRIES - 1:
            await asyncio.sleep(_JUDGE_BACKOFF_BASE * (2**attempt))
    return parse_eq_pair_lines(last_text, len(rubric), len(agent_items)), last_status, last_error, last_text


async def judge_beam_order(
    client: httpx.AsyncClient,
    base_url: str,
    question: str,
    standard_answer: str,
    agent_answer: str,
    rubric: List[str],
    category: str,
    timeout: float = 60.0,
    user_id: str = "judge_beam_order_user",
    question_id: str = "",
) -> Dict[str, Any]:
    """event_ordering：Listing 21 逐对 YES/NO，本地拼 align / τ。"""
    _ = question
    _ = standard_answer
    _ = category
    items = split_agent_items(agent_answer)
    if not rubric:
        empty = {"rubric_scores": [], "passed": False, "reason": "empty rubric", "align": []}
        return attach_order_metrics(empty, rubric)
    if not items:
        empty = {
            "rubric_scores": [0] * len(rubric),
            "passed": False,
            "reason": "Agent 答案无条目",
            "align": [None] * len(rubric),
        }
        return attach_order_metrics(empty, rubric)
    mat, status, err, text = await _eq_matrix_once(client, base_url, rubric, items, timeout, user_id)
    align = align_from_eq_matrix(mat)
    pair_n, yes_n = pair_line_stats(text)
    if should_double_judge(question_id):
        mat2, _st2, _err2, text2 = await _eq_matrix_once(client, base_url, rubric, items, timeout, user_id + "_b")
        align = majority_align(align, align_from_eq_matrix(mat2))
        p2, y2 = pair_line_stats(text2)
        pair_n += p2
        yes_n += y2
    scores = [1 if x is not None else 0 for x in align]
    parsed: Dict[str, Any] = {
        "align": align,
        "rubric_scores": scores,
        "passed": False,
        "reason": (
            f"Listing21 pairs={len(rubric) * len(items)} pair_lines={pair_n} yes={yes_n} status={status} {err[:80]}"
        ),
    }
    return attach_order_metrics(parsed, rubric)


def parse_beam_judge_response(text: str, rubric: List[str]) -> Dict[str, Any]:
    """解析 BEAM 风格评判输出。

    兼容 LLM 偶发给出 ``correct``（LongMemEval 风格）或只给 PASS/FAIL 字样的情况，
    全部 fallback 到"全部命中/全部未命中"两种极端结果，由调用方决定是否重判。
    """
    fallback = {
        "rubric_scores": [0] * len(rubric),
        "passed": False,
        "reason": f"无法解析: {text[:200]}",
    }
    if not text:
        return {**fallback, "reason": "评判回复为空"}

    parsed: Dict[str, Any] | None = None

    # 1) 直接 JSON
    try:
        candidate = json.loads(text.strip())
        if isinstance(candidate, dict):
            parsed = candidate
    except json.JSONDecodeError:
        pass

    # 2) Markdown 代码块
    if parsed is None:
        json_pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
        for match in re.findall(json_pattern, text, re.DOTALL):
            try:
                candidate = json.loads(match.strip())
                if isinstance(candidate, dict):
                    parsed = candidate
                    break
            except json.JSONDecodeError:
                continue

    # 3) 大括号内 JSON
    if parsed is None:
        brace_pattern = r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
        for match in re.findall(brace_pattern, text):
            try:
                candidate = json.loads(match)
                if isinstance(candidate, dict):
                    parsed = candidate
                    break
            except json.JSONDecodeError:
                continue

    if parsed is None:
        # 兜底：尝试 LongMemEval 风格 correct 字段
        lm = parse_judge_response(text)
        if lm.get("correct") is True:
            return {
                "rubric_scores": [1] * len(rubric),
                "passed": True,
                "reason": "LongMemEval-style judge fallback: correct=True",
            }
        return fallback

    raw_scores = parsed.get("rubric_scores") or []
    rubric_scores: List[int] = []
    for v in raw_scores:
        try:
            rubric_scores.append(1 if int(v) >= 1 else 0)
        except (TypeError, ValueError):
            rubric_scores.append(0)
    # 对齐长度
    while len(rubric_scores) < len(rubric):
        rubric_scores.append(0)
    rubric_scores = rubric_scores[: len(rubric)]

    if "passed" in parsed:
        passed = bool(parsed["passed"])
    else:
        passed = all(s == 1 for s in rubric_scores) and len(rubric_scores) > 0

    out: Dict[str, Any] = {
        "rubric_scores": rubric_scores,
        "passed": passed,
        "reason": str(parsed["reason"] if "reason" in parsed else ""),
    }
    if "align" in parsed:
        out["align"] = parsed["align"]
    return out
