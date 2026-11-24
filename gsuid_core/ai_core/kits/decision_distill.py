"""``gscore.decision_distill``：零 LLM 决策轨迹归档（H08）。

常规对话不再额外调用模型蒸馏决策结论；只把本轮的思考摘要、工具名与结果
按批写入 SELF 记忆，供后续检索当作可解释轨迹。
"""

from __future__ import annotations

from typing import List
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit

_FLUSH_EVERY = 5


@dataclass
class _Pending:
    thinking: str
    tools: List[str]
    result: str
    bot_self_id: str
    persona_name: str
    scope_key: str
    owner_user_id: str


_BUFFER: List[_Pending] = []


class DecisionDistillKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.AFTER_RUN, priority=180, kit_id=self.kit_id)(self.collect)

    async def collect(self, ctx: AgentHookContext) -> None:
        from gsuid_core.ai_core.memory.scope import scope_key_for_conversation

        thinking = (ctx.thinking_text or "").strip()
        if not thinking and not ctx.tool_names_called:
            return
        if not ctx.tool_names_called and len(thinking) < 200:
            return
        scope_key = scope_key_for_conversation(ctx.group_id, ctx.user_id) if (ctx.group_id or ctx.user_id) else ""
        _BUFFER.append(
            _Pending(
                thinking=thinking[-800:],
                tools=list(ctx.tool_names_called),
                result=(ctx.result_text or "")[:200],
                bot_self_id=ctx.bot_self_id,
                persona_name=ctx.persona_name or "",
                scope_key=scope_key,
                owner_user_id=ctx.user_id or "",
            )
        )
        if len(_BUFFER) >= _FLUSH_EVERY:
            await flush_decision_buffer()


async def flush_decision_buffer() -> None:
    """攒批 flush：纯本地规则摘要按会话 scope 写入记忆，禁止跨群污染。"""
    batch = list(_BUFFER)
    _BUFFER.clear()
    for item in batch:
        decision = _rule_summary(item)
        if not decision:
            continue
        await _persist_decision(
            item.bot_self_id,
            item.persona_name,
            decision,
            scope_key=item.scope_key,
            owner_user_id=item.owner_user_id,
        )


async def _persist_decision(
    bot_self_id: str,
    persona_name: str,
    decision: str,
    *,
    scope_key: str = "",
    owner_user_id: str = "",
) -> None:
    from gsuid_core.ai_core.memory.scope import self_scope_key
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember

    bid = bot_self_id or "default"
    resolved_scope = scope_key or self_scope_key(bid, persona_name)
    try:
        await remember(
            MemoryWrite(
                kind=CogKind.EPISODE,
                ref=f"decision_{bid}_{hash(decision) & 0xFFFFFFFF:x}",
                scope_key=resolved_scope,
                owner_user_id=owner_user_id,
                title="决策结论",
                summary=decision,
                source="decision_distill",
            )
        )
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning(t("log.ai.decision_distill_remember_fail", e=e))


def _rule_summary(item: _Pending) -> str:
    tools = "、".join(item.tools[:6]) if item.tools else "无工具"
    rationale = (item.thinking or "").replace("\n", " ")[:80]
    outcome = item.result or "（无台词）"
    return f"决策：走 {tools}。理由：{rationale or '（过程略）'}。结局：{outcome}"[:200]


KIT = register_agent_kit(
    DecisionDistillKit(
        kit_id="gscore.decision_distill",
        slot="decision_distill",
        display_name="决策轨迹归档",
        owns_tools=(),
    )
)
