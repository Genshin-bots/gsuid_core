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


_BUFFER: List[_Pending] = []


class DecisionDistillKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.AFTER_RUN, priority=180, kit_id=self.kit_id)(self.collect)

    async def collect(self, ctx: AgentHookContext) -> None:
        thinking = (ctx.thinking_text or "").strip()
        if not thinking and not ctx.tool_names_called:
            return
        if not ctx.tool_names_called and len(thinking) < 200:
            return
        _BUFFER.append(
            _Pending(
                thinking=thinking[-800:],
                tools=list(ctx.tool_names_called),
                result=(ctx.result_text or "")[:200],
                bot_self_id=ctx.bot_self_id,
                persona_name=ctx.persona_name or "",
            )
        )
        if len(_BUFFER) >= _FLUSH_EVERY:
            await flush_decision_buffer()


async def flush_decision_buffer() -> None:
    """攒批 flush：纯本地规则摘要写 SELF episode。"""
    batch = list(_BUFFER)
    _BUFFER.clear()
    for item in batch:
        decision = _rule_summary(item)
        if not decision:
            continue
        await _persist_decision(item.bot_self_id, item.persona_name, decision)


async def _persist_decision(bot_self_id: str, persona_name: str, decision: str) -> None:
    from gsuid_core.ai_core.memory.scope import self_scope_key
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember

    bid = bot_self_id or "default"
    try:
        await remember(
            MemoryWrite(
                kind=CogKind.EPISODE,
                ref=f"decision_{bid}_{hash(decision) & 0xFFFFFFFF:x}",
                scope_key=self_scope_key(bid, persona_name),
                owner_user_id="",
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
