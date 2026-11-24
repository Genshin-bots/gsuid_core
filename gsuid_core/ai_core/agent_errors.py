"""Agent 错误文案、分类、用户脱敏及主人 DM 告警。"""

from __future__ import annotations

import re
from typing import List, Union, Literal, Optional, Protocol

from pydantic_ai.exceptions import ModelHTTPError

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.segment import Message
from gsuid_core.ai_core.const import (
    _RETRYABLE_4XX,
    _CONTENT_REJECT_CODES,
    _CONTENT_REJECT_HINTS,
    ERROR_QUOTA_EXHAUSTED,
)

# run 失败返回值协议：生产端(gs_agent)与全部消费端引用同一组常量做嗅探。
ERROR_RESULT_PREFIX = "执行出错"
ERROR_CONTENT_REJECTED = "内容被模型安全策略拒绝"


def is_agent_error_text(text: str) -> bool:
    """上游 agent 的错误回执（``执行出错: …``）或网关超时回显，不得当正常产出消费。"""
    stripped = (text or "").strip()
    return stripped.startswith(ERROR_RESULT_PREFIX) or "Request timed out" in stripped


ERROR_TIMEOUT_TEXT = "请求超时"
NO_RESULT_TEXT = "Agent 执行完成，但未返回有效结果"

_CONTROL_BLOCK_RE = re.compile(r"<control\b[^>]*>.*?</control>", re.DOTALL | re.IGNORECASE)
_MAX_ITER_LEAK_RE = re.compile(r"⚠️\s*已达最大思考轮数[^\n]*(?:\n中间产物[^\n]*)?")
_INTERNAL_CHANNEL_RE = re.compile(
    r"（这条是内部通道，不向用户解释。）|"
    r"本段是框架内部通道，不是群友发言：不要向用户解释、道歉或复述本段。"
)


def strip_framework_user_leaks(text: str) -> str:
    """剥进用户可见正文的控制信封 / 超轮数 / 内部通道。空则调用方当沉默。

    保留（至多一个）空行：``send_chat_result`` 靠 ``\\n\\s*\\n`` 拆多条气泡，
    早期版本这里 splitlines+join 把空行全压成单换行，等于让拆条永久失效。
    """
    if not text:
        return text
    out = _CONTROL_BLOCK_RE.sub("", text)
    out = _MAX_ITER_LEAK_RE.sub("", out)
    out = _INTERNAL_CHANNEL_RE.sub("", out)
    if NO_RESULT_TEXT in out:
        out = out.replace(NO_RESULT_TEXT, "")
    kept: list[str] = []
    for ln in out.split("\n"):
        if ln.strip():
            kept.append(ln)
        elif kept and kept[-1] != "":
            kept.append("")
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept).strip()


def _is_non_retryable_model_error(e: BaseException) -> bool:
    """该异常是否为"重试也必然复现"的永久性模型错误（4xx 客户端错误，排除 408/429）。"""
    return isinstance(e, ModelHTTPError) and 400 <= e.status_code < 500 and e.status_code not in _RETRYABLE_4XX


def _is_content_rejected(e: ModelHTTPError) -> bool:
    """4xx 错误是否为"内容被模型安全 / 审核策略拒绝"（用于更友好的文案与统计分类）。"""
    blob = (str(e.body or "") + " " + str(e)).lower()
    if any(hint in blob for hint in _CONTENT_REJECT_HINTS):
        return True
    return any(re.search(rf"\b{code}\b", blob) for code in _CONTENT_REJECT_CODES)


def _is_retryable_client_error(e: BaseException) -> bool:
    """永久性 4xx 中「非内容审核拒绝」的那部分是否值得一次干净历史重试。

    模型退化产生的畸形请求（如重复键工具参数）是**随机性**的：从未被污染的
    self.history 重跑大概率成功；而内容审核拒绝是确定性的，重试必复现。
    """
    return isinstance(e, ModelHTTPError) and _is_non_retryable_model_error(e) and not _is_content_rejected(e)


def sanitize_error_for_user(result_text: str, persona_name: str | None = None) -> str:
    """把 ``执行出错: <内部细节>`` 转成不泄漏内部细节的用户可见短文案。

    原始错误串含 provider body / model_name / tool_call_id 等内部信息，直接发进
    群聊既难看又泄漏实现；完整细节已由 log_error 落日志，用户侧只需要知道失败了。
    """
    from gsuid_core.ai_core.persona.settings import get_persona_setting

    if result_text == NO_RESULT_TEXT:
        return get_persona_setting(persona_name, "error_generic")
    if not result_text.startswith(ERROR_RESULT_PREFIX):
        return result_text
    if ERROR_CONTENT_REJECTED in result_text:
        return get_persona_setting(persona_name, "error_content_policy")
    if ERROR_QUOTA_EXHAUSTED in result_text:
        return get_persona_setting(persona_name, "error_quota")
    if ERROR_TIMEOUT_TEXT in result_text:
        return get_persona_setting(persona_name, "error_timeout")
    return get_persona_setting(persona_name, "error_generic")


_ERROR_TYPE_LABEL_NO_RESULT = "无有效结果"
_ERROR_TYPE_LABEL_CONTENT = "内容安全"
_ERROR_TYPE_LABEL_TIMEOUT = "超时"
_ERROR_TYPE_LABEL_QUOTA = "套餐用量打满"
_ERROR_TYPE_LABEL_OTHER = "其他错误"
_ERROR_TYPE_LABEL_UNKNOWN = "未知"

_MASTER_DM_RAW_TEXT_MAX = 200
_MASTER_DM_RESULT_MAX = 500


class _MasterDMTarget(Protocol):
    """能向指定目标发送私聊/群消息的最小接口。"""

    async def target_send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        target_type: Literal["group", "direct", "channel", "sub_channel"],
        target_id: Optional[str],
        at_sender: bool = False,
        sender_id: str = "",
        send_source_group: Optional[str] = None,
        wait_recall: bool = False,
    ) -> Optional[List[str]]: ...


class _MasterDMEvent(Protocol):
    """主人 DM 所需的事件字段子集。"""

    @property
    def session_id(self) -> str: ...
    @property
    def user_id(self) -> str: ...
    @property
    def group_id(self) -> Optional[str]: ...
    @property
    def bot_id(self) -> str: ...
    @property
    def raw_text(self) -> str: ...


def classify_error_type(result_text: str) -> str:
    """把 agent run 返回的错误串归类成给主人看的中文标签。"""
    if result_text == NO_RESULT_TEXT:
        return _ERROR_TYPE_LABEL_NO_RESULT
    if not result_text.startswith(ERROR_RESULT_PREFIX):
        return _ERROR_TYPE_LABEL_UNKNOWN
    if ERROR_CONTENT_REJECTED in result_text:
        return _ERROR_TYPE_LABEL_CONTENT
    if ERROR_QUOTA_EXHAUSTED in result_text:
        return _ERROR_TYPE_LABEL_QUOTA
    if ERROR_TIMEOUT_TEXT in result_text:
        return _ERROR_TYPE_LABEL_TIMEOUT
    return _ERROR_TYPE_LABEL_OTHER


def _build_agent_error_report(
    *,
    session_id: str,
    user_id: str,
    group_id: Optional[str],
    bot_id: str,
    error_type: str,
    result_text: str,
    user_facing: str,
    raw_text: str,
) -> str:
    """构造给主人的「AI 执行失败」结构化小报告。"""
    group_label = group_id if group_id else "私聊"
    truncated_raw = raw_text.strip()
    if len(truncated_raw) > _MASTER_DM_RAW_TEXT_MAX:
        truncated_raw = truncated_raw[:_MASTER_DM_RAW_TEXT_MAX] + "..."
    truncated_err = result_text.strip()
    if len(truncated_err) > _MASTER_DM_RESULT_MAX:
        truncated_err = truncated_err[:_MASTER_DM_RESULT_MAX] + "..."
    return (
        "[AI 执行失败]\n"
        f"• 会话: {session_id}\n"
        f"• 用户: {user_id} (群: {group_label})\n"
        f"• Bot: {bot_id}\n"
        f"• 类型: {error_type}\n"
        f"• 用户看到: {user_facing}\n"
        f"• 原始错误: {truncated_err}\n"
        f"• 用户原文: {truncated_raw}"
    )


def _build_budget_block_report(
    *,
    session_id: str,
    user_id: str,
    group_id: Optional[str],
    bot_id: str,
    block_scope_label: str,
    raw_text: str,
) -> str:
    """构造给主人的「AI 预算超额拦截」结构化小报告。"""
    group_label = group_id if group_id else "私聊"
    truncated_raw = raw_text.strip()
    if len(truncated_raw) > _MASTER_DM_RAW_TEXT_MAX:
        truncated_raw = truncated_raw[:_MASTER_DM_RAW_TEXT_MAX] + "..."
    return (
        "[AI 预算超额拦截]\n"
        f"• 会话: {session_id}\n"
        f"• 用户: {user_id} (群: {group_label})\n"
        f"• Bot: {bot_id}\n"
        f"• 拦截维度: {block_scope_label}\n"
        f"• 用户原文: {truncated_raw}"
    )


async def _dispatch_master_dm(
    bot: _MasterDMTarget,
    report: str,
    masters: List[str],
    log_prefix: str,
) -> None:
    """把 ``report`` 私聊发给每个主人；单个主人失败不影响其他主人。"""
    for master_id in masters:
        master_id = str(master_id)
        if not master_id:
            continue
        try:
            await bot.target_send(report, "direct", target_id=master_id)
        except Exception as e:
            logger.warning(
                i18n_t(
                    "log.ai.master_notification_id_fail",
                    p0=log_prefix,
                    master_id=master_id,
                    e=e,
                )
            )


async def notify_master_of_agent_error(
    bot: _MasterDMTarget,
    ev: _MasterDMEvent,
    *,
    error_type: str,
    result_text: str,
    user_facing: str,
) -> None:
    """Agent run 失败后，把结构化错误报告私聊发给每个主人。

    未配置 ``masters`` 时 no-op；单个主人 DM 失败被吞掉，不污染主流程。
    """
    from gsuid_core.config import core_config

    masters: List[str] = [str(m) for m in (core_config.get_config("masters") or [])]
    if not masters:
        return
    report = _build_agent_error_report(
        session_id=ev.session_id,
        user_id=ev.user_id,
        group_id=ev.group_id,
        bot_id=ev.bot_id,
        error_type=error_type,
        result_text=result_text,
        user_facing=user_facing,
        raw_text=ev.raw_text,
    )
    await _dispatch_master_dm(bot, report, masters, log_prefix="🧠 [GsCore][AI]")


class _BudgetBlockDecision(Protocol):
    block_scope_label: str


async def notify_master_of_budget_block(
    bot: _MasterDMTarget,
    ev: _MasterDMEvent,
    *,
    decision: _BudgetBlockDecision,
) -> None:
    """预算超额拦截时，把结构化告警私聊发给每个主人。

    ``decision`` 避免在 utils.py 顶层 import ``BudgetDecision``，防止与 budget 子模块
    循环 import；调用方保证传入的是该 dataclass 实例。
    """
    from gsuid_core.config import core_config

    masters: List[str] = [str(m) for m in (core_config.get_config("masters") or [])]
    if not masters:
        return
    block_scope_label = str(decision.block_scope_label or "")
    report = _build_budget_block_report(
        session_id=ev.session_id,
        user_id=ev.user_id,
        group_id=ev.group_id,
        bot_id=ev.bot_id,
        block_scope_label=block_scope_label,
        raw_text=ev.raw_text,
    )
    await _dispatch_master_dm(bot, report, masters, log_prefix="💰 [GsCore][AI]")
