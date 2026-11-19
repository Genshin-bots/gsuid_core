"""前置单轮路由器：一次无思考轻量调用判定意图、记忆模态与软触发开口。

替代历史正则分类器：输出结构化 ``TurnRouteDecision``，下游 ``handle_ai`` /
MemoryKit / ScaffoldKit 只读结论，不再各自扫文本猜语义。
"""

from __future__ import annotations

import re
import json
import asyncio
from typing import Literal, Sequence
from datetime import datetime
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.control.lines import CLOCK_LINE_RE, MSG_SECTION_MARK
from gsuid_core.ai_core.configs.task_kinds import TASK_TURN_ROUTER

TurnIntent = Literal["闲聊", "工具", "问答"]
MemoryIntent = Literal[
    "none",
    "point_latest",
    "version_diff",
    "timeline_order",
    "broad_summary",
]
AddressingKind = Literal[
    "direct_ask",
    "follow_self",
    "to_other",
    "comment_receipt",
    "repeat",
    "noise",
]
_SPEAK_ADDR_KINDS: frozenset[AddressingKind] = frozenset({"direct_ask", "follow_self"})

ROUTER_TIMEOUT_S = 8.0
_ROUTER_MAX_TOKENS = 512

# 纯语法/通用交际结构（不含垂直业务词表，遵守 §1.9）：供离线/直通退化识别提问与委托。
_IMPERATIVE_RE = re.compile(
    r"(?:帮我|帮忙|麻烦你?|请你?|查一下|查下|查询|搜一下|搜下|搜索|看看|看下|列一下|列出|算一下|"
    r"总结|整理|翻译|提醒我|告诉我|对比一下|画一?[张个幅]|发一?[个条份]|写一?[个篇份段]|"
    r"做一?[个张份]|删[掉除]?|改一?下|修改|设置|设一?个|生成|下载|导出|保存|记录|分析|统计|检查|排查)"
)
_INTERROGATIVE_RE = re.compile(
    r"(?:如何|怎么|怎样|为什么|为啥|多少|几个|几点|几天|多久|哪[里个些种位儿]|谁[是能会说]|什么|啥[时候意思叫]?|是否|能否|可不可以|有没有|(?:吗|呢)\s*$)"
)
_DIFF_MODALITY_RE = re.compile(r"(?:对比|比较|变化|区别|不同之处|改了什么|前后)")
_ORDER_MODALITY_RE = re.compile(r"(?:时间线|先后顺序|按顺序|从头到尾|先后)")
_SUM_MODALITY_RE = re.compile(r"(?:总结|汇总|梳理一下|一共|总共|全部列出|盘点)")


@dataclass(frozen=True, slots=True)
class TurnRouteDecision:
    intent: TurnIntent
    memory_intent: MemoryIntent
    soft_speak: bool
    is_live_lookup: bool
    needs_committed_work: bool
    source: Literal["llm", "structural_fallback"] = "llm"
    addressing: AddressingKind | None = None


def strip_router_header(text: str) -> str:
    """去掉生产 payload 的「--- 消息 ---」头与墙上时钟行，只留正文。"""
    body = text or ""
    if MSG_SECTION_MARK in body:
        body = body.split(MSG_SECTION_MARK, 1)[-1]
    body = CLOCK_LINE_RE.sub("", body)
    first_line = body.splitlines()[0] if body.splitlines() else ""
    if first_line.startswith("---") and first_line.endswith("---"):
        body = "\n".join(body.splitlines()[1:])
    return body.strip()


def is_sentence_start_name(text: str, persona_names: Sequence[str]) -> bool:
    """人格称呼是否出现在句首（去前导空白与标点）；用于区分直接呼叫与句中第三人称提及。"""
    cleaned = text.strip().lstrip("@＠,，、:：!！?？~～.。· ")
    if not cleaned:
        return False
    low = cleaned.lower()
    for name in persona_names:
        n = name.strip().lower()
        if n and low.startswith(n):
            return True
    return False


def structural_route(
    query: str,
    *,
    prev_turn_used_tools: bool = False,
    soft_triggered: bool = False,
    wake_kind: str = "",
    persona_names: Sequence[str] = (),
) -> TurnRouteDecision:
    """离线/直通确定性路由：按通用疑问/祈使语法与唤醒结构判意图与开口。"""
    body = strip_router_header(query)
    if not body:
        intent: TurnIntent = "闲聊"
    elif _IMPERATIVE_RE.search(body):
        intent = "工具"
    elif prev_turn_used_tools and len(body) <= 24:
        intent = "工具"
    elif body.endswith(("?", "？")) or _INTERROGATIVE_RE.search(body):
        intent = "问答"
    else:
        intent = "闲聊"

    memory_intent: MemoryIntent = "none" if intent == "闲聊" else "point_latest"
    if body:
        if _DIFF_MODALITY_RE.search(body):
            memory_intent = "version_diff"
        elif _ORDER_MODALITY_RE.search(body):
            memory_intent = "timeline_order"
        elif _SUM_MODALITY_RE.search(body):
            memory_intent = "broad_summary"
        elif wake_kind in ("mention", "private") and memory_intent == "none" and len(body) >= 3:
            # 显式 @Bot 或私聊直通时不因缺问号饿死记忆检索（保留 1–2 字纯寒暄为 none）
            memory_intent = "point_latest"

    if wake_kind in ("mention", "private"):
        soft_speak = True
        addr: AddressingKind = "direct_ask"
    elif wake_kind == "name":
        soft_speak = True if not persona_names else is_sentence_start_name(body, persona_names)
        addr = "direct_ask" if soft_speak else "to_other"
    elif soft_triggered or wake_kind in ("quote_self", "followup", "receipt"):
        soft_speak = False
        addr = "noise"
    else:
        soft_speak = False
        addr = "noise"

    return TurnRouteDecision(
        intent=intent,
        memory_intent=memory_intent,
        soft_speak=soft_speak,
        is_live_lookup=False,
        needs_committed_work=intent == "工具",
        source="structural_fallback",
        addressing=addr,
    )


_ROUTER_SYSTEM = """你是群聊前置路由器。只输出一行 JSON，不要解释、不要 markdown。

字段与取值（a 必须排第一位，先定话头归属再填其余字段）：
- a: 互斥归属枚举（6 选 1）
  * "direct_ask": 明确在对你说话（提问、委托、吐槽、表情互动）
  * "follow_self": 紧接你刚回复给本人的话继续追问、反驳、评价或撒娇互动（尤其安静群/熟人）
  * "to_other": 句首在叫别人、群友互聊中第三人称提起你/项目名、或其他机器人的播报
  * "comment_receipt": 对着命令回执或别人触发的卡片/图发表围观感叹/吐槽，未向你提问
  * "repeat": 复读（+1）或换词起哄你刚答复过的同一件事（若前次带〔依据: 替某人查…〕且换了另一人查自己的，选 direct_ask）
  * "noise": 无关插话，或无需接话的普通成功命令回执（仅报错/异常回执值得开口时选 direct_ask）
- i: "chat" | "tool" | "qa"
- m: "none" | "latest" | "diff" | "order" | "sum"
- l: bool。是否在查纯粹的实时外部数据且不依赖过往私人记录；为 true 且 m=none 时跳过个人记忆预灌。
- w: bool。是否明确委托 Bot 执行具体事务、查数或设提醒（已完成命令回执一律为 false）。

判据：
- chat=寒暄、表情、感叹、纯接梗，无待办也无查证事实。
- tool=要求执行动作、查当前数据、设提醒或调用能力。
- qa=需要知识、解释、方法，或对已有信息的回答。
- none=纯寒暄或本轮上下文自足；latest=常规事实点查；diff=前后对比/变更；order=时间线；sum=跨会话总结统计。
- 含泛指历史指代（“那个/上次/之前/后来呢”）时 m 不得为 none，保底 latest。

只输出：{"a":"noise","i":"chat","m":"none","l":false,"w":false}"""


_INTENT_MAP: dict[str, TurnIntent] = {"chat": "闲聊", "tool": "工具", "qa": "问答"}
_MEMORY_MAP: dict[str, MemoryIntent] = {
    "none": "none",
    "latest": "point_latest",
    "diff": "version_diff",
    "order": "timeline_order",
    "sum": "broad_summary",
}
_ADDR_MAP: dict[str, AddressingKind] = {
    "direct_ask": "direct_ask",
    "follow_self": "follow_self",
    "to_other": "to_other",
    "comment_receipt": "comment_receipt",
    "repeat": "repeat",
    "noise": "noise",
}


def _build_router_prompt(
    body: str,
    *,
    prev_turn_used_tools: bool,
    soft_triggered: bool,
    clock_at: datetime | None,
    thread_view: str,
    persona_names: Sequence[str] = (),
) -> str:
    clock = (clock_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = [f"[当前时间] {clock}", f"[上一轮用过工具] {'yes' if prev_turn_used_tools else 'no'}"]
    names = [n.strip() for n in persona_names if n and n.strip()]
    if names:
        lines.append(f"[你的称呼] {' / '.join(names)}")
    if soft_triggered:
        lines.append("[软触发] yes")
    if thread_view:
        lines.append(thread_view)
    lines.extend(["[本轮消息]", body])
    return "\n".join(lines)


def _parse_decision(raw: str) -> TurnRouteDecision | None:
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw_i = data["i"] if "i" in data and isinstance(data["i"], str) else ""
    if raw_i not in _INTENT_MAP:
        return None
    intent = _INTENT_MAP[raw_i]
    raw_m = data["m"] if "m" in data and isinstance(data["m"], str) else ""
    default_m: MemoryIntent = "none" if intent == "闲聊" else "point_latest"
    memory_intent: MemoryIntent = _MEMORY_MAP[raw_m] if raw_m in _MEMORY_MAP else default_m
    raw_a = data["a"] if "a" in data and isinstance(data["a"], str) else ""
    addressing: AddressingKind | None = _ADDR_MAP[raw_a] if raw_a in _ADDR_MAP else None
    soft_speak = (
        addressing in _SPEAK_ADDR_KINDS if addressing is not None else bool(data["s"]) if "s" in data else False
    )
    return TurnRouteDecision(
        intent=intent,
        memory_intent=memory_intent,
        soft_speak=soft_speak,
        is_live_lookup=bool(data["l"]) if "l" in data else False,
        needs_committed_work=bool(data["w"]) if "w" in data else False,
        source="llm",
        addressing=addressing,
    )


def _can_call_llm() -> bool:
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        from gsuid_core.ai_core.configs.models import get_config_name_for_task

        return bool(get_config_name_for_task("low", TASK_TURN_ROUTER))
    except (ImportError, ValueError, AttributeError):
        return False


async def route_turn(
    query: str,
    *,
    prev_turn_used_tools: bool = False,
    soft_triggered: bool = False,
    clock_at: datetime | None = None,
    thread_view: str = "",
    persona_names: Sequence[str] = (),
    wake_kind: str = "",
) -> TurnRouteDecision:
    """一次无思考轻量路由；不可用/超时/输出非法时退化到结构规则。"""
    body = strip_router_header(query)
    fallback = structural_route(
        body,
        prev_turn_used_tools=prev_turn_used_tools,
        soft_triggered=soft_triggered,
        wake_kind=wake_kind,
        persona_names=persona_names,
    )
    if not body or not _can_call_llm():
        return fallback

    prompt = _build_router_prompt(
        body,
        prev_turn_used_tools=prev_turn_used_tools,
        soft_triggered=soft_triggered,
        clock_at=clock_at,
        thread_view=thread_view,
        persona_names=persona_names,
    )

    try:
        from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

        agent = GsCoreAIAgent(
            system_prompt=_ROUTER_SYSTEM,
            max_tokens=_ROUTER_MAX_TOKENS,
            create_by="TurnRouter",
            task_kind=TASK_TURN_ROUTER,
            task_level="low",
            is_subagent=True,
            dynamic_tools=False,
            thinking=False,
        )
        try:
            raw = await asyncio.wait_for(agent.run(user_message=prompt, return_mode="return"), timeout=ROUTER_TIMEOUT_S)
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
