"""主动触达统一准入策略门（Heartbeat / Autonomy / ScheduledTask / Kanban 共用）。

集中裁决「此刻能否向目标主动发声」：AI 总开关、会话禁言、作息窗口、会话并发忙闲、
群聊发言忙闲、最近 AI 发言冷却、C8 防撞车间隔、按人格隔离的频次配额与私聊互惠退避。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from dataclasses import dataclass

if TYPE_CHECKING:
    from gsuid_core.ai_core.proactive.dispatcher import UnifiedProactiveDispatcher

ProactiveChannel = Literal[
    "heartbeat",
    "autonomy_tick",
    "autonomy_send",
    "scheduled_task",
    "kanban",
    "tool",
]

ProactiveDenyCode = Literal[
    "",
    "ai_off",
    "scope_banned",
    "outside_active_hours",
    "session_busy",
    "human_busy",
    "ai_recent_cooldown",
    "c8_min_gap",
    "rate_limit",
    "dm_reciprocity_backoff",
]

# 禁言策略：拦截主动搭话（heartbeat/autonomy），放行用户预约的定时任务与长任务回执。
_SCOPE_BAN_EXEMPT_CHANNELS: frozenset[str] = frozenset({"scheduled_task", "kanban"})

_DENY_REASONS: dict[ProactiveDenyCode, str] = {
    "": "",
    "ai_off": "AI 总开关未启用",
    "scope_banned": "目标会话已被禁言或关闭 AI",
    "outside_active_hours": "当前不在人格活跃时段内",
    "session_busy": "会话正在活跃处理中（防与被动回复撞车）",
    "human_busy": "群内刚刚有人发言（防打断对话）",
    "ai_recent_cooldown": "AI 最近已发言（防刷屏）",
    "c8_min_gap": "C8 网关抑制（近期已有主动输出，防撞车）",
    "rate_limit": "主动发言频率已达上限",
    "dm_reciprocity_backoff": "对方连续两次未回复主动私聊，私聊推送已暂停",
}


@dataclass(frozen=True, slots=True)
class ProactiveGateInput:
    """统一主动发言门控输入（纯值对象，便于单测与审计）。"""

    channel: ProactiveChannel
    persona_name: str
    target_key: str
    is_direct: bool
    ai_enabled: bool = True
    scope_banned: bool = False
    within_active_hours: bool = True
    hours_waived: bool = False
    session_busy: bool = False
    last_human_age_sec: float | None = None
    last_ai_age_sec: float | None = None
    human_busy_sec: float = 0.0
    ai_cooldown_sec: float = 0.0
    check_c8_gap: bool = False
    c8_gap_blocked: bool = False
    check_rate: bool = False
    rate_blocked: bool = False
    dm_reciprocity_blocked: bool = False


@dataclass(frozen=True, slots=True)
class ProactiveGateDecision:
    """统一主动发言门控裁决结果。"""

    allowed: bool
    code: ProactiveDenyCode = ""
    reason: str = ""


def _deny(code: ProactiveDenyCode) -> ProactiveGateDecision:
    return ProactiveGateDecision(allowed=False, code=code, reason=_DENY_REASONS[code])


def evaluate_proactive_gate(inp: ProactiveGateInput) -> ProactiveGateDecision:
    """按「硬开关 → 禁言 → 作息 → 忙闲/冷却 → C8/配额 → 私聊礼仪」短路裁决。"""
    if not inp.ai_enabled:
        return _deny("ai_off")
    if inp.scope_banned and inp.channel not in _SCOPE_BAN_EXEMPT_CHANNELS:
        return _deny("scope_banned")
    if not inp.hours_waived and not inp.within_active_hours:
        return _deny("outside_active_hours")
    if inp.session_busy:
        return _deny("session_busy")
    if inp.human_busy_sec > 0 and inp.last_human_age_sec is not None and inp.last_human_age_sec < inp.human_busy_sec:
        return _deny("human_busy")
    if inp.ai_cooldown_sec > 0 and inp.last_ai_age_sec is not None and inp.last_ai_age_sec < inp.ai_cooldown_sec:
        return _deny("ai_recent_cooldown")
    if inp.check_c8_gap and inp.c8_gap_blocked:
        return _deny("c8_min_gap")
    if inp.check_rate and inp.rate_blocked:
        return _deny("rate_limit")
    if inp.is_direct and inp.dm_reciprocity_blocked:
        return _deny("dm_reciprocity_backoff")
    return ProactiveGateDecision(allowed=True)


def dm_allowed(*, target_is_master: bool, has_prior_dm: bool, explicit_request: bool) -> bool:
    """主动私聊门控（纯函数）：仅目标为主人、存在既有私聊历史或显式请求时放行。"""
    return target_is_master or has_prior_dm or explicit_request


def check_target_rate_and_gap(
    dispatcher: UnifiedProactiveDispatcher,
    target_key: str,
    *,
    persona_name: str,
    max_per_hour: int | None = None,
    max_per_day: int | None = None,
) -> tuple[bool, bool]:
    """同时校验 C8 最小间隔与按人格隔离的频次配额，强制传 ``persona_name`` 防漏键。"""
    gap_blocked = dispatcher.should_suppress_heartbeat(target_key)
    rate_blocked = dispatcher.should_suppress_by_rate(
        target_key,
        max_per_hour=max_per_hour,
        max_per_day=max_per_day,
        persona_name=persona_name,
    )
    return gap_blocked, rate_blocked


def is_session_busy(session_id: str) -> bool:
    """会话正在执行 run、或收件箱处于待收敛/运行中时返回 True。"""
    if not session_id:
        return False
    from gsuid_core.ai_core.inbox.scheduler import get_inbox_scheduler
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    session = get_ai_session_registry().get_ai_session(session_id)
    if session is not None and session.is_running:
        return True
    return get_inbox_scheduler().has_active_session(session_id)


def is_session_scope_banned(session_id: str) -> bool:
    """查询会话是否被管理员命令禁言或屏蔽 AI。"""
    if not session_id:
        return False
    from gsuid_core.buildin_plugins.core_command.core_ai_control.state import is_scope_banned

    return is_scope_banned(session_id)


async def is_dm_reciprocity_blocked(user_id: str, persona_name: str) -> bool:
    """查询目标用户是否连续 >= 2 次未回复该人格的主动私聊。"""
    uid = (user_id or "").strip()
    pname = (persona_name or "").strip()
    if not uid or not pname:
        return False
    from gsuid_core.ai_core.database.models import UserFavorability

    streaks = await UserFavorability.get_dm_unanswered_streaks([uid], pname)
    return (streaks[uid] if uid in streaks else 0) >= 2
