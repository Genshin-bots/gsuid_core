"""前置单轮路由器：一次无思考轻量调用判定意图、记忆模态与软触发开口。

替代历史正则分类器：输出结构化 ``TurnRouteDecision``，下游 ReactiveGate /
MemoryKit / ScaffoldKit 只读结论，不再各自扫文本猜语义。
"""

from __future__ import annotations

import re
import json
import asyncio
from typing import Literal, Protocol, Sequence
from datetime import date, datetime
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.control.lines import MSG_SECTION_MARK

TurnIntent = Literal["闲聊", "工具", "问答"]
MemoryIntent = Literal[
    "none",
    "point_latest",
    "version_diff",
    "timeline_order",
    "broad_summary",
]

_ROUTER_TIMEOUT = 4.0
_ROUTER_MAX_TOKENS = 96

_CLOCK_RE = re.compile(r"(?:【当前时间】[^\n]*|\[当前时间[：:][^\n]*\])")


class _UserTurnRecord(Protocol):
    role: Literal["user", "assistant", "system"]
    user_id: str
    content: str


@dataclass(frozen=True, slots=True)
class TurnRouteDecision:
    intent: TurnIntent
    memory_intent: MemoryIntent
    soft_speak: bool
    is_live_lookup: bool
    needs_committed_work: bool
    time_start: datetime | None = None
    time_end: datetime | None = None
    source: Literal["llm", "structural_fallback"] = "llm"


def strip_router_header(text: str) -> str:
    """去掉生产 payload 的「--- 消息 ---」头与墙上时钟行，只留正文。"""
    body = text or ""
    if MSG_SECTION_MARK in body:
        body = body.split(MSG_SECTION_MARK, 1)[-1]
    body = _CLOCK_RE.sub("", body)
    first_line = body.splitlines()[0] if body.splitlines() else ""
    if first_line.startswith("---") and first_line.endswith("---"):
        body = "\n".join(body.splitlines()[1:])
    return body.strip()


def collect_prior_user_turns(
    records: Sequence[_UserTurnRecord],
    current_user_id: str,
    *,
    max_turns: int = 3,
) -> list[str]:
    """从历史记录提取同一用户近期原文（旧→新）。"""
    out: list[str] = []
    for rec in records:
        if rec.role != "user":
            continue
        uid = str(rec.user_id or "")
        if uid and current_user_id and uid != str(current_user_id):
            continue
        body = strip_router_header(rec.content if isinstance(rec.content, str) else "")
        if body:
            out.append(body)
    return out[-max_turns:]


def structural_route(
    query: str,
    *,
    prev_turn_used_tools: bool = False,
    soft_triggered: bool = False,
) -> TurnRouteDecision:
    """离线/超时确定性退化：只判空串、句末问号与省略跟进长度。"""
    body = strip_router_header(query)
    if not body:
        intent: TurnIntent = "闲聊"
    elif body.endswith(("?", "？")):
        intent = "问答"
    elif prev_turn_used_tools and len(body) <= 24:
        intent = "工具"
    else:
        intent = "闲聊"
    memory_intent: MemoryIntent = "none" if intent == "闲聊" else "point_latest"
    return TurnRouteDecision(
        intent=intent,
        memory_intent=memory_intent,
        soft_speak=False,
        is_live_lookup=False,
        needs_committed_work=False,
        source="structural_fallback",
    )


_ROUTER_SYSTEM = """你是群聊前置路由器。只输出一行 JSON，不要解释、不要 markdown。

字段与取值：
- i: "chat" | "tool" | "qa"
- m: "none" | "latest" | "diff" | "order" | "sum"
- s: bool。免唤醒软触发时，当前消息是否确实在对 Bot 延续对话或值得自然接话。默认 false（偏沉默）。
- l: bool。是否在查实时外部数据（实时天气/股价/面板等），为 true 时跳过个人历史记忆预灌。
- w: bool。是否明确委托 Bot 执行具体事务、查数或设提醒。
- ts / te: 若用户显式问某个时间区间，结合基准时钟输出 "YYYY-MM-DD"；否则 null。

判据：
- chat=寒暄、表情、感叹、纯接梗，没有待办也没有需要查证的事实。
- tool=要求执行动作、查询当前数据、设置提醒或调用能力。
- qa=需要知识、解释、方法，或对已有信息的回答。
- none=纯寒暄或本轮上下文自足；latest=常规事实/属性点查；diff=变更历史/前后对比/矛盾核实；
  order=事件先后/时间线；sum=跨会话总结/数量统计/集合盘点。
- 若消息含必须依赖历史的泛指指代（如“那个/上次/之前/后来呢”），m 不得为 none，保底 latest。

只输出：{"i":"chat","m":"none","s":false,"l":false,"w":false,"ts":null,"te":null}"""


def _build_router_prompt(
    body: str,
    *,
    prior_user_turns: Sequence[str],
    prev_turn_used_tools: bool,
    soft_triggered: bool,
    clock_at: datetime | None,
    recent_group_messages: Sequence[str],
) -> str:
    clock = (clock_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = [f"[当前时间] {clock}", f"[上一轮用过工具] {'yes' if prev_turn_used_tools else 'no'}"]
    if soft_triggered:
        lines.append("[软触发] yes")
        msgs = [m.strip() for m in recent_group_messages if m and m.strip()][-6:]
        if msgs:
            lines.append("[近期群消息]")
            lines.extend(f"- {m}" for m in msgs)
    priors = [p.strip() for p in prior_user_turns if p and p.strip()][-3:]
    if priors:
        lines.append("[同一用户近期发言]")
        lines.extend(f"- {p}" for p in priors)
    lines.append("[本轮消息]")
    lines.append(body)
    return "\n".join(lines)


def _parse_date(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
    except ValueError:
        return None


def _parse_decision(raw: str) -> TurnRouteDecision | None:
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    raw_i = data["i"] if "i" in data and isinstance(data["i"], str) else ""
    intent_map: dict[str, TurnIntent] = {"chat": "闲聊", "tool": "工具", "qa": "问答"}
    if raw_i not in intent_map:
        return None
    raw_m = data["m"] if "m" in data and isinstance(data["m"], str) else ""
    memory_map: dict[str, MemoryIntent] = {
        "none": "none",
        "latest": "point_latest",
        "diff": "version_diff",
        "order": "timeline_order",
        "sum": "broad_summary",
    }
    memory_intent: MemoryIntent = memory_map[raw_m] if raw_m in memory_map else "point_latest"
    if intent_map[raw_i] == "闲聊" and raw_m not in memory_map:
        memory_intent = "none"
    return TurnRouteDecision(
        intent=intent_map[raw_i],
        memory_intent=memory_intent,
        soft_speak=bool(data["s"]) if "s" in data else False,
        is_live_lookup=bool(data["l"]) if "l" in data else False,
        needs_committed_work=bool(data["w"]) if "w" in data else False,
        time_start=_parse_date(data["ts"] if "ts" in data else None),
        time_end=_parse_date(data["te"] if "te" in data else None),
        source="llm",
    )


def _can_call_llm() -> bool:
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        from gsuid_core.ai_core.configs.models import get_config_name_for_task

        return bool(get_config_name_for_task("low"))
    except (ImportError, ValueError, AttributeError):
        return False


async def route_turn(
    query: str,
    *,
    prior_user_turns: Sequence[str] = (),
    prev_turn_used_tools: bool = False,
    soft_triggered: bool = False,
    clock_at: datetime | None = None,
    recent_group_messages: Sequence[str] = (),
) -> TurnRouteDecision:
    """一次无思考轻量路由；不可用/超时/输出非法时退化到结构规则。"""
    body = strip_router_header(query)
    fallback = structural_route(
        body,
        prev_turn_used_tools=prev_turn_used_tools,
        soft_triggered=soft_triggered,
    )
    if not body or not _can_call_llm():
        return fallback

    prompt = _build_router_prompt(
        body,
        prior_user_turns=prior_user_turns,
        prev_turn_used_tools=prev_turn_used_tools,
        soft_triggered=soft_triggered,
        clock_at=clock_at,
        recent_group_messages=recent_group_messages,
    )

    try:
        from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
        from gsuid_core.ai_core.configs.models import get_fast_model_for_task

        agent = GsCoreAIAgent(
            openai_chat_model=get_fast_model_for_task("low"),
            system_prompt=_ROUTER_SYSTEM,
            max_tokens=_ROUTER_MAX_TOKENS,
            create_by="TurnRouter",
            task_level="low",
            is_subagent=True,
            dynamic_tools=False,
            thinking=False,
        )
        try:
            raw = await asyncio.wait_for(agent.run(user_message=prompt, return_mode="return"), timeout=_ROUTER_TIMEOUT)
        finally:
            agent._session_logger.close()
        decision = _parse_decision(raw if isinstance(raw, str) else str(raw))
        if decision is None:
            logger.debug(t("log.ai.turn_router_invalid_output"))
            return fallback
        return decision
    except (asyncio.TimeoutError, OSError, RuntimeError, ValueError) as e:
        logger.debug(t("log.ai.turn_router_fallback", e=e))
        return fallback
