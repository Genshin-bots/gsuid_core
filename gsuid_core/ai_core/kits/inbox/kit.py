"""``gscore.inbox``：收件箱待回应块（H06）。

当前 ``InboxTurn``（contextvar）存在时，把（待你回应）/（工具身份）/（相关往来）
/ 回执上下文合成 ``inbox_pending`` 命名块；无轮次（工具自跑 / 后台任务）不写块。
"""

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit


class InboxKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=180, kit_id=self.kit_id)(self.inject)

    async def inject(self, ctx: AgentHookContext) -> None:
        from gsuid_core.message_history import get_history_manager
        from gsuid_core.ai_core.inbox.turn import current_turn
        from gsuid_core.ai_core.inbox.blocks import (
            quote_fact_lines,
            render_pending_block,
            render_related_block,
            render_identity_block,
        )
        from gsuid_core.ai_core.inbox.receipt import receipt_context_lines

        turn = current_turn()
        if turn is None or ctx.ev is None:
            return
        records = get_history_manager().get_history(ctx.ev)
        pending = render_pending_block(turn, records)
        facts = await quote_fact_lines(turn, records)
        if facts:
            pending = f"{pending}\n" + "\n".join(facts)
        from gsuid_core.ai_core.inbox.feedback import feedback_fact_line

        pieces = [
            pending,
            render_identity_block(turn, records),
            render_related_block(turn, records),
            receipt_context_lines(turn, records),
            feedback_fact_line(str(ctx.ev.group_id)) if ctx.ev.group_id else "",
        ]
        text = "\n".join(p for p in pieces if p)
        if text:
            ctx.set_context_block("inbox_pending", text)


KIT = register_agent_kit(InboxKit(kit_id="gscore.inbox", slot="inbox", display_name="收件箱待回应块"))
