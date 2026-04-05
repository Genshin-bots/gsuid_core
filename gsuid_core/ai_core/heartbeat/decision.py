"""
AI 主动发言决策模块

负责判断 AI 是否应该主动发言，以及生成主动发言内容。
根据 Agent 的性格和历史记录上下文做出决策。
"""

import re
from typing import Any, List, Tuple, Optional
from datetime import datetime

from gsuid_core.logger import logger
from gsuid_core.ai_core.history import format_history_for_agent
from gsuid_core.ai_core.persona import build_persona_prompt
from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
from gsuid_core.ai_core.ai_config import persona_config

# 决策提示词模板
DECISION_PROMPT_TEMPLATE = """
你正在参与一个群聊/私聊，请根据以下历史对话记录，判断你现在是否应该主动发言。

【历史对话记录】
{history_context}

【你的性格特征】
{persona_traits}

【决策规则】
1. 如果对话已经自然结束（大家已经说完、话题已收尾），不要发言
2. 如果最近的话题是你感兴趣的（{interests}），可以考虑参与
3. 如果用户表现出困惑、需要帮助，应该主动提供帮助
4. 如果对话冷场超过5分钟，可以主动开启新话题或分享趣事
5. 如果你刚刚已经主动发言过，不要连续发言
6. 根据你的性格，{personality_guidance}

请分析后给出决策，格式如下：
决策: [应该发言/不应该发言]
原因: [简要说明原因]
发言内容建议: [如果应该发言，建议说什么；如果不应该，留空]
"""

# 主动发言生成提示词模板
PROACTIVE_MESSAGE_PROMPT = """
你决定主动参与对话。请根据以下上下文生成一条自然的主动发言。

【历史对话记录】
{history_context}

【触发原因】
{trigger_reason}

【你的性格特征】
{persona_traits}

【发言要求】
1. 符合你的性格特征和说话风格
2. 自然地融入当前对话上下文
3. 不要显得突兀或刻意
4. 简短自然，像真人一样
5. 可以是对之前话题的回应，也可以是新话题的开启
6. 字数控制在 5-30 字之间

请直接输出发言内容，不要有任何前缀或解释。
"""


async def should_ai_speak(
    history: List[Any],
    group_id: Optional[str],
    user_id: str,
) -> Tuple[bool, str]:
    """
    判断 AI 是否应该主动发言

    Args:
        history: 历史记录列表
        group_id: 群聊 ID（私聊时为 None）
        user_id: 用户 ID

    Returns:
        (是否应该发言, 原因说明)
    """
    try:
        # 如果没有历史记录，不需要发言
        if not history:
            return False, "无历史记录"

        # 检查最后一条消息的时间
        last_message = history[-1]
        last_time = datetime.fromtimestamp(last_message.timestamp)
        now = datetime.now()
        time_diff_minutes = (now - last_time).total_seconds() / 60

        # 如果最后一条消息是 AI 自己发的，不需要发言
        if last_message.role == "assistant":
            # 检查是否是主动发言
            metadata = last_message.metadata or {}
            if metadata.get("proactive", False):
                return False, "刚刚已主动发言"
            return False, "刚刚已回应用户"

        # 获取启用的性格/人设
        persona_traits = await _get_persona_traits(user_id, group_id)

        # 根据性格特征判断
        decision = _make_decision_by_persona(
            history=history,
            time_diff_minutes=time_diff_minutes,
            persona_traits=persona_traits,
        )

        return decision

    except Exception as e:
        logger.exception(f"🫀 [Decision] 决策过程出错: {e}")
        return False, f"决策出错: {str(e)}"


def _make_decision_by_persona(
    history: List[Any],
    time_diff_minutes: float,
    persona_traits: dict,
) -> Tuple[bool, str]:
    """
    根据性格特征做出决策

    Args:
        history: 历史记录
        time_diff_minutes: 距离最后消息的时间差（分钟）
        persona_traits: 性格特征

    Returns:
        (是否应该发言, 原因)
    """
    # 获取性格参数
    presence = persona_traits.get("presence", "低")  # 活跃度: 高/中/低
    interests = persona_traits.get("interests", [])
    # proactive_topics = persona_traits.get("proactive_topics", [])

    # 检查最近话题是否感兴趣
    recent_content = " ".join([h.content for h in history[-3:]])
    is_interested = any(interest in recent_content for interest in interests)

    # 决策逻辑
    # 1. 高活跃度性格：更容易主动发言
    if presence == "高":
        if time_diff_minutes > 3 and is_interested:
            return True, "高活跃度性格，话题感兴趣且对话暂停"
        if time_diff_minutes > 10:
            return True, "高活跃度性格，对话冷场"

    # 2. 中活跃度性格：适度主动
    elif presence == "中":
        if time_diff_minutes > 5 and is_interested:
            return True, "话题感兴趣且对话暂停一段时间"
        if time_diff_minutes > 15:
            return True, "对话冷场较长时间"

    # 3. 低活跃度性格（默认）：很少主动发言
    else:
        # 低活跃度只在特定情况下发言
        if time_diff_minutes > 20 and is_interested:
            return True, "虽然低活跃度，但话题非常感兴趣且冷场很久"

        # 检查是否有用户表现出需要帮助
        help_keywords = ["怎么办", "求助", "不懂", "不会", "怎么", "为什么"]
        if any(kw in recent_content for kw in help_keywords):
            if time_diff_minutes > 2:
                return True, "用户可能需要帮助"

    # 默认不发言
    return (
        False,
        f"不符合主动发言条件（活跃度: {presence}, 时间差: {time_diff_minutes:.1f}分钟, 感兴趣: {is_interested}）",
    )


async def _get_persona_traits(user_id: str, group_id: Optional[str]) -> dict:
    """
    获取指定用户/群组的 AI 性格特征

    通过读取 persona 文件内容并解析关键特征。

    Args:
        user_id: 用户 ID
        group_id: 群聊 ID

    Returns:
        性格特征字典
    """
    try:
        # 获取启用的性格列表
        enabled_personas = persona_config.get_config("enable_persona").data

        if not enabled_personas:
            # 默认性格特征
            return {
                "presence": "低",
                "interests": ["游戏", "动漫", "科技"],
                "proactive_topics": ["有趣的事情", "日常分享"],
                "description": "默认性格，较为被动",
            }

        # 获取针对该会话的性格设置
        persona_for_session = persona_config.get_config("persona_for_session").data
        session_key = f"{group_id}:{user_id}" if group_id else f"private:{user_id}"

        # 确定使用哪个性格
        active_persona = enabled_personas[0]  # 默认使用第一个启用的性格
        if session_key in persona_for_session:
            active_persona = persona_for_session[session_key]

        # 读取 persona 文件内容
        try:
            persona_content = await build_persona_prompt(active_persona)
        except Exception as e:
            logger.debug(f"🫀 [Decision] 读取 persona 文件失败: {e}")
            persona_content = ""

        # 从 persona 内容中解析特征
        traits = _parse_persona_content(persona_content)
        traits["name"] = active_persona

        return traits

    except Exception as e:
        logger.debug(f"🫀 [Decision] 获取性格特征失败: {e}")
        return {
            "presence": "低",
            "interests": [],
            "proactive_topics": [],
        }


def _parse_persona_content(content: str) -> dict:
    """
    从 persona 文件内容中解析性格特征

    Args:
        content: persona 文件内容

    Returns:
        性格特征字典
    """
    traits = {
        "presence": "低",  # 默认低活跃度
        "interests": [],
        "proactive_topics": [],
        "description": "",
        "style": "",
    }

    if not content:
        return traits

    # 解析 Presence (活跃度)
    # 查找类似 "Presence (活跃度, 一般角色都默认潜水, 非必要不现身，除非是感兴趣的话题):" 的行
    presence_match = re.search(r"Presence\s*\([^)]*活跃度[^)]*\):\s*([^\n]+)", content, re.IGNORECASE)
    if presence_match:
        presence_text = presence_match.group(1).lower()
        if "高" in presence_text or "活跃" in presence_text:
            traits["presence"] = "高"
        elif "中" in presence_text:
            traits["presence"] = "中"
        else:
            traits["presence"] = "低"

    # 解析 Interest (兴趣)
    # 查找类似 "Interest: 寻找隐蔽的地方睡觉、长高、逃避巫女的抓捕、吃紫菜包饭。" 的行
    interest_match = re.search(r"Interest:\s*([^\n]+)", content, re.IGNORECASE)
    if interest_match:
        interest_text = interest_match.group(1)
        # 分割兴趣项（支持中文和英文分隔符）
        interests = re.split(r"[、,，;；]+", interest_text)
        traits["interests"] = [i.strip() for i in interests if i.strip()]

    # 解析感兴趣的话题（Social Interaction 部分）
    # 查找类似 "感兴趣的话题: 快速长高的方法、绝佳的睡觉地点、好吃的饭团。" 的行
    topic_match = re.search(r"感兴趣的话题[:：]\s*([^\n]+)", content, re.IGNORECASE)
    if topic_match:
        topic_text = topic_match.group(1)
        topics = re.split(r"[、,，;；]+", topic_text)
        traits["proactive_topics"] = [t.strip() for t in topics if t.strip()]

    # 解析主动发言内容
    # 查找类似 "主动发言： 发现了一个晒太阳的好地方…呼。" 的行
    proactive_match = re.search(r"主动发言[:：]\s*([^\n]+)", content, re.IGNORECASE)
    if proactive_match:
        traits["proactive_example"] = proactive_match.group(1).strip()

    # 解析 Style (风格)
    style_match = re.search(r"Style\s*\([^)]*\):\s*([^\n]+)", content, re.IGNORECASE)
    if style_match:
        traits["style"] = style_match.group(1).strip()

    # 解析 Identity (身份)
    identity_match = re.search(r"Identity:\s*([^\n]+)", content, re.IGNORECASE)
    if identity_match:
        traits["description"] = identity_match.group(1).strip()

    return traits


async def generate_proactive_message(
    history: List[Any],
    session: GsCoreAIAgent,
    user_id: str,
    group_id: Optional[str],
    trigger_reason: str,
) -> Optional[str]:
    """
    生成主动发言内容

    Args:
        history: 历史记录
        session: AI Session
        user_id: 用户 ID
        group_id: 群聊 ID
        trigger_reason: 触发原因

    Returns:
        生成的消息内容，或 None 如果不需要发言
    """
    try:
        # 格式化历史记录
        history_context = format_history_for_agent(
            history=history,
            current_user_id=user_id,
        )

        # 获取性格特征
        persona_traits = await _get_persona_traits(user_id, group_id)
        persona_description = _format_persona_traits(persona_traits)

        # 构建提示词
        prompt = PROACTIVE_MESSAGE_PROMPT.format(
            history_context=history_context,
            trigger_reason=trigger_reason,
            persona_traits=persona_description,
        )

        # 使用 Agent 生成回复
        response = await session.run(user_message=prompt)

        if response and response.strip():
            # 清理回复内容
            message = response.strip()
            # 移除可能的引号
            message = message.strip('"""').strip("'''")
            return message

        return None

    except Exception as e:
        logger.exception(f"🫀 [Decision] 生成主动消息失败: {e}")
        return None


def _format_persona_traits(traits: dict) -> str:
    """
    格式化性格特征为字符串

    Args:
        traits: 性格特征字典

    Returns:
        格式化后的字符串
    """
    lines = []

    if "name" in traits:
        lines.append(f"角色名称: {traits['name']}")

    if "description" in traits and traits["description"]:
        lines.append(f"身份: {traits['description']}")

    if "presence" in traits:
        lines.append(f"活跃度: {traits['presence']}")

    if "interests" in traits and traits["interests"]:
        lines.append(f"兴趣: {', '.join(traits['interests'])}")

    if "proactive_topics" in traits and traits["proactive_topics"]:
        lines.append(f"感兴趣的话题: {', '.join(traits['proactive_topics'])}")

    if "style" in traits and traits["style"]:
        lines.append(f"说话风格: {traits['style']}")

    return "\n".join(lines) if lines else "普通性格"
