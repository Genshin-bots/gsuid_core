"""群组画像（Group Profile）

维护每个群组的整体语境特征，包括：
- 语境标签（primary/secondary tags）：该群主要讨论什么
- 词汇映射表（term_mappings）：群内特有的别名/简称 → 正式名称
- 最近更新时间

群组画像随对话积累自动维护，无需人工配置。
底层复用通用持久状态存储（state_store），
state_key 为记忆系统的 scope_key（如 "group:929275476"）。
"""

from typing import Any, Dict, List, TypedDict
from datetime import datetime

from gsuid_core.logger import logger

# 群组画像在 state_store 中的 scope。
# 用带双下划线的保留命名，与用户/插件的 scope 形式（user:xxx / group:xxx / global）
# 区分开，避免某个插件恰好用了同名 scope 而覆盖框架内部数据。
_PROFILE_SCOPE = "__gscore_group_profile__"

# 词汇映射表与标签的容量上限，防止无限膨胀
_MAX_TERM_MAPPINGS = 60
_MAX_TAGS = 40


class GroupProfileData(TypedDict):
    """群组画像的结构化数据。

    底层以 JSON 存于 state_store，读取后经 _normalize 规整为本结构。
    """

    scope_key: str
    tag_counts: Dict[str, int]  # {标签: 累计出现频次}
    term_mappings: Dict[str, str]  # {别名: 正式名称}
    last_updated: str  # ISO 时间字符串，空串表示尚未写入


def _normalize(raw: Any, scope_key: str) -> GroupProfileData:
    """将 state_store 读出的原始值规整为 GroupProfileData。

    state_store 的值是任意 JSON（静态类型 Any），可能为 None、旧版结构或损坏数据，
    因此逐字段用 isinstance 守卫取值，而非直接信任其形状。
    """
    if not isinstance(raw, dict):
        return GroupProfileData(scope_key=scope_key, tag_counts={}, term_mappings={}, last_updated="")
    raw_tags = raw["tag_counts"] if "tag_counts" in raw else None
    raw_terms = raw["term_mappings"] if "term_mappings" in raw else None
    raw_updated = raw["last_updated"] if "last_updated" in raw else None
    return GroupProfileData(
        scope_key=scope_key,
        tag_counts=raw_tags if isinstance(raw_tags, dict) else {},
        term_mappings=raw_terms if isinstance(raw_terms, dict) else {},
        last_updated=raw_updated if isinstance(raw_updated, str) else "",
    )


async def get_group_profile(scope_key: str) -> GroupProfileData:
    """读取群组画像，不存在时返回空结构。"""
    from gsuid_core.ai_core.state_store import state_get_value

    raw = await state_get_value(_PROFILE_SCOPE, scope_key)
    return _normalize(raw, scope_key)


def _as_profile(current: Any, scope_key: str) -> GroupProfileData:
    """把 state_mutate 传入的当前值规整为完整 profile 结构并刷新更新时间。"""
    profile = _normalize(current, scope_key)
    profile["last_updated"] = datetime.now().isoformat(timespec="seconds")
    return profile


async def record_term_mappings(scope_key: str, mappings: Dict[str, str]) -> None:
    """记录一批别名 → 正式名称的映射到群组画像。

    通过 state_store 的 state_mutate 乐观锁完成读-改-写，避免并发摄入时
    多个 worker 同时读到旧画像、各自写回导致互相覆盖。

    Args:
        scope_key: 记忆系统的 scope_key
        mappings: {别名: 正式名称}
    """
    if not mappings:
        return
    from gsuid_core.ai_core.state_store import state_mutate

    def _mutate(current: Any) -> GroupProfileData:
        profile = _as_profile(current, scope_key)
        term_mappings: Dict[str, str] = dict(profile["term_mappings"])
        for alias, formal in mappings.items():
            if alias and formal:
                term_mappings[alias] = formal
        # 容量控制：超限时丢弃最早写入的映射
        if len(term_mappings) > _MAX_TERM_MAPPINGS:
            term_mappings = dict(list(term_mappings.items())[-_MAX_TERM_MAPPINGS:])
        profile["term_mappings"] = term_mappings
        return profile

    await state_mutate(_PROFILE_SCOPE, scope_key, _mutate)
    logger.debug(f"🧠 [GroupProfile] {scope_key} 词汇映射已更新: {mappings}")


async def record_entity_tags(scope_key: str, tags: List[str]) -> None:
    """累计实体标签的出现频次，用于推断群组主要语境标签。

    与 record_term_mappings 一样走 state_mutate 乐观锁——频次累加属于
    典型的"读-改-写"，并发下若用简单 get→改→set 会丢失计数。
    """
    if not tags:
        return
    # 过滤掉对语境无意义的结构性标签
    ignore = {"Speaker", "Nickname", "Entity", "Concept"}
    meaningful = [t for t in tags if t and t not in ignore]
    if not meaningful:
        return

    from gsuid_core.ai_core.state_store import state_mutate

    def _mutate(current: Any) -> GroupProfileData:
        profile = _as_profile(current, scope_key)
        tag_counts: Dict[str, int] = dict(profile["tag_counts"])
        for t in meaningful:
            tag_counts[t] = (tag_counts[t] if t in tag_counts else 0) + 1
        # 容量控制：只保留频次最高的 N 个标签
        if len(tag_counts) > _MAX_TAGS:
            tag_counts = dict(sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_TAGS])
        profile["tag_counts"] = tag_counts
        return profile

    await state_mutate(_PROFILE_SCOPE, scope_key, _mutate)


async def get_term_mappings(scope_key: str) -> Dict[str, str]:
    """获取群组的词汇映射表。"""
    profile = await get_group_profile(scope_key)
    return profile["term_mappings"]


async def get_context_tags(scope_key: str, top_n: int = 8) -> List[str]:
    """获取群组的主要语境标签（按累计频次降序）。"""
    profile = await get_group_profile(scope_key)
    tag_counts = profile["tag_counts"]
    if not tag_counts:
        return []
    ranked = sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)
    return [t for t, _ in ranked[:top_n]]


def expand_query_with_aliases(query: str, term_mappings: Dict[str, str]) -> str:
    """若 query 中出现别名，则在末尾附加其正式名称，提升记忆检索召回。"""
    if not query or not term_mappings:
        return query
    appended: List[str] = []
    for alias, formal in term_mappings.items():
        if alias and alias in query and formal and formal not in query:
            appended.append(formal)
    if not appended:
        return query
    return f"{query} {' '.join(dict.fromkeys(appended))}"


async def format_context_injection(
    scope_key: str,
    max_chars: int = 400,
) -> str:
    """生成可注入对话的【当前群聊语境】文本。

    包含主要话题与词汇映射说明，让 Agent 无需自行推理就知道
    "深渊"在本群指什么、某个外号对应哪个角色。

    Args:
        scope_key: 群组 scope key
        max_chars: 最大字符数限制，默认 400，超出则截断词汇映射条目
    """
    profile = await get_group_profile(scope_key)
    tags = await get_context_tags(scope_key, top_n=6)
    term_mappings = profile["term_mappings"]

    if not tags and not term_mappings:
        return ""

    lines: List[str] = ["【当前群聊语境】"]
    if tags:
        lines.append(f"主要话题: {'、'.join(tags)}")
    if term_mappings:
        lines.append("语境说明（群内特有词汇）:")
        # 按频次降序截断，超预算则停止添加词汇映射
        for alias, formal in list(term_mappings.items())[:12]:
            entry = f'  - "{alias}" = {formal}'
            if sum(len(line) for line in lines) + len(entry) > max_chars:
                break
            lines.append(entry)
    return "\n".join(lines)
