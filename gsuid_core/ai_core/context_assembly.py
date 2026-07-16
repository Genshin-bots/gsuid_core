"""Agent 上下文装配的共享层（§5.3 装配统一）。

生产入口（handle_ai.handle_ai_chat）与本地评测入口（webconsole.chat_with_history_api）
此前各自手工复刻"system prompt + 每轮动态注入"的装配片段，O-3 落地后立即漂移过一次
（评测端点的 system prompt 缺稳定前缀/关系行）。本模块是两个入口共同消费的唯一装配点：

- :func:`build_session_system_prompt`：persona + 群简介 + 稳定前缀（self_model/群画像）
  → session 级 system prompt。ai_router 建会话 / TTL 刷新、评测端点共用。
- :func:`assemble_dynamic_context`：每轮 user 侧动态注入（历史/情绪/关系行/口吻锚点/
  自我情景/长任务/长期记忆/软触发提示）的**唯一**顺序定义。
- :func:`fetch_favorability`：好感度查询的共享降级封装。

漂移防线：tests/test_context_assembly.py 以源码级断言锁定两个入口都消费本模块。
"""

import re
import asyncio
from typing import List, Tuple, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event

# C3-c 自我情景记忆召回触发词：用户回指 Bot 自己曾经的言行（从 handle_ai 移入）
_SELF_RECALL_RE = re.compile(r"(你之前|你上次|你不是说|你说过|你还记得|你刚才说|你答应)")

# 软触发（免唤醒续聊）的默认偏沉默提示——生产/评测共用同一文案
SOFT_TRIGGER_NOTE = (
    "（**续聊软触发**：这条来自最近找过你的人，但**没有 @ 你**，默认按'路过'处理。"
    "只有当它明显在接着你们刚才的话题（追问 / 补充 / 直接回应你）时才回应；"
    "若是泛泛感慨、像在跟群里别人说、或换了与你无关的新话题，请直接输出 <SILENCE> 保持沉默。"
    "拿不准时优先沉默，不要为了续上话而硬接。）"
)


async def fetch_favorability(user_id: str, bot_id: str) -> Optional[int]:
    """好感度查询（外部存储，非模型推断）；失败降级为 None（无注入）。"""
    try:
        from gsuid_core.ai_core.database.models import UserFavorability

        user_data = await UserFavorability.get_user_favorability(user_id=user_id, bot_id=bot_id)
        if user_data:
            return user_data.favorability
    except Exception as e:
        logger.debug(t("🧠 [ContextAssembly] 好感度查询失败，降级为无注入: {e}", e=e))
    return None


async def build_stable_context(event: Event) -> str:
    """建 session / TTL 刷新时组装固化进 system_prompt 的慢变上下文（§优化 O-3）。

    = self_model 自述块（bot/scope 级，**不含** per-user 关系行）+ 群画像/词汇映射。
    这些会话期内基本不变，进稳定前缀可跨轮命中缓存；关系/情绪/记忆/历史仍每轮进 user 侧。
    任一子项失败不影响建 session（返回已拼到的部分）。
    """
    from gsuid_core.ai_core.memory.scope import scope_key_for_conversation

    scope_key = scope_key_for_conversation(event.group_id, str(event.user_id))
    parts: List[str] = []

    async def _self_model_block() -> str:
        from gsuid_core.ai_core.self_cognition import build_self_cognition_context

        return await build_self_cognition_context(bot_id=event.bot_id, scope_key=scope_key, include_relationship=False)

    async def _group_profile_block() -> str:
        if not event.group_id:
            return ""
        from gsuid_core.ai_core.memory.group_profile import format_context_injection

        return await format_context_injection(scope_key)

    results = await asyncio.gather(_self_model_block(), _group_profile_block(), return_exceptions=True)
    for name, r in zip(("self_model 稳定块", "群画像稳定块"), results):
        if isinstance(r, BaseException):
            logger.debug(t("🪞 [ContextAssembly] {name}注入失败: {r}", name=name, r=r))
        elif r:
            parts.append(r)

    return "\n\n".join(parts)


async def build_session_system_prompt(event: Event, persona_name: str) -> str:
    """session 级 system prompt 的唯一装配点：persona + 群简介 + 稳定前缀。

    ai_router 的建会话与 TTL 刷新、评测端点共用；两处此前各写一份已漂移过（F9/§5.3）。
    **不传 mood_key**：mood 每轮在 user 侧注入（:func:`assemble_dynamic_context`），
    再进 system prompt 就是同一信息双写、且最多滞后一个 TTL 与每轮值互相矛盾；更关键的是
    mood 常变会让 TTL 刷新必然产出不同的 system prompt 白白打掉 provider 前缀缓存——
    不含 mood 时画像/自述未变的刷新产出逐字节相同的串，缓存自然保持。
    """
    from gsuid_core.ai_core.persona import build_persona_prompt
    from gsuid_core.ai_core.persona.group_context import get_group_context

    group_description = ""
    if event.group_id:
        group_description = await get_group_context(group_id=event.group_id)
    extra_stable_context = await build_stable_context(event)
    return await build_persona_prompt(
        persona_name,
        group_description=group_description or None,
        extra_stable_context=extra_stable_context or None,
    )


async def assemble_dynamic_context(
    *,
    query: str,
    user_id: str,
    bot_id: str,
    persona_name: Optional[str],
    mood_key: str,
    group_id: Optional[str] = None,
    favorability: Optional[int] = None,
    history_context: str = "",
    memory_context_text: str = "",
    memory_guide: str = "",
    soft_triggered: bool = False,
) -> Tuple[str, bool]:
    """每轮 user 侧动态注入的唯一顺序定义。返回 ``(full_context, has_actionable_task)``。

    顺序（与缓存前缀稳定性从高到低排列，改动须两个入口同时生效——这正是抽出本函数的目的）：
    历史对话 → 情绪 → 关系行 → 口吻锚点 → 自我情景 → 长任务 → 长期记忆 → 软触发提示。
    ``history_context`` 传已带「【历史对话】」标头的成品文本（评测端点历史走
    agent.history，则传空）；``memory_guide`` 是记忆使用准则（评测端点专用，生产为空）。
    各子项失败一律降级跳过，不影响其余注入。
    """
    context_parts: List[str] = []
    if history_context:
        context_parts.append(history_context)

    # Prompt-2.5: 用括号包裹情绪状态，暗示这是内心状态而非对话指令
    if persona_name:
        try:
            from gsuid_core.ai_core.persona.mood import get_mood_description

            mood_desc = await get_mood_description(persona_name, mood_key)
            if mood_desc:
                context_parts.append(f"（{mood_desc}。）")
        except Exception as e:
            logger.debug(t("🎭 [Mood] 情绪描述获取失败: {e}", e=e))

    # C3-a/c: per-user 关系行（群聊共享 session，关系随当前对话者变化，不能冻进共享前缀）
    self_episode_text = ""
    try:
        from gsuid_core.ai_core.self_cognition import retrieve_self_episodes, build_relationship_context

        relationship_text = build_relationship_context(user_id, favorability)
        if relationship_text:
            context_parts.append(relationship_text)
        # C3-c: 用户回指 Bot 自己曾经的言行时，召回自我情景记忆
        if _SELF_RECALL_RE.search(query):
            self_episode_text = await retrieve_self_episodes(bot_id)
    except Exception as e:
        logger.debug(t("🪞 [SelfCognition] 关系上下文注入失败: {e}", e=e))

    # 逐轮人格口吻锚点（治理长会话的人格漂移）：人格只在会话创建时固化进
    # system_prompt，越聊越靠后、注意力越稀释。此处每轮补一行紧凑口吻自述。
    if persona_name:
        try:
            from gsuid_core.ai_core.persona import get_voice_anchor

            voice_anchor = get_voice_anchor(persona_name)
            if voice_anchor:
                # 口吻/行为分离：锚点只约束语气——实测"慵懒"会从语气渗漏成行为
                # （以困/懒为由拒设提醒、对直接请求敷衍），框架层显式钉住边界。
                context_parts.append(
                    f"（口吻锚点：{voice_anchor}——口吻只决定**怎么说**，不决定**做不做**："
                    "该回应的回应、该办的事照办，不拿角色性格当拒绝或敷衍的理由）"
                )
        except Exception as e:
            logger.debug(t("🧠 [ContextAssembly] 人格口吻锚点注入失败: {e}", e=e))

    if self_episode_text:
        context_parts.append(self_episode_text)

    # C5: 长任务进度动态注入（短序号、无 UUID），让用户可追问"那个任务怎么样了"
    has_actionable = False
    try:
        from gsuid_core.ai_core.planning.context import build_task_context, has_actionable_task

        # §24 跨群脱敏：显式 group_id 判群（不借 mood_key 哨兵反推，评审修复 E14）；
        # has_actionable 同口径过滤，防他群任务在本群挂载 kanban 工具族（评审修复 E15）
        task_context_text = await build_task_context(user_id, current_group_id=group_id)
        has_actionable = await has_actionable_task(user_id, current_group_id=group_id)
        if task_context_text:
            context_parts.append(task_context_text)
    except Exception as e:
        logger.debug(t("📋 [Planning] 长任务上下文注入失败: {e}", e=e))

    if memory_context_text:
        guide = f"{memory_guide}" if memory_guide else ""
        context_parts.append(f"{guide}【长期记忆】\n{memory_context_text}")

    # 软触发（免唤醒续聊）默认偏沉默：这条没 @ 你，按"路过"处理，仅明确接续才回应。
    # 与硬触发（@/关键词/私聊）相反——硬触发是"明确在找你，必须回应"。
    if soft_triggered:
        context_parts.append(SOFT_TRIGGER_NOTE)

    return "\n\n".join(context_parts), has_actionable
