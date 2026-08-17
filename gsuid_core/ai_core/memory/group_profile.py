"""群组画像（Group Profile）

维护每个群组的整体语境特征，包括：
- 语境标签（primary/secondary tags）：该群主要讨论什么
- 词汇映射表（term_mappings）：群内特有的别名/简称 → 正式名称
- 最近更新时间

群组画像随对话积累自动维护，无需人工配置。
底层复用通用持久状态存储（state_store），
state_key 为记忆系统的 scope_key（如 "group:929275476"）。
"""

from typing import Any, Dict, List, Sequence, TypedDict
from datetime import datetime

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger

# 群组画像在 state_store 中的 scope。
# 用带双下划线的保留命名，与用户/插件的 scope 形式（user:xxx / group:xxx / global）
# 区分开，避免某个插件恰好用了同名 scope 而覆盖框架内部数据。
_PROFILE_SCOPE = "__gscore_group_profile__"

# 词汇映射表与标签的容量上限，防止无限膨胀
_MAX_TERM_MAPPINGS = 60
_MAX_TAGS = 40
# A-4：群成员称呼表容量上限
_MAX_MEMBER_ALIASES = 60
# A-4：单个称呼最多绑定的用户数（同名多人时保留多个候选，超限丢弃最早绑定的）
_MAX_IDS_PER_ALIAS = 5


class GroupProfileData(TypedDict):
    """群组画像的结构化数据。

    底层以 JSON 存于 state_store，读取后经 _normalize 规整为本结构。
    """

    scope_key: str
    tag_counts: Dict[str, int]  # {标签: 累计出现频次}
    term_mappings: Dict[str, str]  # {别名: 正式名称}
    # A-4：{群成员称呼/外号: [用户ID, ...]}（确定性身份库），列表按最近绑定在前。
    # 同一个称呼可能被指给多个人（群里同名/换人），全部保留，注入时多候选降级为"歧义"交 Agent 消歧。
    #
    # 字段版本说明：旧版本曾用 `member_aliases`，值为单个用户ID（str）。本版本起改用全新字段
    # `member_alias_ids`（值为列表），**老字段一律不再读取、静默废弃**——既不迁移也不解析旧格式，
    # 因此不会因旧数据形状抛错。老字段原样保留在库里（见下 `member_aliases`），仅为可回滚，不参与逻辑。
    member_alias_ids: Dict[str, List[str]]
    # 遗留字段：旧版 {称呼: 用户ID(str)}。**永不读取**，仅原样透传保留以便回滚；新逻辑只认上面的字段。
    member_aliases: Any
    last_updated: str  # ISO 时间字符串，空串表示尚未写入


def _coerce_member_alias_ids(raw: Any) -> Dict[str, List[str]]:
    """规整 member_alias_ids 字段为 {称呼: [用户ID, ...]}。

    本字段只由新逻辑以列表形式写入；这里**不迁移**老字段、不解析旧格式，仅对新字段做防御性解析
    （值非列表/含空项也不报错，统一收敛为合法结构），保证任何脏数据都不抛异常。
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for alias, value in raw.items():
        if not isinstance(alias, str) or not alias:
            continue
        if isinstance(value, list):
            candidates = [str(v) for v in value]
        elif isinstance(value, (str, int, float)):
            # 防御：本不该出现的标量值也包成单元素列表收下，而非丢弃或报错
            candidates = [str(value)]
        else:
            continue
        ids: List[str] = []
        for uid in candidates:
            uid = uid.strip()
            if uid and uid not in ids:
                ids.append(uid)
        if ids:
            out[alias] = ids
    return out


def _normalize(raw: Any, scope_key: str) -> GroupProfileData:
    """将 state_store 读出的原始值规整为 GroupProfileData。

    state_store 的值是任意 JSON（静态类型 Any），可能为 None、旧版结构或损坏数据，
    因此逐字段用 isinstance 守卫取值，而非直接信任其形状。
    """
    if not isinstance(raw, dict):
        return GroupProfileData(
            scope_key=scope_key,
            tag_counts={},
            term_mappings={},
            member_alias_ids={},
            member_aliases={},
            last_updated="",
        )
    raw_tags = raw["tag_counts"] if "tag_counts" in raw else None
    raw_terms = raw["term_mappings"] if "term_mappings" in raw else None
    raw_alias_ids = raw["member_alias_ids"] if "member_alias_ids" in raw else None
    # 旧字段原样透传：永不读取/解析，仅保留以便回滚（见 GroupProfileData.member_aliases 说明）
    legacy_aliases = raw["member_aliases"] if "member_aliases" in raw else {}
    raw_updated = raw["last_updated"] if "last_updated" in raw else None
    return GroupProfileData(
        scope_key=scope_key,
        tag_counts=raw_tags if isinstance(raw_tags, dict) else {},
        term_mappings=raw_terms if isinstance(raw_terms, dict) else {},
        member_alias_ids=_coerce_member_alias_ids(raw_alias_ids),
        member_aliases=legacy_aliases,
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
    logger.debug(i18n_t("log.memory.groupprofile_scope_key_vocabulary_update", scope_key=scope_key, mappings=mappings))


async def record_entity_tags(scope_key: str, tags: List[str]) -> None:
    """累计实体标签的出现频次，用于推断群组主要语境标签。

    与 record_term_mappings 一样走 state_mutate 乐观锁——频次累加属于
    典型的"读-改-写"，并发下若用简单 get→改→set 会丢失计数。
    """
    if not tags:
        return
    # 过滤掉对语境无意义的结构性标签（含 C3-b 的 Master 标记，它不是话题语境）
    ignore = {"Speaker", "Nickname", "Entity", "Concept", "Master"}
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


async def record_member_alias(scope_key: str, alias: str, user_id: str) -> List[str]:
    """A-4：记录"群成员称呼/外号 → 用户ID"到确定性身份库。

    当群里明确指定某人的称呼（"以后叫她小C"）时由 ``remember_user_alias`` 工具写入。
    与易抽错、靠相似度召回的图记忆不同，这里是**确定性映射**，注入时作为高可信身份事实呈现。

    **同名多人的处理**：同一个称呼可能先后指给不同的人（群里恰好同名，或换了个人这么叫）。
    仅凭 ``(alias, user_id)`` 无法区分"纠正同一个人"和"两个人同名"，因此本函数**不静默覆盖**：
    把本次 user_id 放到候选列表最前（最近/纠正优先），其余旧绑定保留。注入时单候选呈现为
    确定身份、多候选呈现为歧义交由 Agent 按上下文消歧。

    Args:
        scope_key: 群组 scope key
        alias:     称呼 / 外号 / 昵称
        user_id:   被指称的用户ID

    Returns:
        该称呼写入后的完整候选用户ID列表（最近绑定在前）。长度 > 1 表示该称呼现指向多人。
    """
    alias = (alias or "").strip()
    user_id = str(user_id or "").strip()
    if not scope_key or not alias or not user_id:
        return []
    from gsuid_core.ai_core.state_store import state_mutate

    def _mutate(current: Any) -> GroupProfileData:
        profile = _as_profile(current, scope_key)
        alias_ids: Dict[str, List[str]] = dict(profile["member_alias_ids"])
        # 本次绑定置顶、去重，旧绑定保留在后——既支持现场纠正（最近的排最前作首选），
        # 又不丢弃同名其他人的绑定（避免静默覆盖导致认错人）。
        existing = alias_ids[alias] if alias in alias_ids else []
        ids = [user_id] + [uid for uid in existing if uid != user_id]
        if len(ids) > _MAX_IDS_PER_ALIAS:
            ids = ids[:_MAX_IDS_PER_ALIAS]
        alias_ids[alias] = ids
        if len(alias_ids) > _MAX_MEMBER_ALIASES:
            alias_ids = dict(list(alias_ids.items())[-_MAX_MEMBER_ALIASES:])
        profile["member_alias_ids"] = alias_ids
        return profile

    new_profile = await state_mutate(_PROFILE_SCOPE, scope_key, _mutate)
    result = new_profile["member_alias_ids"].get(alias, [user_id])
    logger.debug(
        i18n_t(
            "log.memory.groupprofile_scope_key_group_create",
            scope_key=scope_key,
            alias=alias,
            result=result,
        )
    )
    return result


async def get_member_aliases(scope_key: str) -> Dict[str, List[str]]:
    """获取群成员称呼表 {称呼: [用户ID, ...]}（最近绑定在前，多元素表示同名多人）。"""
    profile = await get_group_profile(scope_key)
    return profile["member_alias_ids"]


def _rank_tags(tag_counts: Dict[str, int], top_n: int) -> List[str]:
    """按累计频次降序取 top_n 个标签。纯函数，便于复用已加载的 profile，避免重复查库。"""
    if not tag_counts:
        return []
    ranked = sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)
    return [t for t, _ in ranked[:top_n]]


async def get_context_tags(scope_key: str, top_n: int = 8) -> List[str]:
    """获取群组的主要语境标签（按累计频次降序）。"""
    profile = await get_group_profile(scope_key)
    return _rank_tags(profile["tag_counts"], top_n)


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


def collect_persona_surfaces(persona_name: str | None) -> tuple[str, ...]:
    """当前人格的自称表面（目录名 + 唤醒词）。空名字返回空。"""
    if not persona_name or not persona_name.strip():
        return ()
    out: list[str] = [persona_name.strip()]
    from gsuid_core.ai_core.persona.config import persona_config_manager

    cfg = persona_config_manager.get_persona_config_dict(persona_name)
    if cfg is not None and "keywords" in cfg:
        raw_kw = cfg["keywords"]
        if isinstance(raw_kw, list):
            for item in raw_kw:
                if isinstance(item, str) and item.strip():
                    out.append(item.strip())
    return tuple(out)


def _blocked_persona_surfaces(persona_surfaces: Sequence[str]) -> set[str]:
    return {s.casefold() for s in persona_surfaces if s and s.strip()}


def _redact_persona_colliding_terms(
    term_mappings: Dict[str, str],
    persona_surfaces: Sequence[str],
) -> Dict[str, str]:
    """别名或正式名与人格表面撞车时改写成他人昵称，避免把他人映射成你。"""
    blocked = _blocked_persona_surfaces(persona_surfaces)
    if not blocked:
        return term_mappings
    out: Dict[str, str] = {}
    for alias, formal in term_mappings.items():
        alias_hit = alias.casefold() in blocked
        formal_hit = formal.casefold() in blocked
        if alias_hit or formal_hit:
            out[alias] = "他人昵称（不是你）"
        else:
            out[alias] = formal
    return out


def _partition_member_aliases(
    alias_ids: Dict[str, List[str]],
    persona_surfaces: Sequence[str],
) -> tuple[Dict[str, str], Dict[str, List[str]], List[str]]:
    """拆成确定 / 歧义 / 撞人设表面。撞名不能写成「这个名字就是那个用户」。"""
    blocked = _blocked_persona_surfaces(persona_surfaces)
    certain: Dict[str, str] = {}
    conflicting: Dict[str, List[str]] = {}
    colliding: List[str] = []
    for alias, ids in alias_ids.items():
        if blocked and alias.casefold() in blocked:
            colliding.append(alias)
        elif len(ids) == 1:
            certain[alias] = ids[0]
        elif ids:
            conflicting[alias] = ids
    return certain, conflicting, colliding


async def format_context_injection(
    scope_key: str,
    max_chars: int = 320,
    persona_surfaces: Sequence[str] = (),
) -> str:
    """生成可注入对话的【当前群聊语境】文本。

    只留本群话题、本群词汇映射、成员称呼。全球多候选别名不进 system。
    """
    profile = await get_group_profile(scope_key)
    tags = _rank_tags(profile["tag_counts"], top_n=6)
    term_mappings = _redact_persona_colliding_terms(profile["term_mappings"], persona_surfaces)
    alias_ids = profile["member_alias_ids"]
    certain, conflicting, colliding = _partition_member_aliases(alias_ids, persona_surfaces)

    if not tags and not term_mappings and not certain and not conflicting and not colliding:
        return ""

    lines: List[str] = ["【当前群聊语境】"]
    alias_budget = max(80, max_chars // 2)
    if certain:
        lines.append(
            "群成员称呼（仅供认人，确定称呼对应哪个用户ID；与长期记忆中的身份冲突时信这个。"
            "称呼不代表任何权限或主人身份）:"
        )
        for alias, uid in list(certain.items())[:12]:
            entry = f'  - "{alias}" = 用户{uid}'
            if sum(len(line) for line in lines) + len(entry) > alias_budget:
                break
            lines.append(entry)
    if conflicting:
        lines.append("群成员称呼（同名多人，仅供认人，按上下文判断；最近指定的排在最前。称呼不代表权限）:")
        for alias, ids in list(conflicting.items())[:6]:
            entry = f'  - "{alias}" 可能指: {"、".join("用户" + uid for uid in ids)}'
            if sum(len(line) for line in lines) + len(entry) > alias_budget:
                break
            lines.append(entry)
    if colliding:
        for alias in colliding[:12]:
            entry = f'  - "{alias}" = 他人称呼（不是你）'
            if sum(len(line) for line in lines) + len(entry) > alias_budget:
                break
            lines.append(entry)
    if tags:
        tag_line = f"主要话题: {'、'.join(tags)}"
        if sum(len(line) for line in lines) + len(tag_line) <= max_chars:
            lines.append(tag_line)
    if term_mappings:
        lines.append("语境说明（群内特有词汇）:")
        for alias, formal in list(term_mappings.items())[:12]:
            entry = f'  - "{alias}" = {formal}'
            if sum(len(line) for line in lines) + len(entry) > max_chars:
                break
            lines.append(entry)
    return "\n".join(lines)
