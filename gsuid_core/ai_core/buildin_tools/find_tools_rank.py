"""find_tools 分档：专用能力 > 专用工具 > 通用能力 > 通用工具。

代码做分层；回执分组列出。通用节点（调研/记忆/日报/日程）不得压过插件专用工具。
exclusive 工具不出现在可调用名单，折叠成所属专用能力。
"""

import re
from typing import Literal, Sequence
from dataclasses import dataclass

ToolTier = Literal["dedicated", "generic", "fold"]

GENERIC_CAPABILITY_NODE_IDS: frozenset[str] = frozenset(
    {
        "research_agent",
        "memory_curator",
        "internal_reporter",
        "scheduler_assistant",
    }
)

_CORE_PLUGINS: frozenset[str] = frozenset({"", "core", "unknown"})
_FOLD_CATEGORIES: frozenset[str] = frozenset({"media", "plugin_dev", "default", "meta"})
_CJK_MIN_WINDOW = 4
_COVER_IN_NEED_MIN = 3
_MAX_DEDICATED_AGENTS = 3
_MAX_DEDICATED_TOOLS = 6
_MAX_GENERIC_AGENTS = 3
_MAX_GENERIC_TOOLS = 2


@dataclass(frozen=True)
class RankedHit:
    name: str
    label: str
    score: float = 0.0


@dataclass(frozen=True)
class FindToolsPlan:
    dedicated_agents: tuple[RankedHit, ...]
    dedicated_tools: tuple[RankedHit, ...]
    generic_agents: tuple[RankedHit, ...]
    generic_tools: tuple[RankedHit, ...]
    pinned_names: tuple[str, ...] = ()

    def has_dedicated(self) -> bool:
        return bool(self.dedicated_agents or self.dedicated_tools)

    def is_empty(self) -> bool:
        return not (self.dedicated_agents or self.dedicated_tools or self.generic_agents or self.generic_tools)

    def loadable_tool_names(self) -> list[str]:
        names = [h.name for h in self.dedicated_tools]
        for h in self.generic_tools:
            if (not self.has_dedicated() or h.name in self.pinned_names) and h.name not in names:
                names.append(h.name)
        return names

    def fingerprint(self) -> list[str]:
        fp: list[str] = [f"agent:{h.name}" for h in self.dedicated_agents]
        fp.extend(h.name for h in self.dedicated_tools)
        if not self.has_dedicated():
            fp.extend(f"agent:{h.name}" for h in self.generic_agents)
            fp.extend(h.name for h in self.generic_tools)
        else:
            for h in self.generic_tools:
                if h.name in self.pinned_names and h.name not in fp:
                    fp.append(h.name)
        return fp


def agent_is_dedicated(node_id: str) -> bool:
    return bool(node_id) and node_id not in GENERIC_CAPABILITY_NODE_IDS


def classify_callable_tool(
    *,
    name: str,
    category: str,
    plugin: str,
    hide_from_main: bool,
    exclusive: set[str],
) -> ToolTier:
    """主人格可调=dedicated/generic；exclusive/隐藏类=fold 成所属节点。"""
    if name in exclusive or hide_from_main or category in _FOLD_CATEGORIES:
        return "fold"
    if category in {"by_trigger", "mcp"}:
        return "dedicated"
    if plugin not in _CORE_PLUGINS:
        return "dedicated"
    return "generic"


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _hay_has_cjk_window(need: str, hay: str, min_len: int = _CJK_MIN_WINDOW) -> bool:
    """need 里连续 CJK ≥min_len 的窗口若出现在 hay，算命中。短词（分析）不够长。"""
    if not hay:
        return False
    i = 0
    n = len(need)
    while i < n:
        if not _is_cjk(need[i]):
            i += 1
            continue
        j = i
        while j < n and _is_cjk(need[j]):
            j += 1
        run = need[i:j]
        if len(run) >= min_len:
            for k in range(0, len(run) - min_len + 1):
                if run[k : k + min_len] in hay:
                    return True
        i = j
    return False


def common_keyword_stems(keywords: Sequence[str], min_len: int = 2) -> list[str]:
    """从节点自描述关键词中提取多词共享的公共词素（零外部词表）。"""
    kws = [k.strip().lower() for k in keywords if k and len(k.strip()) >= min_len]
    if len(kws) < 2:
        return []
    substr_counts: dict[str, int] = {}
    for kw in kws:
        seen_in_kw: set[str] = set()
        n = len(kw)
        for w in range(min_len, n + 1):
            for i in range(n - w + 1):
                sub = kw[i : i + w]
                if sub not in seen_in_kw:
                    seen_in_kw.add(sub)
                    substr_counts[sub] = substr_counts.get(sub, 0) + 1
    shared = [sub for sub, cnt in substr_counts.items() if cnt >= 2]
    # 频次优先，同频取长；仅当频次未增加时才被包含它的长词素抑制
    shared.sort(key=lambda s: (substr_counts[s], len(s)), reverse=True)
    filtered: list[str] = []
    for s in shared:
        if not any(s in longer and substr_counts[s] <= substr_counts[longer] for longer in filtered):
            filtered.append(s)
    return filtered


def need_matches_node_text(
    need: str,
    *,
    when_to_use: str,
    display_name: str,
    keywords: Sequence[str],
    covers: Sequence[str],
) -> bool:
    """节点是否对口：when_to_use / 名 / 关键词 / covers 与 need 过单向命中。"""
    cover_list = [c for c in covers if c]
    hay = " ".join(
        p
        for p in (
            when_to_use,
            display_name,
            " ".join(k for k in keywords if k),
            " ".join(cover_list),
        )
        if p and p.strip()
    )
    if need_matches_tool_text(need, hay, cover_list):
        return True
    need_low = (need or "").strip().lower()
    if not need_low:
        return False
    # 自适应自描述对口：节点关键词高频公共词素命中 + 行为意图词素重叠
    stems = common_keyword_stems(keywords)
    for stem in stems:
        if stem in need_low:
            kw_blob = " ".join(keywords)
            intent_text = f"{when_to_use} {kw_blob}".lower()
            intent_tokens = [w for w in re.split(r"[\s,，、；;。！？!?/|]+", intent_text) if len(w) >= 2]
            for it in intent_tokens:
                it_clean = it.replace(stem, "").strip()
                if len(it_clean) >= 2 and it_clean in need_low:
                    return True
    return False


_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9_+#.-]{2,}")


def _extract_lexical_units(text: str) -> list[str]:
    """通用词素单元：拉丁单词 + CJK 步长为 2 的二元组。"""
    body = (text or "").lower()
    units: list[str] = []
    for m in _LATIN_WORD_RE.finditer(body):
        units.append(m.group(0))
    for m in _CJK_RUN_RE.finditer(body):
        run = m.group(0)
        if len(run) <= 3:
            units.append(run)
        else:
            for i in range(0, len(run) - 1, 2):
                units.append(run[i : i + 2])
            if len(run) % 2 != 0:
                units.append(run[-2:])
    return units


def need_matches_tool_text(need: str, retrieval_text: str, covers: list[str]) -> bool:
    """单向命中：need∈retrieval、cover 蕴涵/包含在 need、或 CJK 词素重叠达标。"""
    n = (need or "").strip().lower()
    hay = (retrieval_text or "").strip().lower()
    if not n:
        return False
    if hay and n in hay:
        return True
    for c in covers:
        cl = (c or "").strip().lower()
        if not cl:
            continue
        if len(cl) >= _COVER_IN_NEED_MIN and cl in n:
            return True
        c_units = _extract_lexical_units(cl)
        if c_units and all(u in n for u in c_units):
            return True
    if _hay_has_cjk_window(n, hay):
        return True
    tokens = _extract_lexical_units(n)
    if not tokens or not hay:
        return False
    hits = sum(1 for t in tokens if t in hay)
    return hits >= max(1, (len(tokens) + 1) // 2)


def covers_from_trigger_keywords(
    keyword: str | tuple[str, ...],
    covers: list[str] | None,
) -> list[str]:
    """触发器中文命令词（≥3 字）并入 covers；跳过 jz/sy 这类缩写。"""
    out: list[str] = []
    seen: set[str] = set()
    for c in covers or []:
        s = (c or "").strip()
        if s and s not in seen:
            out.append(s)
            seen.add(s)
    items = keyword if isinstance(keyword, tuple) else (keyword,)
    for k in items:
        s = (k or "").strip()
        if len(s) < 3:
            continue
        if not all(_is_cjk(ch) for ch in s):
            continue
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out


def _dedupe_hits(hits: list[RankedHit]) -> list[RankedHit]:
    seen: set[str] = set()
    out: list[RankedHit] = []
    for h in hits:
        if h.name in seen:
            continue
        seen.add(h.name)
        out.append(h)
    return out


def build_find_tools_plan(
    *,
    dedicated_agents: list[RankedHit],
    dedicated_tools: list[RankedHit],
    generic_agents: list[RankedHit],
    generic_tools: list[RankedHit],
    pinned_names: Sequence[str] = (),
) -> FindToolsPlan:
    """有专用项时丢掉非点名通用档，避免模型改选调研/记忆管家。"""
    pinned_set = {n for n in pinned_names if n}
    d_agents = _dedupe_hits(dedicated_agents)[:_MAX_DEDICATED_AGENTS]
    d_tools = _dedupe_hits(dedicated_tools)[:_MAX_DEDICATED_TOOLS]
    g_agents = _dedupe_hits(generic_agents)
    g_tools = _dedupe_hits(generic_tools)
    if d_agents or d_tools:
        g_agents = []
        g_tools = [h for h in g_tools if h.name in pinned_set]
    else:
        g_agents = g_agents[:_MAX_GENERIC_AGENTS]
        g_tools = g_tools[:_MAX_GENERIC_TOOLS]
    return FindToolsPlan(
        dedicated_agents=tuple(d_agents),
        dedicated_tools=tuple(d_tools),
        generic_agents=tuple(g_agents),
        generic_tools=tuple(g_tools),
        pinned_names=tuple(sorted(pinned_set)),
    )


def format_find_tools_plan(plan: FindToolsPlan) -> str:
    """分组回执。空计划由调用方走缺口文案。"""
    if plan.is_empty():
        return ""
    parts: list[str] = []
    if plan.dedicated_agents:
        parts.append("【专用能力】有则 create_subagent(agent_profile=node_id)，不要自调渲染/代码工具")
        for h in plan.dedicated_agents:
            parts.append(f"- `{h.name}`：{h.label}")
    if plan.dedicated_tools:
        parts.append("【专用工具】下一步直接调用，有则不要 create_subagent")
        for h in plan.dedicated_tools:
            parts.append(f"- {h.name}：{h.label}")
    if plan.generic_agents:
        parts.append("【通用能力】仅当上面没有对口项时 create_subagent")
        for h in plan.generic_agents:
            parts.append(f"- `{h.name}`：{h.label}")
    if plan.generic_tools:
        parts.append("【通用工具】仅当上面没有对口项时调用")
        for h in plan.generic_tools:
            parts.append(f"- {h.name}：{h.label}")
    if plan.has_dedicated():
        parts.append("有专用项时禁止选通用项。")
    return "\n".join(parts)


def tool_brief(*, covers: list[str], description: str) -> str:
    if covers:
        first = (covers[0] or "").strip()
        if first:
            return first
    line = (description or "").strip().split("\n", 1)[0].strip()
    return line[:40] if line else ""
