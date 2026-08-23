"""主人格条件触发的袖珍规划（不执行，只给 plan_hint）。"""

from __future__ import annotations

import re

# 通用结构信号：并列/承接/时间跨度/清单形态，不绑定业务域。
_MULTI_TASK_RE = re.compile(r"(并且|然后|顺便|同时|另外)")
_CLAUSE_SPLIT_RE = re.compile(r"[，。；、]|并且|然后|顺便|同时|另外")
_EXPLICIT_PLAN_RE = re.compile(r"(帮我安排|计划一下|先.{1,12}再)")
_SPAN_RE = re.compile(r"(近|最近|未来|接下来|过去).{0,8}(天|日|周|月|年)")
_LISTISH_RE = re.compile(r"(汇总|对照|对比|清单|一览)")


def should_plan_first(query: str, *, recent_eval: bool = False) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    if recent_eval:
        return True
    if _EXPLICIT_PLAN_RE.search(q):
        return True
    if _SPAN_RE.search(q) or _LISTISH_RE.search(q):
        return True
    if _MULTI_TASK_RE.search(q):
        clauses = [c for c in _CLAUSE_SPLIT_RE.split(q) if len(c.strip()) >= 2]
        return len(clauses) >= 2
    return False


def _wants_render(query: str) -> bool:
    q = (query or "").strip()
    return bool(_SPAN_RE.search(q) or _LISTISH_RE.search(q))


def _plan_line(head: str, query: str, *, via: str) -> str:
    render_bit = " → 长结构则 render 出图" if _wants_render(query) else ""
    return f"计划：{head}{via}{render_bit} → 短句交付"


def build_plan_hint(query: str, *, eval_summary: str = "") -> str:
    """结构计划一行。选路交给花名册，不按业务词分支。"""
    from gsuid_core.ai_core.agent_node.registry import match_capability_node

    node = match_capability_node(query)
    if eval_summary.strip():
        head = eval_summary.strip().split("\n", 1)[0][:40]
        return _plan_line(head, query, via="")
    if node:
        return _plan_line(f'create_subagent(agent_profile="{node}")', query, via="")
    if should_plan_first(query):
        return _plan_line("capability_map 或 find_tools", query, via="")
    return _plan_line("find_tools 召回", query, via="")


def _format_eval_plan(summary: str, subtasks: list[str], query: str = "") -> str:
    head = summary.strip().split("\n", 1)[0][:40] if summary.strip() else ""
    if not head and subtasks:
        head = subtasks[0][:40]
    if not head:
        return ""
    return _plan_line(head, query or summary, via="")


async def compose_plan_hint(query: str, user_id: str = "") -> str:
    """近期评估复用，否则结构计划。不在装配路径另开 LLM。"""
    if user_id:
        from gsuid_core.ai_core.capability_agents.evaluator import get_recent_evaluation

        recent = get_recent_evaluation(user_id, query)
        if recent is not None:
            subs = [s.description for s in recent.suggested_subtasks if s.description]
            formatted = _format_eval_plan(recent.summary, subs, query)
            if formatted:
                return formatted
    return build_plan_hint(query)
