"""双路检索引擎（Dual-Route Retrieval）

并行执行 System-1（向量相似度）和 System-2（分层图遍历），
合并去重后经 Reranker 重排序，输出最终的 MemoryContext。
"""

import re
import time
import asyncio
from typing import TypeVar, Optional, Sequence, TypedDict
from datetime import datetime
from dataclasses import field, dataclass
from concurrent.futures import ThreadPoolExecutor

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.rag.reranker import RerankerProvider, get_reranker
from gsuid_core.ai_core.content_guard import wrap_untrusted
from gsuid_core.ai_core.memory.config import memory_config

# §6 残句拦截判据定义在摄入侧，注入侧兜底复用同一常量（"用户X提到"悬空谓语垃圾）
from gsuid_core.ai_core.memory.ingestion.edge import _DANGLING_FACT_RE

from .types import Edge, Entity, Episode, Category, RetrievalMeta
from .system1 import System1Result, system1_search
from .system2 import System2Result, system2_global_selection

# untrusted 栅栏自身的字符开销：episodes 预算与终装配截断都要预留它，
# 否则 </untrusted> 闭合标签会被尾截断切掉（评审修复 F9）
_UNTRUSTED_WRAP_OVERHEAD = len(wrap_untrusted("memory_recall", ""))


class PreferencePrompt(TypedDict):
    """注入 Prompt 的单条偏好规则（``MemoryContext.preferences`` 的元素）。

    抽成 TypedDict 替代裸 dict，让 ``to_prompt_text`` / 检索侧构造 / WebConsole 序列化
    三处的字段访问自文档化，统一键名契约。
    """

    target_context: str
    preference_rule: str
    polarity: str  # "do" / "dont"
    is_correction: bool
    # 仅供 WebConsole search 接口透传（注入渲染不用）；检索侧构造时带，历史 dict 兼容缺省。
    id: Optional[str]


T = TypeVar("T", bound=dict)

# OPT-01: Reranker 是 CPU/GPU 密集型，使用线程池避免阻塞事件循环
_RERANK_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="reranker")

# A-5 降噪：事件型 trivia（"X提及/提到/在唱/演唱/询问…"这类一次性提及）信息密度低，
# 注入核心事实时下沉到末尾，把预算优先留给实体/关系型高价值事实。
_TRIVIA_FACT_RE = re.compile(r"(提及|提到|在唱|演唱|唱歌|询问|聊到|讨论|说起|谈到)")

# A-3：身份等价类 fact 的特征词。这类 fact 主语极易抽错（"谈论小C"会被固化成
# "<说话人>本人是小C"），故①禁止用 source_name 强补主语 ②注入时统一标"待证"。
_IDENTITY_FACT_KEYWORDS = ("本人是", "就是", "叫做", "别名")

# §7 第三方隐私拦截：婚恋/财务/健康/联络四**类目**的敏感事实仅当事人在场才注入；
# 词表按类目组织（非个案关键词），部署者可经 memory_sensitive_extra_terms 扩展。
_SENSITIVE_FACT_RE = re.compile(
    r"催婚|相亲|离婚|分手|出轨|怀孕|堕胎|"  # 婚恋
    r"工资|薪资|月薪|年薪|收入|欠钱|欠款|负债|贷款|房贷|房租|"  # 财务
    r"抑郁|焦虑症|生病|住院|确诊|病历|"  # 健康
    r"住址|家庭地址|身份证|手机号|电话号|银行卡"  # 联络/证件
)


def _get_sensitive_extra_terms() -> list[str]:
    """部署者扩展敏感词（memory_sensitive_extra_terms）。入口取一次供整轮复用（评审修复 E17）。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    extra = ai_config.get_config("memory_sensitive_extra_terms").data
    if isinstance(extra, list):
        return [str(w).strip() for w in extra if str(w).strip()]
    return []


def _sensitive_fact_hit(fact: str, extra_terms: Sequence[str] = ()) -> bool:
    """内置敏感类目词表 + 部署者扩展词表任一命中。

    匹配前过 normalize_for_match（与 output_firewall 同款防拆字/零宽规避，评审修复 F5-reuse）。
    """
    from gsuid_core.ai_core.content_guard import normalize_for_match

    normalized = normalize_for_match(fact)
    if _SENSITIVE_FACT_RE.search(normalized) or _SENSITIVE_FACT_RE.search(fact):
        return True
    return any(w in fact or w in normalized for w in extra_terms)


def _fact_mentions_speaker(edge: "Edge", speaker_ids: set) -> bool:
    """该 edge 是否以当前说话人为主语/当事人（source_name 或 fact 文本含其标识）。

    数字 ID 按整串边界匹配：短 QQ 号是别人长号的子串时不得误判在场（评审修复 E6）。
    """
    blob = f"{edge['source_name'] or ''}|{edge['fact'] or ''}"
    for sid in speaker_ids:
        s = str(sid).strip()
        if not s:
            continue
        if s.isdigit():
            if re.search(rf"(?<!\d){re.escape(s)}(?!\d)", blob):
                return True
        elif s in blob:
            return True
    return False


def _edge_date_prefix(e: "Edge") -> str:
    """edge 的 [YYYY-MM-DD] 日期前缀；无 valid_at_ts（旧数据/迁移缺失）返回空串。"""
    ts = e["valid_at_ts"] if "valid_at_ts" in e else None
    if not ts:
        return ""
    try:
        return f"[{datetime.fromtimestamp(ts).strftime('%Y-%m-%d')}] "
    except Exception:
        return ""


# 时间范围检索：query 中显式出现的日期（ISO / 中文 / 斜杠格式）。命中≥1 个日期视为
# 时间锚定问题（"从X到Y依次…"、"X期间…"），语义相似检索对这类枚举/时序问题召回
# 严重不足（相似度只召回字面相近片段，漏掉时间窗内大量相关片段），需按时间窗直查补召回。
_QUERY_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?")

# 时序/枚举/总结类意图词：时间范围补召回只对这类问题有益（需要整段时间线覆盖）；
# 对"某个时点的具体取值"类精确问题反而会稀释语义命中（BEAM 教训：带两个日期的
# preference/knowledge_update 点查题被时间泛洪拖垮），故意图词与 ≥2 日期须同时满足。
_TEMPORAL_INTENT_RE = re.compile(
    r"(in order|sequence|chronolog|progress|summar|overview|evol|develop|timeline|history|"
    r"依次|顺序|时间线|先后|经过|演变|变化|历程|总结|概述|回顾)",
    re.IGNORECASE,
)


def _extract_time_range(query: str) -> Optional[tuple[datetime, datetime]]:
    """从 query 中提取显式时间范围；无日期或解析失败返回 None。

    仅当 query 含 ≥2 个日期 **且** 带时序/枚举/总结意图词才触发 → [最早, 最晚+1天]。
    单个日期或纯点查（"X 那天的值是多少"）不触发：点查靠语义检索更准。
    """
    if not _TEMPORAL_INTENT_RE.search(query):
        return None
    dates: list[datetime] = []
    for m in _QUERY_DATE_RE.finditer(query):
        try:
            dates.append(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    if len(set(dates)) < 2:
        return None
    from datetime import timedelta

    return min(dates), max(dates) + timedelta(days=1)


async def _fetch_temporal_episodes(
    query: str,
    scope_keys: list[str],
    start: datetime,
    end: datetime,
    buckets: int = 6,
    per_bucket: int = 8,
) -> list[Episode]:
    """时间分桶语义检索：把时间窗切成 buckets 段，每段内做带 valid_at_ts 过滤的
    语义检索取 top per_bucket，合并去重后按时间升序返回。

    大语料下时间窗内可能有上万条 Episode，均匀采样命中率≈0；纯语义检索则整窗
    集中在少数高相似片段、漏掉时段中后段进展。分桶语义检索同时保证"相关"与
    "整段时间线覆盖"。
    """
    from gsuid_core.ai_core.memory.vector.ops import search_episodes_in_range

    start_ts = start.timestamp()
    end_ts = end.timestamp()
    span = (end_ts - start_ts) / max(buckets, 1)

    async def _one_bucket(i: int) -> list[Episode]:
        try:
            return await search_episodes_in_range(
                query,
                scope_keys,
                start_ts + i * span,
                start_ts + (i + 1) * span,
                top_k=per_bucket,
            )
        except Exception as e:
            logger.debug(i18n_t("🧠 [Memory] 时间分桶检索 bucket={i} 失败: {e}", i=i, e=e))
            return []

    results = await asyncio.gather(*[_one_bucket(i) for i in range(buckets)])
    seen: dict[str, Episode] = {}
    for eps in results:
        for ep in eps:
            seen[ep["id"]] = ep
    merged = list(seen.values())
    merged.sort(key=lambda ep: ep["valid_at"] or "")
    return merged


def _on_pref_task_done(t: "asyncio.Task") -> None:
    """偏好 last_applied 刷新后台任务的回调：吞掉取消、记录异常，避免未捕获告警。"""
    if t.cancelled():
        return
    exc = t.exception()
    if exc is not None:
        logger.warning(i18n_t("🧠 [Memory] 刷新 preference last_applied 后台任务异常: {exc}", exc=exc))


async def _run_sync_rerank(
    reranker: RerankerProvider,
    query: str,
    texts: list[str],
) -> list[float]:
    """在线程池里运行同步 reranker，包括完整的 generator 迭代。

    关键：reranker.rerank() 是 generator function，run_in_executor 只把
    创建 generator 对象的调用放进线程，ONNX 推理发生在迭代时（list()）。
    必须用 lambda 把 list() 也包进去，否则推理仍在事件循环线程执行。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _RERANK_EXECUTOR,
        lambda: list(reranker.rerank(query, texts)),  # list() 也在线程里执行
    )


def _complete_fact_subject(fact: str, source_name: str) -> str:
    """给缺少主语的 fact 补全主语。

    历史数据中部分 fact 是"建议关注中证白酒指数"这类缺主语的短语，
    这里用边的 source_name 补成"用户xxx建议关注…"的完整句子。
    纯数字名称视为用户 ID，补成"用户{id}"。
    source_name 由检索阶段直接填充到 Edge.source_name，不再依赖运行时查找。
    """
    fact = fact.strip()
    if not fact:
        return ""
    # 身份等价类 fact 禁止用 source_name 强补主语——否则"谈论小C"会被固化成
    # "<说话人>本人是小C"，引发身份级联错乱。
    _identity = any(k in fact for k in _IDENTITY_FACT_KEYWORDS)
    if source_name and source_name not in fact and not _identity:
        subject = f"用户{source_name}" if source_name.isdigit() else source_name
        if subject not in fact:
            fact = f"{subject}{fact}"
    return fact


def compute_edge_confidence(mention_count: Optional[int], decay_score: Optional[float]) -> float:
    """由"佐证次数 × 新鲜度"折算事实置信度 weight ∈ (0, 1]（置信度轴，与相关性正交）。

    - ``mention_count``（C1 跨发言者归并计数）：同一事实被独立复述越多越可信，
      用 ``1 - 0.5**mc`` 饱和映射（1→0.5、2→0.75、3→0.875…），单次提及给 0.5 基线；
    - ``decay_score``（C11 时效衰减分，新鲜=1.0、久未命中→下降）：作为新鲜度系数夹到 [0,1]。

    此值只衡量"这条事实有多可信"，与当前 query 无关——相关性由 reranker score 负责。
    旧库 ALTER 补列前个别行可能为 None，故按 None→默认值兜住（mc→1、decay→1.0）。
    """
    mc = mention_count if (mention_count and mention_count > 0) else 1
    corroboration = 1.0 - 0.5**mc
    decay = decay_score if decay_score is not None else 1.0
    decay = min(max(decay, 0.0), 1.0)
    return round(corroboration * decay, 4)


@dataclass
class MemoryContext:
    """双路检索的最终输出，直接注入 Prompt"""

    episodes: list[Episode] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    # C11 矛盾提示：命中边对应的 AIMemConflict 摘要。旧矛盾边已被软删除、检索不可见，
    # 注入摘要让 Agent 知道该事实历史上存在相反陈述（应指出矛盾而非武断单侧结论）。
    conflicts: list[str] = field(default_factory=list)
    # 程序性/偏好记忆规则（SQL-only，须严格遵守的硬约束，置顶注入）。
    # 字段契约见 PreferencePrompt（target_context / preference_rule / polarity / is_correction / id）。
    preferences: list[PreferencePrompt] = field(default_factory=list)
    retrieval_meta: RetrievalMeta = field(
        default_factory=lambda: RetrievalMeta(s1_episodes=0, s2_episodes=0, scope_keys=[])
    )
    retrieval_paths: list[list[dict]] = field(default_factory=list)  # System-2 检索路径
    # 时间范围检索命中标记：query 含显式日期范围时置 True。此时问题是枚举/时序型，
    # 时间线证据在 episodes 里，注入预算向片段倾斜（事实占比 55% → 30%）。
    temporal_mode: bool = False

    def to_prompt_text(
        self,
        max_chars: int = 2000,
        priority_speakers: Optional[set] = None,
        current_speaker_ids: Optional[set] = None,
    ) -> str:
        """格式化为可注入 System Prompt 的记忆上下文文本。

        采用 Token 预算控制，按"信息密度"分配空间：
        - 核心事实（edges）：约 55%，是最可供 Agent 推理的内容，优先保证
        - 语义类目（categories）：约 15%，提供话题大纲
        - 相关对话片段（episodes）：约 30%，只保留少量最相关轮次

        每个区块在自己的预算内逐条累加，超预算即停止，避免低价值内容挤占空间。

        Args:
            max_chars: 注入预算字符数
            priority_speakers: C4 预算优先级——这些发言者（如主人）相关的 edge
                会被稳定上浮到核心事实区块最前，优先占用预算。
            current_speaker_ids: 当前说话人的标识集合（user_id/昵称）。§7 第三方隐私
                拦截为**默认拒绝**：不传（后台/工具路径）时敏感类目一律不注入，
                只有敏感事实主语含说话人标识才放行（评审修复 F7）。
        """

        def _take(items: list[str], budget: int) -> list[str]:
            """在字符预算内尽量多地累加条目。"""
            out: list[str] = []
            used = 0
            for line in items:
                if used + len(line) > budget and out:
                    break
                out.append(line)
                used += len(line)
            return out

        parts: list[str] = []
        pref_block: Optional[str] = None

        # 程序性/偏好规则（最高优先级，置顶 + 强约束语气）：区别于"核心事实"的背景陈述，
        # 这是针对 Agent 未来行为的硬约束（如"调 generate_image 用竖图"），必须让工具调用
        # LLM 当作规则而非背景。占独立小预算，先于核心事实占用。
        if self.preferences:
            pref_budget = int(max_chars * memory_config.preference_inject_budget_ratio)
            pref_lines: list[str] = []
            for p in self.preferences[: memory_config.preference_max_inject]:
                rule = (p["preference_rule"] if "preference_rule" in p else "").strip()
                if not rule:
                    continue
                ctx_tag = (p["target_context"] if "target_context" in p else "") or ""
                tag = f"[{ctx_tag}] " if ctx_tag and ctx_tag != "general" else ""
                mark = "（纠正过）" if (p["is_correction"] if "is_correction" in p else False) else ""
                pref_lines.append(f"• {tag}{rule}{mark}")
            taken = _take(pref_lines, pref_budget)
            if taken:
                # §9 仲裁语：旧规则常缺触发条件（"不要提及睡觉"），模型会自行猜测适用面
                # 并在完全无关场合套用/合理化——显式钉住"按字面最小范围理解"。
                # 偏好块独立变量而非事后按标题前缀分拣：措辞变更不得静默改变防线归属（评审修复 G2）
                pref_block = (
                    "【用户偏好/纠错 - 须严格遵守】"
                    "（各规则按其触发条件适用；未写明条件的按字面最小范围理解，不扩大化）\n" + "\n".join(taken)
                )

        # 核心事实（最高优先级）
        if self.edges:
            fact_budget = int(max_chars * (0.3 if self.temporal_mode else 0.55))
            edges = self.edges[: memory_config.search_edge_count]

            # C4 预算优先级：主人 edge 稳定上浮；A-5 降噪：事件型 trivia 下沉，
            # 让高价值实体/关系事实优先占用注入预算（stable sort 不打乱 rerank 内部序）。
            def _edge_rank(e: Edge) -> tuple[int, int]:
                is_priority = 0 if (priority_speakers and e["source_name"] in priority_speakers) else 1
                is_trivia = 1 if _TRIVIA_FACT_RE.search(e["fact"] or "") else 0
                return (is_priority, is_trivia)

            edges = sorted(edges, key=_edge_rank)

            # C11 注入期矛盾兜底：同 (src,tgt) 命中边极性相反时双方标 ⚠️，
            # 补摄入期矛盾引擎对共存矛盾边（旧数据/跨窗口）的漏检。
            from gsuid_core.ai_core.memory.ingestion.edge import _fact_polarity

            _pair_pol: dict[tuple, set] = {}
            for e in edges:
                key = (e["source_id"], e["target_id"])
                _pair_pol.setdefault(key, set()).add(_fact_polarity(e["fact"] or ""))
            conflicted_pairs = {k for k, v in _pair_pol.items() if len(v) > 1}

            # C11 后置拦截器：按 fact 归一化签名去重，避免近义重复事实挤占注入预算
            fact_lines: list[str] = []
            seen_facts: set = set()
            now_ts = time.time()
            _extra_sensitive = _get_sensitive_extra_terms()
            for e in edges:
                # 过滤已失效边（invalid_at_ts 过期）与低置信边（weight 低于阈值）
                invalid_at = e["invalid_at_ts"]
                if invalid_at and invalid_at < now_ts:
                    continue
                if e["weight"] < memory_config.min_edge_weight:
                    continue
                fact = _complete_fact_subject(e["fact"], e["source_name"])
                if not fact:
                    continue
                # §6 残句拦截：悬空谓语结尾（"用户X提到"）零信息量，不进注入预算
                if _DANGLING_FACT_RE.search(fact):
                    continue
                # §7 第三方隐私默认拒绝：敏感事实仅当事人在场才注入；未传 speaker
                # 的路径（后台/工具）一律拦截，防调用点遗漏成为旁路（评审修复 F7）
                if _sensitive_fact_hit(fact, _extra_sensitive) and not (
                    current_speaker_ids and _fact_mentions_speaker(e, current_speaker_ids)
                ):
                    continue
                sig = fact.strip().lower().replace(" ", "")[:24]
                if sig in seen_facts:
                    continue
                seen_facts.add(sig)
                # 身份/称呼类事实极易抽错，加"待证"标注，避免被当成铁证盲信
                _id = any(k in fact for k in _IDENTITY_FACT_KEYWORDS)
                # 带上事实的记录日期：knowledge_update 类问题依赖"同一属性取最新值"，
                # 没有时间戳时多值冲突的 edge 无从排序（BEAM §7 教训）
                _dt = _edge_date_prefix(e)
                _cf = "⚠️[与其他陈述矛盾] " if (e["source_id"], e["target_id"]) in conflicted_pairs else ""
                fact_lines.append(f"• {_dt}{_cf}{'（记忆·待证）' if _id else ''}{fact}")
                # §25(4) 条数硬上限（可配 fact_max_inject，与字符预算双限取严）：
                # 够数即停，剩余 edge 不再白做加工（评审修复 E17/F7-cfg）
                if len(fact_lines) >= memory_config.fact_max_inject:
                    break
            taken = _take(fact_lines, fact_budget)
            if taken:
                parts.append("【核心事实 - 与当前问题相关】\n" + "\n".join(taken))

        # C11 矛盾提示：紧跟核心事实。历史上有过相反陈述的事实，Agent 应指出矛盾
        # 并请用户澄清，而不是把（可能是误抽的）单侧最新值当作定论。
        if self.conflicts:
            conf_lines = [f"• {s[:300]}" for s in self.conflicts[:6]]
            taken = _take(conf_lines, int(max_chars * 0.12))
            if taken:
                parts.append(
                    "【矛盾记录 - 该话题存在相互冲突的历史陈述，回答涉及时请明确指出矛盾并请用户澄清哪个正确】\n"
                    + "\n".join(taken)
                )

        # 语义类目摘要（话题大纲）
        if self.categories:
            cat_budget = int(max_chars * 0.15)
            sorted_cats = sorted(self.categories, key=lambda c: c["layer"], reverse=True)
            cat_lines = [f"• [L{c['layer']}] {c['name']}: {(c['summary'] or '')[:100]}" for c in sorted_cats[:6]]
            taken = _take(cat_lines, cat_budget)
            if taken:
                parts.append("【语义类目摘要】\n" + "\n".join(taken))

        # 相关对话片段：吃掉前面区块（偏好/事实/类目）用剩的全部预算——纯 episode-RAG
        # （无图谱时，如大语料回灌 / 评测）episodes 是唯一召回源，必须给足空间，否则被
        # 旧的固定 30% + 3 条 × 200 字硬上限饿死（单条事实 200 字截断后召回到也答不出）。
        if self.episodes:
            # 预算须扣除偏好块与 untrusted 栅栏开销，否则终装配必超 max_chars、
            # 闭合标签被尾截断切掉（评审修复 F9）
            _pref_used = (len(pref_block) + 2) if pref_block else 0
            used = sum(len(p) for p in parts) + 2 * len(parts) + _pref_used + _UNTRUSTED_WRAP_OVERHEAD
            ep_budget = max_chars - used
            if ep_budget > 120:
                eps = self.episodes
                # temporal_mode（时间范围/时序问题）：单条内容上限压到 600 字，
                # 让更多不同时段的片段进入预算（列表序=语义相关性优先，时间补位在尾）。
                ep_cap = 600 if self.temporal_mode else 1000
                ep_lines = [f"[{ep['valid_at'][:16].replace('T', ' ')}] {ep['content'][:ep_cap]}" for ep in eps]
                taken = _take(ep_lines, ep_budget)
                if taken:
                    parts.append("【相关对话片段】\n" + "\n".join(taken))

        # §8 注入防线对齐：偏好保持裸注入可执行（写入端有闸）；其余召回统一 untrusted
        # 栅栏——复用 content_guard.wrap_untrusted，栅栏格式全通道唯一定义（评审修复 F9）。
        blocks: list[str] = [pref_block] if pref_block else []
        if parts:
            recall_text = "\n\n".join(parts)
            # 截断在包装**之前**并预留栅栏开销：</untrusted> 永不被尾截断切掉——
            # 不闭合的栅栏会把其后的正常上下文拖进"不可信"语义区（评审修复 F9）
            _marker = "\n...[记忆已截断]"
            _budget = max_chars - ((len(pref_block) + 2) if pref_block else 0) - _UNTRUSTED_WRAP_OVERHEAD
            if _budget <= len(_marker):
                recall_text = ""
            elif len(recall_text) > _budget:
                recall_text = recall_text[: _budget - len(_marker)] + _marker
            if recall_text:
                blocks.append(wrap_untrusted("memory_recall", recall_text))
        return "\n\n".join(blocks)

    def to_memory_text(self, max_chars: int = 24000) -> str:
        """格式化为可注入 Memory 的记忆上下文文本。

        含【已知事实】(edges) + 【相关对话片段】(episodes)：纯 episode-RAG（无图谱）时
        edges 为空，必须带上 episodes，否则该字段恒空（探针 ``memory`` 字段失真、且无图谱
        部署召回到的对话片段无从注入）。
        """

        parts: list[str] = []

        if self.edges:
            fact_lines: list[str] = []
            for e in self.edges[: memory_config.search_edge_count]:
                fact = _complete_fact_subject(e["fact"], e["source_name"])
                if fact:
                    fact_lines.append(f"• {_edge_date_prefix(e)}{fact}")
            facts_text = "\n".join(fact_lines)
            parts.append(f"【已知事实】\n{facts_text if facts_text else '暂无已知事实'}")

        if self.conflicts:
            parts.append(
                "【矛盾记录 - 存在相互冲突的历史陈述，回答涉及时请指出矛盾并请用户澄清】\n"
                + "\n".join(f"• {s[:300]}" for s in self.conflicts[:6])
            )

        if self.episodes:
            ep_lines = [f"[{ep['valid_at'][:16].replace('T', ' ')}] {ep['content']}" for ep in self.episodes]
            parts.append("【相关对话片段】\n" + "\n".join(ep_lines))

        result = str("\n\n".join(parts))
        if len(result) > max_chars:
            result = result[:max_chars] + "\n...[记忆已截断]"
        return str(result)


def _merge_episodes(list_a: Sequence[Episode], list_b: Sequence[Episode]) -> list[Episode]:
    """合并两个 Episode 列表，按 id 去重。"""
    seen: dict[str, Episode] = {}
    for item in list(list_a) + list(list_b):
        seen[item["id"]] = item
    return list(seen.values())


def _merge_entities(list_a: Sequence[Entity], list_b: Sequence[Entity]) -> list[Entity]:
    """合并两个 Entity 列表，按 id 去重。"""
    seen: dict[str, Entity] = {}
    for item in list(list_a) + list(list_b):
        seen[item["id"]] = item
    return list(seen.values())


def _merge_edges(list_a: Sequence[Edge], list_b: Sequence[Edge]) -> list[Edge]:
    """合并两个 Edge 列表，按 id 去重。"""
    seen: dict[str, Edge] = {}
    for item in list(list_a) + list(list_b):
        seen[item["id"]] = item
    return list(seen.values())


def _merge_categories(list_a: Sequence[Category], list_b: Sequence[Category]) -> list[Category]:
    """合并两个 Category 列表，按 id 去重。"""
    seen: dict[str, Category] = {}
    for item in list(list_a) + list(list_b):
        seen[item["id"]] = item
    return list(seen.values())


async def _rerank_episodes(query: str, items: list[Episode], top_k: int) -> list[Episode]:
    """对 Episode 列表进行 Rerank（OPT-01: 使用线程池避免阻塞）"""
    if not items:
        return []
    reranker = get_reranker()
    if reranker is None:
        return items[:top_k]
    texts = [item["content"] for item in items]
    scores = list(await _run_sync_rerank(reranker, query, texts))
    if len(scores) != len(items):
        logger.warning(i18n_t("🧠 [Memory] Reranker scores 长度不一致，跳过 Rerank"))
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:top_k]]


async def _rerank_entities(query: str, items: list[Entity], top_k: int) -> list[Entity]:
    """对 Entity 列表进行 Rerank（OPT-01: 使用线程池避免阻塞）"""
    if not items:
        return []
    reranker = get_reranker()
    if reranker is None:
        logger.warning(i18n_t("log.memory.reranker_unavailable"))
        return items[:top_k]
    texts = [item["summary"] for item in items]
    scores = list(await _run_sync_rerank(reranker, query, texts))
    if len(scores) != len(items):
        logger.warning(i18n_t("🧠 [Memory] Reranker scores 长度不一致，跳过 Rerank"))
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:top_k]]


async def _rerank_edges(query: str, items: list[Edge], top_k: int) -> list[Edge]:
    """对 Edge 列表进行 Rerank（OPT-01: 使用线程池避免阻塞）。

    Reranker 分数即"事实与当前 query 的相关性"——把它回写进 ``item["score"]`` 供下游
    使用，并按 ``min_edge_rerank_score`` 剔除完全无关的边（相关性轴，与 weight 置信度无关）。
    无 Reranker 时没有相关性信号，仅做 top_k 截断、不过滤。
    """
    if not items:
        return []
    reranker = get_reranker()
    if reranker is None:
        return items[:top_k]
    texts = [item["fact"] for item in items]
    scores = list(await _run_sync_rerank(reranker, query, texts))
    if len(scores) != len(items):
        logger.warning(i18n_t("🧠 [Memory] Reranker scores 长度不一致，跳过 Rerank"))
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    kept: list[Edge] = []
    for score, item in ranked[:top_k]:
        item["score"] = score  # 回写相关性分数，供注入阶段与日志排查使用
        if score < memory_config.min_edge_rerank_score:
            continue  # 相关性过滤：剔除被判为完全无关（负分/低分）的事实
        kept.append(item)
    return kept


async def dual_route_retrieve(
    query: str,
    user_id: str,
    group_id: Optional[str] = None,
    top_k: int = 20,
    enable_system2: bool = True,
    enable_user_global: bool = True,
    inject_preferences: bool = True,
    preference_contexts: Optional[list[str]] = None,
) -> MemoryContext:
    """双路检索主入口。在 handle_ai.py 中，AI 准备回复前调用此函数。

    Args:
        query:              用户的原始查询文本
        group_id:           原始群组 ID（如 "789012"）
        user_id:            触发用户的 ID（可选，用于联合用户全局记忆）
        session:            SQLAlchemy AsyncSession
        top_k:              最终返回的 Episode 数量上限
        enable_system2:     是否启用 System-2 全局选择（成本较高）
        enable_user_global: 是否联合查询用户跨群画像
        inject_preferences: 是否注入程序性/偏好规则（意图门：纯闲聊轮可传 False 整轮跳过）
        preference_contexts: 选择性注入——本轮相关的能力域/工具名集合。``None`` = 不过滤
            （注入全部活跃规则，旧行为）；传入列表（可为空）则启用过滤：纠错规则与 ``general``
            通用规则永远注入，其余非纠错规则仅当 ``target_context`` 命中本集合时才注入，避免
            无关工具规则挤占预算、分散工具调用注意力。
    """
    scope_keys: list[str] = []
    group_scope = None
    if group_id:
        group_scope = make_scope_key(
            ScopeType.GROUP,
            group_id,
        )
        scope_keys.append(group_scope)

        # 别名展开：若 query 中出现群内别名，附加正式名称以提升记忆召回
        try:
            from gsuid_core.ai_core.memory.group_profile import (
                get_term_mappings,
                expand_query_with_aliases,
            )

            mappings = await get_term_mappings(group_scope)
            expanded = expand_query_with_aliases(query, mappings)
            if expanded != query:
                logger.debug(
                    i18n_t(
                        "🧠 [Memory] query 别名展开: {query} -> {expanded}", query=repr(query), expanded=repr(expanded)
                    )
                )
                query = expanded
        except Exception as e:
            logger.debug(i18n_t("🧠 [Memory] 别名展开失败: {e}", e=e))

    # 私聊 / 无群上下文（group_id 为空）：user_global 是该用户记忆的**主** scope（observer 对
    # 私聊消息即写此处），必须检索，否则私聊与评测（group_id=None）召回恒空。群聊时则仅当
    # enable_user_global 才把用户跨群画像并入群 scope。
    if user_id and (not group_id or enable_user_global):
        user_scope = make_scope_key(
            ScopeType.USER_GLOBAL,
            user_id,
        )
        scope_keys.append(user_scope)
    else:
        user_scope = None

    # RF-Mem 熟悉度路由（默认关，零影响）：用一次零 LLM 的向量探针的 s̄/熵 逐查询决定
    # "检索多深"，把 System-2 从全局静态开关降为"按不确定性触发"。路由只在"低熟悉/高
    # 不确定"时才放行 System-2，且**永远受用户总开关约束**——用户关了 System-2 就永不
    # 触发 LLM 深检索。关闭路由时 effective_enable_system2 == enable_system2，行为不变。
    effective_enable_system2 = enable_system2
    is_recollection_route = False
    probe_vec: Optional[list[float]] = None
    # P1：仅当探针结论会被消费时才发探针——System-2 开（可被熟悉度抑制）或回忆环可用
    # （enable_recollection_path + remote Qdrant）。两者皆无时探针白跑一次 embedding+dense、
    # 改变不了任何分支，直接短路省成本。
    _probe_can_act = enable_system2 or (
        memory_config.enable_recollection_path and memory_config.qdrant_provider == "remote"
    )
    if memory_config.enable_familiarity_routing and scope_keys and _probe_can_act:
        from .familiarity import ROUTE_RECOLLECTION, probe_and_route

        route, _signal, probe_vec = await probe_and_route(query, scope_keys)
        is_recollection_route = route == ROUTE_RECOLLECTION
        effective_enable_system2 = enable_system2 and is_recollection_route

    # 时间范围补召回：query 显式含日期时按时间窗直查 Episode（与 S1/S2 并行），
    # 解决枚举/时序类问题（"从X到Y依次…"）语义相似检索召回不足的问题。
    time_range = _extract_time_range(query)
    temporal_task: Optional[asyncio.Task] = None
    if time_range and scope_keys:
        temporal_task = asyncio.create_task(_fetch_temporal_episodes(query, scope_keys, time_range[0], time_range[1]))

    # OPT-02: S1 和 S2 真正并行 - 使用 asyncio.gather 同时等待所有任务
    s1_task = asyncio.create_task(
        system1_search(
            query,
            scope_keys,
            top_k=top_k,
        )
    )

    # System-2 对 group_scope 和 user_scope 都执行
    s2_tasks: list[asyncio.Task] = []
    s2_scope_keys: list[str] = []
    if effective_enable_system2:
        if group_scope:
            s2_tasks.append(asyncio.create_task(system2_global_selection(query, group_scope)))
            s2_scope_keys.append(group_scope)
        if user_scope:
            s2_tasks.append(asyncio.create_task(system2_global_selection(query, user_scope)))
            s2_scope_keys.append(user_scope)

    # OPT-02: 同时等待 S1 和 S2，谁先完成谁先用，不存在先后阻塞
    all_tasks = [s1_task] + s2_tasks
    all_results = await asyncio.gather(*all_tasks, return_exceptions=True)

    # 处理 System-1 结果
    s1_raw = all_results[0]
    if isinstance(s1_raw, Exception):
        logger.error(i18n_t("🧠 [Memory] System-1 检索失败: {s1_raw}", s1_raw=s1_raw))
        s1: System1Result = System1Result()
    else:
        s1 = s1_raw  # type: ignore[assignment]

    s2_results: list[System2Result] = []
    for i, raw_result in enumerate(all_results[1:], start=1):
        if isinstance(raw_result, Exception):
            logger.error(
                i18n_t(
                    "🧠 [Memory] System-2 检索失败 (scope={p0}): {raw_result}",
                    p0=s2_scope_keys[i - 1],
                    raw_result=raw_result,
                )
            )
        elif isinstance(raw_result, System2Result):
            s2_results.append(raw_result)
            logger.debug(
                i18n_t(
                    "🧠 [Memory] System-2 检索完成 (scope={p0})，共 {p1} 条 Episode, {p2} 个 Entity, {p3} 条 Edge",
                    p0=s2_scope_keys[i - 1],
                    p1=len(raw_result.episodes),
                    p2=len(raw_result.selected_entities),
                    p3=len(raw_result.edges),
                )
            )

    logger.debug(
        i18n_t(
            "🧠 [Memory] System-1 检索完成，共 {p0} 条 Episode, {p1} 个 Entity, {p2} 条 Edge",
            p0=len(s1.episodes),
            p1=len(s1.entities),
            p2=len(s1.edges),
        )
    )

    # 合并去重（多个 S2 结果之间也要去重）
    s2_episodes = []
    s2_entities = []
    s2_edges = []
    s2_categories = []
    for s2 in s2_results:
        s2_episodes.extend(s2.episodes)
        s2_entities.extend(s2.selected_entities)
        s2_edges.extend(s2.edges)
        s2_categories.extend(s2.categories)

    # 收集 System-2 检索路径
    s2_retrieval_paths: list[list[dict]] = []
    for s2 in s2_results:
        s2_retrieval_paths.extend(s2.retrieval_paths)

    # 先合并 S1 + S2 结果（去重）
    all_episodes: list[Episode] = _merge_episodes(s1.episodes if s1 else [], s2_episodes)
    all_entities: list[Entity] = _merge_entities(s1.entities if s1 else [], s2_entities)
    all_edges: list[Edge] = _merge_edges(s1.edges if s1 else [], s2_edges)
    all_categories: list[Category] = _merge_categories([], s2_categories)

    # RF-Mem 回忆环（默认关，且仅 remote Qdrant）：当路由判为低熟悉、System-2 未实际触发
    # 时，用零 LLM 的 KMeans+α-mix 向量回忆补召回，并把召回的 Episode 链**关系投影**成链上
    # 精准 Edge 事实（与 System-1 独立 Edge 检索取并集、不替代）。本地嵌入式 Qdrant 下回忆
    # 会成倍放大 O(N) 暴力扫，故强绑 qdrant_provider=remote。
    if (
        memory_config.enable_familiarity_routing
        and memory_config.enable_recollection_path
        and is_recollection_route
        and not effective_enable_system2
        and memory_config.qdrant_provider == "remote"
        and scope_keys
    ):
        try:
            from .familiarity import recollection_search, project_episodes_to_edges

            recalled = await recollection_search(
                query,
                scope_keys,
                top_k=top_k,
                beam=memory_config.recollection_beam,
                fanout=memory_config.recollection_fanout,
                rounds=memory_config.recollection_rounds,
                alpha=memory_config.recollection_alpha,
                query_vector=probe_vec,  # 复用探针向量，省一次嵌入
            )
            if recalled:
                all_episodes = _merge_episodes(all_episodes, recalled)
                projected = await project_episodes_to_edges([e["id"] for e in recalled], scope_keys)
                if projected:
                    all_edges = _merge_edges(all_edges, projected)
        except Exception as e:
            logger.warning(i18n_t("🧠 [RF-Mem] 回忆环检索失败: {e}", e=e))

    # 类型隔离 Rerank（Type Isolation）：
    # Category 节点完全跳过 Reranker，给予固定最高优先级。
    # 原因：交叉编码器（Cross-Encoder）的打分强依赖文本字面重合度，
    # Category 摘要（如"Physical Health: 包含个体的健康状况..."）与用户 query
    # 字面重合度极低，统一 Rerank 会被"误杀"踢出 top_k。
    # 保证 LLM 永远能看到大纲（Category），再看细节（Episode/Entity/Edge）。
    # P-04 优化：三路 Reranker 并行执行，避免串行等待
    ranked_episodes, ranked_entities, ranked_edges = await asyncio.gather(
        _rerank_episodes(query, all_episodes, top_k),
        _rerank_entities(query, all_entities, top_k * 2),
        _rerank_edges(query, all_edges, top_k * 2),
    )
    # Category 按 layer 降序排列（最抽象的在前），不经过 Reranker
    ranked_categories: list[Category] = sorted(all_categories, key=lambda c: c["layer"], reverse=True)

    # 时间范围补召回结果：绕过 Reranker 直接并入（Reranker 按字面相似打分，会把时间窗
    # 内低字面重合但时序上关键的片段踢掉）。合并后整体按时间升序，方便 LLM 重建时间线。
    if temporal_task is not None:
        try:
            temporal_eps = await temporal_task
            if temporal_eps:
                # 语义命中在前（rerank 相关性序）、时间分桶结果补尾：BEAM 教训——重排为
                # 时间序会把语义命中挤出注入预算，换进大量同时段但无关主题的片段。
                # 语义头部截 20 条给时间补位留出预算空间；temporal 部分按时间轴均匀采样到
                # 24 条——它是时间升序的，若超预算被尾部截断会恒丢时间窗后半段（BEAM eo__0
                # 教训：rubric 后半段检查点全 miss）。片段自带日期戳，时序重建交给 LLM。
                if len(temporal_eps) > 24:
                    _step = len(temporal_eps) / 24
                    temporal_eps = [temporal_eps[int(i * _step)] for i in range(24)]
                ranked_episodes = _merge_episodes(ranked_episodes[:20], temporal_eps)
                logger.info(
                    i18n_t(
                        "🧠 [Memory] 时间范围补召回 {p0} 条 Episode ({p1:%Y-%m-%d} ~ {p2:%Y-%m-%d})",
                        p0=len(temporal_eps),
                        p1=time_range[0],
                        p2=time_range[1],
                    )
                )
        except Exception as e:
            logger.warning(i18n_t("🧠 [Memory] 时间范围补召回失败: {e}", e=e))

    logger.info(
        i18n_t(
            "🧠 [Memory] 共计 {p0} 条 Episode, {p1} 个 Entity, {p2} 条 Edge, {p3} 个 Category",
            p0=len(all_episodes),
            p1=len(all_entities),
            p2=len(all_edges),
            p3=len(all_categories),
        )
    )

    # C11：把本次命中的 Edge 标记为"刚被检索"，刷新 last_accessed 供衰减 Worker 判定。
    # 后台 fire-and-forget，不阻塞检索返回。
    # "id" 是 ranked_edges 的固定字段，仅过滤 falsy 值（空串）以防上游异常数据。
    edge_ids = [e["id"] for e in ranked_edges if "id" in e and e["id"]]
    if edge_ids:
        from gsuid_core.ai_core.memory.database.models import AIMemEdge

        # 置信度富集（weight 轴）：从 DB 取每条边的 mention_count / decay_score，折算成
        # weight=佐证×新鲜度，覆盖构造期占位的 0.0。with_session 失败会吞异常返回 None，
        # 故 if 守一手；查不到的边保留占位 0.0（默认 min_edge_weight=0.0 时不影响）。
        conf_inputs = await AIMemEdge.get_confidence_inputs(edge_ids)
        if conf_inputs:
            for e in ranked_edges:
                if e["id"] in conf_inputs:
                    mc, decay = conf_inputs[e["id"]]
                    e["weight"] = compute_edge_confidence(mc, decay)

        async def _touch_edges_accessed() -> None:
            try:
                await AIMemEdge.touch_accessed(edge_ids)
            except Exception as _e:
                logger.debug(i18n_t("🧠 [Memory] 刷新 edge last_accessed 失败: {_e}", _e=_e))

        def _on_task_done(t):
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.warning(i18n_t("🧠 [Memory] 刷新 edge last_accessed 后台任务异常: {exc}", exc=exc))

        task = asyncio.create_task(_touch_edges_accessed())
        task.add_done_callback(_on_task_done)

    # C11 矛盾提示：命中边的 (src,tgt) 若有 Conflict 记录，取摘要随上下文注入。
    # 一次带索引 IN 查询（ix_mem_conflict_scope_sig），失败静默降级不影响检索。
    conflict_summaries: list[str] = []
    if ranked_edges and scope_keys:
        try:
            from gsuid_core.ai_core.memory.database.models import AIMemConflict

            sigs = list(
                {
                    f"{e['source_id']}|{e['target_id']}"
                    for e in ranked_edges[: memory_config.search_edge_count]
                    if e["source_id"] and e["target_id"]
                }
            )
            conflict_summaries = await AIMemConflict.get_by_signatures(scope_keys, sigs)
            if conflict_summaries:
                logger.info(i18n_t("🧠 [Memory] 矛盾提示命中 {p0} 条 Conflict 摘要", p0=len(conflict_summaries)))
        except Exception as e:
            logger.debug(i18n_t("🧠 [Memory] 矛盾提示查询失败: {e}", e=e))

    # 程序性/偏好记忆（默认开）：SQL-only 取本 user/scope 下的活跃规则，置顶强约束注入。
    # 选择性注入（意图门 + 能力域过滤）由 inject_preferences / preference_contexts 控制，避免
    # 每条回复都注入全部规则。命中规则刷新 last_applied_at（生命周期保护依据），后台 fire-and-forget。
    preference_items: list[PreferencePrompt] = []
    if memory_config.enable_preference_memory and scope_keys and inject_preferences:
        try:
            from gsuid_core.ai_core.memory.database.models import AIMemPreference

            # 过滤会先剔除部分行，故按相关能力域过滤时适当多取以免 cap 提前截断（偏好行本就有
            # per_context 上限，总量小，over-fetch 廉价）；不过滤时按 cap 直接取。
            fetch_limit = (
                memory_config.preference_max_inject * 3
                if preference_contexts is not None
                else memory_config.preference_max_inject
            )
            pref_rows = await AIMemPreference.get_active(scope_keys, limit=fetch_limit)
            if preference_contexts is not None:
                # 纠错规则与 general 永远保留（高价值、紧扣"刚纠正完的下一轮"场景）；
                # 其余软偏好仅当 target_context 命中本轮相关能力域时保留。get_active 已按
                # 纠错→高频→最近排序，过滤后再 cap 保持优先级。
                ctx_set = set(preference_contexts)
                pref_rows = [
                    r
                    for r in pref_rows
                    if r.is_correction or r.target_context == "general" or r.target_context in ctx_set
                ]
            pref_rows = pref_rows[: memory_config.preference_max_inject]
            preference_items = [
                {
                    "id": r.id,
                    "target_context": r.target_context,
                    "preference_rule": r.preference_rule,
                    "polarity": r.polarity,
                    "is_correction": r.is_correction,
                }
                for r in pref_rows
            ]
            if pref_rows:
                pref_ids = [r.id for r in pref_rows]

                async def _touch_prefs() -> None:
                    try:
                        await AIMemPreference.touch_applied(pref_ids)
                    except Exception as _e:
                        logger.debug(i18n_t("🧠 [Memory] 刷新 preference last_applied 失败: {_e}", _e=_e))

                pref_task = asyncio.create_task(_touch_prefs())
                pref_task.add_done_callback(_on_pref_task_done)
        except Exception as e:
            logger.warning(i18n_t("🧠 [Memory] 偏好检索失败: {e}", e=e))

    return MemoryContext(
        episodes=ranked_episodes,
        entities=ranked_entities,
        edges=ranked_edges,
        categories=ranked_categories,
        preferences=preference_items,
        conflicts=conflict_summaries,
        retrieval_meta={
            "s1_episodes": len(s1.episodes) if s1 else 0,
            "s2_episodes": sum(len(r.episodes) for r in s2_results),
            "scope_keys": scope_keys,
        },
        retrieval_paths=s2_retrieval_paths,
        temporal_mode=time_range is not None,
    )
