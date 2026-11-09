"""双路检索引擎（Dual-Route Retrieval - schema_v2）。

意图感知与混合检索（System-1 稠密检索 + FTS5 精确检索 + RRF 融合），
结合 schema_v2 四态记忆代数与多模态 Episode 上下文组装。
"""

from __future__ import annotations

import re
import time
import asyncio
from typing import Optional, Sequence, TypedDict
from datetime import datetime
from dataclasses import field, dataclass
from concurrent.futures import ThreadPoolExecutor

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.rag.reranker import RerankerProvider, get_reranker
from gsuid_core.ai_core.content_guard import wrap_untrusted
from gsuid_core.ai_core.control.lines import (
    MEMORY_STAGING_NOTE,
    MEMORY_CONFLICT_NOTE,
    MEMORY_TIMELINE_ORDER_NOTE,
)
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.memory.ingestion.algebra import _DANGLING_FACT_RE

from .types import (
    Edge,
    Entity,
    Episode,
    Category,
    RetrievalMeta,
    ThreadSummary,
    MemoryEventCue,
)
from .system1 import System1Result, system1_search
from .event_time import (
    query_time_window,
    event_at_from_text,
    looks_like_span_query,
    has_relative_time_span,
    looks_like_order_query,
    looks_like_summary_query,
    query_explicit_time_range,
)

MemoryIntent = str
_UNTRUSTED_WRAP_OVERHEAD = len(wrap_untrusted("memory_recall", ""))
_POINT_LOOKUP_EP_BUDGET = 8000
_TS_MAX = 4_102_444_800.0

_RERANK_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="reranker")
_TRIVIA_FACT_RE = re.compile(r"(提及|提到|在唱|演唱|唱歌|询问|聊到|讨论|说起|谈到)")
_IDENTITY_FACT_KEYWORDS = ("本人是", "就是", "叫做", "别名")

_SENSITIVE_FACT_RE = re.compile(
    r"催婚|相亲|离婚|分手|出轨|怀孕|堕胎|"
    r"工资|薪资|月薪|年薪|收入|欠钱|欠款|负债|贷款|房贷|房租|"
    r"抑郁|焦虑症|生病|住院|确诊|病历|"
    r"住址|家庭地址|身份证|手机号|电话号|银行卡"
)


class PreferencePrompt(TypedDict):
    """注入 Prompt 的单条偏好规则。"""

    id: Optional[str]
    target_context: str
    preference_rule: str
    polarity: str
    is_correction: bool


def _get_sensitive_extra_terms() -> list[str]:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    extra = ai_config.get_config("memory_sensitive_extra_terms").data
    if isinstance(extra, list):
        return [str(w).strip() for w in extra if str(w).strip()]
    return []


def _sensitive_fact_hit(fact: str, extra_terms: Sequence[str] = ()) -> bool:
    from gsuid_core.ai_core.content_guard import normalize_for_match

    normalized = normalize_for_match(fact)
    if _SENSITIVE_FACT_RE.search(normalized) or _SENSITIVE_FACT_RE.search(fact):
        return True
    return any(w in fact or w in normalized for w in extra_terms)


def _fact_mentions_speaker(edge: Edge, speaker_ids: set) -> bool:
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


def coalesce_ts(edge: Edge) -> float:
    if "event_at_ts" in edge:
        event_ts = edge["event_at_ts"]
        if isinstance(event_ts, (int, float)) and event_ts > 0:
            return float(event_ts)
    valid = edge["valid_at_ts"]
    return float(valid) if valid else 0.0


def _ts_to_dt(ts: float | int | None) -> datetime | None:
    if not isinstance(ts, (int, float)) or ts <= 0 or ts > _TS_MAX:
        return None
    return datetime.fromtimestamp(float(ts))


def _edge_date_prefix(e: Edge) -> str:
    said = _ts_to_dt(e["valid_at_ts"] if "valid_at_ts" in e else None)
    if said is None:
        return ""
    stamp = f"[{said.strftime('%Y-%m-%d')}] "
    ev = event_at_from_text(e["fact"] or "", said)
    if ev.date() != said.date():
        stamp += f"[发生 {ev.strftime('%Y-%m-%d')}] "
    return stamp


def _complete_fact_subject(fact: str, source_name: str) -> str:
    fact = fact.strip()
    if not fact:
        return ""
    _identity = any(k in fact for k in _IDENTITY_FACT_KEYWORDS)
    if source_name and source_name not in fact and not _identity:
        subject = f"用户{source_name}" if source_name.isdigit() else source_name
        if subject not in fact:
            fact = f"{subject}{fact}"
    return fact


def compute_edge_confidence(mention_count: Optional[int], decay_score: Optional[float]) -> float:
    mc = mention_count if (mention_count and mention_count > 0) else 1
    corroboration = 1.0 - 0.5**mc
    decay = decay_score if decay_score is not None else 1.0
    decay = min(max(decay, 0.0), 1.0)
    return round(corroboration * decay, 4)


def _edge_stamp(edge: Edge) -> str:
    stamp = coalesce_ts(edge)
    if stamp <= 0 or stamp > _TS_MAX:
        return ""
    return f" · {datetime.fromtimestamp(stamp).strftime('%Y-%m-%d')}"


@dataclass
class MemoryContext:
    episodes: list[Episode] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    events: list[MemoryEventCue] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    preferences: list[PreferencePrompt] = field(default_factory=list)
    retrieval_meta: RetrievalMeta = field(
        default_factory=lambda: RetrievalMeta(s1_episodes=0, s2_episodes=0, scope_keys=[])
    )
    retrieval_paths: list[list[dict[str, object]]] = field(default_factory=list)
    threads: list[ThreadSummary] = field(default_factory=list)
    temporal_mode: bool = False
    time_range: Optional[tuple[datetime, datetime]] = None
    seed_ids: list[str] = field(default_factory=list)
    session_scores: dict[str, float] = field(default_factory=dict)
    low_evidence: bool = False
    order_stages: list[Episode] = field(default_factory=list)
    pool_ids: list[str] = field(default_factory=list)
    inject_ids: list[str] = field(default_factory=list)
    skeleton_ids: list[str] = field(default_factory=list)
    covered: bool = False
    cover_ids: frozenset[str] = field(default_factory=frozenset)
    asker_id: str = ""
    reserved_episodes: list[Episode] = field(default_factory=list)
    memory_intent: MemoryIntent = "point_latest"

    def to_prompt_text(
        self,
        max_chars: int,
        priority_speakers: Optional[set] = None,
        current_speaker_ids: Optional[set] = None,
        query: str = "",
        wrap_recall: bool = True,
        now: datetime | None = None,
        memory_intent: str | None = None,
    ) -> str:
        intent = memory_intent or self.memory_intent

        def _take(items: list[str], budget: int) -> list[str]:
            out: list[str] = []
            used = 0
            for line in items:
                if used + len(line) > budget and out:
                    break
                out.append(line)
                used += len(line)
            return out

        def _take_even(items: list[str], budget: int) -> list[str]:
            if not items:
                return []
            if sum(len(x) for x in items) <= budget:
                return list(items)
            share = max(1, budget // len(items))
            out: list[str] = []
            used = 0
            for line in items:
                text = line if len(line) <= share else line[:share]
                if used + len(text) > budget and out:
                    break
                out.append(text)
                used += len(text)
            return out

        def _take_head_tail(items: list[str], budget: int) -> list[str]:
            n = len(items)
            if n == 0:
                return []
            if sum(len(x) for x in items) <= budget:
                return items
            keep: set[int] = set()
            used = 0
            i, j = 0, n - 1
            left = True
            while i <= j:
                idx = i if left else j
                ln = len(items[idx])
                if left:
                    i += 1
                else:
                    j -= 1
                left = not left
                if used + ln > budget and keep:
                    continue
                keep.add(idx)
                used += ln
            return [items[k] for k in range(n) if k in keep]

        from gsuid_core.ai_core.memory.retrieval.lexical import SPEECH_ACT_HINT

        parts: list[str] = []
        pref_block: Optional[str] = None
        self.inject_ids = []
        self.skeleton_ids = []
        speech_noted = False

        def _speech(block: str) -> str:
            nonlocal speech_noted
            if speech_noted:
                return block
            speech_noted = True
            return "（" + SPEECH_ACT_HINT + "）\n" + block

        if self.low_evidence:
            parts.append("【检索提示】记忆中没有与该问题直接相关的证据。")

        if self.events:
            if (
                intent in ("timeline_order", "broad_summary")
                or looks_like_order_query(query)
                or looks_like_summary_query(query)
                or looks_like_span_query(query)
            ):
                ev_lines: list[str] = []
                for ev in self.events[:16]:
                    src = ev["source"] if "source" in ev else ""
                    if src not in ("llm", "milestone"):
                        continue
                    stamp = (ev["stated_at"] or ev["event_at"] or "")[:10]
                    ev_lines.append(f"{len(ev_lines) + 1}. {stamp} · {ev['summary']}")
                if ev_lines:
                    parts.append(_speech("【事件线索】\n" + "\n".join(ev_lines)))

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
                pref_block = (
                    "【用户偏好/纠错 - 须严格遵守】"
                    "（各规则按其触发条件适用；未写明条件的按字面最小范围理解，不扩大化）\n" + "\n".join(taken)
                )

        if self.edges:
            fact_budget = int(max_chars * (0.15 if intent == "timeline_order" else 0.55))
            edges = list(self.edges[: memory_config.search_edge_count])

            def _edge_rank(e: Edge) -> tuple[int, int, float, float]:
                is_priority = 0 if (priority_speakers and e["source_name"] in priority_speakers) else 1
                is_trivia = 1 if _TRIVIA_FACT_RE.search(e["fact"] or "") else 0
                score = float(e["score"]) if "score" in e and e["score"] else 0.0
                return (is_priority, is_trivia, -round(score, 3), -coalesce_ts(e))

            if intent == "timeline_order":
                edges.sort(key=coalesce_ts)
            elif intent == "version_diff":
                edges.sort(key=lambda e: e["valid_at_ts"] or 0.0)
            else:
                edges.sort(key=_edge_rank)

            conflict = any(("resolution_state" in e and e["resolution_state"] == "contradicted") for e in edges)
            staging = any(("resolution_state" in e and e["resolution_state"] == "staging") for e in edges)

            from gsuid_core.ai_core.memory.retrieval.lexical import looks_like_latest_slot_query
            from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_revision_query

            keep_versions = (
                intent == "version_diff" or looks_like_revision_query(query) or looks_like_latest_slot_query(query)
            )
            now_ts = time.time()
            _extra_sensitive = _get_sensitive_extra_terms()
            fact_lines: list[str] = []
            seen_facts: set[str] = set()

            for i, e in enumerate(edges, start=1):
                invalid_at = e["invalid_at_ts"]
                if invalid_at and invalid_at < now_ts and not keep_versions:
                    continue
                if e["weight"] < memory_config.min_edge_weight:
                    continue
                fact = _complete_fact_subject(e["fact"], e["source_name"])
                if not fact or _DANGLING_FACT_RE.search(fact):
                    continue
                # 语义相关性防线：剔除仅因同日期/低分召回且与问句实体完全无关的边
                sc = float(e["score"] or 0.0) if "score" in e else 0.0
                if sc < 0.35 and query.strip():
                    from gsuid_core.ai_core.memory.retrieval.lexical import query_tokens

                    _stop_words = (
                        "今天",
                        "昨天",
                        "明天",
                        "现在",
                        "刚才",
                        "如何",
                        "怎样",
                        "什么",
                        "查查",
                        "帮我",
                        "一下",
                        "问问",
                    )
                    substantive = [tok for tok in query_tokens(query) if tok not in _stop_words]
                    s_name = str(e["source_name"] or "") if "source_name" in e else ""
                    t_name = str(e["target_name"] or "") if "target_name" in e else ""
                    if substantive and not any(tok in fact or tok in s_name or tok in t_name for tok in substantive):
                        continue
                if _sensitive_fact_hit(fact, _extra_sensitive) and not (
                    current_speaker_ids and _fact_mentions_speaker(e, current_speaker_ids)
                ):
                    continue
                sig = fact.strip().lower().replace(" ", "")[:24]
                if sig in seen_facts:
                    continue
                seen_facts.add(sig)

                state = e["resolution_state"] if "resolution_state" in e else "active"
                mark = ""
                if state == "contradicted":
                    mark = "⚠️[存在未消解矛盾] "
                elif state == "staging":
                    mark = "[待证实传闻] "
                elif state == "superseded" and intent == "version_diff":
                    mark = "[已于后续更新] "

                _id = any(k in fact for k in _IDENTITY_FACT_KEYWORDS)
                _dt = _edge_date_prefix(e)
                id_tag = "（记忆·待证）" if _id else ""
                fact_lines.append(f"• [记录 #{i}{_edge_stamp(e)}] {_dt}{mark}{id_tag}{fact}")
                if len(fact_lines) >= memory_config.fact_max_inject:
                    break

            taken = _take(fact_lines, fact_budget)
            if taken:
                header = "【核心事实 - 与当前问题相关】"
                if intent == "timeline_order":
                    header += MEMORY_TIMELINE_ORDER_NOTE
                if conflict:
                    header += MEMORY_CONFLICT_NOTE
                if staging:
                    header += MEMORY_STAGING_NOTE
                parts.append(_speech(header + "\n" + "\n".join(taken)))

        if self.conflicts:
            conf_lines = [f"• {s[:300]}" for s in self.conflicts[:6]]
            taken = _take(conf_lines, int(max_chars * 0.12))
            if taken:
                parts.append(f"【陈述不一致】{MEMORY_CONFLICT_NOTE}\n" + "\n".join(taken))

        if self.categories:
            cats = [c for c in self.categories if (not query or (c["name"] and c["name"] in query))]
            if cats:
                cat_budget = int(max_chars * 0.15)
                sorted_cats = sorted(cats, key=lambda c: c["layer"], reverse=True)
                cat_lines = [f"• [L{c['layer']}] {c['name']}: {(c['summary'] or '')[:100]}" for c in sorted_cats[:6]]
                taken = _take(cat_lines, cat_budget)
                if taken:
                    parts.append("【语义类目摘要】\n" + "\n".join(taken))

        if self.episodes:
            _pref_used = (len(pref_block) + 2) if pref_block else 0
            used = sum(len(p) for p in parts) + 2 * len(parts) + _pref_used + _UNTRUSTED_WRAP_OVERHEAD
            ep_budget = max_chars - used
            if ep_budget > 120:
                from gsuid_core.ai_core.memory.retrieval.lexical import (
                    is_fact_sheet,
                    is_session_read,
                    excerpt_session_read,
                    looks_like_set_query,
                    looks_like_sum_query,
                    looks_like_count_query,
                    apply_query_episode_pack,
                    looks_like_compare_query,
                    collapse_document_episodes,
                    looks_like_attribute_query,
                    is_ingested_document_episode,
                    looks_like_latest_slot_query,
                    wants_parallel_document_coverage,
                )
                from gsuid_core.ai_core.memory.retrieval.event_time import (
                    query_only_item_cap,
                    looks_like_duration_query,
                )

                cross_session = (
                    intent in ("timeline_order", "broad_summary")
                    or looks_like_order_query(query)
                    or looks_like_span_query(query)
                    or looks_like_summary_query(query)
                    or looks_like_count_query(query)
                    or looks_like_sum_query(query)
                    or looks_like_compare_query(query)
                )
                from gsuid_core.ai_core.memory.retrieval.lexical import (
                    query_tokens,
                    token_in_text,
                )

                eps = list(self.episodes)
                q_toks = query_tokens(query) if query else []
                self_eps = [
                    e
                    for e in eps
                    if (e["scope_key"] or "").startswith("self:")
                    and (not q_toks or any(token_in_text(t, e["content"] or "") for t in q_toks))
                ]
                other_eps = [e for e in eps if not (e["scope_key"] or "").startswith("self:")]

                clock = now if now is not None else datetime.now()
                recent_self: list[Episode] = []
                old_self: list[Episode] = []
                for ep in self_eps:
                    ts_raw = (ep["valid_at"] or "").strip()[:19].replace("T", " ")
                    recent = False
                    if ts_raw:
                        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                            try:
                                ts = datetime.strptime(ts_raw, fmt)
                                recent = 0 <= (clock - ts).total_seconds() <= 2 * 3600
                                break
                            except ValueError:
                                pass
                    if recent:
                        recent_self.append(ep)
                    else:
                        old_self.append(ep)

                self_budget = (
                    0 if (cross_session or not old_self) else min(int(max_chars * 0.10), max(0, ep_budget // 5))
                )
                recent_budget = 0 if (cross_session or not recent_self) else min(int(ep_budget * 0.25), 400)
                other_budget = max(0, ep_budget - self_budget - recent_budget)
                ep_cap = 480 if (self.temporal_mode or intent == "timeline_order") else 1000
                parallel_q = wants_parallel_document_coverage(query) or self.covered
                doc_cap = max_chars if parallel_q else (12000 if looks_like_set_query(query) else max(ep_cap, 2400))

                def _assistant_turn(raw: str) -> bool:
                    low = raw.lstrip().lower()
                    return low.startswith("assistant:") or raw.lstrip().startswith("[我此前说过]")

                def _ep_lines(items: list[Episode], *, self_mark: bool) -> list[str]:
                    from gsuid_core.ai_core.memory.retrieval.lexical import parse_episode_valid_at

                    dated: list[str] = []
                    undated: list[str] = []
                    seen_content: set[str] = set()
                    for ep in items:
                        raw = (ep["content"] or "").strip()
                        if len(raw) < 4:
                            continue
                        if "[决策结论]" in raw or "决策：走 " in raw:
                            continue
                        key = raw[:80]
                        if key in seen_content:
                            continue
                        seen_content.add(key)
                        prefix = "[我此前说过] " if self_mark else ""
                        shown = raw[:ep_cap]
                        if is_ingested_document_episode(raw):
                            undated.append(f"{prefix}{raw[:doc_cap]}")
                            continue
                        if is_session_read(ep):
                            shown = excerpt_session_read(raw, query) or raw[:2400]
                        elif is_fact_sheet(ep):
                            shown = raw[:4800]
                        elif _assistant_turn(raw) and len(raw) > ep_cap:
                            from gsuid_core.ai_core.memory.retrieval.lexical import (
                                excerpt_keep_numbers,
                                excerpt_around_ordinal,
                            )

                            shown = excerpt_around_ordinal(raw, query, max(ep_cap, 900))
                            if not shown:
                                shown = excerpt_keep_numbers(raw, ep_cap)
                        if (looks_like_summary_query(query) or intent == "broad_summary") and _assistant_turn(raw):
                            from gsuid_core.ai_core.memory.retrieval.lexical import excerpt_around_tokens

                            shown = excerpt_around_tokens(raw, query, 480) or shown
                        ts = (ep["valid_at"] or "").strip()[:19].replace("T", " ")
                        if ts:
                            stamp = f"[{ts}] "
                            said = parse_episode_valid_at(ep["valid_at"] or "")
                            if said is not None:
                                ev = event_at_from_text(raw, said)
                                if ev.date() != said.date():
                                    stamp += f"[发生 {ev.strftime('%Y-%m-%d')}] "
                            dated.append(f"{stamp}{prefix}{shown}")
                        else:
                            undated.append(f"{prefix}{shown}")
                    return dated + undated

                if self.temporal_mode and self.time_range is not None:
                    from gsuid_core.ai_core.memory.retrieval.lexical import parse_episode_valid_at

                    _t0, _t1 = self.time_range

                    def _in_win(ep: Episode) -> bool:
                        dt = parse_episode_valid_at(ep["valid_at"] or "")
                        return dt is not None and _t0 <= dt < _t1

                    # 窗内优先前置，窗外后置保底，避免时间窗预估偏差导致证据硬丢失
                    other_eps = [e for e in other_eps if _in_win(e)] + [e for e in other_eps if not _in_win(e)]

                if (
                    looks_like_order_query(query)
                    or looks_like_span_query(query)
                    or looks_like_summary_query(query)
                    or intent in ("timeline_order", "broad_summary")
                ):
                    ep_cap = 480 if not self.covered else max(ep_cap, 4800)
                haystack = list(other_eps)

                if self.covered:
                    packed = list(haystack)
                    cover_first = [ep for ep in packed if "id" in ep and str(ep["id"]) in self.cover_ids]
                    rest_pack = [ep for ep in packed if "id" not in ep or str(ep["id"]) not in self.cover_ids]
                    packed = cover_first + rest_pack
                else:
                    wide_pack = cross_session or looks_like_set_query(query) or parallel_q
                    pack_cap = max_chars if wide_pack else _POINT_LOOKUP_EP_BUDGET
                    packed = apply_query_episode_pack(
                        haystack,
                        query,
                        temporal_mode=self.temporal_mode,
                        time_range=self.time_range,
                        char_budget=min(other_budget, pack_cap),
                        asker_id=self.asker_id,
                        memory_intent=intent,
                    )

                if looks_like_set_query(query):
                    packed = collapse_document_episodes(packed)
                if cross_session:
                    packed = packed[:60]
                if (
                    looks_like_order_query(query)
                    or looks_like_summary_query(query)
                    or intent in ("timeline_order", "broad_summary")
                ):
                    sheet_tail = [e for e in packed if is_fact_sheet(e)]
                    core_packed = [e for e in packed if not is_fact_sheet(e)]
                    core_packed.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
                    packed = core_packed + sheet_tail

                if looks_like_order_query(query) or intent == "timeline_order":
                    from gsuid_core.ai_core.memory.retrieval.lexical import (
                        pack_order_dialogue,
                        format_order_skeleton,
                    )

                    non_sheet_packed = [e for e in packed if not is_fact_sheet(e)]
                    stage_eps = (
                        self.order_stages
                        if self.order_stages and len(self.order_stages) >= len(non_sheet_packed)
                        else non_sheet_packed
                    )
                    skel = format_order_skeleton(stage_eps, query=query)
                    self.skeleton_ids = [e["id"] for e in stage_eps if "id" in e]
                    if skel:
                        _sk_block = (
                            "【事件顺序（按时间）】"
                            "（Chronological event sequence; answer strictly following this order）：\n"
                            + "\n".join(skel)
                        )
                        parts.append(_sk_block)
                        other_budget = max(0, other_budget - len(_sk_block) - 2)
                    n = query_only_item_cap(query) or len(skel)
                    dial_n = max(n * 2, min(n * 3, 24)) if n else 16
                    packed = pack_order_dialogue(haystack, query, dial_n)
                    self.inject_ids = [e["id"] for e in packed if "id" in e]

                if not self.inject_ids:
                    self.inject_ids = [e["id"] for e in packed if "id" in e]

                user_eps = [e for e in packed if not _assistant_turn(e["content"] or "")]
                asst_eps = [e for e in packed if _assistant_turn(e["content"] or "")]

                if (
                    self.reserved_episodes
                    and looks_like_attribute_query(query)
                    and not looks_like_count_query(query)
                    and not looks_like_sum_query(query)
                    and not looks_like_order_query(query)
                ):
                    from gsuid_core.ai_core.memory.retrieval.lexical import render_value_timeline

                    reserve_budget = min(3600, max(0, other_budget // 2))
                    reserved_block = render_value_timeline(self.reserved_episodes, query, reserve_budget)
                    if reserved_block:
                        parts.append(_speech(reserved_block))
                        other_budget = max(0, other_budget - len(reserved_block) - 2)
                        reserved_ids = {ep["id"] for ep in self.reserved_episodes if "id" in ep}
                        user_eps = [ep for ep in user_eps if "id" not in ep or ep["id"] not in reserved_ids]

                timeline_q = (
                    looks_like_span_query(query) or looks_like_duration_query(query) or looks_like_summary_query(query)
                )

                if (
                    intent in ("timeline_order", "broad_summary")
                    or looks_like_order_query(query)
                    or looks_like_span_query(query)
                    or looks_like_summary_query(query)
                ):
                    core_user_eps = [e for e in user_eps if not is_fact_sheet(e)]
                    sheet_eps = [e for e in user_eps if is_fact_sheet(e)]
                    want_asst = (intent == "broad_summary" or looks_like_summary_query(query)) and bool(asst_eps)
                    user_share = (other_budget * 3) // 4 if want_asst else other_budget
                    taken = _take_head_tail(_ep_lines(core_user_eps, self_mark=False), user_share)
                    used_chars = sum(len(x) for x in taken)
                    if want_asst and other_budget - used_chars > 120:
                        taken_asst = _take_head_tail(
                            _ep_lines(asst_eps, self_mark=False),
                            other_budget - used_chars,
                        )
                        taken = taken + taken_asst
                        used_chars = sum(len(x) for x in taken)
                    if sheet_eps and other_budget - used_chars > 200:
                        taken = taken + _take(
                            _ep_lines(sheet_eps, self_mark=False),
                            other_budget - used_chars,
                        )
                elif self.temporal_mode:
                    taken = _take(_ep_lines(user_eps, self_mark=False), other_budget)
                elif not self.covered and (
                    looks_like_count_query(query)
                    or looks_like_sum_query(query)
                    or looks_like_compare_query(query)
                    or looks_like_latest_slot_query(query)
                ):
                    count_budget = (
                        other_budget
                        if (
                            looks_like_count_query(query)
                            or looks_like_sum_query(query)
                            or looks_like_compare_query(query)
                        )
                        else min(other_budget, _POINT_LOOKUP_EP_BUDGET)
                    )
                    taken = _take(_ep_lines(user_eps, self_mark=False), count_budget)
                elif looks_like_duration_query(query) and not self.covered:
                    taken = _take_head_tail(
                        _ep_lines(user_eps, self_mark=False), min(other_budget, _POINT_LOOKUP_EP_BUDGET)
                    )
                elif timeline_q and not self.covered:
                    taken = _take(_ep_lines(user_eps, self_mark=False), min(other_budget, _POINT_LOOKUP_EP_BUDGET))
                else:
                    _ep_taken = _ep_lines(user_eps, self_mark=False)
                    if self.covered and self.cover_ids:
                        cover_eps = [ep for ep in user_eps if "id" in ep and str(ep["id"]) in self.cover_ids]
                        rest_eps = [ep for ep in user_eps if "id" not in ep or str(ep["id"]) not in self.cover_ids]
                        if cover_eps:
                            taken_cover = _take_even(_ep_lines(cover_eps, self_mark=False), other_budget)
                            rest_budget = max(0, other_budget - sum(len(x) for x in taken_cover))
                            taken_other = taken_cover + _take(_ep_lines(rest_eps, self_mark=False), rest_budget)
                        else:
                            taken_other = _take_even(_ep_taken, other_budget)
                    else:
                        _picker = _take_even if self.covered else _take
                        taken_other = _picker(_ep_taken, other_budget)
                    rest = other_budget - sum(len(x) for x in taken_other)
                    if rest > 80 and asst_eps:
                        taken_other.extend(_take(_ep_lines(asst_eps, self_mark=False), rest))
                    taken_recent = _take(_ep_lines(recent_self, self_mark=True), recent_budget)
                    taken_self = _take(_ep_lines(old_self, self_mark=True), min(self_budget, 400))
                    taken = taken_other + taken_recent + taken_self

                if taken:
                    ep_header = "【相关对话片段】"
                    if intent == "timeline_order":
                        ep_header += MEMORY_TIMELINE_ORDER_NOTE
                    parts.append(_speech(ep_header + "\n" + "\n".join(taken)))

        blocks: list[str] = [pref_block] if pref_block else []
        if parts:
            recall_text = "\n\n".join(parts)
            marker = "\n...[记忆已截断]"
            budget = max_chars - (len(pref_block) + 2 if pref_block else 0) - _UNTRUSTED_WRAP_OVERHEAD
            if budget <= len(marker):
                recall_text = ""
            elif len(recall_text) > budget:
                recall_text = recall_text[: budget - len(marker)] + marker
            if recall_text:
                blocks.append(wrap_untrusted("memory_recall", recall_text) if wrap_recall else recall_text)
        return "\n\n".join(blocks)


def _on_pref_task_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(i18n_t("log.memory.background_task_refreshing_preference", exc=exc))


async def _run_sync_rerank(reranker: RerankerProvider, query: str, texts: list[str]) -> list[float]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _RERANK_EXECUTOR,
        lambda: list(reranker.rerank(query, texts)),
    )


async def _rerank_episodes(query: str, items: list[Episode], top_k: int) -> list[Episode]:
    if not items:
        return []
    reranker = get_reranker()
    if reranker is None:
        return items[:top_k]
    texts = [item["content"] for item in items]
    scores = list(await _run_sync_rerank(reranker, query, texts))
    if len(scores) != len(items):
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:top_k]]


async def _rerank_entities(query: str, items: list[Entity], top_k: int) -> list[Entity]:
    if not items:
        return []
    reranker = get_reranker()
    if reranker is None:
        return items[:top_k]
    texts = [item["summary"] for item in items]
    scores = list(await _run_sync_rerank(reranker, query, texts))
    if len(scores) != len(items):
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:top_k]]


async def _rerank_edges(query: str, items: list[Edge], top_k: int) -> list[Edge]:
    if not items:
        return []
    reranker = get_reranker()
    if reranker is None:
        return items[:top_k]
    texts = [item["fact"] for item in items]
    scores = list(await _run_sync_rerank(reranker, query, texts))
    if len(scores) != len(items):
        return items[:top_k]
    ranked = sorted(zip(scores, items), key=lambda x: x[0], reverse=True)
    kept: list[Edge] = []
    for score, item in ranked[:top_k]:
        item["score"] = score
        if score < memory_config.min_edge_rerank_score:
            continue
        kept.append(item)
    return kept


def _merge_episodes(*lists: Sequence[Episode]) -> list[Episode]:
    seen: dict[str, Episode] = {}
    for items in lists:
        for item in items:
            seen[item["id"]] = item
    return list(seen.values())


def _merge_edges(*lists: Sequence[Edge]) -> list[Edge]:
    seen: dict[str, Edge] = {}
    for items in lists:
        for item in items:
            seen[item["id"]] = item
    return list(seen.values())


async def _fts_episodes(query: str, scope_keys: list[str], limit: int) -> list[Episode]:
    from sqlmodel import col, select

    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.fts import match_episode_ids
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.lexical import query_tokens

    ids = await match_episode_ids(scope_keys, query_tokens(query), limit)
    if not ids or async_maker is None:
        return []
    async with async_maker() as session:
        result = await session.execute(select(AIMemEpisode).where(col(AIMemEpisode.id).in_(ids)))
        by_id = {row.id: row for row in result.scalars().all()}
    out: list[Episode] = []
    for eid in ids:
        row = by_id[eid] if eid in by_id else None
        if row is None:
            continue
        out.append(
            {
                "id": row.id,
                "content": row.content,
                "valid_at": str(row.valid_at),
                "scope_key": row.scope_key,
                "embedding": [],
                "session_id": row.session_id or "",
                "thread_id": row.thread_id or "",
                "turn_index": row.turn_index,
            }
        )
    return out


def _edge_to_typed(row: object, names: dict[str, str]) -> Edge | None:
    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    if not isinstance(row, AIMemEdge):
        return None
    return {
        "id": row.id,
        "source_id": row.source_entity_id,
        "target_id": row.target_entity_id,
        "source_name": names[row.source_entity_id] if row.source_entity_id in names else "",
        "target_name": names[row.target_entity_id] if row.target_entity_id in names else "",
        "fact": row.fact,
        "weight": 1.0,
        "score": 0.0,
        "valid_at_ts": row.valid_at.timestamp() if row.valid_at else None,
        "invalid_at_ts": row.invalid_at.timestamp() if row.invalid_at else None,
        "expired_at_ts": row.expired_at.timestamp() if row.expired_at else None,
        "edge_kind": row.edge_kind,
        "slot_key": row.slot_key,
        "resolution_state": row.resolution_state,
        "superseded_by_id": row.superseded_by_id,
        "contradicts_edge_id": row.contradicts_edge_id,
        "provenance_tier": "transcribed" if row.resolution_state == "staging" else "self",
        "event_at_ts": row.event_at.timestamp() if row.event_at else None,
    }


async def _refresh_edges_from_db(edges: list[Edge], scope_keys: list[str]) -> list[Edge]:
    from sqlmodel import col, select

    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity

    if not edges:
        return edges
    ids = [e["id"] for e in edges if e["id"]]
    score_by_id = {e["id"]: (e["score"], e["weight"]) for e in edges if e["id"]}
    async with async_maker() as session:
        result = await session.execute(select(AIMemEdge).where(col(AIMemEdge.id).in_(ids)))
        rows = list(result.scalars().all())
    names = await AIMemEntity.get_names_by_ids(
        list({r.source_entity_id for r in rows} | {r.target_entity_id for r in rows})
    )
    by_id: dict[str, Edge] = {}
    for row in rows:
        typed = _edge_to_typed(row, names)
        if typed is not None:
            if row.id in score_by_id:
                typed["score"], typed["weight"] = score_by_id[row.id]
            by_id[row.id] = typed
    refreshed = [by_id[e["id"]] if e["id"] in by_id else e for e in edges]

    source_ids = list({e["source_id"] for e in refreshed if e["source_id"]})
    slot_scores: dict[tuple[str, str], float] = {}
    linked_scores: dict[str, float] = {}
    for e in refreshed:
        sc = e["score"]
        slot = e["slot_key"] if "slot_key" in e and e["slot_key"] else ""
        if slot and e["source_id"]:
            key = (e["source_id"], slot)
            if key not in slot_scores or sc > slot_scores[key]:
                slot_scores[key] = sc
        sup_id = e["superseded_by_id"] if "superseded_by_id" in e and e["superseded_by_id"] else ""
        if sup_id:
            prev_sup = linked_scores[sup_id] if sup_id in linked_scores else 0.0
            if sc > prev_sup:
                linked_scores[sup_id] = sc
        con_id = e["contradicts_edge_id"] if "contradicts_edge_id" in e and e["contradicts_edge_id"] else ""
        if con_id:
            prev_con = linked_scores[con_id] if con_id in linked_scores else 0.0
            if sc > prev_con:
                linked_scores[con_id] = sc

    extra: list[Edge] = []
    have = {e["id"] for e in refreshed}
    for sk in scope_keys:
        for row in await AIMemEdge.get_for_entities(source_ids, sk, limit=60):
            if row.id in have:
                continue
            slot_key = (row.source_entity_id, row.slot_key or "")
            if row.id in linked_scores:
                inherited_score = linked_scores[row.id]
            elif row.superseded_by_id and row.superseded_by_id in score_by_id:
                inherited_score = score_by_id[row.superseded_by_id][0]
            elif row.contradicts_edge_id and row.contradicts_edge_id in score_by_id:
                inherited_score = score_by_id[row.contradicts_edge_id][0]
            elif row.slot_key and slot_key in slot_scores:
                inherited_score = slot_scores[slot_key]
            elif row.resolution_state == "contradicted":
                inherited_score = 0.1
            else:
                continue
            typed = _edge_to_typed(row, names)
            if typed is not None:
                typed["score"] = inherited_score
                extra.append(typed)
                have.add(row.id)
    return refreshed + extra


async def _apply_intent_edges(
    intent: str,
    ranked_edges: list[Edge],
    scope_keys: list[str],
    *,
    clock_at: datetime | None,
) -> list[Edge]:
    del clock_at
    if not scope_keys:
        return ranked_edges
    ranked_edges = await _refresh_edges_from_db(ranked_edges, scope_keys)
    if intent == "point_latest":
        filtered = [
            e
            for e in ranked_edges
            if ("resolution_state" not in e or e["resolution_state"] in ("active", "contradicted"))
        ]
        blocking: set[tuple[str, str]] = set()
        for e in filtered:
            state = e["resolution_state"] if "resolution_state" in e else "active"
            slot = e["slot_key"] if "slot_key" in e else ""
            if state == "active" and slot:
                blocking.add((e["source_id"], slot))
        return [
            e
            for e in filtered
            if not (
                ("resolution_state" in e and e["resolution_state"] == "staging")
                and ("slot_key" in e and e["slot_key"])
                and (e["source_id"], e["slot_key"]) in blocking
            )
        ]

    if intent == "broad_summary":
        return [e for e in ranked_edges if ("edge_kind" not in e or e["edge_kind"] in ("attribute", "relation"))]

    if intent == "version_diff":
        from gsuid_core.ai_core.memory.database.models import AIMemEdge

        pairs = [(e["source_id"], e["target_id"]) for e in ranked_edges[:12]]
        have = {e["id"] for e in ranked_edges}
        edge_names: dict[str, str] = {}
        for e in ranked_edges:
            edge_names[e["source_id"]] = e["source_name"]
            edge_names[e["target_id"]] = e["target_name"]
        extra: list[Edge] = []
        for sk in scope_keys:
            for row in await AIMemEdge.list_pair_versions(sk, pairs, limit=60):
                if row.id in have:
                    continue
                typed = _edge_to_typed(row, edge_names)
                if typed is None:
                    continue
                extra.append(typed)
                have.add(row.id)
        return ranked_edges + extra

    if intent == "timeline_order":
        from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity, AIMemThread

        threads: list[tuple[str, str]] = []
        for sk in scope_keys:
            for th in await AIMemThread.list_active(sk, limit=20):
                threads.append((sk, th.id))
        extra_edges: list[Edge] = []
        milestone_names: dict[str, str] = {}
        for sk, thread_id in threads:
            rows = await AIMemEdge.list_thread_milestones(sk, thread_id, limit=80, ascending=True)
            entity_ids = list({r.source_entity_id for r in rows})
            milestone_names.update(await AIMemEntity.get_names_by_ids(entity_ids))
            have = {e["id"] for e in ranked_edges}
            for row in rows:
                if row.id in have:
                    continue
                typed = _edge_to_typed(row, milestone_names)
                if typed is not None:
                    extra_edges.append(typed)
        merged = _merge_edges(extra_edges, ranked_edges)
        merged.sort(key=coalesce_ts)
        return merged
    return ranked_edges


def _episode_from_stored(
    row_id: str,
    content: str | None,
    valid_at: datetime | None,
    scope_key: str,
) -> Episode:
    return {
        "id": row_id,
        "content": content or "",
        "valid_at": valid_at.strftime("%Y-%m-%d %H:%M:%S") if valid_at else "",
        "scope_key": scope_key,
        "embedding": [],
        "session_id": "",
        "turn_index": 0,
    }


async def _parallel_document_sources(scope_keys: list[str]) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.lexical import episode_source_key, is_ingested_document_episode

    seen: set[str] = set()
    for sk in scope_keys:
        try:
            heads = await AIMemEpisode.list_ingested_document_heads(sk, limit=4000)
        except (OSError, RuntimeError, SQLAlchemyError) as exc:
            logger.warning(i18n_t("log.memory.document_pool_load_failed", e=exc))
            return 0
        for snippet in heads:
            if not is_ingested_document_episode(snippet):
                continue
            key = episode_source_key(snippet)
            if key:
                seen.add(key)
            if len(seen) >= 2:
                return len(seen)
    return len(seen)


async def _load_document_coverage(query: str, scope_keys: list[str]) -> list[Episode]:
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.lexical import (
        DOC_COVER_EXTRACT_BUDGET,
        cover_document_excerpts,
        has_document_coverage_tokens,
    )

    if await _parallel_document_sources(scope_keys) < 2:
        return []
    if not has_document_coverage_tokens(query):
        return []
    pool: list[Episode] = []
    for sk in scope_keys:
        try:
            rows = await AIMemEpisode.list_ingested_documents(sk, limit=12000)
        except (OSError, RuntimeError, SQLAlchemyError) as exc:
            logger.warning(i18n_t("log.memory.document_pool_load_failed", e=exc))
            return []
        for row in rows:
            pool.append(_episode_from_stored(row.id, row.content, row.valid_at, row.scope_key))
    if not pool:
        return []
    return cover_document_excerpts(pool, query, char_budget=DOC_COVER_EXTRACT_BUDGET)


async def _expand_document_siblings(hits: list[Episode], scope_keys: list[str]) -> list[Episode]:
    from sqlalchemy.exc import SQLAlchemyError

    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.lexical import (
        episode_source_key,
        is_ingested_document_episode,
        merge_document_sibling_episodes,
    )

    titles: list[str] = []
    seen_titles: set[str] = set()
    for ep in hits:
        raw = ep["content"] or ""
        if not is_ingested_document_episode(raw):
            continue
        key = episode_source_key(raw)
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        titles.append(key)
        if len(titles) >= 8:
            break
    if not titles or not scope_keys:
        return hits
    pool: list[Episode] = []
    for sk in scope_keys:
        for title in titles:
            try:
                rows = await AIMemEpisode.list_document_chunks(sk, title, limit=40)
            except (OSError, RuntimeError, SQLAlchemyError) as exc:
                logger.warning(i18n_t("log.memory.document_pool_load_failed", e=exc))
                return hits
            for row in rows:
                pool.append(_episode_from_stored(row.id, row.content, row.valid_at, row.scope_key))
    if not pool:
        return hits
    return merge_document_sibling_episodes(hits, pool)


async def _fetch_temporal_episodes(
    query: str,
    scope_keys: list[str],
    start: datetime,
    end: datetime,
    buckets: int = 6,
    per_bucket: int = 8,
) -> list[Episode]:
    from gsuid_core.ai_core.memory.vector.ops import search_episodes_in_range

    from .event_time import temporal_search_query

    topic = temporal_search_query(query)
    if len(topic) < 8:
        return []
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    span = (end_ts - start_ts) / max(buckets, 1)

    async def _one_bucket(i: int) -> list[Episode]:
        try:
            return await search_episodes_in_range(
                topic,
                scope_keys,
                start_ts + i * span,
                start_ts + (i + 1) * span,
                top_k=per_bucket,
            )
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug(i18n_t("log.memory.time_bucket_retrieval", i=i, e=e))
            return []

    results = await asyncio.gather(*[_one_bucket(i) for i in range(buckets)])
    seen: dict[str, Episode] = {}
    for eps in results:
        for ep in eps:
            seen[ep["id"]] = ep
    merged = list(seen.values())
    merged.sort(key=lambda ep: ep["valid_at"] or "")
    return merged


async def _select_order_stages(query: str, episodes: list[Episode], n: int) -> list[Episode]:
    from gsuid_core.ai_core.memory.retrieval.lexical import (
        _embedding_map,
        _assistant_turn,
        _topic_hit_count,
        _skip_order_noise,
        _speaker_stripped,
        pack_first_mention_episodes,
    )
    from gsuid_core.ai_core.memory.retrieval.event_time import order_topic_span, temporal_search_query
    from gsuid_core.ai_core.memory.retrieval.order_reconstruct import select_by_topic_scores

    if n <= 0 or not episodes:
        return []
    user: list[Episode] = []
    for ep in episodes:
        raw = ep["content"] or ""
        if _assistant_turn(raw) or _skip_order_noise(_speaker_stripped(raw)):
            continue
        user.append(ep)
    if not user:
        return []
    topic = order_topic_span(query) or temporal_search_query(query) or query
    if topic and len(user) > 120:
        user = sorted(user, key=lambda e: -_topic_hit_count(topic, e["content"] or ""))[:120]
        user.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    missing = [e["id"] for e in user if "id" in e and not (e["embedding"] if "embedding" in e else [])]
    if missing:
        from gsuid_core.ai_core.memory.vector.ops import retrieve_episode_dense_vectors

        vecs = await retrieve_episode_dense_vectors(missing)
        for e in user:
            eid = e["id"] if "id" in e else ""
            if eid and eid in vecs:
                e["embedding"] = vecs[eid]
    reranker = get_reranker()
    pool = user
    if reranker is not None and topic:
        texts = [_speaker_stripped(e["content"] or "")[:200] for e in user]
        scores = list(await _run_sync_rerank(reranker, topic, texts))
        if len(scores) == len(user):
            have = _embedding_map(user)
            return select_by_topic_scores(user, scores, n, have)
    return pack_first_mention_episodes(pool, query, n)


def _rrf_fuse_episodes(
    dense_eps: list[Episode],
    fts_eps: list[Episode],
    keep_assistant: bool,
) -> list[Episode]:
    from gsuid_core.ai_core.memory.retrieval.lexical import _assistant_turn

    scores: dict[str, float] = {}
    ep_map: dict[str, Episode] = {}
    for rank, ep in enumerate(dense_eps):
        eid = ep["id"]
        ep_map[eid] = ep
        asst = _assistant_turn(ep["content"] or "")
        mult = 0.8 if asst and not keep_assistant else 1.25
        scores[eid] = scores.get(eid, 0.0) + (1.0 / (60.0 + rank)) * mult
    for rank, ep in enumerate(fts_eps):
        eid = ep["id"]
        ep_map[eid] = ep
        asst = _assistant_turn(ep["content"] or "")
        mult = 0.8 if asst and not keep_assistant else 1.5
        scores[eid] = scores.get(eid, 0.0) + (1.0 / (60.0 + rank)) * mult
    sorted_eids = sorted(scores, key=lambda x: scores[x], reverse=True)
    return [ep_map[eid] for eid in sorted_eids]


async def dual_route_retrieve(
    query: str,
    user_id: str,
    *,
    enable_system2: bool,
    group_id: Optional[str] = None,
    top_k: int = 20,
    enable_user_global: bool = True,
    inject_preferences: bool = True,
    preference_contexts: Optional[list[str]] = None,
    bot_id: str = "",
    bot_self_id: str = "",
    include_self: bool = True,
    persona_name: str = "",
    memory_intent: str = "point_latest",
    clock_at: datetime | None = None,
) -> MemoryContext:
    del enable_system2
    from gsuid_core.ai_core.memory.observer import pending_episodes_for_scopes
    from gsuid_core.ai_core.memory.database.models import AIMemEntity, AIMemPreference
    from gsuid_core.ai_core.memory.retrieval.lexical import (
        token_in_text,
        looks_like_sum_query,
        expand_lexical_recall,
        looks_like_count_query,
        looks_like_attribute_query,
        should_keep_assistant_hits,
        wants_parallel_document_coverage,
    )

    scope_keys: list[str] = []
    if group_id:
        group_scope = make_scope_key(ScopeType.GROUP, group_id)
        scope_keys.append(group_scope)
        try:
            from gsuid_core.ai_core.memory.group_profile import (
                get_term_mappings,
                expand_query_with_aliases,
            )

            mappings = await get_term_mappings(group_scope)
            expanded = expand_query_with_aliases(query, mappings)
            if expanded != query:
                query = expanded
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug(i18n_t("log.memory.alias_expansion", e=e))
    if user_id and (not group_id or enable_user_global):
        scope_keys.append(make_scope_key(ScopeType.USER_GLOBAL, user_id))
    if include_self and bot_self_id.strip():
        from gsuid_core.ai_core.memory.scope import self_scope_read_keys

        for self_scope in self_scope_read_keys(bot_self_id.strip(), persona_name):
            if self_scope not in scope_keys:
                scope_keys.append(self_scope)

    preference_items: list[PreferencePrompt] = []
    if memory_config.enable_preference_memory and scope_keys and inject_preferences:
        try:
            fetch_limit = (
                memory_config.preference_max_inject * 3
                if preference_contexts is not None
                else memory_config.preference_max_inject
            )
            pref_rows = await AIMemPreference.get_active(scope_keys, limit=fetch_limit)
            pref_rows = [r for r in pref_rows if r.target_context != "possession"]
            if preference_contexts is not None:
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
                    except (OSError, RuntimeError, ValueError) as e:
                        logger.debug(i18n_t("log.memory.refresh_preference_last_applied", _e=e))

                task = asyncio.create_task(_touch_prefs())
                task.add_done_callback(_on_pref_task_done)
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning(i18n_t("log.memory.preference_retrieval", e=e))

    if memory_intent == "none":
        entity_cards: list[Entity] = []
        if scope_keys:
            try:
                names = await AIMemEntity.get_frequent_names(scope_keys[0], limit=8)
                if names:
                    entity_cards = [
                        {
                            "id": "",
                            "name": n,
                            "summary": "高频群成员/实体",
                            "entity_type": "familiar",
                            "layer": 0,
                            "score": 0.0,
                        }
                        for n in names
                    ]
            except (OSError, RuntimeError, ValueError):
                entity_cards = []
        return MemoryContext(
            preferences=preference_items,
            entities=entity_cards,
            retrieval_meta={"s1_episodes": 0, "s2_episodes": 0, "scope_keys": scope_keys},
            memory_intent=memory_intent,
        )

    time_range = query_explicit_time_range(query)
    if time_range is None and clock_at is not None and has_relative_time_span(query):
        time_range = query_time_window(query, clock_at)

    parallel_q = wants_parallel_document_coverage(query)
    counting_q = looks_like_count_query(query) or looks_like_sum_query(query)
    cand_k = (
        max(top_k * 6, 128)
        if (parallel_q or counting_q)
        else (
            max(top_k * 3, 96)
            if memory_intent in ("timeline_order", "broad_summary", "version_diff")
            else max(top_k * 2, 64)
        )
    )

    s1_task = asyncio.create_task(system1_search(query, scope_keys, top_k=cand_k))
    fts_task = asyncio.create_task(_fts_episodes(query, scope_keys, limit=cand_k))
    cover_task = asyncio.create_task(_load_document_coverage(query, scope_keys)) if scope_keys else None

    from .thread_recall import recall_span_threads

    span_task = (
        asyncio.create_task(recall_span_threads(query, scope_keys, user_id, group_id))
        if (
            memory_intent in ("timeline_order", "broad_summary")
            or looks_like_order_query(query)
            or looks_like_summary_query(query)
        )
        and user_id
        and scope_keys
        else None
    )

    temporal_task = (
        asyncio.create_task(_fetch_temporal_episodes(query, scope_keys, time_range[0], time_range[1]))
        if time_range is not None and scope_keys
        else None
    )

    s1_res, fts_res = await asyncio.gather(s1_task, fts_task)
    s1: System1Result = s1_res if isinstance(s1_res, System1Result) else System1Result()
    fts_eps: list[Episode] = fts_res if isinstance(fts_res, list) else []

    keep_asst = should_keep_assistant_hits(query)
    episodes = _rrf_fuse_episodes(s1.episodes, fts_eps, keep_asst)

    covered: list[Episode] = []
    if cover_task is not None:
        try:
            covered = await cover_task
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(i18n_t("log.memory.document_coverage_failed", e=exc))
            covered = []

    if not covered:
        episodes = await _expand_document_siblings(episodes, scope_keys)

    pending = pending_episodes_for_scopes(scope_keys, cap=16)
    if pending:
        from gsuid_core.ai_core.memory.retrieval.lexical import query_tokens

        q_stripped = query.strip()
        q_toks = query_tokens(query)
        matched_pending: list[Episode] = []
        for ep in pending:
            raw = (ep["content"] or "").strip()
            if not raw or (q_stripped and raw.endswith(f": {q_stripped}")):
                continue
            if not q_toks or any(token_in_text(t, raw) for t in q_toks):
                matched_pending.append(ep)
        if matched_pending:
            episodes = _merge_episodes(episodes, matched_pending)

    span_eps: list[Episode] = []
    thread_cues: list[MemoryEventCue] = []
    if span_task is not None:
        try:
            span_eps, thread_cues = await asyncio.wait_for(span_task, timeout=6.0)
        except (TimeoutError, OSError, RuntimeError, ConnectionError, ValueError) as e:
            logger.debug(i18n_t("log.memory.thread_recall_fail", e=e))
            span_eps, thread_cues = [], []

    if span_eps:
        episodes = _merge_episodes(episodes, span_eps)

    if temporal_task is not None:
        try:
            temporal_eps = await temporal_task
            if temporal_eps:
                episodes = _merge_episodes(episodes, temporal_eps)
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug(i18n_t("log.memory.time_range_supplemental_retrieval", e=e))

    if time_range is not None and memory_intent in ("timeline_order", "version_diff", "broad_summary"):
        from gsuid_core.ai_core.memory.database.models import AIMemEpisode

        start, end = time_range
        in_window: list[Episode] = []
        for sk in scope_keys:
            for row in await AIMemEpisode.search_by_valid_at_range(sk, start, end, limit=80, ascending=True):
                in_window.append(
                    {
                        "id": row.id,
                        "content": row.content,
                        "valid_at": str(row.valid_at),
                        "scope_key": row.scope_key,
                        "embedding": [],
                        "session_id": row.session_id or "",
                        "thread_id": row.thread_id or "",
                        "turn_index": row.turn_index,
                    }
                )
        if in_window:
            episodes = _merge_episodes(episodes, in_window)

    if covered:
        from gsuid_core.ai_core.memory.retrieval.lexical import merge_coverage_with_metric_hits

        episodes = merge_coverage_with_metric_hits(covered, episodes)

    # 排序题阶段重构
    order_stages: list[Episode] = []
    if (looks_like_order_query(query) or memory_intent == "timeline_order") and episodes:
        from gsuid_core.ai_core.memory.retrieval.event_time import query_only_item_cap

        _n_stages = query_only_item_cap(query) or 8
        try:
            order_stages = await asyncio.wait_for(
                _select_order_stages(query, episodes, _n_stages),
                timeout=8.0,
            )
        except (TimeoutError, OSError, RuntimeError, ValueError):
            order_stages = []

    # 属性问答原句单独保留
    reserved_episodes: list[Episode] = []
    if (
        looks_like_attribute_query(query)
        and not looks_like_count_query(query)
        and not looks_like_sum_query(query)
        and not looks_like_order_query(query)
    ):
        q_tokens = [tok for tok in query.split() if len(tok) >= 2]
        for ep in episodes:
            raw = ep["content"] or ""
            if re.search(r"\d", raw) and any(token_in_text(t, raw) for t in q_tokens):
                reserved_episodes.append(ep)

    if memory_intent in ("point_latest", "broad_summary") and not episodes and scope_keys:
        episodes = await expand_lexical_recall(
            episodes,
            query=query,
            user_id=user_id,
            group_id=group_id,
            clock=clock_at,
            reserved=[],
            memory_intent=memory_intent,
        )

    stance_conflicts: list[str] = []

    ranked_entities = await _rerank_entities(query, s1.entities, top_k * 2)
    ranked_edges = await _rerank_edges(query, s1.edges, top_k * 2)
    ranked_edges = await _apply_intent_edges(memory_intent, ranked_edges, scope_keys, clock_at=clock_at)

    edge_ids = [e["id"] for e in ranked_edges if e["id"]]
    if edge_ids:
        from gsuid_core.ai_core.memory.database.models import AIMemEdge

        conf_inputs = await AIMemEdge.get_confidence_inputs(edge_ids)
        if conf_inputs:
            for e in ranked_edges:
                if e["id"] in conf_inputs:
                    mc, decay = conf_inputs[e["id"]]
                    e["weight"] = compute_edge_confidence(mc, decay)

        async def _touch_edges_accessed() -> None:
            try:
                await AIMemEdge.touch_accessed(edge_ids)
            except (OSError, RuntimeError, ValueError) as e:
                logger.debug(i18n_t("log.memory.refresh_edge_last_accessed", _e=e))

        def _on_task_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.warning(i18n_t("log.memory.background_task_refreshing_edge", exc=exc))

        task = asyncio.create_task(_touch_edges_accessed())
        task.add_done_callback(_on_task_done)

    cover_ids = frozenset(str(ep["id"]) for ep in covered if "id" in ep) if covered else frozenset()
    return MemoryContext(
        episodes=episodes,
        entities=ranked_entities,
        edges=ranked_edges,
        conflicts=stance_conflicts,
        covered=bool(covered),
        cover_ids=cover_ids,
        preferences=preference_items,
        retrieval_meta={
            "s1_episodes": len(s1.episodes),
            "s2_episodes": 0,
            "scope_keys": scope_keys,
        },
        temporal_mode=time_range is not None,
        time_range=time_range,
        events=thread_cues,
        order_stages=order_stages,
        asker_id=user_id if group_id else "",
        reserved_episodes=reserved_episodes,
        pool_ids=[e["id"] for e in episodes if "id" in e],
        memory_intent=memory_intent,
    )
