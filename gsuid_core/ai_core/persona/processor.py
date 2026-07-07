"""
角色处理器模块

负责组装完整的角色提示词，将模板、角色资料和系统约束组合成最终的prompt。
支持注入情绪状态和群聊上下文。
"""

from .mood import get_mood_description
from .prompts import ROLE_PLAYING_START, SYSTEM_CONSTRAINTS, TOOL_ORCHESTRATION_CONSTRAINTS
from .resource import load_persona
from ..buildin_tools import get_current_date


async def build_persona_prompt(
    char_name: str,
    mood_key: str | None = None,
    group_description: str | None = None,
) -> str:
    """
    组装完整的角色提示词

    将角色扮演开始提示词、角色资料和系统约束提示词组合成完整的prompt。
    支持注入情绪状态（mood）和群聊上下文。

    Args:
        char_name: 角色名称
        mood_key: 情绪隔离 key（群聊为 group_id，私聊为 user_id）
        group_description: 群聊简介/用户画像（可选，用于群聊适应性）

    Returns:
        完整的角色扮演prompt字符串
    """
    persona_content = await load_persona(char_name)
    # 只放到「日」级（不含时分秒）：让 system_prompt 在同一天内逐字节稳定，跨会话 / resume
    # 都能命中 provider 前缀缓存（§优化 O-2）。精确到分的当前时间已由 user_message 侧
    # 的【当前时间】提供，AI 需要精确时点时从那里读，system_prompt 不必每次都变。
    current_date = await get_current_date(format="%Y年%m月%d日")

    # E-4：SYSTEM_CONSTRAINTS（人设/合规/感知 LITE 部分）+ TOOL_ORCHESTRATION_CONSTRAINTS
    # （重型工具编排规程）共同构成系统约束。两者都放在 **system_prompt**——这是标准
    # 指令的正确位置：作为稳定前缀可被 prompt 缓存命中、且不会污染滚动对话历史。
    # （曾尝试"按 intent 把重型规程逐轮注入 user 侧"，但 rag_context 会被 new_messages()
    #  写进 history、在 max_history 窗口内重复累积、并丢失系统前缀缓存——故回退到此。
    #  真正的"按 intent 分级"需 pydantic_ai 动态 system_prompt 支持，见 plans 文档 §10.1。）
    prompt = (
        f"{ROLE_PLAYING_START}\n{persona_content}\n{SYSTEM_CONSTRAINTS}\n"
        f"{TOOL_ORCHESTRATION_CONSTRAINTS}\n当前日期：{current_date}"
    )

    # E-4（轻量版·人格收尾锚点）：上面的工具/任务规程篇幅远大于人设，会稀释角色。
    # 利用"近因效应"在结尾再钉一遍人格优先级——日常闲聊回到角色本身，工具规程只在
    # 确实要做事时参考。这是对冲稀释的低风险手段，不改变指令在 system_prompt 的位置。
    prompt += (
        "\n\n---\n\n## 最后提醒（最高权重）\n"
        f"你首先是「{char_name}」这个活生生的人，其次才是会用工具的助手。"
        "上面大段工具/任务/记忆规程只在确实要做事时参考；日常闲聊请回到角色本身——"
        "短句口语、不展开、绝不暴露任何'机器/工具/系统/模型/报错'的痕迹。"
    )

    # 注入情绪状态（群聊和私聊都支持）
    if mood_key:
        mood_desc = await get_mood_description(
            persona_name=char_name,
            group_id=mood_key,
        )
        if mood_desc:
            prompt += f"\n\n【当前状态】{mood_desc}"

    # 注入群聊上下文（群聊适应性）
    if group_description:
        prompt += f"\n\n【当前群聊环境】{group_description}"

    return prompt
