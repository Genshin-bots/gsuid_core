"""
角色处理器模块

负责组装完整的角色提示词，将模板、角色资料和系统约束组合成最终的prompt。
支持注入情绪状态和群聊上下文。
"""

from .mood import get_mood_description
from .resource import load_persona
from .settings import get_master_title
from ..buildin_tools import get_current_date


def _render_system_constraints(char_name: str) -> str:
    """把主人 ID 列表与口头称呼填进宪法。会话创建时调用一次。"""
    from gsuid_core.config import core_config
    from gsuid_core.ai_core.prompt_group.store import render_prompt

    masters = ", ".join(str(m) for m in (core_config.get_config("masters") or []))
    title = get_master_title(char_name)
    return render_prompt(char_name, "system_constraints", masters=masters, master_title=title)


async def build_persona_prompt(
    char_name: str,
    mood_key: str | None = None,
    group_description: str | None = None,
    extra_stable_context: str | None = None,
    clock_date: str | None = None,
    include_meme_protocol: bool = True,
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
        clock_date: 评测 HTTP ``clock_at`` 的日标签；缺省用墙上日期。生产 WS 不传。
        include_meme_protocol: 看板转译等不经主会话呈现层的路径传 False，避免写出 ``<meme:>``。

    Returns:
        完整的角色扮演prompt字符串
    """
    persona_content = await load_persona(char_name)
    from gsuid_core.ai_core.persona.appearance import load_appearance_line

    appearance = load_appearance_line(char_name)
    from gsuid_core.ai_core.prompt_group.store import render_prompt
    from gsuid_core.ai_core.prompt_group.render import join_around_card, join_prompt_parts, prefix_closing_rule

    if appearance:
        line = render_prompt(char_name, "appearance_line", appearance=appearance)
        persona_content += f"\n{line}"

    from .chat_style import resolve_chat_style

    style = resolve_chat_style(char_name)
    speech = render_prompt(
        char_name,
        "speech_block",
        soft=str(style.soft),
        segment_hint=style.segment_hint,
    )
    answer = render_prompt(char_name, "answer_contract")
    speech_and_answer = join_prompt_parts(speech, answer)
    if speech_and_answer:
        persona_content += f"\n{speech_and_answer}"
    # 只放到「日」级（不含时分秒）：让 system_prompt 在同一天内逐字节稳定。
    # 精确到分的当前时间已由 user_message 侧补上。
    current_date = clock_date if clock_date else await get_current_date(format="%Y年%m月%d日")

    # 稳定前缀：人设 + 合规 + 工具编排。正文来自该人格绑定的提示词组。
    role = render_prompt(char_name, "role_playing_start")
    constraints = _render_system_constraints(char_name)
    tools = render_prompt(char_name, "tool_orchestration")
    date_line = render_prompt(char_name, "date_line", date=current_date)
    from gsuid_core.ai_core.meme.vocab import SCENE_WORDS, EMOTION_WORDS, join_words
    from gsuid_core.ai_core.persona.config import persona_allows_meme_reply
    from gsuid_core.ai_core.persona.meme_style import resolve_meme_frequency_line

    meme_protocol = ""
    if include_meme_protocol and persona_allows_meme_reply(char_name):
        meme_protocol = render_prompt(
            char_name,
            "meme_protocol",
            emotion_words=join_words(EMOTION_WORDS),
            scene_words=join_words(SCENE_WORDS),
            meme_frequency=resolve_meme_frequency_line(char_name),
        )
    prompt = join_around_card(
        join_prompt_parts(role),
        persona_content,
        join_prompt_parts(constraints, tools, meme_protocol, date_line),
    )

    # 能力代理花名册：进 system 可缓存，避免每轮塞进 user 侧
    from gsuid_core.ai_core.agent_node.registry import (
        format_capability_roster,
        persona_allows_capability_agent,
    )

    roster = format_capability_roster(char_name)
    if roster:
        heading = render_prompt(char_name, "capability_heading")
        prompt += f"\n\n{heading}\n{roster}"

    from gsuid_core.ai_core.prompt_group.catalog import RENDER_CLAUSE_WHEN_ALLOWED

    render_clause = RENDER_CLAUSE_WHEN_ALLOWED if persona_allows_capability_agent(char_name, "render_agent") else ""
    closing = prefix_closing_rule(
        render_prompt(
            char_name,
            "closing",
            char_name=char_name,
            render_clause=render_clause,
        )
    )
    if closing:
        prompt += f"\n\n{closing}"

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
