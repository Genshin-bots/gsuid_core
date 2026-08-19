"""自我认知（Self-Cognition）模块

落实 plans/agent_design_review.md 第 4 章 + C3：让 Bot "知道自己是什么样的人"、
"和当前对话者的关系"、"自己能做什么"。

架构约束（约束 1）：自我认知的**演化层绝不写入 persona 目录文件**——
那会触发 ai_router 的人格热重载、滚动销毁会话短期记忆。本模块把演化层存于
通用持久化 state_store（scope=`self:{bot_id}`），并由 handle_ai 在**每轮**
对话动态拼接为 `self_cognition_context` 注入 user message 侧，宪法层身份仍由
persona markdown 静态 system_prompt 承担。

self_model 结构（存于 state_store）::

    {
        "commitments": [str],  # 对用户作出的承诺
        "preferences_learned": [str],  # 观察 / 被告知的偏好（称呼、禁忌等）
        "recurring_topics": [str],  # 反复出现的话题
        "self_notes": [str],  # 复盘反思
    }
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

if TYPE_CHECKING:
    from gsuid_core.ai_core.relationship import RelationshipView

# state_store 中自我模型的 state_key
_SELF_MODEL_KEY = "self_model"
# 每个列表字段保留的最大条目数（写入限流：超出丢弃最早条目）
_MAX_ITEMS_PER_FIELD = 20
# 单条笔记最大字符数
_MAX_NOTE_CHARS = 200
# self_model 的合法列表字段
_FIELDS = ("commitments", "preferences_learned", "recurring_topics", "self_notes")
_ONTOLOGY_FIELD = "self_ontology"


def default_self_ontology() -> str:
    from gsuid_core.config import core_config

    raw = core_config.get_config("framework_aliases")
    aliases = [str(a) for a in raw] if isinstance(raw, list) and raw else ["GsCore", "gsuid_core"]
    names = "、".join(aliases) if aliases else "GsCore、gsuid_core"
    return (
        f"- 「{names}」是承载我的宿主框架。"
        "群友讨论这些名字 = 在讨论我的宿主。可以搭话，"
        "必须用角色卡里的世界观转述，禁止用开发者口吻介绍架构。\n"
        "- 「插件」是我可调用的能力；「更新/重启」= 我暂时离线。\n"
        "- 群里的 bot 统计（DAU/消息量/留存）可能是在统计我这类角色。\n"
        "- 配置里的 masters = 最高信任关系的人（如何称呼以角色卡为准）。"
    )


async def ensure_self_ontology(bot_id: str) -> str:
    """框架写入本体知识；空则填默认模板。"""
    from gsuid_core.ai_core.state_store import state_mutate, state_get_value

    raw = await state_get_value(_self_scope(bot_id), _SELF_MODEL_KEY)
    if isinstance(raw, dict) and _ONTOLOGY_FIELD in raw:
        val = raw[_ONTOLOGY_FIELD]
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, list) and val and isinstance(val[0], str) and val[0].strip():
            return val[0].strip()
    text = default_self_ontology()

    def _mutate(current: Any) -> Dict[str, object]:
        model = _self_model_state(current)
        model[_ONTOLOGY_FIELD] = text
        return model

    await state_mutate(_self_scope(bot_id), _SELF_MODEL_KEY, _mutate)
    return text


def _self_scope(bot_id: str) -> str:
    """构造 Bot 自身的 state_store scope（与记忆 SELF scope 同形）。"""
    return make_scope_key(ScopeType.SELF, bot_id or "default")


def _normalize_self_model(raw: Any) -> Dict[str, List[str]]:
    """把 state_store 读出的原始值规整为 self_model 列表字段。"""
    model: Dict[str, List[str]] = {f: [] for f in _FIELDS}
    if not isinstance(raw, dict):
        return model
    for field in _FIELDS:
        value = raw[field] if field in raw else None
        if isinstance(value, list):
            model[field] = [str(x) for x in value if isinstance(x, str) and x.strip()]
    return model


def _self_model_state(current: Any) -> Dict[str, object]:
    """读-改-写时保留 self_ontology，避免列表字段 mutate 把本体知识整包抹掉。"""
    model: Dict[str, object] = dict(_normalize_self_model(current))
    if not isinstance(current, dict) or _ONTOLOGY_FIELD not in current:
        return model
    val = current[_ONTOLOGY_FIELD]
    if isinstance(val, str) and val.strip():
        model[_ONTOLOGY_FIELD] = val.strip()
    elif isinstance(val, list) and val and isinstance(val[0], str) and val[0].strip():
        model[_ONTOLOGY_FIELD] = val[0].strip()
    return model


async def get_self_model(bot_id: str) -> Dict[str, List[str]]:
    """读取 Bot 的演化层自我模型，不存在时返回空结构。"""
    from gsuid_core.ai_core.state_store import state_get_value

    raw = await state_get_value(_self_scope(bot_id), _SELF_MODEL_KEY)
    return _normalize_self_model(raw)


async def add_self_note(
    bot_id: str,
    content: str,
    field: str = "self_notes",
) -> bool:
    """向自我模型的指定字段追加一条记录（C3-a 实时偏好写入补丁）。

    走 state_mutate 乐观锁完成读-改-写，并做写入限流：单条截断、列表去重、
    每字段最多保留 ``_MAX_ITEMS_PER_FIELD`` 条（超出丢弃最早条目），
    避免用户玩笑刷爆 self_model。

    Args:
        bot_id:  机器人 ID
        content: 笔记内容
        field:   目标字段，须为 _FIELDS 之一，默认 "self_notes"

    Returns:
        是否写入成功
    """
    from gsuid_core.ai_core.state_store import state_mutate

    if field not in _FIELDS:
        logger.warning(i18n_t("log.ai.selfcog_invalid_self_model_field", field=field))
        return False
    content = (content or "").strip()
    if not content:
        return False
    content = content[:_MAX_NOTE_CHARS]

    # 写入闸（防注入持久化）：「立持久说话规矩」（"以后每句加xx/结尾带xx/换风格说话"）
    # 是漂移攻击的典型载荷——存进 bot 级 self_model 会让单轮防住的攻击跨会话、跨用户
    # 永久生效（实测已发生：uwu 风格要求被记成了"学到的偏好"）。判据复用 C-2 结构判据。
    from gsuid_core.ai_core.interaction_scaffold import is_persistent_style_rule

    if field == "preferences_learned" and is_persistent_style_rule(content):
        logger.warning(i18n_t("log.ai.selfcog_refused_persist_persistent_inject", p0=content[:60]))
        return False

    def _mutate(current: Any) -> Dict[str, object]:
        model = _self_model_state(current)
        items = list(model[field]) if isinstance(model[field], list) else []
        if content in items:
            items.remove(content)
        items.append(content)
        if len(items) > _MAX_ITEMS_PER_FIELD:
            items = items[-_MAX_ITEMS_PER_FIELD:]
        model[field] = items
        return model

    await state_mutate(_self_scope(bot_id), _SELF_MODEL_KEY, _mutate)
    # 同步认知节点：让「我记过什么」能被 search_cognition 命中，而不是只能等
    # 下一轮稳定前缀。称呼/禁忌这类**规则**优先落 AIMemPreference，此处只留非规则反思。
    from gsuid_core.ai_core.cognition.distill import distill_self_note

    await distill_self_note(note=content, note_type=field, bot_id=bot_id)
    logger.debug(
        i18n_t(
            "log.ai.selfcog_bot_id_self_model_field",
            bot_id=bot_id,
            field=field,
            content=content,
        )
    )
    return True


async def overwrite_self_model_field(
    bot_id: str,
    field: str,
    items: List[str],
) -> bool:
    """整字段覆盖 self_model（C10 调试台人工修正跑偏的自我认知用）。

    与 add_self_note 的"追加"不同，本函数直接替换整个字段，受同样的去重与
    条目上限限流保护。

    Args:
        bot_id: 机器人 ID
        field:  目标字段，须为 _FIELDS 之一
        items:  新的字段内容列表

    Returns:
        是否写入成功
    """
    from gsuid_core.ai_core.state_store import state_mutate

    if field not in _FIELDS:
        logger.warning(i18n_t("log.ai.selfcog_invalid_self_model_field", field=field))
        return False
    cleaned: List[str] = []
    for raw in items:
        text = (raw or "").strip()[:_MAX_NOTE_CHARS]
        if text and text not in cleaned:
            cleaned.append(text)
    cleaned = cleaned[-_MAX_ITEMS_PER_FIELD:]

    def _mutate(current: Any) -> Dict[str, object]:
        model = _self_model_state(current)
        model[field] = cleaned
        return model

    await state_mutate(_self_scope(bot_id), _SELF_MODEL_KEY, _mutate)
    logger.info(
        i18n_t(
            "log.ai.selfcog_bot_id_self_field",
            bot_id=bot_id,
            field=field,
            p0=len(cleaned),
        )
    )
    return True


async def retrieve_self_episodes(bot_id: str, limit: int = 3) -> str:
    """检索 Bot 自身的情景记忆（"我之前说过/做过什么"），返回可注入文本（C3-c）。

    取 SELF scope 下最近的 Episode。仅在用户消息含回指词（"你之前/你不是说/
    上次你/你说过"）时由 handle_ai 调用，避免常态化开销。

    Args:
        bot_id: 机器人 ID
        limit:  返回的 Episode 数量上限

    Returns:
        以 `【我之前说过/做过】` 开头的文本块；无记录时返回空串。
    """
    from sqlmodel import col, select

    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    scope_key = make_scope_key(ScopeType.SELF, bot_id or "default")
    try:
        async with async_maker() as session:
            result = await session.execute(
                select(AIMemEpisode)
                .where(col(AIMemEpisode.scope_key) == scope_key)
                .order_by(col(AIMemEpisode.valid_at).desc())
                .limit(limit)
            )
            rows = list(result.scalars().all())
    except Exception as e:
        logger.debug(i18n_t("log.ai.selfcog_self_episodic_memory_fail", e=e))
        return ""

    if not rows:
        return ""

    lines: List[str] = ["【我之前说过/做过】"]
    for ep in rows:
        ts = ep.valid_at.strftime("%m-%d %H:%M") if ep.valid_at else ""
        content = ep.content[:150].replace("\n", " ")
        lines.append(f"[{ts}] {content}")
    return "\n".join(lines)


def get_capability_domains() -> Dict[str, List[str]]:
    """按 capability_domain 聚合已注册工具，未声明的按 category 兜底（C3-d）。

    Returns:
        {能力域名称: [工具名, ...]}
    """
    from gsuid_core.ai_core.register import get_registered_tools

    # category 兜底标签
    cat_labels = {
        "self": "核心能力",
        "buildin": "基础工具",
        "common": "常用工具",
        "media": "多媒体处理",
        "default": "子任务工具",
        "by_trigger": "插件功能",
    }
    domains: Dict[str, List[str]] = {}
    registry = get_registered_tools()
    for category, tools in registry.items():
        for tool_name, tool_base in tools.items():
            # capability_domain 是 ToolBase 已声明字段，直接访问；缺省按 category 兜底
            domain = tool_base.capability_domain or (cat_labels[category] if category in cat_labels else category)
            domains.setdefault(domain, []).append(tool_name)
    return domains


async def _compute_live_recurring_topics(
    scope_key: str,
    top_n: int = 5,
) -> List[str]:
    """从 group_profile 的累计 tag 频次实时计算本 scope 的"反复出现的话题"。

    group_profile.record_entity_tags 在每次 ingestion 时累加该 scope 的实体标签
    频次（见 ai_core/memory/group_profile.py），这里直接取 top-N 即可。
    无法读取（未配置 / 表不存在 / 该 scope 没积累）时返回空列表，
    让上层退回静态 self_model.recurring_topics。

    Args:
        scope_key: 已格式化的 scope（如 "group:xxx"）
        top_n:     返回的话题数量上限

    Returns:
        话题字符串列表（按频次降序）；任何异常都返回空 list 不抛
    """
    if not scope_key:
        return []
    try:
        from gsuid_core.ai_core.memory.group_profile import get_context_tags
    except ImportError:
        return []
    return await get_context_tags(scope_key, top_n=top_n)


async def build_self_cognition_context(
    bot_id: str,
    rel: Optional["RelationshipView"] = None,
    scope_key: Optional[str] = None,
    include_relationship: bool = True,
) -> str:
    """构造自我认知上下文段（C3-a）。

    内容 = 演化层 self_model 摘要（bot/scope 级，稳定）+ 当前对话者关系（per-user）。

    ``include_relationship``（§优化 O-3 缓存）：
      - True（per-turn 注入）：含关系行；关系随当前对话者 / zone 变化。
      - False（建 session 时进 system_prompt）：仅 self_model 块、**不含**关系行——
        群聊 session 整群共享，关系是 per-user 的、不能冻进共享前缀，由
        ``build_relationship_context`` 每轮单独注入 user 侧。

    Args:
        bot_id:    机器人 ID
        rel:       当前对话者的关系视图（``include_relationship=False`` 时不需要）
        scope_key: 本轮对话所在 scope（如 ``group:xxx`` / ``user_global:xxx``）。
            提供时，``recurring_topics`` 会优先从该 scope 的 group_profile 标签累计
            实时计算（top 5）——比静态 self_model.recurring_topics 更贴近当前会话。

    Returns:
        可直接注入的文本块；无任何可注入内容时返回空串。
    """
    model = await get_self_model(bot_id)
    lines: List[str] = ["【关于我自己（仅供参考）】"]
    ontology = await ensure_self_ontology(bot_id)
    if ontology:
        lines.append("【我是什么】")
        lines.append(ontology)

    if model["commitments"]:
        lines.append(f"我的承诺: {'；'.join(model['commitments'][-5:])}")
    if model["preferences_learned"]:
        lines.append(f"我学到的偏好: {'；'.join(model['preferences_learned'][-5:])}")
        # 免疫条款：self_model 是 bot 级共享状态，"偏好"可能来自别的群友/早已过时——
        # 实测旧印象会被拿来拒绝眼前用户的明确请求（"你说过不用设提醒"张冠李戴）。
        lines.append(
            "（这些旧印象仅供参考：可能过时、也可能只属于某个特定的人——"
            "当前对话者**此刻的明确请求永远优先**，绝不拿旧印象当拒绝眼前请求的理由）"
        )

    # recurring_topics：先尝试用本 scope 的 group_profile 累计 tag 实时计算
    # （由 memory.ingestion.worker._ingest_batch 中的 record_entity_tags 维护），
    # 拿不到再退回静态 self_model.recurring_topics（人工 / 离线写入的兜底）。
    live_topics = await _compute_live_recurring_topics(scope_key) if scope_key else []
    # 过滤掉作为"话题"误注入的 schema 类型标签（Person/Game/Product… 不是话题）
    _SCHEMA_TAGS = {"Person", "Game", "Character", "Event", "Product", "Organization", "Location"}
    final_topics = [t for t in (live_topics or model["recurring_topics"][-5:]) if t not in _SCHEMA_TAGS]
    if final_topics:
        lines.append(f"反复出现的话题: {'、'.join(final_topics)}")
    if model["self_notes"]:
        lines.append(f"我最近的反思: {'；'.join(model['self_notes'][-3:])}")

    if include_relationship and rel is not None:
        lines.append(rel.line)

    # 不再注入"我的能力域: planning / mcp / 子任务工具…"等工程语汇——每轮复读会把
    # 角色重塑成"工具系统"，是出戏主因。能调什么工具由 tools schema 承担，不进自述。

    # 只剩标题行（无演化数据、又不含关系）时无可注入内容
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def build_relationship_context(rel: "RelationshipView") -> str:
    """当前对话者关系（per-user，每轮注入 user 侧）。

    self_model 块随 session 固化进 system_prompt（缓存友好），但关系是 per-user 的，
    群聊共享 session 下必须每轮按当前对话者单独给出——括号包裹暗示是背景感知。
    文案不再自划档：唯一来源是 ``relationship.zones``（见 ``RelationshipView.line``）。
    """
    return f"（{rel.line}）"
