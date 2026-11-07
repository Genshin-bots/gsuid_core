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
    NegSignal,
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


_DAILY_LOSS_CAP: int = 20


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
        remaining = _DAILY_LOSS_CAP - daily_loss
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
    is_master: bool = False,
    guard_flags: "GuardFlags | None" = None,
    user_name: str = "",
    persona_name: str = "",
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
        is_master=is_master,
        guard=guard_flags,
        meaningful_min_len=_cfg_int("favor_meaningful_min_len"),
    )

    if not engine_enabled():
        view = await _read_view(user_id, bot_id, persona_name, is_master)
        return SettleOutcome(0, REASON_NO_SIGNAL, view.score, view.score, view, signals, wrote=False)

    existing = await UserFavorability.get_user_favorability(user_id, bot_id, persona_name)
    score_before = existing.favorability if existing is not None else 0
    now_ts = int(time.time())
    today = _today()

    # 换日：清空日预算
    if existing is None or existing.daily_ymd != today:
        daily_gain = 0
        daily_loss = 0
    else:
        daily_gain = existing.daily_gain
        daily_loss = existing.daily_loss

    # 「当日首次有内容」= 今天还没涨过分。一天一次，不看具体是哪条正信号先命中。
    first_meaningful_today = daily_gain == 0

    # 会话窗节流：距最近一次正向互动不足窗口且当日已吃满窗口配额 → 普通正信号不再加
    window_seconds = _cfg_int("favor_session_window_minutes") * 60
    session_gain_used = (
        existing is not None
        and existing.last_positive_interact_at > 0
        and now_ts - existing.last_positive_interact_at < window_seconds
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

    # 未打分用户若本轮无分值变化且无负向信号，不落库空 0 分，保持 unscored（打过照面的群友）
    if existing is None and delta == 0 and not signals.has_negative:
        logger.debug(t("log.ai.relationship_settle_noop", user=user_id, reason=reason))
        view = view_from_score(None, is_master)
        return SettleOutcome(0, reason, 0, 0, view, signals, wrote=False)

    new_score = _clamp(score_before + delta)
    applied = new_score - score_before
    # interaction_count 只在有效互动时 +1；管理侧 set 不冒充互动
    bump_interaction = bool(effective and not silenced and not error)
    refresh_positive = bool(applied > 0 or (bump_interaction and not signals.has_negative))

    await UserFavorability.apply_settlement(
        user_id,
        bot_id,
        persona_name,
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

    view = view_from_score(
        new_score,
        is_master,
        vibe=(existing.interaction_vibe or "") if existing is not None else "",
        callbacks=tuple(existing.shared_callbacks_list) if existing is not None else (),
    )
    return SettleOutcome(applied, reason, score_before, new_score, view, signals, wrote=True)


async def _read_view(user_id: str, bot_id: str, persona_name: str, is_master: bool) -> RelationshipView:
    from gsuid_core.ai_core.database.models import UserFavorability

    record = await UserFavorability.get_user_favorability(user_id, bot_id, persona_name)
    if record is None:
        return view_from_score(None, is_master)
    return view_from_score(
        record.favorability,
        is_master,
        vibe=record.interaction_vibe or "",
        callbacks=tuple(record.shared_callbacks_list),
    )


async def apply_model_delta(
    *,
    user_id: str,
    bot_id: str,
    delta: int,
    user_name: str = "",
    persona_name: str = "",
) -> SettleOutcome:
    """内部增量（吃与框架结算同一份日预算）。模型侧增量工具已删除，勿再接回工具表。"""
    from gsuid_core.ai_core.database.models import UserFavorability

    if not engine_enabled() or delta == 0:
        view = await _read_view(user_id, bot_id, persona_name, False)
        return SettleOutcome(0, REASON_NO_SIGNAL, view.score, view.score, view, None, wrote=False)

    record = await UserFavorability.get_or_create_user_favorability(user_id, bot_id, persona_name, user_name)
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
        persona_name,
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
    return SettleOutcome(
        applied,
        reason,
        before,
        new_score,
        view_from_score(
            new_score,
            False,
            vibe=record.interaction_vibe or "",
            callbacks=tuple(record.shared_callbacks_list),
        ),
        None,
        wrote=True,
    )


async def apply_admin_set(
    *,
    user_id: str,
    bot_id: str,
    value: int,
    user_name: str = "",
    is_master_target: bool = False,
    persona_name: str = "",
) -> SettleOutcome:
    """管理侧绝对值覆盖（``set_user_favorability`` 工具，仅主人可调）。

    走引擎是为了让 ``last_reason`` / ``last_eval_at`` 有记录，但**不吃日预算**
    （管理操作不是互动），也不刷 ``last_positive_interact_at``。
    """
    from gsuid_core.ai_core.database.models import UserFavorability

    record = await UserFavorability.get_or_create_user_favorability(user_id, bot_id, persona_name, user_name)
    before = record.favorability
    new_score = _clamp(value)
    now_ts = int(time.time())
    await UserFavorability.apply_settlement(
        user_id,
        bot_id,
        persona_name,
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
        view_from_score(
            new_score,
            is_master_target,
            vibe=record.interaction_vibe or "",
            callbacks=tuple(record.shared_callbacks_list),
        ),
        None,
        wrote=True,
    )


async def settle_semantic(
    *,
    user_id: str,
    bot_id: str,
    persona_name: str,
    signal: NegSignal,
    evidence: str,
    user_name: str = "",
) -> SettleOutcome:
    """语义负信号异步结算（复用抽取调用产出，零额外 LLM 请求）。

    规则：
    - 主人（masters）不扣分；
    - 走与 settle_turn 相同的 clip_to_budget 和 apply_settlement；
    - 仅负信号扣分，teasing/friendly/neutral 不加分；
    - evidence 记入 last_reason。
    """
    from gsuid_core.config import core_config
    from gsuid_core.ai_core.database.models import UserFavorability

    clean_uid = user_id.strip()
    clean_bot = bot_id.strip()
    clean_persona = persona_name.strip()
    if not clean_uid or not clean_bot or not clean_persona or not engine_enabled():
        view = await _read_view(clean_uid, clean_bot, clean_persona, False)
        return SettleOutcome(0, REASON_NO_SIGNAL, view.score, view.score, view, None, wrote=False)

    raw_masters = core_config.get_config("masters")
    masters = (
        {str(m).strip() for m in raw_masters if isinstance(m, (str, int)) and not isinstance(m, bool)}
        if isinstance(raw_masters, list)
        else set()
    )
    is_master = clean_uid in masters
    if is_master:
        view = await _read_view(clean_uid, clean_bot, clean_persona, True)
        return SettleOutcome(0, "master_exempt", view.score, view.score, view, None, wrote=False)

    if signal not in (NegSignal.PROVOKE, NegSignal.HOSTILE):
        view = await _read_view(clean_uid, clean_bot, clean_persona, False)
        return SettleOutcome(0, REASON_NO_SIGNAL, view.score, view.score, view, None, wrote=False)

    raw_delta = NEG_WEIGHTS[signal]

    existing = await UserFavorability.get_or_create_user_favorability(clean_uid, clean_bot, clean_persona, user_name)
    score_before = existing.favorability
    now_ts = int(time.time())
    today = _today()
    day_rolled = existing.daily_ymd != today
    daily_gain = 0 if day_rolled else existing.daily_gain
    daily_loss = 0 if day_rolled else existing.daily_loss

    delta = clip_to_budget(raw_delta, daily_gain=daily_gain, daily_loss=daily_loss)
    if delta == 0:
        view = view_from_score(score_before, is_master=False)
        return SettleOutcome(0, REASON_BUDGET, score_before, score_before, view, None, wrote=False)

    new_score = score_before + delta
    reason_str = f"{signal.value}:{evidence[:60]}" if evidence else signal.value
    await UserFavorability.apply_settlement(
        clean_uid,
        clean_bot,
        clean_persona,
        new_score=new_score,
        delta=delta,
        reason=reason_str,
        now_ts=now_ts,
        daily_ymd=today,
        daily_gain=daily_gain,
        daily_loss=daily_loss + abs(delta),
        bump_interaction=False,
        refresh_positive=False,
        user_name=user_name,
    )

    view_after = view_from_score(
        new_score,
        is_master=False,
        vibe=(existing.interaction_vibe or "") if existing is not None else "",
        callbacks=tuple(existing.shared_callbacks_list) if existing is not None else (),
    )
    return SettleOutcome(
        delta=delta,
        reason=signal.value,
        score_before=score_before,
        score_after=new_score,
        view=view_after,
        signals=None,
        wrote=True,
    )
