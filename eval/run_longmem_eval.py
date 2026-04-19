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
  python eval/run_longmem_eval.py run --base-url http://127.0.0.1:8765

  # 运行 Phase 2（评判，需要 gsuid_core 服务已启动）
  python eval/run_longmem_eval.py judge --answers-file eval/results/answers.json

  # 一键运行全部
  python eval/run_longmem_eval.py all --base-url http://127.0.0.1:8765

  # 指定题目范围
  python eval/run_longmem_eval.py run --start 0 --end 5 --base-url http://127.0.0.1:8765

  # 指定输出目录
  python eval/run_longmem_eval.py run --output-dir eval/results

  # 不使用 LLM 评判，改用字符串匹配
  python eval/run_longmem_eval.py judge --answers-file eval/results/answers.json --no-llm-judge
"""

import os
import re
import json
import time
import asyncio
import argparse
from typing import Any, Dict, List, Optional
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 默认配置
DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_CHAT_API = "/api/chat_with_history"
DEFAULT_SEND_API = "/api/send_msg"
DEFAULT_TIMEOUT = 1000.0  # 单次请求超时（秒），长对话可能需要较长时间

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


def load_eval_data(json_path: str) -> List[Dict[str, Any]]:
    """加载 longmemeval_s_cleaned.json 数据"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"[Loader] 已加载 {len(data)} 道题目，来自 {json_path}")
    return data


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


# ---------------------------------------------------------------------------
# Phase 1: 运行评估 - 通过 /api/chat_with_history 传入 history + question
# ---------------------------------------------------------------------------


async def call_chat_with_history(
    client: httpx.AsyncClient,
    base_url: str,
    user_id: str,
    message: str,
    history: List[Dict[str, str]],
    persona_name: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    调用 /api/chat_with_history 接口

    请求体:
        user_id      : str          - 用户ID
        message      : str          - 当前用户消息（问题）
        history      : List[dict]   - 历史对话 [{"role": "user/assistant", "content": "..."}]
        persona_name : str|None     - Persona名称（可选）
        bot_id       : str          - Bot ID（默认 "HTTP"）
        group_id     : str|None     - 群组ID（私聊时为 None）

    响应体:
        {"status_code": 200, "data": "Agent的回复文本"}
        {"status_code": -101, "data": None, "error": "message is required"}
        {"status_code": -102, "data": None, "error": "异常信息"}
    """
    url = f"{base_url}{DEFAULT_CHAT_API}"

    payload = {
        "user_id": user_id,
        "message": message,
        "history": history,
        "bot_id": "HTTP",
        "group_id": None,  # 私聊模式
    }
    if persona_name:
        payload["persona_name"] = persona_name

    try:
        response = await client.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException:
        print(f"  [WARN] 请求超时 ({timeout}s): {message[:50]}...")
        return {"status_code": -1, "data": None, "error": "timeout"}
    except httpx.HTTPStatusError as e:
        print(f"  [WARN] HTTP 错误 {e.response.status_code}: {message[:50]}...")
        return {"status_code": e.response.status_code, "data": None, "error": str(e)}
    except Exception as e:
        print(f"  [WARN] 请求异常: {e}")
        return {"status_code": -1, "data": None, "error": str(e)}


async def call_send_msg(
    client: httpx.AsyncClient,
    base_url: str,
    text: str,
    user_id: str = "eval_user",
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    调用 /api/send_msg 接口（用于评判阶段）

    以私聊模式发送消息。
    """
    url = f"{base_url}{DEFAULT_SEND_API}"

    payload = {
        "bot_id": "eval",
        "bot_self_id": "eval_bot",
        "msg_id": "",
        "user_type": "direct",
        "group_id": None,
        "user_id": user_id,
        "sender": {},
        "user_pm": 3,
        "content": [{"type": "text", "data": text}],
    }

    try:
        response = await client.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException:
        print(f"  [WARN] 请求超时 ({timeout}s): {text[:50]}...")
        return {"status_code": -1, "data": None, "error": "timeout"}
    except httpx.HTTPStatusError as e:
        print(f"  [WARN] HTTP 错误 {e.response.status_code}: {text[:50]}...")
        return {"status_code": e.response.status_code, "data": None, "error": str(e)}
    except Exception as e:
        print(f"  [WARN] 请求异常: {e}")
        return {"status_code": -1, "data": None, "error": str(e)}


def extract_text_from_response(response_data: Any) -> str:
    """
    从 HTTP 响应中提取 Agent 回复的文本

    /api/chat_with_history 返回的 data 直接是字符串
    /api/send_msg 返回的 data 可能是 MessageSend 格式
    """
    if response_data is None:
        return ""

    # 如果是字符串，直接返回
    if isinstance(response_data, str):
        return response_data.strip()

    # 如果是字典，尝试提取 content
    if isinstance(response_data, dict):
        content = response_data.get("content")
        if content and isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    data = item.get("data", "")
                    if data:
                        texts.append(str(data))
            return "\n".join(texts).strip()

        if "data" in response_data:
            return extract_text_from_response(response_data["data"])

    # 如果是列表，遍历提取
    if isinstance(response_data, list):
        texts = []
        for item in response_data:
            text = extract_text_from_response(item)
            if text:
                texts.append(text)
        return "\n".join(texts).strip()

    return str(response_data).strip()


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

    # 提取回答文本
    agent_answer = ""
    status_code = resp.get("status_code", -1)
    if status_code == 200:
        raw_data = resp.get("data")
        agent_answer = extract_text_from_response(raw_data)
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

    # 回答文件路径
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    answers_file = os.path.join(output_dir, f"answers_{timestamp}.json")

    # 运行评估
    results: List[Dict[str, Any]] = []

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
            with open(answers_file, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)

    # 最终统计
    success_count = sum(1 for r in results if r.get("status_code") == 200)
    error_count = len(results) - success_count
    print(f"\n{'=' * 60}")
    print("[Phase1] 完成!")
    print(f"  总题数: {len(results)}")
    print(f"  成功: {success_count}")
    print(f"  失败: {error_count}")
    print(f"  回答已保存至: {answers_file}")

    return answers_file


# ---------------------------------------------------------------------------
# Phase 2: 评判 - 对比答案，计算准确率
# ---------------------------------------------------------------------------


async def judge_single_answer(
    client: httpx.AsyncClient,
    base_url: str,
    question: str,
    standard_answer: str,
    agent_answer: str,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """
    使用 LLM Agent 评判单道题的回答

    通过 /api/chat_with_history 接口发送评判请求，让 LLM 判断回答是否正确。
    使用独立的 user_id 避免与其他会话冲突。

    Args:
        client: httpx 异步客户端
        base_url: 服务基础 URL
        question: 问题
        standard_answer: 标准答案
        agent_answer: Agent 的回答
        timeout: 请求超时

    Returns:
        评判结果 {"correct": bool, "reason": str}
    """
    judge_prompt = f"""请判断 Agent 的回答是否正确。

问题: {question}

标准答案: {standard_answer}

Agent 的回答: {agent_answer}

请判断 Agent 的回答是否与标准答案语义一致，只输出 JSON:
{{"correct": true/false, "reason": "判断理由"}}"""

    # 使用 /api/chat_with_history 接口，传入空的 history
    resp = await call_chat_with_history(
        client=client,
        base_url=base_url,
        user_id="judge_user",
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
    """
    解析评判 Agent 的回复，提取 correct 和 reason

    支持多种格式:
    - 纯 JSON: {"correct": true, "reason": "..."}
    - Markdown 代码块包裹的 JSON
    - 包含 JSON 的混合文本
    """
    if not text:
        return {"correct": False, "reason": "评判回复为空"}

    # 尝试直接解析
    try:
        result = json.loads(text.strip())
        if isinstance(result, dict) and "correct" in result:
            return {
                "correct": bool(result["correct"]),
                "reason": str(result.get("reason", "")),
            }
    except json.JSONDecodeError:
        pass

    # 尝试从 Markdown 代码块中提取
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

    # 尝试从文本中提取 JSON 片段
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

    # 无法解析，基于关键词简单判断
    text_lower = text.lower()
    if '"correct": true' in text_lower or "'correct': true" in text_lower:
        return {"correct": True, "reason": f"基于关键词判断: {text[:200]}"}
    elif '"correct": false' in text_lower or "'correct': false" in text_lower:
        return {"correct": False, "reason": f"基于关键词判断: {text[:200]}"}

    return {"correct": False, "reason": f"无法解析评判回复: {text[:200]}"}


def simple_string_match(standard_answer: str, agent_answer: str) -> bool:
    """
    简单的字符串匹配评判（作为 LLM 评判的备选方案）

    判断标准答案的核心词是否出现在 Agent 回答中
    """
    if not agent_answer or agent_answer.startswith("[ERROR]"):
        return False

    # 标准答案转小写
    std_lower = standard_answer.lower().strip()
    agent_lower = agent_answer.lower().strip()

    # 完全包含
    if std_lower in agent_lower:
        return True

    # 分词匹配（按空格和标点分割）
    std_words = set(re.findall(r"\w+", std_lower))
    agent_words = set(re.findall(r"\w+", agent_lower))

    if not std_words:
        return False

    # 如果标准答案的大部分词都出现在 Agent 回答中
    overlap = std_words & agent_words
    ratio = len(overlap) / len(std_words) if std_words else 0

    return ratio >= 0.8


async def run_phase2(
    answers_file: str,
    base_url: str,
    output_dir: str,
    eval_data_path: Optional[str] = None,
    use_llm_judge: bool = True,
    timeout: float = 60.0,
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
    with open(answers_file, "r", encoding="utf-8") as f:
        answers = json.load(f)
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
        for idx, answer_data in enumerate(answers):
            question_id = answer_data.get("question_id", f"unknown_{idx}")
            question_type = answer_data.get("question_type", "unknown")
            question = answer_data.get("question", "")
            standard_answer = answer_data.get("standard_answer", "")
            agent_answer = answer_data.get("agent_answer", "")

            print(f"\n[Judge {idx}/{len(answers)}] ID: {question_id}")

            # 跳过执行失败的题目
            if agent_answer.startswith("[ERROR]"):
                judge_result: Dict[str, Any] = {
                    "correct": False,
                    "reason": "Agent 执行失败",
                }
                error_count += 1
            elif use_llm_judge:
                judge_result = await judge_single_answer(
                    client=client,
                    base_url=base_url,
                    question=question,
                    standard_answer=standard_answer,
                    agent_answer=agent_answer,
                    timeout=timeout,
                )
            else:
                is_correct = simple_string_match(standard_answer, agent_answer)
                judge_result = {
                    "correct": is_correct,
                    "reason": "简单字符串匹配",
                }

            is_correct = judge_result.get("correct", False)
            if is_correct:
                correct_count += 1
                status_icon = "✅"
            else:
                wrong_count += 1
                status_icon = "❌"

            print(f"  {status_icon} {'正确' if is_correct else '错误'} - {judge_result.get('reason', '')}")

            # 按类型统计
            if question_type not in type_stats:
                type_stats[question_type] = {"correct": 0, "wrong": 0, "error": 0}
            if is_correct:
                type_stats[question_type]["correct"] += 1
            elif agent_answer.startswith("[ERROR]"):
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

    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

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
        default="eval/results",
        help="输出目录 (默认: eval/results)",
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
        default="eval/results",
        help="输出目录 (默认: eval/results)",
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
        default="eval/results",
        help="输出目录 (默认: eval/results)",
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
