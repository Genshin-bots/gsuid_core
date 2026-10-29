"""自主行动能不能开工。纯函数，调度器只负责填参数。"""

from gsuid_core.ai_core.autonomy.models import ObservationDigest
from gsuid_core.ai_core.autonomy.select import digest_is_empty

FIRST_DELAY_MIN_SECONDS = 10 * 60
FIRST_DELAY_MAX_SECONDS = 20 * 60
FAIL_BACKOFF_SECONDS = 10 * 60


def autonomy_is_due(
    last_created_at: float | None,
    *,
    now: float,
    cooldown_seconds: float,
    first_due_at: float | None,
    backoff_until: float | None,
) -> bool:
    """表里的最后一行是冷却时钟。表是空的时，用内存里的首次延迟。"""
    if backoff_until is not None and now < backoff_until:
        return False
    if last_created_at is not None:
        return now >= last_created_at + cooldown_seconds
    if first_due_at is None:
        return False
    return now >= first_due_at


def gate_reason(
    *,
    ai_enabled: bool,
    mode_on: bool,
    scope_enabled: bool,
    within_hours: bool,
    chat_busy: bool,
    due: bool,
    budget_ok: bool,
    force: bool,
) -> str:
    """空字符串表示可以进入观察。手动触发只跳过作息、忙闲和冷却。"""
    if not ai_enabled:
        return "ai_off"
    if not mode_on:
        return "mode_off"
    if not scope_enabled:
        return "scope_off"
    if not force and not within_hours:
        return "hours"
    if not force and chat_busy:
        return "busy"
    if not force and not due:
        return "cooldown"
    if not budget_ok:
        return "budget"
    return ""


def should_skip_model(digest: ObservationDigest) -> bool:
    """底片是空的就不要为了休息再打一次模型。"""
    return digest_is_empty(digest)
