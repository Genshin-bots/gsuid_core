"""表情包选择器

MemeSelector 负责根据情境检索和选择合适的表情包发送。
优先使用向量检索，降级为随机选取。
"""

import time
from typing import Dict, List, Optional, Sequence

from gsuid_core.logger import logger
from gsuid_core.ai_core.meme.config import meme_config
from gsuid_core.ai_core.meme.library import MemeLibrary
from gsuid_core.ai_core.meme.database_model import AiMemeRecord

# 发送冷却记录 {session_id: last_send_timestamp}
_send_cooldowns: Dict[str, float] = {}


def _is_on_cooldown(session_id: str) -> bool:
    """检查会话是否在冷却中"""
    cooldown_sec: int = meme_config.get_config("meme_send_cooldown_sec").data
    last_send = _send_cooldowns.get(session_id, 0.0)
    return (time.time() - last_send) < cooldown_sec


def _record_send(session_id: str) -> None:
    """记录发送时间"""
    _send_cooldowns[session_id] = time.time()


def _pick_from(
    records: Sequence[AiMemeRecord],
    exclude_ids: List[str],
) -> Optional[AiMemeRecord]:
    """从候选列表中选取一张未被排除的表情包

    Args:
        records: 候选记录列表
        exclude_ids: 需要排除的 meme_id 列表

    Returns:
        选中的记录或 None
    """
    for record in records:
        if record.meme_id not in exclude_ids:
            return record
    return None


async def pick(
    mood: str,
    scene: str,
    persona: str,
    session_id: str,
) -> Optional[AiMemeRecord]:
    """根据情境选择一张表情包

    检索策略：
    1. 用 mood + scene 生成向量查询 Qdrant（带相似度阈值过滤）
    2. 过滤 folder 为 persona_{persona} 或 common，排除最近 N 条已发
    3. 仅当无查询文本时，降级为随机/最久未使用选取
    4. 若无匹配图片，返回 None（不会强行发不相关的图）

    Args:
        mood: 情绪描述（如 "开心", "无语"）
        scene: 场景描述（如 "吐槽", "安慰"）
        persona: 当前 persona 名称
        session_id: 会话 ID

    Returns:
        选中的 AiMemeRecord 或 None
    """
    # 冷却检查
    if _is_on_cooldown(session_id):
        logger.debug(f"[Meme] 会话 {session_id} 在冷却中，跳过发送")
        return None

    # 排除最近已发的图片
    from gsuid_core.ai_core.meme.config import MEME_RECENT_EXCLUDE_COUNT

    exclude_ids = _get_recent_sent(session_id, MEME_RECENT_EXCLUDE_COUNT)

    query_text = f"{mood} {scene}".strip()

    # 按优先级检索：先 persona 专属，再通用库
    for folder in [f"persona_{persona}", "common"]:
        if query_text:
            # 向量检索（带相似度阈值，低于阈值的结果会被过滤）
            results = await MemeLibrary.search_by_text(
                query_text=query_text,
                folder=folder,
                top_k=3,
            )
            record = _pick_from(results, exclude_ids)
            if record:
                _record_send(session_id)
                _add_recent_sent(session_id, record.meme_id, MEME_RECENT_EXCLUDE_COUNT)
                return record
            # 向量检索无匹配 → 不降级到随机，继续检查下一个 folder
            logger.debug(f"[Meme] 向量检索无匹配: folder={folder}, query={query_text!r}")
            continue

        # 仅当无查询文本时才降级：随机选取
        record = await AiMemeRecord.random_pick(folder, exclude_ids)
        if record:
            _record_send(session_id)
            _add_recent_sent(session_id, record.meme_id, MEME_RECENT_EXCLUDE_COUNT)
            return record

        # 再降级：最久未使用
        record = await AiMemeRecord.least_used_pick(folder, exclude_ids)
        if record:
            _record_send(session_id)
            _add_recent_sent(session_id, record.meme_id, MEME_RECENT_EXCLUDE_COUNT)
            return record

    logger.debug(f"[Meme] 未找到匹配的表情包: mood={mood}, scene={scene}, persona={persona}")
    return None


# ── 最近发送记录管理 ──

# {session_id: [meme_id, ...]} 最近发送的 meme_id 列表
_recent_sent: Dict[str, List[str]] = {}


def _get_recent_sent(session_id: str, count: int) -> List[str]:
    """获取最近发送的 meme_id 列表"""
    return _recent_sent.get(session_id, [])[-count:]


def _add_recent_sent(session_id: str, meme_id: str, max_count: int) -> None:
    """添加到最近发送记录"""
    if session_id not in _recent_sent:
        _recent_sent[session_id] = []
    _recent_sent[session_id].append(meme_id)
    # 只保留最近 max_count 条
    if len(_recent_sent[session_id]) > max_count:
        _recent_sent[session_id] = _recent_sent[session_id][-max_count:]
