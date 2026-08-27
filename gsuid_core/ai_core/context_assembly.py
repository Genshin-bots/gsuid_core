"""Agent 上下文装配的共享层（§5.3 装配统一）。

生产入口与评测入口共用 ``handle_ai.run_interactive_turn``；本模块是那一轮里
system prompt / 每轮动态块的唯一装配点：

- :func:`build_session_system_prompt`：persona + 群简介 + 稳定前缀（self_model/群画像）
  → session 级 system prompt。ai_router 建会话 / TTL 刷新、评测端点共用。
- :func:`assemble_dynamic_context`：每轮 user 侧动态注入的**唯一**顺序定义。块名与顺序
  来自 ``kits.base.CONTEXT_BLOCK_ORDER``（跨计划冻结接口），各来源只填命名块。

关系温度读入口是 ``relationship.fetch_relationship``（返回 ``RelationshipView``），
不再是返回裸 int 的 ``fetch_favorability``——那个返回值让每个消费点自己划档。

漂移防线：tests/test_context_assembly.py 以源码级断言锁定两个入口都消费本模块。
"""

import asyncio
from typing import TYPE_CHECKING, Dict, List, Tuple, Optional

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.kits.base import join_named_blocks
from gsuid_core.ai_core.relationship import RelationshipView

if TYPE_CHECKING:
    from gsuid_core.ai_core.hooks import AgentHookContext

# 软触发（免唤醒续聊）的默认偏沉默提示——生产/评测共用同一文案
SOFT_TRIGGER_NOTE = (
    "（**续聊软触发**：这条来自最近找过你的人，但**没有 @ 你**，默认按'路过'处理。"
    "只有当它明显在接着你们刚才的话题（追问 / 补充 / 直接回应你）时才回应；"
    "若是泛泛感慨、像在跟群里别人说、或换了与你无关的新话题，请直接输出 <SILENCE> 保持沉默。"
    "拿不准时优先沉默，不要为了续上话而硬接。）"
)


async def build_stable_context(event: Event, persona_name: str = "") -> str:
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

        # include_relationship=False：关系是 per-user，群 session 共享稳定前缀
        return await build_self_cognition_context(bot_id=event.bot_id, scope_key=scope_key, include_relationship=False)

    async def _group_profile_block() -> str:
        if not event.group_id:
            return ""
        from gsuid_core.ai_core.memory.group_profile import (
            collect_persona_surfaces,
            format_context_injection,
        )

        surfaces = collect_persona_surfaces(persona_name)
        return await format_context_injection(scope_key, persona_surfaces=surfaces)

    results = await asyncio.gather(_self_model_block(), _group_profile_block(), return_exceptions=True)
    for name, r in zip(("self_model 稳定块", "群画像稳定块"), results):
        if isinstance(r, BaseException):
            logger.debug(t("log.ai.contextassembly_name_injection", name=name, r=r))
        elif r:
            parts.append(r)

    return "\n\n".join(parts)


async def fire_stable_context_hooks(event: Event, persona_name: str = "") -> str:
    """建 session 时开火 H29，收集套件贡献的稳定块（self_model / 群画像）。

    这是**唯一**允许写 system 的点位；dispatcher 会硬拒非建 session 阶段的调用。
    运行中仍然禁止改 system（会打光 provider 前缀缓存）。
    """
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, fire_hooks, should_fire

    if not should_fire(AgentHookPoint.ON_STABLE_CONTEXT):
        return ""
    ctx = AgentHookContext(
        point=AgentHookPoint.ON_STABLE_CONTEXT,
        ev=event,
        session_id=event.session_id,
        persona_name=persona_name or None,
        create_by="Chat",
    )
    await fire_hooks(AgentHookPoint.ON_STABLE_CONTEXT, ctx, stable_context_phase=True)
    from gsuid_core.ai_core.kits.base import STABLE_BLOCK_NAMES

    parts = [ctx.blocks[name] for name in sorted(STABLE_BLOCK_NAMES) if name in ctx.blocks and ctx.blocks[name]]
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
    # 套件（self_cognition / group_profile）走 H29 贡献稳定块；无套件时回落内核实现
    extra_stable_context = await fire_stable_context_hooks(event, persona_name) or await build_stable_context(
        event, persona_name
    )
    return await build_persona_prompt(
        persona_name,
        group_description=group_description or None,
        extra_stable_context=extra_stable_context or None,
    )


def join_context_blocks(blocks: Dict[str, str]) -> str:
    """按 ``CONTEXT_BLOCK_ORDER`` 拼装命名块（顺序的**唯一**执行点）。"""
    return join_named_blocks(blocks)


async def assemble_dynamic_context(
    *,
    query: str,
    user_id: str,
    bot_id: str,
    persona_name: Optional[str],
    mood_key: str,
    group_id: Optional[str] = None,
    rel: Optional[RelationshipView] = None,
    history_context: str = "",
    memory_context_text: str = "",
    memory_guide: str = "",
    soft_triggered: bool = False,
    intent: str = "",
    recent_report_titles: Tuple[str, ...] = (),
    prev_turn_used_tools: bool = False,
    event: Optional[Event] = None,
    bot: Optional[object] = None,
    hook_ctx: Optional["AgentHookContext"] = None,
) -> Tuple[str, bool]:
    """每轮 user 侧动态注入的唯一入口。返回 ``(full_context, has_actionable_task)``。

    自身不再拼产品块：内核只填 ``history``（消息基础设施）与已检索好的 ``memory``
    文本，其余块由各套件在 H06 ``set_context_block`` 贡献，顺序由
    ``CONTEXT_BLOCK_ORDER`` 单源定义（情绪 → 关系 → 口吻 → 身份 → 历史 → 记忆 → 任务
    → 闲聊风格 → 事务优先级 → 资料图标题 → 软触发 → 插件 hint）。

    生产入口与评测入口共用本函数**且共用同一次 fire_hooks**，否则评测端点会变成
    「有装配没套件」的第三套语义。``hook_ctx`` 允许调用方复用本轮已建好的 Context
    （携带 H05 检索结果 / TurnGraph / 优先发言者等已填字段）。
    """
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.kits.compose import compose_dynamic_context

    ctx = hook_ctx if hook_ctx is not None else AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    ctx.point = AgentHookPoint.COMPOSE_CONTEXT
    if ctx.ev is None and event is not None:
        ctx.ev = event
    if isinstance(bot, Bot):
        ctx.bot = bot
    ctx.query = query
    ctx.persona_name = persona_name
    ctx.mood_key = mood_key
    ctx.intent = intent or None
    ctx.relationship = rel
    ctx.soft_triggered = soft_triggered
    ctx.prev_turn_used_tools = prev_turn_used_tools
    ctx.recent_report_titles = recent_report_titles
    ctx.memory_guide = memory_guide

    # 内核填的两块：history 是消息基础设施；memory 文本由 ⑧ 已检索好（或套件 H05 暂存）
    if history_context:
        ctx.blocks["history"] = history_context
    if memory_context_text and "memory" not in ctx.retrieved:
        ctx.retrieved["memory"] = memory_context_text.strip()

    _, has_actionable = await compose_dynamic_context(ctx)
    # 内核补齐无人认领的自有块，然后重拼。**不能**只在结果为空时兜底：
    # 总线关闭 / 槽位 off 时调用方传进来的记忆文本会被静默丢掉。
    _ensure_kernel_blocks(ctx)
    _apply_suffix_block_policy(ctx)
    _inject_master_title_hint(ctx)
    return join_context_blocks(ctx.blocks), has_actionable


def _ensure_kernel_blocks(ctx: "AgentHookContext") -> None:
    """补齐内核自有块（身份锚 / 记忆正文），已有套件贡献则不覆盖。

    身份锚是密封块（关不掉）；记忆正文由调用方或 H05 提供，渲染成块的格式在这里兜底，
    保证「总闸关 / memory 槽 off」时不丢调用方已经拿到的文本。
    """
    if ctx.persona_name and "identity" not in ctx.blocks and ctx.ev is not None and not ctx.ev.group_id:
        ctx.blocks["identity"] = (
            f"（身份：你是「{ctx.persona_name}」。自我指称只按角色卡；"
            "他人绰号不等于你的身份，禁止改物种/性别/名字去迎合。）"
        )
    if ctx.relationship is not None and "relationship" not in ctx.blocks:
        from gsuid_core.ai_core.self_cognition import build_relationship_context

        ctx.blocks["relationship"] = build_relationship_context(ctx.relationship)
    mem = ctx.retrieved["memory"] if "memory" in ctx.retrieved else ""
    if mem and "memory" not in ctx.blocks:
        ctx.blocks["memory"] = f"{ctx.memory_guide}[长期记忆]\n{mem}\n（需要更多细节请调 search_cognition）"


_ADDRESSED_FULL_BLOCKS: frozenset[str] = frozenset(
    {
        "voice_anchor",
        "mood",
        "relationship",
        "task",
        "plan_hint",
        "soft_trigger",
        "memory",
        "history",
        "plugin_hints",
    }
)
# 点名 suffix 产品块合计帽；voice_anchor 在帽外。history 排最后。
_SUFFIX_PRODUCT_CAP = 400
_SUFFIX_EXEMPT_BLOCKS: frozenset[str] = frozenset({"voice_anchor"})
_SUFFIX_KEEP_ORDER: tuple[str, ...] = (
    "task",
    "plan_hint",
    "relationship",
    "mood",
    "memory",
    "soft_trigger",
    "plugin_hints",
    "history",
)


def suffix_allowed_blocks(ctx: "AgentHookContext") -> frozenset[str] | None:
    """群聊 suffix 允许的产品块。None = 不过滤（私聊 / 无 TurnGraph）。"""
    tg = ctx.turn_graph
    if tg is None or not tg.is_group:
        return None
    addressed = bool(tg.call_to_self or tg.ellipsis_followup or tg.task_management)
    if ctx.cheap_gate == "light" or not addressed:
        return frozenset()
    return _ADDRESSED_FULL_BLOCKS


def _cap_group_suffix_blocks(blocks: Dict[str, str], cap: int) -> None:
    kept: Dict[str, str] = {}
    for name in _SUFFIX_EXEMPT_BLOCKS:
        if name not in blocks:
            continue
        text = blocks[name].strip()
        if text:
            kept[name] = text
    used = 0
    for name in _SUFFIX_KEEP_ORDER:
        if name not in blocks:
            continue
        text = blocks[name].strip()
        if not text:
            continue
        room = cap - used
        if room <= 0:
            break
        if len(text) > room:
            text = text[: max(0, room - 1)] + "…"
        kept[name] = text
        used += len(text)
    for name in list(blocks):
        if name in kept:
            blocks[name] = kept[name]
        else:
            del blocks[name]


def _apply_suffix_block_policy(ctx: "AgentHookContext") -> None:
    allowed = suffix_allowed_blocks(ctx)
    if allowed is None:
        return
    for name in list(ctx.blocks):
        if name not in allowed:
            del ctx.blocks[name]
    if allowed:
        _cap_group_suffix_blocks(ctx.blocks, _SUFFIX_PRODUCT_CAP)


def master_title_turn_hint(ctx: "AgentHookContext") -> str:
    """非主人群聊轮：可剥的禁止 TITLE hint。真主人轮不写。"""
    if ctx.ev is None or not ctx.ev.group_id:
        return ""
    rel = ctx.relationship
    if rel is None or rel.is_master:
        return ""
    from gsuid_core.ai_core.persona.settings import get_master_title

    title = get_master_title(ctx.persona_name)
    if not title:
        return ""
    return f"（系统：本轮说话人不是主人，禁止称「{title}」。）"


def _inject_master_title_hint(ctx: "AgentHookContext") -> None:
    hint = master_title_turn_hint(ctx)
    if not hint:
        return
    existing = ctx.blocks["relationship"] if "relationship" in ctx.blocks else ""
    ctx.blocks["relationship"] = f"{existing}\n{hint}".strip() if existing else hint
