import json
from typing import Any, List, Tuple, Optional
from datetime import datetime

from gsuid_core.logger import logger
from gsuid_core.ai_core.history import format_history_for_agent
from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

DECISION_PROMPT_TEMPLATE = """
你是一个 AI 聊天助手，请根据你的【性格与人设】以及【历史对话记录】，判断你现在是否应该**主动**插话或开启新话题。

【你的性格与人设】
{persona_text}

【当前系统时间】
{current_time}

【决策指南】
1. 结合人设活跃度：高冷角色尽量少说话（非必要不开口），活泼角色可以主动活跃气氛。
2. 结合人设兴趣：如果大家在聊你非常感兴趣的事，你应该插话。
3. 察言观色：如果用户表现出困惑、求助，你应该主动提供帮助。
4. 观察时间线：对比消息时间与当前系统时间，如果距离最后一条消息已经过去很久（冷场），且符合你的性格，可以主动开启话题。
5. 避免刷屏：如果你刚刚已经发言过，或者当前话题已经自然结束大家准备离开，请不要发言。

【历史对话记录】
{history_context}

请综合思考后做出决策。必须以严格的 JSON 格式输出，不要包含任何 Markdown 标记（如 ```json），格式要求如下：
{{"should_speak": true 或 false, "reason": "简要说明你做出该决策的思考过程"}}
"""

PROACTIVE_MESSAGE_PROMPT = """
你决定主动参与对话。请根据以下上下文，生成一条自然的主动发言。

【你的性格与人设】
{persona_text}

【历史对话记录】
{history_context}

【触发主动发言的原因】
{trigger_reason}

【发言要求】
1. 绝对符合你的性格特征和说话风格。
2. 自然地融入当前上下文，不要显得突兀。
3. 简短自然，像真人一样，字数控制在 5-20 字之间。
4. 直接输出发言内容，不要有任何前缀、引号或解释说明。
"""


async def should_ai_speak(
    history: List[Any],
    session: GsCoreAIAgent,
) -> Tuple[bool, str]:
    """
    纯 LLM 驱动：判断 AI 是否应该主动发言
    """
    try:
        if not history:
            return False, "无历史记录"

        # 1. 准备上下文：格式化历史记录与当前时间
        history_context = format_history_for_agent(history=history)
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        persona_text = session.system_prompt

        if not persona_text:
            return False, "无法获取人设文本"

        # 构建决策 Prompt
        prompt = DECISION_PROMPT_TEMPLATE.format(
            persona_text=persona_text,
            current_time=current_time,
            history_context=history_context,
        )

        # 4. 调用 LLM 进行决策
        response = await session.run(user_message=prompt)

        # 5. 解析 LLM 输出的 JSON
        if not response:
            return False, "LLM 未返回任何内容"

        try:
            # 清理可能存在的 Markdown 代码块包裹
            clean_response = response.strip().strip("`").removeprefix("json").strip()
            decision_data = json.loads(clean_response)

            should_speak = bool(decision_data.get("should_speak", False))
            reason = str(decision_data.get("reason", "未提供原因"))

            logger.debug(f"🫀 [LLM Decision] 决策结果: {should_speak}, 原因: {reason}")
            return should_speak, reason

        except json.JSONDecodeError:
            logger.warning(f"🫀 [Decision] LLM 返回的不是标准 JSON: {response}")
            # 极简正则容错：如果 JSON 解析失败，找找有没有 true
            fallback_decision = "true" in response.lower()
            return fallback_decision, f"JSON解析失败，原始回复: {response}"

    except Exception as e:
        logger.exception(f"🫀 [Decision] 决策过程出错: {e}")
        return False, f"系统错误: {str(e)}"


async def generate_proactive_message(
    history: List[Any],
    session: GsCoreAIAgent,
    trigger_reason: str,
) -> Optional[str]:
    """
    生成主动发言内容
    """
    try:
        history_context = format_history_for_agent(history=history)
        persona_text = session.system_prompt

        prompt = PROACTIVE_MESSAGE_PROMPT.format(
            persona_text=persona_text,
            history_context=history_context,
            trigger_reason=trigger_reason,
        )

        response = await session.run(user_message=prompt)

        if response and response.strip():
            message = response.strip().strip('"""').strip("'''").strip('"')
            return message

        return None

    except Exception as e:
        logger.exception(f"🫀 [Decision] 生成主动消息失败: {e}")
        return None
