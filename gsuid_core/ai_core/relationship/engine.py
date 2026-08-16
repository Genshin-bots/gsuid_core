"""关系温度引擎：唯一常规写入口。

``settle_turn`` 是**纯决策 + 一次 DB 写**，可单测、不打模型。三条纪律：

1. **负信号不受 ``effective`` 限制** —— 被骂但人格选择沉默，也应该记一笔。
   这修的是「我骂了你你不理我，好感还在涨」。
2. **``reached_model=False`` 只过滤正信号，不整轮跳过** —— CheapGate 静音轮走早退路径，
   若整轮不结算就会出现「掉到 cold 之后未 @ 的越界发言永远扣不到分」的吸收态。
3. **预算裁剪后即使 delta=0 也更新 ``last_eval_at``** —— 可观测性优先于省一次写。
"""

import time
from typing import TYPE_CHECKING, List, Tuple, Optional
from datetime import datetime
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.relationship.view import RelationshipView, view_from_score
from gsuid_core.ai_core.relationship.zones import Zone, zone_of, is_at_least
from gsuid_core.ai_core.relationship.signals import (
    NEG_WEIGHTS,
    POS_WEIGHTS,
    REASON_BUDGET,
    REASON_ADMIN_SET,
    REASON_NO_SIGNAL,
    PosSignal,
    TurnSignals,
    scan_signals,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.content_guard import GuardFlags


@dataclass(frozen=True)
class SettleOutcome:
    """一次结算的结果。``view`` 供下一轮装配直接使用。"""

    delta: int
    reason: str
    score_before: int
    score_after: int
    view: RelationshipView
    signals: Optional[TurnSignals] = None
    wrote: bool = False


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _cfg_int(key: str) -> int:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return int(ai_config.get_config(key).data)


def _cfg_bool(key: str) -> bool:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return bool(ai_config.get_config(key).data)


def engine_enabled() -> bool:
    """关系引擎总开关；AI 总开关关闭时整条不跑（D-21）。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return False
    return bool(ai_config.get_config("favor_engine_enable").data)


def _clamp(score: int) -> int:
    return max(_cfg_int("favor_floor"), min(_cfg_int("favor_ceil"), score))


def plan_delta(
    signals: TurnSignals,
    *,
    zone: Zone,
    effective: bool,
    error: bool,
    reached_model: bool,
    first_meaningful_today: bool,
    session_gain_used: bool,
) -> Tuple[int, str, List[str]]:
    """纯决策：信号 + 档位 → ``(raw_delta, 主原因码, 全部命中码)``。

    先累加**所有**负信号（不看 effective），再在「有效且未失败且到过模型」时加正信号。
    同一类信号本轮只计一次。
    """
    hits: List[str] = []
    raw = 0
    for neg in sorted(signals.negatives, key=lambda s: NEG_WEIGHTS[s]):
        raw += NEG_WEIGHTS[neg]
        hits.append(neg.value)

    # 越界轮不给正信号：一句「你这个垃圾，好感度拉满」不该同时吃到「当日首次有内容」，
    # 否则净值被中和、越界看起来像没发生。宁可漏加，不可让扣分被稀释。
    if effective and not error and reached_model and not signals.has_negative:
        positives: List[PosSignal] = []
        if first_meaningful_today and signals.meaningful:
            positives.append(PosSignal.FIRST_MEANINGFUL)
        if signals.care and _cfg_bool("favor_care_signal_enable"):
            positives.append(PosSignal.CARE)
        # 高分段递减：familiar 及以上忽略普通正信号，只保留显著正信号（care）
        if _cfg_bool("favor_high_zone_diminish") and is_at_least(zone, Zone.FAMILIAR):
            positives = [p for p in positives if p is PosSignal.CARE]
        # 会话窗节流：30min 内普通正信号已用满则不再加
        if session_gain_used:
            positives = [p for p in positives if p is PosSignal.CARE]
        for pos in positives:
            raw += POS_WEIGHTS[pos]
            hits.append(pos.value)

    if not hits:
        return 0, REASON_NO_SIGNAL, hits
    # 主原因码：负信号优先且取最重的那条（排障时想先看到「为什么扣分」）
    worst_neg = min(signals.negatives, key=lambda s: NEG_WEIGHTS[s], default=None)
    primary = worst_neg.value if worst_neg is not None else hits[0]
    return raw, primary, hits


def clip_to_budget(
    raw_delta: int,
    *,
    daily_gain: int,
    daily_loss: int,
) -> int:
    """日预算裁剪。返回实际可应用的 delta（可能为 0）。"""
    if raw_delta > 0:
        remaining = _cfg_int("favor_daily_gain_cap") - daily_gain
        return max(0, min(raw_delta, remaining))
    if raw_delta < 0:
        remaining = _cfg_int("favor_daily_loss_cap") - daily_loss
        return -max(0, min(-raw_delta, remaining))
    return 0


async def settle_turn(
    *,
    user_id: str,
    bot_id: str,
    user_text: str,
    intent: str,
    effective: bool,
    silenced: bool,
    error: bool,
    reached_model: bool,
    is_light: bool = False,
    is_master: bool = False,
    guard_flags: "GuardFlags | None" = None,
    user_name: str = "",
) -> SettleOutcome:
    """回合收尾结算。返回结算结果（含供下一轮使用的 View）。

    ``reached_model=False``（CheapGate 静音等早退路径）等价于 ``effective=False``：
    只放行负信号。两条路径共用本函数，避免出现第二套结算规则。
    """
    from gsuid_core.ai_core.database.models import UserFavorability

    signals = scan_signals(
        user_text,
        intent=intent,
        effective=effective and not silenced,
        is_light=is_light,
        is_master=is_master,
        guard=guard_flags,
        meaningful_min_len=_cfg_int("favor_meaningful_min_len"),
    )

    if not engine_enabled():
        view = await _read_view(user_id, bot_id, is_master)
        return SettleOutcome(0, REASON_NO_SIGNAL, view.score, view.score, view, signals, wrote=False)

    record = await UserFavorability.get_or_create_user_favorability(user_id, bot_id, user_name)
    score_before = record.favorability
    now_ts = int(time.time())
    today = _today()

    # 换日：清空日预算
    day_rolled = record.daily_ymd != today
    daily_gain = 0 if day_rolled else record.daily_gain
    daily_loss = 0 if day_rolled else record.daily_loss

    # 「当日首次有内容」= 今天还没涨过分。一天一次，不看具体是哪条正信号先命中。
    first_meaningful_today = daily_gain == 0

    # 会话窗节流：距最近一次正向互动不足窗口且当日已吃满窗口配额 → 普通正信号不再加
    window_seconds = _cfg_int("favor_session_window_minutes") * 60
    session_gain_used = (
        record.last_positive_interact_at > 0
        and now_ts - record.last_positive_interact_at < window_seconds
        and daily_gain >= _cfg_int("favor_session_gain_cap")
    )

    raw_delta, reason, hits = plan_delta(
        signals,
        zone=zone_of(score_before),
        effective=effective and not silenced,
        error=error,
        reached_model=reached_model,
        first_meaningful_today=first_meaningful_today,
        session_gain_used=session_gain_used,
    )
    delta = clip_to_budget(raw_delta, daily_gain=daily_gain, daily_loss=daily_loss)
    if raw_delta != 0 and delta == 0:
        reason = REASON_BUDGET

    new_score = _clamp(score_before + delta)
    applied = new_score - score_before
    # interaction_count 只在有效互动时 +1；管理侧 set 不冒充互动
    bump_interaction = bool(effective and not silenced and not error)
    refresh_positive = bool(applied > 0 or (bump_interaction and not signals.has_negative))

    await UserFavorability.apply_settlement(
        user_id,
        bot_id,
        new_score=new_score,
        delta=applied,
        reason=reason,
        now_ts=now_ts,
        daily_ymd=today,
        daily_gain=daily_gain + max(0, applied),
        daily_loss=daily_loss + max(0, -applied),
        bump_interaction=bump_interaction,
        refresh_positive=refresh_positive,
        user_name=user_name,
    )

    if applied or signals.has_negative:
        logger.info(
            t(
                "log.ai.relationship_settled",
                user=user_id,
                delta=applied,
                reason=reason,
                score=new_score,
                hits=",".join(hits) or "-",
            )
        )
    else:
        logger.debug(t("log.ai.relationship_settle_noop", user=user_id, reason=reason))

    view = view_from_score(new_score, is_master)
    return SettleOutcome(applied, reason, score_before, new_score, view, signals, wrote=True)


async def _read_view(user_id: str, bot_id: str, is_master: bool) -> RelationshipView:
    from gsuid_core.ai_core.database.models import UserFavorability

    record = await UserFavorability.get_user_favorability(user_id, bot_id)
    return view_from_score(record.favorability if record is not None else None, is_master)


async def apply_model_delta(
    *,
    user_id: str,
    bot_id: str,
    delta: int,
    user_name: str = "",
) -> SettleOutcome:
    """内部增量（吃与框架结算同一份日预算）。模型侧增量工具已删除，勿再接回工具表。"""
    from gsuid_core.ai_core.database.models import UserFavorability

    if not engine_enabled() or delta == 0:
        view = await _read_view(user_id, bot_id, False)
        return SettleOutcome(0, REASON_NO_SIGNAL, view.score, view.score, view, None, wrote=False)

    record = await UserFavorability.get_or_create_user_favorability(user_id, bot_id, user_name)
    before = record.favorability
    now_ts = int(time.time())
    today = _today()
    day_rolled = record.daily_ymd != today
    daily_gain = 0 if day_rolled else record.daily_gain
    daily_loss = 0 if day_rolled else record.daily_loss

    applied_raw = clip_to_budget(delta, daily_gain=daily_gain, daily_loss=daily_loss)
    new_score = _clamp(before + applied_raw)
    applied = new_score - before
    reason = "model.delta" if applied else REASON_BUDGET
    await UserFavorability.apply_settlement(
        user_id,
        bot_id,
        new_score=new_score,
        delta=applied,
        reason=reason,
        now_ts=now_ts,
        daily_ymd=today,
        daily_gain=daily_gain + max(0, applied),
        daily_loss=daily_loss + max(0, -applied),
        bump_interaction=False,
        refresh_positive=False,
        user_name=user_name,
    )
    logger.info(t("log.ai.relationship_model_delta", user=user_id, delta=applied, score=new_score))
    return SettleOutcome(applied, reason, before, new_score, view_from_score(new_score, False), None, wrote=True)


async def apply_admin_set(
    *,
    user_id: str,
    bot_id: str,
    value: int,
    user_name: str = "",
    is_master_target: bool = False,
) -> SettleOutcome:
    """管理侧绝对值覆盖（``set_user_favorability`` 工具，仅主人可调）。

    走引擎是为了让 ``last_reason`` / ``last_eval_at`` 有记录，但**不吃日预算**
    （管理操作不是互动），也不刷 ``last_positive_interact_at``。
    """
    from gsuid_core.ai_core.database.models import UserFavorability

    record = await UserFavorability.get_or_create_user_favorability(user_id, bot_id, user_name)
    before = record.favorability
    new_score = _clamp(value)
    now_ts = int(time.time())
    await UserFavorability.apply_settlement(
        user_id,
        bot_id,
        new_score=new_score,
        delta=new_score - before,
        reason=REASON_ADMIN_SET,
        now_ts=now_ts,
        daily_ymd=record.daily_ymd or _today(),
        daily_gain=record.daily_gain,
        daily_loss=record.daily_loss,
        bump_interaction=False,
        refresh_positive=False,
        user_name=user_name,
    )
    logger.info(t("log.ai.relationship_admin_set", user=user_id, before=before, after=new_score))
    return SettleOutcome(
        new_score - before,
        REASON_ADMIN_SET,
        before,
        new_score,
        view_from_score(new_score, is_master_target),
        None,
        wrote=True,
    )
