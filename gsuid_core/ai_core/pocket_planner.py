"""主人格条件触发的袖珍规划（不执行，只给 plan_hint）。"""

from __future__ import annotations

import re

# 通用结构信号：并列/承接，不绑定业务域。
_MULTI_TASK_RE = re.compile(r"(并且|然后|顺便|同时|另外)")
_CLAUSE_SPLIT_RE = re.compile(r"[，。；、]|并且|然后|顺便|同时|另外")
_EXPLICIT_PLAN_RE = re.compile(r"(帮我安排|计划一下|先.{1,12}再)")


def should_plan_first(query: str, *, recent_eval: bool = False) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    if recent_eval:
        return True
    if _EXPLICIT_PLAN_RE.search(q):
        return True
    if _MULTI_TASK_RE.search(q):
        clauses = [c for c in _CLAUSE_SPLIT_RE.split(q) if len(c.strip()) >= 2]
        return len(clauses) >= 2
    return False


def build_plan_hint(query: str, *, eval_summary: str = "") -> str:
    """结构计划。选路交给花名册/评估缓存，主路径不加 LLM。"""
    _ = query
    steps: list[str] = ["[本轮计划]"]
    if eval_summary.strip():
        steps.append(eval_summary.strip()[:200])
    steps.extend(
        [
            "1) 缺工具则 find_tools（或 capability_map 看目录）",
            "2) 组合任务按花名册委派专职节点，自己只做轻查询与收口",
            "3) 一两句角色口吻汇总；长结构走资料呈现通道，勿当台词念",
        ]
    )
    return "\n".join(steps)


def _format_eval_plan(summary: str, subtasks: list[str]) -> str:
    lines = ["[本轮计划·沿用近 1h 评估]"]
    if summary.strip():
        lines.append(summary.strip()[:120])
    for i, desc in enumerate(subtasks[:4], start=1):
        lines.append(f"{i}) {desc[:80]}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


async def compose_plan_hint(query: str, user_id: str = "") -> str:
    """近期评估复用，否则结构计划。不在装配路径另开 LLM。"""
    if user_id:
        from gsuid_core.ai_core.capability_agents.evaluator import get_recent_evaluation

        recent = get_recent_evaluation(user_id, query)
        if recent is not None:
            subs = [s.description for s in recent.suggested_subtasks if s.description]
            formatted = _format_eval_plan(recent.summary, subs)
            if formatted:
                return formatted
    return build_plan_hint(query)
