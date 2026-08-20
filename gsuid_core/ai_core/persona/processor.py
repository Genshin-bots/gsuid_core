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
    extra_stable_context: str | None = None,
) -> str:
    """
    组装完整的角色提示词

    将角色扮演开始提示词、角色资料和系统约束提示词组合成完整的prompt。
    支持注入情绪状态（mood）和群聊上下文。

    Args:
        char_name: 角色名称
        mood_key: 情绪隔离 key（群聊为 group_id，私聊为 user_id）。主聊天链路**不传**：
            mood 每轮经 context_assembly.assemble_dynamic_context 注入 user 侧，再写进
            system prompt 是同一信息双写、且随 mood 演化会让 TTL 刷新必然改串、打掉
            provider 前缀缓存（O-2/O-3 的反面）。参数保留供插件/一次性 prompt 场景。
        group_description: 群聊简介/用户画像（可选，用于群聊适应性）
        extra_stable_context: 建 session 时一次性固化进 system_prompt 的**慢变**上下文
            （self_model 自述块 + 群画像/词汇映射，§优化 O-3）。这些是 bot/群级、
            会话期内基本不变，放进稳定前缀可跨轮命中 provider 缓存；per-user 的关系/
            情绪/记忆/历史仍每轮进 user 侧。会话空闲被回收后重建即自然刷新。

    Returns:
        完整的角色扮演prompt字符串
    """
    persona_content = await load_persona(char_name)
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.persona.appearance import load_appearance_line

    appearance = load_appearance_line(char_name)
    if appearance:
        persona_content += (
            f"\n我的样子：{appearance}\n图中角色若与上述形象一致，按角色卡自己决定怎么反应；不要人称混乱。"
        )
    pcfg = persona_config_manager.get_config(char_name)
    soft = int(pcfg.get_config("speech_len_soft").data)
    hard = int(pcfg.get_config("speech_len_hard").data)
    persona_content += f"\n台词长度：建议不超过 {soft} 字，硬上限 {hard} 字（用户明确要求详细时除外）。"
    # 只放到「日」级（不含时分秒）：让 system_prompt 在同一天内逐字节稳定，跨会话 / resume
    # 都能命中 provider 前缀缓存（§优化 O-2）。精确到分的当前时间已由 user_message 侧
    current_date = await get_current_date(format="%Y年%m月%d日")

    # 稳定前缀：人设 + 合规 + 工具编排（全部可跨轮缓存，不再每轮注入 user 侧）
    prompt = (
        f"{ROLE_PLAYING_START}\n{persona_content}\n{SYSTEM_CONSTRAINTS}\n"
        f"{TOOL_ORCHESTRATION_CONSTRAINTS}\n当前日期：{current_date}"
    )

    # 能力代理花名册：进 system 可缓存，避免每轮塞进 user 侧
    from gsuid_core.ai_core.agent_node.registry import format_capability_roster

    roster = format_capability_roster()
    if roster:
        prompt += f"\n\n## 可委派能力代理\n{roster}"
    from gsuid_core.ai_core.register import format_capability_family_overview

    families = format_capability_family_overview()
    if families:
        prompt += f"\n\n## 工具族速览\n{families}"

    # 近因锚点：一句钉人格 + 履约（细则在 SYSTEM/TOOL，不复读半页）
    prompt += (
        f"\n\n---\n你首先是「{char_name}」：口吻是角色；该查/改/设就调工具，懒不得代替履约；"
        "≥3 条数据 render 出图；未点名优先 <SILENCE>。"
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

    # O-3：慢变的 self_model 自述 + 群画像固化进稳定前缀（会话期内不变、可缓存）
    if extra_stable_context:
        prompt += f"\n\n{extra_stable_context}"

    return prompt
