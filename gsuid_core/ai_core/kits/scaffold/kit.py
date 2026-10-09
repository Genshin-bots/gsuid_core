"""``gscore.scaffold``：交互脚手架的**注入侧**（C-1/C-2 + 风格短句）。

留在内核的三件（迁移时最容易一起搬走，务必分清）：
1. ``TurnGraph`` 构建——门与装配共用的权威结构源；
2. C-3 寻址门的**零工具硬约束**（``addr_gated`` 时不打 ASSEMBLE_TOOLS）；
3. CheapGate 的判定调用点——它决定「进不进主 loop」，是编排而非产品。

本套件只负责把结构结论**渲染成 user 侧提示**。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class ScaffoldKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=170, kit_id=self.kit_id)(self.inject_style)

    async def inject_style(self, ctx: AgentHookContext) -> None:
        """闲聊风格 / 事务优先级 / 上一轮资料图标题。

        intent 不可靠：上轮用过工具或有活跃任务时**不压短**，否则会把正在办事的轮次
        压成寒暄短句。
        """
        tg = ctx.turn_graph
        is_group = tg is not None and tg.is_group
        # 私聊建议不是寒暄。群聊闲聊才压短，长度跟发言形态档。
        if is_group and ctx.intent == "闲聊" and not ctx.prev_turn_used_tools and not ctx.has_actionable:
            last_had_tick = False
            history = ctx.gate_history
            if not history and ctx.ev is not None:
                from gsuid_core.message_history import get_history_manager

                history = get_history_manager().get_history(ctx.ev, limit=8)
            from gsuid_core.ai_core.persona.resource import get_tone_markers, reply_ends_with_tone_marker
            from gsuid_core.ai_core.persona.chat_style import resolve_chat_style

            markers = get_tone_markers(ctx.persona_name)
            for rec in reversed(history):
                if rec.role == "assistant":
                    last_had_tick = reply_ends_with_tone_marker(rec.content or "", markers)
                    break
            quota = "（口癖配额：每3–5条至多1条带语气词结尾；其余条不带。）"
            if last_had_tick:
                quota += "上一条已带口癖，本条禁带。"
            style = resolve_chat_style(ctx.persona_name)
            ctx.set_context_block(
                "chitchat_style",
                f"（若纯寒暄：建议不超过 {style.soft} 字/条，至多 {style.bubbles} 条；若需查数/办事仍调工具。）{quota}",
            )
        # 私聊 FULL 一律催工具。群聊问答/工具，或办事/查数；不认单独的「看看这」。
        from gsuid_core.ai_core.interaction_scaffold import looks_like_committed_work

        q = ctx.query or ""
        need_tools = (not is_group) or ctx.intent in ("工具", "问答") or looks_like_committed_work(q)
        if need_tools and not ctx.memory_eval:
            ctx.set_context_block(
                "transaction_priority",
                "（本轮有实际事务，优先调工具完成；困/懒/麻烦不是跳过理由。）",
            )
        if ctx.recent_report_titles:
            titles = "、".join(ctx.recent_report_titles[-3:])
            ctx.set_context_block("report_titles", f"（上一轮资料图：{titles}）")


KIT = register_agent_kit(ScaffoldKit(kit_id="gscore.scaffold", slot="scaffold", display_name="交互脚手架"))
