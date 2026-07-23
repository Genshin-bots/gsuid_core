"""
角色处理器模块

负责组装完整的角色提示词，将模板、角色资料和系统约束组合成最终的prompt。
支持注入情绪状态和群聊上下文。
"""

from .mood import get_mood_description
from .prompts import ROLE_PLAYING_START, SYSTEM_CONSTRAINTS
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
    # 只放到「日」级（不含时分秒）：让 system_prompt 在同一天内逐字节稳定，跨会话 / resume
    # 都能命中 provider 前缀缓存（§优化 O-2）。精确到分的当前时间已由 user_message 侧
    current_date = await get_current_date(format="%Y年%m月%d日")

    # 稳定前缀只放人设 + 合规 LITE（可跨轮缓存）。重型 TOOL_ORCHESTRATION 改由 assemble_dynamic_context 在非闲聊轮注入
    # user 侧；_relean_user_turn 会剥 rag
    prompt = f"{ROLE_PLAYING_START}\n{persona_content}\n{SYSTEM_CONSTRAINTS}\n当前日期：{current_date}"

    # 近因锚点：system 末尾钉人格（工具规程不在此，避免闲聊稀释）
    prompt += (
        "\n\n---\n\n## 最后提醒（最高权重）\n"
        f"你首先是「{char_name}」这个活生生的人，其次才是会用工具的助手。\n"
        "- 口吻决定**怎么说**；该办的事照办，不拿性格当拒绝理由\n"
        "- 无论是否调过工具，开口永远是角色的声音\n"
        "- 短句、语气词、碎片化——这是本能，不是表演\n"
        "- 结构化数据放 <report> 块，角色台词里禁止表格/编号/加粗标题\n"
        "- 查到 10 分只说 3 分，剩下的让 report 块承载\n"
        "- 日常闲聊回到角色本身，绝不暴露任何'机器/工具/系统/模型'的痕迹"
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
