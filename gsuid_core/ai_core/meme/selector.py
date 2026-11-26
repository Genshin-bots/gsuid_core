"""表情包选择器

按真实余弦分 + 结构化词面分综合排序，收紧 Top-4 窄池并做相对分差截断；
对紧邻最近 1–2 张硬防连发、其余最近 10 张软衰减，并支持纯表情预选与延迟落账。
"""

from __future__ import annotations

import math
import time
import random
from typing import Dict, List, Tuple, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.meme.library import ScoredMeme, MemeLibrary
from gsuid_core.ai_core.meme.database_model import AiMemeRecord

# 发送冷却记录 {session_id: last_send_timestamp}
_send_cooldowns: Dict[str, float] = {}

# {session_id: [meme_id, ...]} 最近发送的 meme_id 列表
_recent_sent: Dict[str, List[str]] = {}

RELATIVE_SCORE_RATIO: float = 0.78
SOFT_RECENT_DECAY: float = 0.25

# pick() 的失败原因，供调用方做日志或纯表情重说决策
PICK_OK = "ok"
PICK_COOLDOWN = "cooldown"
PICK_DISABLED = "disabled"
PICK_NO_PERSONA = "no_persona"
PICK_NO_BIND = "no_bind"
PICK_NO_MATCH = "no_match"
PICK_EXHAUSTED = "exhausted"


def _is_on_cooldown(session_id: str, cooldown_sec: int) -> bool:
    """检查会话是否在冷却中。"""
    if cooldown_sec <= 0:
        return False
    last_send = _send_cooldowns[session_id] if session_id in _send_cooldowns else 0.0
    return (time.time() - last_send) < cooldown_sec


def _record_send(session_id: str) -> None:
    """记录发送时间。"""
    _send_cooldowns[session_id] = time.time()


def _get_recent_sent(session_id: str, count: int) -> List[str]:
    """获取最近发送的 meme_id 列表。"""
    if session_id not in _recent_sent:
        return []
    return _recent_sent[session_id][-count:]


def _add_recent_sent(session_id: str, meme_id: str, max_count: int) -> None:
    """添加到最近发送记录。"""
    if session_id not in _recent_sent:
        _recent_sent[session_id] = []
    _recent_sent[session_id].append(meme_id)
    if len(_recent_sent[session_id]) > max_count:
        _recent_sent[session_id] = _recent_sent[session_id][-max_count:]


def commit_pick(session_id: str, meme_id: str) -> None:
    """真正出站后落账冷却与最近发送列表。"""
    from gsuid_core.ai_core.meme.config import MEME_RECENT_EXCLUDE_COUNT

    _record_send(session_id)
    _add_recent_sent(session_id, meme_id, MEME_RECENT_EXCLUDE_COUNT)


def select_from_scored(
    scored: List[ScoredMeme],
    recent: List[str],
    *,
    threshold: float,
    allow_immediate_repeat: bool = False,
) -> Tuple[Optional[AiMemeRecord], str]:
    """窄池相对分差截断 + 分级硬防连发 + 软衰减加权采样。"""
    if not scored:
        return None, PICK_NO_MATCH

    best = scored[0].score
    cutoff = max(threshold, best * RELATIVE_SCORE_RATIO)
    pool = [item for item in scored if item.score >= cutoff]
    if not pool:
        pool = [scored[0]]

    if len(pool) >= 3:
        hard_exclude = set(recent[-2:])
    elif len(pool) == 2:
        hard_exclude = set(recent[-1:])
    else:
        only_id = pool[0].record.meme_id
        if recent and only_id == recent[-1] and not allow_immediate_repeat:
            hard_exclude = {only_id}
        else:
            hard_exclude = set()

    eligible = [item for item in pool if item.record.meme_id not in hard_exclude]
    if not eligible:
        return None, PICK_EXHAUSTED

    recent_set = set(recent)
    weights = [
        max(1e-6, (item.score**2))
        * (SOFT_RECENT_DECAY if item.record.meme_id in recent_set else 1.0)
        / (1.0 + 0.25 * math.log1p(max(0, item.record.use_count)))
        for item in eligible
    ]
    chosen = random.choices(eligible, weights=weights, k=1)[0].record
    return chosen, PICK_OK


async def pick(
    query: str,
    persona: str | None,
    session_id: str,
    *,
    scene: str = "",
    ignore_cooldown: bool = False,
    commit: bool = True,
    is_pure_meme: bool = False,
) -> Tuple[Optional[AiMemeRecord], str]:
    """从该人格已绑定的包里选一张可发送表情。没有绑定行则不发。"""
    if persona is None or persona == "":
        return None, PICK_NO_PERSONA

    from gsuid_core.ai_core.persona.meme_style import resolve_meme_style

    style = resolve_meme_style(persona)
    if not style.enabled:
        return None, PICK_DISABLED

    if not ignore_cooldown and _is_on_cooldown(session_id, style.cooldown_sec):
        logger.debug(t("log.meme.session_id_cooling_skipping", session_id=session_id))
        return None, PICK_COOLDOWN

    from gsuid_core.ai_core.meme.config import (
        MEME_PICK_CANDIDATE_COUNT,
        MEME_RECENT_EXCLUDE_COUNT,
        meme_config,
    )
    from gsuid_core.ai_core.meme.pack_model import PackStore

    pack_ids = await PackStore.pack_ids_for_persona(persona)
    if not pack_ids:
        return None, PICK_NO_BIND

    recent = _get_recent_sent(session_id, MEME_RECENT_EXCLUDE_COUNT)
    query_text = f"{query} {scene}".strip()
    threshold = float(meme_config.get_config("meme_search_threshold").data)

    if query_text:
        scored_raw = await MemeLibrary.search_scored(
            query_text=query_text,
            pack_ids=pack_ids,
            top_k=MEME_PICK_CANDIDATE_COUNT,
            score_threshold=threshold,
        )
        confirmed_records = await AiMemeRecord.confirm_sendable(
            [item.record.meme_id for item in scored_raw],
            pack_ids,
        )
        score_by_id = {item.record.meme_id: item for item in scored_raw}
        confirmed_scored = [
            ScoredMeme(
                record=rec,
                score=score_by_id[rec.meme_id].score,
                dense_score=score_by_id[rec.meme_id].dense_score,
                lexical_score=score_by_id[rec.meme_id].lexical_score,
            )
            for rec in confirmed_records
            if rec.meme_id in score_by_id
        ]
        chosen, reason = select_from_scored(
            confirmed_scored,
            recent,
            threshold=threshold,
            allow_immediate_repeat=ignore_cooldown or is_pure_meme,
        )
        if chosen is not None:
            if commit:
                commit_pick(session_id, chosen.meme_id)
            return chosen, PICK_OK
        logger.debug(
            t(
                "log.meme.matching_found_mood",
                mood=query,
                scene=scene,
                persona=persona,
            )
        )
        return None, reason

    record = await AiMemeRecord.random_pick_in_packs(pack_ids, recent[-2:] if recent else None)
    if record is None:
        record = await AiMemeRecord.least_used_pick_in_packs(pack_ids, recent[-1:] if recent else None)
    if record is None:
        record = await AiMemeRecord.least_used_pick_in_packs(pack_ids, None)
    if record is None:
        return None, PICK_NO_MATCH
    if commit:
        commit_pick(session_id, record.meme_id)
    return record, PICK_OK
