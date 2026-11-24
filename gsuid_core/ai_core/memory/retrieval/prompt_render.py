"""记忆上下文结构与提示词渲染。

负责将双路检索得到的事实边、事件线索、偏好规则、语义类目与对话片段
按预算裁剪、去敏及顺序重组后格式化为注入模型的记忆上下文文本。
"""

from __future__ import annotations

import re
import time
from typing import Optional, Sequence, TypedDict
from datetime import datetime
from dataclasses import field, dataclass

from gsuid_core.ai_core.content_guard import wrap_untrusted
from gsuid_core.ai_core.control.lines import (
    SPEECH_ACT_HINT,
    MEMORY_STAGING_NOTE,
    MEMORY_CONFLICT_NOTE,
    MEMORY_TIMELINE_ORDER_NOTE,
    MEMORY_SPEAKER_ISOLATION_NOTE,
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
from .event_time import event_at_from_text

MemoryIntent = str
_UNTRUSTED_WRAP_OVERHEAD = len(wrap_untrusted("memory_recall", ""))
_POINT_LOOKUP_EP_BUDGET = 8000
_TS_MAX = 4_102_444_800.0

_TRIVIA_FACT_RE = re.compile(r"(提及|提到|询问|聊到|讨论|说起|谈到)")
_IDENTITY_FACT_KEYWORDS = ("本人是", "就是", "叫做", "别名")

_SENSITIVE_FACT_RE = re.compile(
    r"催婚|相亲|离婚|分手|出轨|怀孕|堕胎|"
    r"工资|薪资|月薪|年薪|收入|欠钱|欠款|负债|贷款|房贷|房租|"
    r"抑郁|焦虑症|生病|住院|确诊|病历|"
    r"住址|家庭地址|身份证|手机号|电话号|银行卡"
)

_EPISODE_STOP_WORDS = frozenset(
    {
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
        "可以",
        "没有",
        "一个",
        "很多",
        "觉得",
        "怎么",
    }
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


def _fact_mentions_speaker(edge: Edge, speaker_ids: set[str] | set[object]) -> bool:
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


def _take_lines(items: list[str], budget: int) -> list[str]:
    out: list[str] = []
    used = 0
    for line in items:
        if used + len(line) > budget and out:
            break
        out.append(line)
        used += len(line)
    return out


def _take_even_lines(items: list[str], budget: int) -> list[str]:
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


def _take_head_tail_lines(items: list[str], budget: int) -> list[str]:
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


def _assistant_turn(raw: str) -> bool:
    low = raw.lstrip().lower()
    return low.startswith("assistant:") or raw.lstrip().startswith("[我此前说过]")


def _filter_point_lookup_episodes(
    episodes: list[Episode],
    query: str,
    *,
    dense_anchor_cap: int = 2,
    direct_hit_cap: int = 6,
) -> list[Episode]:
    """点查/闲聊剥离低相关纯稠密凑数片段，保留双路/词面命中与至多 2 条顶部语义锚点。"""
    if not episodes or not query.strip():
        return episodes[: max(dense_anchor_cap, 2)]
    from gsuid_core.ai_core.memory.retrieval.lexical_cover import is_ingested_document_episode
    from gsuid_core.ai_core.memory.retrieval.lexical_query import query_tokens, token_in_text
    from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import is_fact_sheet

    substantive = [tok for tok in query_tokens(query) if tok not in _EPISODE_STOP_WORDS and len(tok) >= 2]
    direct_hits: list[Episode] = []
    dense_anchors: list[Episode] = []
    for ep in episodes:
        raw = ep["content"] or ""
        raw_lower = raw.lower()
        has_lex = bool(substantive) and any(token_in_text(tok, raw_lower) for tok in substantive)
        if is_fact_sheet(ep) or is_ingested_document_episode(raw):
            direct_hits.append(ep)
            continue
        if "score" not in ep:
            if has_lex:
                direct_hits.append(ep)
            continue
        sc = float(ep["score"] or 0.0)
        # RRF >= 0.022 代表命中 FTS5 精确路或 Dense+FTS 双路共识
        if sc >= 0.022 or has_lex:
            direct_hits.append(ep)
        elif sc >= 0.0203 and len(dense_anchors) < dense_anchor_cap:
            dense_anchors.append(ep)
    if direct_hits:
        if not any("score" in h for h in direct_hits) and dense_anchors:
            return (direct_hits + dense_anchors)[:direct_hit_cap]
        return direct_hits[:direct_hit_cap]
    return dense_anchors


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
    is_private: bool = False
    reserved_episodes: list[Episode] = field(default_factory=list)
    memory_intent: MemoryIntent = "point_latest"

    def _render_events_block(self, intent: str) -> str | None:
        if not self.events or intent not in ("timeline_order", "broad_summary"):
            return None
        from gsuid_core.ai_core.memory.privacy import prefix_group_tag

        ev_lines: list[str] = []
        for ev in self.events[:16]:
            src = ev["source"] if "source" in ev else ""
            if src not in ("llm", "milestone"):
                continue
            stamp = (ev["stated_at"] or ev["event_at"] or "")[:10]
            ev_sk = ev["scope_key"] if "scope_key" in ev else ""
            ev_sum = prefix_group_tag(ev_sk, ev["summary"]) if self.is_private else ev["summary"]
            ev_lines.append(f"{len(ev_lines) + 1}. {stamp} · {ev_sum}")
        if not ev_lines:
            return None
        return "【事件线索】\n" + "\n".join(ev_lines)

    def _render_preferences_block(self, max_chars: int) -> Optional[str]:
        if not self.preferences:
            return None
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
        taken = _take_lines(pref_lines, pref_budget)
        if not taken:
            return None
        return (
            "【用户偏好/纠错 - 须严格遵守】"
            "（各规则按其触发条件适用；未写明条件的按字面最小范围理解，不扩大化）\n" + "\n".join(taken)
        )

    def _render_edges_block(
        self,
        max_chars: int,
        intent: str,
        priority_speakers: Optional[set[str] | set[object]],
        current_speaker_ids: Optional[set[str] | set[object]],
        query: str,
    ) -> str | None:
        if not self.edges:
            return None
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
        keep_versions = intent == "version_diff"
        now_ts = time.time()
        extra_sensitive = _get_sensitive_extra_terms()
        fact_lines: list[str] = []
        seen_facts: set[str] = set()
        has_self_edge = False

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
                from gsuid_core.ai_core.memory.retrieval.lexical_query import query_tokens

                stop_words = (
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
                substantive = [tok for tok in query_tokens(query) if tok not in stop_words]
                s_name = str(e["source_name"] or "") if "source_name" in e else ""
                t_name = str(e["target_name"] or "") if "target_name" in e else ""
                if substantive and not any(tok in fact or tok in s_name or tok in t_name for tok in substantive):
                    continue
            if _sensitive_fact_hit(fact, extra_sensitive) and not (
                current_speaker_ids and _fact_mentions_speaker(e, current_speaker_ids)
            ):
                continue
            sig = fact.strip().lower().replace(" ", "")[:24]
            if sig in seen_facts:
                continue
            seen_facts.add(sig)

            state = e["resolution_state"] if "resolution_state" in e else "active"
            mark = ""
            if "scope_key" in e and e["scope_key"].startswith("self:"):
                has_self_edge = True
                mark = "[自我经历] "
            if state == "contradicted":
                mark += "⚠️[存在未消解矛盾] "
            elif state == "staging":
                mark += "[待证实传闻] "
            elif state == "superseded" and intent == "version_diff":
                mark += "[已于后续更新] "

            is_id = any(k in fact for k in _IDENTITY_FACT_KEYWORDS)
            dt_prefix = _edge_date_prefix(e)
            id_tag = "（记忆·待证）" if is_id else ""
            if self.is_private and "scope_key" in e and e["scope_key"].startswith("group:"):
                from gsuid_core.ai_core.memory.privacy import prefix_group_tag

                rendered_fact = prefix_group_tag(e["scope_key"], fact)
            else:
                rendered_fact = fact
            fact_lines.append(f"• [记录 #{i}{_edge_stamp(e)}] {dt_prefix}{mark}{id_tag}{rendered_fact}")
            if len(fact_lines) >= memory_config.fact_max_inject:
                break

        taken = _take_lines(fact_lines, fact_budget)
        if not taken:
            return None
        header = "【核心事实 - 与当前问题相关】"
        if intent == "timeline_order":
            header += MEMORY_TIMELINE_ORDER_NOTE
        if conflict:
            header += MEMORY_CONFLICT_NOTE
        if staging:
            header += MEMORY_STAGING_NOTE
        if has_self_edge:
            header += MEMORY_SPEAKER_ISOLATION_NOTE
        return header + "\n" + "\n".join(taken)

    def _format_episode_lines(
        self,
        items: list[Episode],
        *,
        self_mark: bool,
        ep_cap: int,
        doc_cap: int,
        query: str,
        intent: str,
    ) -> list[str]:
        from gsuid_core.ai_core.register import service_changed_at
        from gsuid_core.ai_core.memory.retrieval.lexical_cover import is_ingested_document_episode
        from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import is_fact_sheet, parse_episode_valid_at

        dated: list[str] = []
        undated: list[str] = []
        seen_content: set[str] = set()
        for ep in items:
            raw = (ep["content"] or "").strip()
            if len(raw) < 4:
                continue
            if self_mark:
                ep_ts = parse_episode_valid_at(ep["valid_at"] or "")
                if ep_ts is not None and any(
                    svc in raw and ep_ts.timestamp() < changed for svc, changed in service_changed_at.items()
                ):
                    continue
            if "[决策结论]" in raw or "决策：走 " in raw:
                continue
            key = raw[:80]
            if key in seen_content:
                continue
            seen_content.add(key)
            prefix = "[我此前说过] " if self_mark else ""
            grp_prefix = ""
            if self.is_private and "scope_key" in ep and ep["scope_key"].startswith("group:"):
                from gsuid_core.ai_core.memory.privacy import parse_group_from_scope

                ep_gid = parse_group_from_scope(ep["scope_key"])
                if ep_gid:
                    grp_prefix = f"[群:{ep_gid}] "
            shown = raw[:ep_cap]
            if is_ingested_document_episode(raw):
                undated.append(f"{grp_prefix}{prefix}{raw[:doc_cap]}")
                continue
            if is_fact_sheet(ep):
                shown = raw[:4800]
            elif _assistant_turn(raw) and len(raw) > ep_cap:
                from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import (
                    excerpt_keep_numbers,
                    excerpt_around_ordinal,
                )

                shown = excerpt_around_ordinal(raw, query, max(ep_cap, 900))
                if not shown:
                    shown = excerpt_keep_numbers(raw, ep_cap)
            if intent == "broad_summary" and _assistant_turn(raw):
                from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import excerpt_around_tokens

                shown = excerpt_around_tokens(raw, query, 480) or shown
            ts = (ep["valid_at"] or "").strip()[:19].replace("T", " ")
            if ts:
                stamp = f"[{ts}] "
                said = parse_episode_valid_at(ep["valid_at"] or "")
                if said is not None:
                    ev = event_at_from_text(raw, said)
                    if ev.date() != said.date():
                        stamp += f"[发生 {ev.strftime('%Y-%m-%d')}] "
                dated.append(f"{stamp}{grp_prefix}{prefix}{shown}")
            else:
                undated.append(f"{grp_prefix}{prefix}{shown}")
        return dated + undated

    def _split_episode_buckets(
        self,
        query: str,
        clock: datetime,
    ) -> tuple[list[Episode], list[Episode], list[Episode]]:
        from gsuid_core.ai_core.memory.retrieval.lexical_query import query_tokens, token_in_text

        eps = list(self.episodes)
        q_toks = query_tokens(query) if query else []
        self_eps = [
            e
            for e in eps
            if (e["scope_key"] or "").startswith("self:")
            and (not q_toks or any(token_in_text(t, e["content"] or "") for t in q_toks))
        ]
        other_eps = [e for e in eps if not (e["scope_key"] or "").startswith("self:")]

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
        return other_eps, recent_self, old_self

    def _render_episodes_section(
        self,
        *,
        max_chars: int,
        ep_budget: int,
        intent: str,
        query: str,
        now: datetime | None,
    ) -> tuple[str | None, str | None, str | None]:
        """返回 (顺序骨架块, 值演进块, 主对话片段块) 三个可选片段。"""
        from gsuid_core.ai_core.memory.retrieval.event_time import query_only_item_cap
        from gsuid_core.ai_core.memory.retrieval.lexical_pack import (
            pack_order_dialogue,
            format_order_skeleton,
            apply_query_episode_pack,
            collapse_document_episodes,
        )
        from gsuid_core.ai_core.memory.retrieval.lexical_query import (
            looks_like_set_query,
            looks_like_sum_query,
            looks_like_count_query,
            wants_parallel_document_coverage,
        )
        from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import is_fact_sheet, parse_episode_valid_at

        cross_session = (
            intent in ("timeline_order", "broad_summary")
            or looks_like_count_query(query)
            or looks_like_sum_query(query, memory_intent=intent)
        )
        clock = now if now is not None else datetime.now()
        other_eps, recent_self, old_self = self._split_episode_buckets(query, clock)

        self_budget = 0 if (cross_session or not old_self) else min(int(max_chars * 0.10), max(0, ep_budget // 5))
        recent_budget = 0 if (cross_session or not recent_self) else min(int(ep_budget * 0.25), 400)
        other_budget = max(0, ep_budget - self_budget - recent_budget)
        ep_cap = 480 if (self.temporal_mode or intent == "timeline_order") else 1000
        parallel_q = wants_parallel_document_coverage(query, memory_intent=intent) or self.covered
        doc_cap = (
            max_chars
            if parallel_q
            else (12000 if looks_like_set_query(query, memory_intent=intent) else max(ep_cap, 2400))
        )

        if self.temporal_mode and self.time_range is not None:
            t0, t1 = self.time_range

            def _in_win(ep: Episode) -> bool:
                dt = parse_episode_valid_at(ep["valid_at"] or "")
                return dt is not None and t0 <= dt < t1

            # 窗内优先前置，窗外后置保底，避免时间窗预估偏差导致证据硬丢失
            other_eps = [e for e in other_eps if _in_win(e)] + [e for e in other_eps if not _in_win(e)]

        if intent in ("timeline_order", "broad_summary"):
            ep_cap = 480 if not self.covered else max(ep_cap, 4800)
        haystack = list(other_eps)

        if self.covered:
            packed = list(haystack)
            cover_first = [ep for ep in packed if "id" in ep and str(ep["id"]) in self.cover_ids]
            rest_pack = [ep for ep in packed if "id" not in ep or str(ep["id"]) not in self.cover_ids]
            packed = cover_first + rest_pack
        else:
            wide_pack = cross_session or looks_like_set_query(query, memory_intent=intent) or parallel_q
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

        if looks_like_set_query(query, memory_intent=intent):
            packed = collapse_document_episodes(packed)
        if cross_session:
            packed = packed[:60]
        if intent in ("timeline_order", "broad_summary"):
            sheet_tail = [e for e in packed if is_fact_sheet(e)]
            core_packed = [e for e in packed if not is_fact_sheet(e)]
            core_packed.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
            packed = core_packed + sheet_tail

        skeleton_block: str | None = None
        if intent == "timeline_order":
            non_sheet_packed = [e for e in packed if not is_fact_sheet(e)]
            stage_eps = (
                self.order_stages
                if self.order_stages and len(self.order_stages) >= len(non_sheet_packed)
                else non_sheet_packed
            )
            skel = format_order_skeleton(stage_eps, query=query)
            self.skeleton_ids = [e["id"] for e in stage_eps if "id" in e]
            if skel:
                skeleton_block = (
                    "【事件顺序（按时间）】"
                    "（Chronological event sequence; answer strictly following this order）：\n" + "\n".join(skel)
                )
                other_budget = max(0, other_budget - len(skeleton_block) - 2)
            n = query_only_item_cap(query) or len(skel)
            dial_n = max(n * 2, min(n * 3, 24)) if n else 16
            packed = pack_order_dialogue(haystack, query, dial_n)
            self.inject_ids = [e["id"] for e in packed if "id" in e]

        if not self.inject_ids:
            self.inject_ids = [e["id"] for e in packed if "id" in e]

        user_eps = [e for e in packed if not _assistant_turn(e["content"] or "")]
        asst_eps = [e for e in packed if _assistant_turn(e["content"] or "")]

        reserved_block_out: str | None = None
        if (
            self.reserved_episodes
            and intent == "version_diff"
            and not looks_like_count_query(query)
            and not looks_like_sum_query(query, memory_intent=intent)
        ):
            from gsuid_core.ai_core.memory.retrieval.lexical_excerpt import render_value_timeline

            reserve_budget = min(3600, max(0, other_budget // 2))
            reserved_block = render_value_timeline(self.reserved_episodes, query, reserve_budget)
            if reserved_block:
                reserved_block_out = reserved_block
                other_budget = max(0, other_budget - len(reserved_block) - 2)
                reserved_ids = {ep["id"] for ep in self.reserved_episodes if "id" in ep}
                user_eps = [ep for ep in user_eps if "id" not in ep or ep["id"] not in reserved_ids]

        def _fmt(eps: list[Episode], *, self_mark: bool) -> list[str]:
            return self._format_episode_lines(
                eps, self_mark=self_mark, ep_cap=ep_cap, doc_cap=doc_cap, query=query, intent=intent
            )

        if intent in ("timeline_order", "broad_summary"):
            core_user_eps = [e for e in user_eps if not is_fact_sheet(e)]
            sheet_eps = [e for e in user_eps if is_fact_sheet(e)]
            want_asst = intent == "broad_summary" and bool(asst_eps)
            user_share = (other_budget * 3) // 4 if want_asst else other_budget
            taken = _take_head_tail_lines(_fmt(core_user_eps, self_mark=False), user_share)
            used_chars = sum(len(x) for x in taken)
            if want_asst and other_budget - used_chars > 120:
                taken = taken + _take_head_tail_lines(_fmt(asst_eps, self_mark=False), other_budget - used_chars)
                used_chars = sum(len(x) for x in taken)
            if sheet_eps and other_budget - used_chars > 200:
                taken = taken + _take_lines(_fmt(sheet_eps, self_mark=False), other_budget - used_chars)
        elif self.temporal_mode:
            taken = _take_lines(_fmt(user_eps, self_mark=False), other_budget)
        elif not self.covered and (
            looks_like_count_query(query)
            or looks_like_sum_query(query, memory_intent=intent)
            or intent == "version_diff"
        ):
            count_budget = (
                other_budget
                if (looks_like_count_query(query) or looks_like_sum_query(query, memory_intent=intent))
                else min(other_budget, _POINT_LOOKUP_EP_BUDGET)
            )
            taken = _take_lines(_fmt(user_eps, self_mark=False), count_budget)
        else:
            if not self.covered and not cross_session:
                user_eps = _filter_point_lookup_episodes(user_eps, query, dense_anchor_cap=2)
                asst_eps = _filter_point_lookup_episodes(asst_eps, query, dense_anchor_cap=0)
            ep_taken = _fmt(user_eps, self_mark=False)
            if self.covered and self.cover_ids:
                cover_eps = [ep for ep in user_eps if "id" in ep and str(ep["id"]) in self.cover_ids]
                rest_eps = [ep for ep in user_eps if "id" not in ep or str(ep["id"]) not in self.cover_ids]
                if cover_eps:
                    taken_cover = _take_even_lines(_fmt(cover_eps, self_mark=False), other_budget)
                    rest_budget = max(0, other_budget - sum(len(x) for x in taken_cover))
                    taken_other = taken_cover + _take_lines(_fmt(rest_eps, self_mark=False), rest_budget)
                else:
                    taken_other = _take_even_lines(ep_taken, other_budget)
            else:
                picker = _take_even_lines if self.covered else _take_lines
                taken_other = picker(ep_taken, other_budget)
            rest = other_budget - sum(len(x) for x in taken_other)
            if rest > 80 and asst_eps:
                taken_other.extend(_take_lines(_fmt(asst_eps, self_mark=False), rest))
            taken_recent = _take_lines(_fmt(recent_self, self_mark=True), recent_budget)
            taken_self = _take_lines(_fmt(old_self, self_mark=True), min(self_budget, 400))
            taken = taken_other + taken_recent + taken_self

        ep_block: str | None = None
        if taken:
            ep_header = "【相关对话片段】"
            if intent == "timeline_order":
                ep_header += MEMORY_TIMELINE_ORDER_NOTE
            ep_block = ep_header + "\n" + "\n".join(taken)
        return skeleton_block, reserved_block_out, ep_block

    def to_prompt_text(
        self,
        max_chars: int,
        priority_speakers: Optional[set[str] | set[object]] = None,
        current_speaker_ids: Optional[set[str] | set[object]] = None,
        query: str = "",
        wrap_recall: bool = True,
        now: datetime | None = None,
        memory_intent: str | None = None,
    ) -> str:
        intent = memory_intent or self.memory_intent
        parts: list[str] = []
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

        ev_block = self._render_events_block(intent)
        if ev_block is not None:
            parts.append(_speech(ev_block))

        pref_block = self._render_preferences_block(max_chars)

        edge_block = self._render_edges_block(max_chars, intent, priority_speakers, current_speaker_ids, query)
        if edge_block is not None:
            parts.append(_speech(edge_block))

        if self.conflicts:
            conf_lines = [f"• {s[:300]}" for s in self.conflicts[:6]]
            taken = _take_lines(conf_lines, int(max_chars * 0.12))
            if taken:
                parts.append(f"【陈述不一致】{MEMORY_CONFLICT_NOTE}\n" + "\n".join(taken))

        if self.categories:
            cats = [c for c in self.categories if (not query or (c["name"] and c["name"] in query))]
            if cats:
                cat_budget = int(max_chars * 0.15)
                sorted_cats = sorted(cats, key=lambda c: c["layer"], reverse=True)
                cat_lines = [f"• [L{c['layer']}] {c['name']}: {(c['summary'] or '')[:100]}" for c in sorted_cats[:6]]
                taken = _take_lines(cat_lines, cat_budget)
                if taken:
                    parts.append("【语义类目摘要】\n" + "\n".join(taken))

        if self.episodes:
            pref_used = (len(pref_block) + 2) if pref_block else 0
            used = sum(len(p) for p in parts) + 2 * len(parts) + pref_used + _UNTRUSTED_WRAP_OVERHEAD
            ep_budget = max_chars - used
            if ep_budget > 120:
                skel_block, reserved_block, ep_block = self._render_episodes_section(
                    max_chars=max_chars,
                    ep_budget=ep_budget,
                    intent=intent,
                    query=query,
                    now=now,
                )
                if skel_block is not None:
                    parts.append(skel_block)
                if reserved_block is not None:
                    parts.append(_speech(reserved_block))
                if ep_block is not None:
                    parts.append(_speech(ep_block))

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
