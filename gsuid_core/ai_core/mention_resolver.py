"""群聊消息提及人解析（Module E）。

在群聊场景中，当用户消息提到其他群成员名称或昵称时，
识别被提及成员并生成 user 侧提示，防止模型误以为在询问自身。
符合 AGENTS.md §1.9 与 §1.11。
"""

import time
from typing import Dict, List, Tuple, Optional

from sqlmodel import col, select

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.control.lines import (
    MENTIONED_USERS_PREFIX,
    MENTIONED_USERS_SUFFIX,
)
from gsuid_core.utils.database.models import CoreUser
from gsuid_core.utils.database.base_models import async_maker

# 过滤停用词、常见泛称与口语高频词（禁止硬编码特定角色名，见 §1.9）
_COMMON_STOPWORDS: set[str] = {
    "什么",
    "怎么",
    "如何",
    "为什么",
    "谁是",
    "哪个",
    "哪里",
    "喜欢",
    "讨厌",
    "觉得",
    "知道",
    "告诉",
    "大家",
    "有人",
    "机器人",
    "管理",
    "群主",
    "群友",
    "这个",
    "那个",
    "今天",
    "明天",
    "昨天",
    "现在",
    "结果",
    "其实",
    "可能",
    "感觉",
    "事情",
    "东西",
    "各位",
    "朋友",
    "帮忙",
    "可以",
    "好的",
    "开心",
    "难过",
    "大佬",
    "打卡",
    "吃饭",
    "睡觉",
    "明白",
    "同学",
    "老师",
    "师傅",
    "哥哥",
    "姐姐",
    "妹妹",
    "弟弟",
    "帅哥",
    "美女",
}

# 群成员称呼内存缓存：group_id -> (timestamp, {clean_name: user_id})
_GROUP_MEMBERS_CACHE: Dict[str, Tuple[float, Dict[str, str]]] = {}
_CACHE_TTL_SECONDS: float = 60.0
_MAX_CACHED_GROUPS: int = 1000


async def _get_group_members(group_id: str) -> Dict[str, str]:
    """获取指定群的成员名与别名映射，带 60 秒内存 TTL 缓存。"""
    now = time.time()
    cached = _GROUP_MEMBERS_CACHE.get(group_id)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    members: Dict[str, str] = {}
    if async_maker is not None:
        try:
            async with async_maker() as session:
                stmt = select(CoreUser.user_id, CoreUser.user_name).where(col(CoreUser.group_id) == group_id)
                res = await session.execute(stmt)
                for uid, name in res.all():
                    if uid and name:
                        clean_name = str(name).strip()
                        # 排除 1 字名、纯数字与高频停用词，防止数字与通用词误伤
                        if len(clean_name) >= 2 and not clean_name.isdigit() and clean_name not in _COMMON_STOPWORDS:
                            members[clean_name] = str(uid)
        except Exception as e:
            logger.debug(i18n_t("log.memory.resolve_mentioned_users_fail", e=str(e)))

    try:
        from gsuid_core.ai_core.memory.group_profile import get_member_aliases

        alias_map = await get_member_aliases(f"group:{group_id}")
        for alias, uids in alias_map.items():
            if alias and len(uids) == 1 and len(alias) >= 2 and alias not in _COMMON_STOPWORDS:
                target_uid = uids[0]
                if alias not in members:
                    members[alias] = target_uid
    except Exception as e:
        logger.debug(i18n_t("log.memory.resolve_mentioned_users_fail", e=str(e)))

    # 控制缓存最大容量，防止内存泄露
    if len(_GROUP_MEMBERS_CACHE) >= _MAX_CACHED_GROUPS:
        expired = [gid for gid, (ts, _) in _GROUP_MEMBERS_CACHE.items() if (now - ts) >= _CACHE_TTL_SECONDS]
        for gid in expired:
            _GROUP_MEMBERS_CACHE.pop(gid, None)
        if len(_GROUP_MEMBERS_CACHE) >= _MAX_CACHED_GROUPS:
            for gid in list(_GROUP_MEMBERS_CACHE.keys())[:100]:
                _GROUP_MEMBERS_CACHE.pop(gid, None)

    _GROUP_MEMBERS_CACHE[group_id] = (now, members)
    return members


async def resolve_mentioned_users(
    query: str,
    *,
    group_id: Optional[str],
    current_user_id: str = "",
    persona_name: Optional[str] = None,
) -> str:
    """解析当前群聊消息中提及的群成员。

    - 候选：CoreUser 中本群成员昵称及 group_profile 中的称呼映射；
    - 边界：分词词元精准匹配或长名子串匹配，排除发言者自身与当前人格名；
    - 最多解析 3 人。
    """
    if not group_id or not query:
        return ""

    q = query.strip()
    if len(q) < 2:
        return ""

    all_members = await _get_group_members(group_id)
    if not all_members:
        return ""

    # 排除发言者自身与当前人格名（动态排除，不写死角色，见 §1.9）
    active_candidates: Dict[str, str] = {}
    for name, uid in all_members.items():
        if uid == str(current_user_id):
            continue
        if persona_name and name == persona_name:
            continue
        active_candidates[name] = uid

    if not active_candidates:
        return ""

    # 优先使用 jieba 进行分词，精准命中词元，防止词中字误伤（§6.1）
    tokens: set[str] = set()
    try:
        import jieba

        tokens = {t.strip() for t in jieba.lcut(q) if t.strip()}
    except Exception:
        tokens = set()

    # 匹配提问中出现的成员（按名字长度倒序匹配，优先匹配长名）
    matches: List[str] = []
    seen_uids: set[str] = set()
    sorted_names = sorted(active_candidates.keys(), key=lambda n: len(n), reverse=True)
    for name in sorted_names:
        # 命中判据：分词词元精准命中，或 >=4 字特定名称完整包含
        is_hit = (name in tokens) if tokens else (name in q)
        if not is_hit and len(name) >= 4 and name in q:
            is_hit = True

        if is_hit:
            uid = active_candidates[name]
            if uid not in seen_uids:
                seen_uids.add(uid)
                matches.append(f"{name} = 群成员 {uid}")
                if len(matches) >= 3:
                    break

    if not matches:
        return ""

    return f"{MENTIONED_USERS_PREFIX}{', '.join(matches)}{MENTIONED_USERS_SUFFIX}"
