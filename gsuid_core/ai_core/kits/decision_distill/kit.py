"""``gscore.decision_distill``：thinking 决策结论后台蒸馏（H08）。"""

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
            )
        )
        if len(_BUFFER) >= _FLUSH_EVERY:
            await flush_decision_buffer()


async def flush_decision_buffer() -> None:
    """攒批 flush：SELF episode + 认知节点。LLM 失败退规则摘要。"""
    batch = list(_BUFFER)
    _BUFFER.clear()
    if not batch:
        return
    distilled = await _llm_summaries(batch)
    for i, item in enumerate(batch):
        decision = distilled[i] if i < len(distilled) and distilled[i] else _rule_summary(item)
        if not decision:
            continue
        await _persist_decision(item.bot_self_id, decision)


async def _persist_decision(bot_self_id: str, decision: str) -> None:
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.memory.observer import observe
    from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember

    bid = bot_self_id or "default"
    try:
        await remember(
            MemoryWrite(
                kind=CogKind.EPISODE,
                ref=f"decision_{bid}_{hash(decision) & 0xFFFFFFFF:x}",
                scope_key=make_scope_key(ScopeType.SELF, bid),
                owner_user_id="",
                title="决策结论",
                summary=decision,
                source="decision_distill",
            )
        )
    except Exception as e:
        logger.warning(t("log.ai.decision_distill_remember_fail", e=e))
    try:
        await observe(
            content=f"[决策结论] {decision}",
            speaker_id=f"__assistant_{bid}__",
            group_id=None,
            bot_self_id=bid,
            observer_blacklist=[],
            message_type="private_msg",
        )
    except Exception as e:
        logger.debug(t("log.ai.decision_distill_observe_fail", e=e))


def _rule_summary(item: _Pending) -> str:
    tools = "、".join(item.tools[:6]) if item.tools else "无工具"
    rationale = (item.thinking or "").replace("\n", " ")[:80]
    outcome = item.result or "（无台词）"
    return f"决策：走 {tools}。理由：{rationale or '（过程略）'}。结局：{outcome}"[:200]


async def _llm_summaries(batch: List[_Pending]) -> List[str]:
    """轻量模型一次蒸整批；解析失败则空列表，调用方退规则摘要。"""
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    from gsuid_core.ai_core.gs_agent import create_agent

    blob_parts: List[str] = []
    for i, item in enumerate(batch, start=1):
        tools = ",".join(item.tools[:6])
        blob_parts.append(f"#{i}\nthinking:{item.thinking[-400:]}\ntools:{tools}\nresult:{item.result}")
    prompt = (
        "从下列 run 各抽出 0-1 条决策。只输出 JSON 数组，"
        '元素为 {"decision":"...","rationale":"...","outcome":"..."}。'
        "闲聊且无工具则输出 []。\n\n" + "\n\n".join(blob_parts)
    )
    try:
        agent = create_agent(
            system_prompt="你是记忆管家，只蒸馏决策结论。禁止角色扮演。",
            max_tokens=600,
            max_iterations=1,
            create_by="DecisionDistill",
            task_level="low",
            is_subagent=True,
            dynamic_tools=False,
        )
        try:
            raw = await agent.run(user_message=prompt, return_mode="return")
        finally:
            if agent._session_logger is not None:
                agent._session_logger.close()
        return _parse_distill_json(str(raw or ""), len(batch))
    except Exception as e:
        logger.debug(t("log.ai.decision_distill_llm_fail", e=e))
        return []


def _parse_distill_json(raw: str, n: int) -> List[str]:
    import json
    from json.decoder import JSONDecodeError

    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: List[str] = []
    for item in data[:n]:
        if not isinstance(item, dict):
            out.append("")
            continue
        decision = item["decision"] if "decision" in item and isinstance(item["decision"], str) else ""
        rationale = item["rationale"] if "rationale" in item and isinstance(item["rationale"], str) else ""
        outcome = item["outcome"] if "outcome" in item and isinstance(item["outcome"], str) else ""
        if not decision and not outcome:
            out.append("")
            continue
        out.append(f"决策：{decision}。理由：{rationale}。结局：{outcome}"[:200])
    while len(out) < n:
        out.append("")
    return out


KIT = register_agent_kit(
    DecisionDistillKit(
        kit_id="gscore.decision_distill",
        slot="decision_distill",
        display_name="决策蒸馏",
        owns_tools=(),
    )
)
