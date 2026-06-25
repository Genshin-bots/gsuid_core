"""LLM 评判 + 简单字符串匹配。

包含两个核心入口：

- :func:`judge_single_answer` —— LongMemEval 风格，给定 (question, standard_answer,
  agent_answer) 直接让 LLM 判 PASS / FAIL，返回 ``{"correct": bool, "reason": str}``；
- :func:`judge_beam_single` —— BEAM-10M 风格，按 rubric 列表逐条给分，返回
  ``{"rubric_scores": [...], "passed": bool, "reason": str}``。
"""

from __future__ import annotations

import re
import json
from typing import Any, Dict, List

import httpx

from .http_client import (
    call_chat_with_history,
    extract_text_from_response,
)

# ─────────────────────────────────────────────
# LongMemEval 风格：单一 PASS / FAIL
# ─────────────────────────────────────────────


async def judge_single_answer(
    client: httpx.AsyncClient,
    base_url: str,
    question: str,
    standard_answer: str,
    agent_answer: str,
    timeout: float = 60.0,
    user_id: str = "judge_user",
) -> Dict[str, Any]:
    """使用 LLM Agent 评判单道题的回答。

    通过 ``/api/chat_with_history`` 接口发送评判请求，让 LLM 判断回答是否正确。
    使用独立的 ``user_id`` 避免与其他会话冲突。
    """
    judge_prompt = f"""请判断 Agent 的回答是否正确。

问题: {question}

标准答案: {standard_answer}

Agent 的回答: {agent_answer}

请判断 Agent 的回答是否与标准答案语义一致，只输出 JSON:
{{"correct": true/false, "reason": "判断理由"}}"""

    resp = await call_chat_with_history(
        client=client,
        base_url=base_url,
        user_id=user_id,
        message=judge_prompt,
        history=[],
        timeout=timeout,
    )

    status_code = resp.get("status_code", -1)
    if status_code != 200:
        error_msg = resp.get("error", "unknown")
        return {"correct": False, "reason": f"评判请求失败: status={status_code}, error={error_msg}"}

    raw_data = resp.get("data")
    judge_text = extract_text_from_response(raw_data)
    return parse_judge_response(judge_text)


def parse_judge_response(text: str) -> Dict[str, Any]:
    """解析评判 Agent 的回复，提取 ``correct`` 和 ``reason``。

    支持多种格式：
      - 纯 JSON：``{"correct": true, "reason": "..."}``
      - Markdown 代码块包裹的 JSON
      - 包含 JSON 片段的混合文本
    """
    if not text:
        return {"correct": False, "reason": "评判回复为空"}

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


def simple_string_match(standard_answer: str, agent_answer: str) -> bool:
    """简单字符串匹配评判（作为 LLM 评判的备选）。

    判断标准答案的核心词是否出现在 Agent 回答中（80% 分词命中率）。
    """
    if not agent_answer or agent_answer.startswith("[ERROR]"):
        return False

    std_lower = standard_answer.lower().strip()
    agent_lower = agent_answer.lower().strip()

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

    resp = await call_chat_with_history(
        client=client,
        base_url=base_url,
        user_id=user_id,
        message=prompt,
        history=[],
        timeout=timeout,
    )

    status_code = resp.get("status_code", -1)
    if status_code != 200:
        error_msg = resp.get("error", "unknown")
        return {
            "rubric_scores": [0] * len(rubric),
            "passed": False,
            "reason": f"评判请求失败: status={status_code}, error={error_msg}",
        }

    raw_data = resp.get("data")
    judge_text = extract_text_from_response(raw_data)
    return parse_beam_judge_response(judge_text, rubric)


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

    return {
        "rubric_scores": rubric_scores,
        "passed": passed,
        "reason": str(parsed.get("reason", "")),
    }
