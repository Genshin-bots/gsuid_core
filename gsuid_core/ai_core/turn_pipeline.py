"""被动交互一轮的**内核**步骤（从 handle_ai 抽出的编排零件）。

这里放的都是「不属于任何产品套件」的东西：预算闸、长度防护、群历史窗口、结果下发。
它们留在内核的理由各不相同，逐条写在函数 docstring 里——不要因为「看起来像产品」
就把它们做成可关的套件槽。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, List, Tuple, Optional, Sequence
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from pydantic_ai.messages import UserContent, ModelMessage

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import (
    NO_RESULT_TEXT,
    ERROR_RESULT_PREFIX,
    send_chat_result,
    is_silence_marker,
    classify_error_type,
    sanitize_error_for_user,
    notify_master_of_agent_error,
    notify_master_of_budget_block,
)
from gsuid_core.message_history import get_history_manager
from gsuid_core.ai_core.history_format import format_history_for_agent
from gsuid_core.message_history.manager import MessageRecord
from gsuid_core.ai_core.persona.settings import persona_name_from_event

if TYPE_CHECKING:
    from gsuid_core.ai_core.budget.manager import BudgetDecision

# 双层长度防护（D-10）：绝对上限硬截断，摘要阈值走子 Agent 智能摘要
ABSOLUTE_MAX_LENGTH = 60000
MAX_SUMMARY_LENGTH = 15000

# 群聊历史窗口：靠紧凑格式 + 当前用户优先，而不是堆 30 条散句
_HISTORY_LIMIT = 20
_MAX_OTHER_RECORDS = 6


async def evaluate_budget(event: Event) -> BudgetDecision | None:
    """预算判定（无 bot.send）。失败返回 None（fail-open）。"""
    try:
        from gsuid_core.ai_core.budget import budget_manager

        return await budget_manager.check_scope(
            str(event.group_id) if event.group_id else "",
            str(event.user_id),
            event.bot_id or "",
            event.session_id,
        )
    except SQLAlchemyError as e:
        logger.warning(t("log.ai.gscore_budget_check_db", e=e))
    except Exception as e:
        logger.exception(t("log.ai.gscore_budget_check_fail", e=e))
    return None


async def check_budget_gate(bot: Bot, event: Event) -> bool:
    """预算闸门（被动交互路径·前置短路）。返回是否放行。

    **不可套件化**：预算闸与 token 记账是配额防线，做成可关的套件等于把防线做成可关的。
    超额早退能省下后续记忆/分类/RAG/主 Agent 的开销；check 本身异常 fail-open。
    """
    decision = await evaluate_budget(event)

    if decision is None or decision.allowed:
        return True

    logger.info(
        t(
            "log.ai.gscore_budget_exceeded_intercepted",
            p0=decision.block_scope_label,
            p1=decision.message,
        )
    )
    if bot is not None:
        if decision.notify and decision.message:
            try:
                await bot.send(decision.message)
            except Exception as e:
                logger.warning(t("log.ai.gscore_budget_exceeded_notice", e=e))
        # 主人告警独立于用户提示：即使 notify=False 也让运维感知拦截事件
        await notify_master_of_budget_block(bot=bot, ev=event, decision=decision)
    # 提示尽力而为，发送失败也无条件早退，绝不放超额消息进完整 AI 流程
    return False


def apply_absolute_length_guard(event: Event, query: str) -> str:
    """第一层长度防护：绝对上限硬截断，防止超大文本把子 Agent 的 Token 打爆。

    **不可套件化**：长度是安全面（D-10），不是产品策略。
    """
    if len(query) <= ABSOLUTE_MAX_LENGTH:
        return query
    logger.warning(
        t(
            "log.ai.gscore_exceeded_absolute_limit",
            raw_text_len=len(query),
            ABSOLUTE_MAX_LENGTH=ABSOLUTE_MAX_LENGTH,
        )
    )
    truncated = query[:ABSOLUTE_MAX_LENGTH] + "...[文本过长，已自动截断]"
    event.raw_text = truncated
    return truncated


async def apply_summary_guard(event: Event, user_messages: Sequence[UserContent]) -> None:
    """第二层长度防护：超长正文走子 Agent 智能摘要，**保留上下文头**只替换正文。

    原地改写 ``user_messages[0]``。摘要失败由调用方的整轮 except 兜底。
    """
    if len(event.raw_text) <= MAX_SUMMARY_LENGTH:
        return
    from gsuid_core.ai_core.buildin_tools.subagent import summarize_long_input

    logger.info(t("log.ai.gscore_long_characters_summarization", p0=len(event.raw_text)))
    summarized = await summarize_long_input(event.raw_text, max_tokens=18000)
    if not isinstance(user_messages, list) or not user_messages or not isinstance(user_messages[0], str):
        return
    marker = "--- 消息 ---\n"
    header_end = user_messages[0].find(marker)
    if header_end != -1:
        header = user_messages[0][: header_end + len(marker)]
        user_messages[0] = header + summarized + "\n[注：原始消息已摘要]"
    else:
        user_messages[0] = summarized
    logger.info(t("log.ai.gscore_summarization_summary_length", p0=len(summarized)))


def current_time_line() -> str:
    """本轮精确时间行的**唯一产出点**（生产与评测共用）。

    人设 system_prompt 只放到「日」级以保住 provider 前缀缓存，分秒级时间按约定由
    user 侧每轮补上。两条入口各自拼这行的话，漏掉的那条会让模型只知道日期不知道
    时刻——问「现在几点」时它只能答「不知道」，而且**没有任何报错**。
    """
    return f"[当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"


def stamp_current_time(user_messages: Sequence[UserContent]) -> None:
    """把精确时间钉在本轮发言块末尾（摘要之后、动态上下文之前）。

    **只进 user 侧**：每轮变化的内容进 system 会让 provider 前缀缓存每轮失效。
    """
    if isinstance(user_messages, list) and user_messages and isinstance(user_messages[0], str):
        user_messages[0] += f"\n{current_time_line()}"


def build_group_history_block(event: Event) -> str:
    """群聊 IM 历史块（带「[历史对话]」标头的成品文本）。私聊返回空串。

    **不可套件化**：HistoryManager 是消息基础设施，不是 AI 产品。私聊时
    pydantic_ai 的 session.history 已覆盖对话，再注入 IM 历史既冗余又破坏缓存前缀。
    """
    if not event.group_id:
        return ""
    history_manager = get_history_manager()
    raw = history_manager.get_history(event, limit=_HISTORY_LIMIT)
    # 排除最后一条（当前用户刚发的消息），避免与 user_messages 重复
    records = raw[:-1] if raw else []
    if not records:
        return ""

    # IM 只注入 B 轨没有的他人句：跳过 assistant（已在 session history）
    records = [r for r in records if r.role != "assistant"]
    if not records:
        return ""

    current_user_id = str(event.user_id)
    others = [r for r in records if r.user_id != current_user_id][-_MAX_OTHER_RECORDS:]
    selected = sorted(others, key=lambda r: r.timestamp)

    block = format_history_for_agent(
        history=selected,
        current_user_id=current_user_id,
        current_user_name=event.sender.get("nickname") if event.sender else None,
        include_current_turn=False,
    )
    if block:
        logger.debug(t("log.ai.gscore_historical", p0=len(selected)))
    return block


def classify_run_result(chat_result: str) -> Tuple[str, bool, bool]:
    """把一次 run 的返回值分类一次，供下发与结算复用。

    返回 ``(result_text, is_silence, is_error)``。判据引用协议常量 / 解析器，
    不写魔法串（评审修复 E11）。
    """
    result_text = chat_result if isinstance(chat_result, str) else str(chat_result or "")
    is_silence = is_silence_marker(result_text.strip())
    is_error = result_text.startswith(ERROR_RESULT_PREFIX) or result_text == NO_RESULT_TEXT
    return result_text, is_silence, is_error


async def deliver_run_result(
    bot: Bot,
    event: Event,
    chat_result: str,
    *,
    result_text: str,
    is_silence: bool,
    is_error: bool,
    intent: str,
) -> None:
    """下发一次 run 的结果（静默 / 失败脱敏 / 正常发送三分支）。

    失败必须让用户可感知，但原始错误串含 provider body 等内部细节，脱敏后发送；
    详情与用户通知解耦——即使发送失败也把详情同步给主人，便于排查。
    """
    if not chat_result:
        return
    if is_silence:
        logger.info(t("log.ai.gscore_persona_chose_silence"))
        return
    if is_error:
        logger.warning(t("log.ai.gscore_sanitized_fallback_user", r=result_text[:200]))
        user_facing = sanitize_error_for_user(result_text, persona_name_from_event(event))
        try:
            await send_chat_result(bot, user_facing, ev=event)
        except Exception as e:
            logger.warning(t("log.ai.gscore_sanitized_fallback", e=e))
        await notify_master_of_agent_error(
            bot=bot,
            ev=event,
            error_type=classify_error_type(result_text),
            result_text=result_text,
            user_facing=user_facing,
        )
        return
    # send_chat_result 只接文本；结构化返回已在 classify_run_result 里 str 化
    await send_chat_result(bot, chat_result if isinstance(chat_result, str) else result_text, ev=event)
    logger.info(t("log.ai.gscore_ai_intent_reply_sent_mode", intent=intent))


def recent_report_titles(history: Sequence[ModelMessage]) -> Tuple[str, ...]:
    """上一轮已发出的资料图标题（防止连续两轮出同名图）。"""
    from pydantic_ai.messages import ModelResponse

    for msg in reversed(list(history)):
        if isinstance(msg, ModelResponse):
            meta = msg.metadata
            if meta and "sent_reports" in meta:
                return tuple(meta["sent_reports"])
            break
    return ()


def prev_turn_used_tools(history: Sequence[ModelMessage], *, max_assistant_turns: int = 6) -> bool:
    """近几轮是否真的用过工具（勿只看当前句）。

    intent 分类与「是否压短闲聊」都要它：上轮在办事的会话不该被压成寒暄短句。
    """
    from pydantic_ai.messages import ToolCallPart, ModelResponse

    seen = 0
    for msg in reversed(list(history)):
        if not isinstance(msg, ModelResponse):
            continue
        if any(isinstance(p, ToolCallPart) for p in msg.parts):
            return True
        seen += 1
        if seen >= max_assistant_turns:
            break
    return False


def prior_turns_from_agent_history(history: Sequence[ModelMessage], query: str) -> List[str]:
    """从 agent.history 抽出同用户先验（评测把请求 history 灌进 session 后用）。"""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    texts: List[str] = []
    for msg in history:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, str) and part.content.strip():
                texts.append(part.content)
    if texts and texts[-1].strip() == query.strip():
        texts = texts[:-1]
    return texts[-4:]


def prior_user_turns_for_intent(event: Event, query: str) -> Tuple[List[str], List[MessageRecord]]:
    """意图分类的同用户先验。返回 ``(prior_turns, 本轮取到的历史)``。

    handler 已把本轮用户句入库，prior 须去掉与 query 相同的末条，否则分类器会把
    当前句当成「上一轮也这么说」的证据。
    """
    from gsuid_core.ai_core.classifier.mode_classifier import collect_prior_user_turns

    history_manager = get_history_manager()
    records = history_manager.get_history(event, limit=30)
    prior = collect_prior_user_turns(records, str(event.user_id), max_turns=5)
    if prior and prior[-1].strip() == query.strip():
        prior = prior[:-1]
    return prior[-4:], records


def stale_request(enqueue_ts: Optional[float], ttl: float) -> bool:
    """O-A 队头阻塞防护：排队过久（全局过载）时话题大概率已翻篇，直接放弃。"""
    if enqueue_ts is None:
        return False
    waited = time.time() - enqueue_ts
    if waited <= ttl:
        return False
    logger.info(t("log.ai.gscore_queue_wait_exceeded", p0=waited))
    return True
