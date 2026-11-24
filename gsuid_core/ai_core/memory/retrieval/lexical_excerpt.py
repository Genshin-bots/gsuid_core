"""记忆片段清洗、摘录、时间聚类与数值/专名抽取辅助（供 lexical / lexical_pack 复用）。"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from gsuid_core.ai_core.control.lines import CODE_FENCE_RE
from gsuid_core.ai_core.memory.retrieval.types import Episode
from gsuid_core.ai_core.memory.retrieval.lexical_query import (
    _HAS_DIGIT_RE,
    query_tokens,
    ordinal_index,
    token_in_text,
    topic_pin_tokens,
    attribute_content_tokens,
)

_FACT_SHEET_KIND = "fact_sheet"
_SESSION_READ_KIND = "session_read"
_PRIMARY_KEEP = 16


def is_fact_sheet(ep: Episode) -> bool:
    return "kind" in ep and ep["kind"] == _FACT_SHEET_KIND


def is_session_read(ep: Episode) -> bool:
    return "kind" in ep and ep["kind"] == _SESSION_READ_KIND


def _without_ids(episodes: list[Episode], banned: set[str]) -> list[Episode]:
    if not banned:
        return episodes
    return [ep for ep in episodes if "id" not in ep or ep["id"] not in banned]


def _assistant_turn(raw: str) -> bool:
    low = raw.lstrip().lower()
    return low.startswith("assistant:") or raw.lstrip().startswith("[我此前说过]")


def parse_episode_valid_at(raw: str) -> datetime | None:
    body = (raw or "").strip()
    if not body:
        return None
    iso = body.replace("Z", "+00:00")
    if "T" not in iso and iso.count(":") == 1:
        iso = f"{iso}:00"
    try:
        dt = datetime.fromisoformat(iso[:32])
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def cluster_episodes_by_time(eps: list[Episode], gap_sec: int | None = None) -> list[list[Episode]]:
    """按发言间隔聚成会话；组内保持时间序。默认用 memory_config.session_gap_seconds。"""
    if gap_sec is None:
        from gsuid_core.ai_core.memory.config import memory_config

        raw_gap = int(memory_config.session_gap_seconds)
        gap_sec = raw_gap if raw_gap > 0 else 1800
    dated: list[tuple[datetime, Episode]] = []
    undated: list[Episode] = []
    for ep in eps:
        dt = parse_episode_valid_at(str(ep["valid_at"]) if "valid_at" in ep else "")
        if dt is None:
            undated.append(ep)
            continue
        dated.append((dt, ep))
    dated.sort(key=lambda x: x[0])
    clusters: list[list[Episode]] = []
    cur: list[Episode] = []
    prev: datetime | None = None
    for dt, ep in dated:
        if prev is not None and (dt - prev).total_seconds() > gap_sec and cur:
            clusters.append(cur)
            cur = [ep]
        else:
            cur.append(ep)
        prev = dt
    if cur:
        clusters.append(cur)
    if undated:
        clusters.append(undated)
    return clusters


def _inclusive_stride_indices(n: int, cap: int) -> list[int]:
    """均匀取样下标，强制含 0 和 n-1。"""
    if cap <= 0 or n <= 0:
        return []
    if n <= cap:
        return list(range(n))
    if cap == 1:
        return [0]
    return [int(i * (n - 1) / (cap - 1)) for i in range(cap)]


def stride_episodes_chrono(episodes: list[Episode], cap: int) -> list[Episode]:
    """按 valid_at 均匀取样，两端都留。禁止取时间序前缀。"""
    if cap <= 0:
        return []
    eps = sorted(episodes, key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return [eps[i] for i in _inclusive_stride_indices(len(eps), cap)]


def episode_mentions_speaker(content: str, user_id: str) -> bool:
    """本条里有没有这个说话人。群聊钉本人，私聊不靠这个过滤。"""
    uid = (user_id or "").strip()
    if not uid:
        return True
    prefix = f"{uid}:"
    bracket = f"[{uid}]:"
    for line in (content or "").splitlines() or [content or ""]:
        head = line.lstrip()
        if head.startswith(prefix) or head.startswith(bracket):
            return True
    return prefix in (content or "") or bracket in (content or "")


def excerpt_around_tokens(text: str, query: str, width: int) -> str:
    """截到问句实词附近。长发言的首尾句经常不是被问到的那半句。"""
    prose = " ".join((text or "").split())
    if width <= 0:
        return ""
    if len(prose) <= width:
        return prose
    toks = attribute_content_tokens(query)
    low = prose.lower()
    idx = -1
    best_len = -1
    for tok in toks:
        at = low.find(tok.lower())
        if at >= 0 and len(tok) > best_len:
            idx = at
            best_len = len(tok)
    if idx < 0:
        half = max(24, (width - 1) // 2)
        return prose[:half].rstrip() + "…" + prose[-half:].lstrip()
    start = max(0, idx - width // 3)
    end = min(len(prose), start + width)
    start = max(0, end - width)
    snippet = prose[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(prose):
        snippet = snippet + "…"
    return snippet


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")
_MONTH_BEFORE_RE = re.compile(
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s*$",
    re.IGNORECASE,
)


def _date_like_number(prose: str, start: int, end: int) -> bool:
    prefix = prose[max(0, start - 16) : start]
    if prose[end : end + 1] in {"日", "月", "号"}:
        return True
    trimmed = prefix.rstrip()
    if trimmed.endswith(("-", "/", "月", "年")):
        return True
    return _MONTH_BEFORE_RE.search(prefix) is not None


def _ordinal_span(prose: str, number: str) -> tuple[int, int] | None:
    listed = re.finditer(rf"(?<!\d){re.escape(number)}\s*[\.、．\)）:：]", prose)
    for hit in listed:
        if not _date_like_number(prose, hit.start(), hit.end()):
            return hit.start(), hit.end()
    for hit in re.finditer(rf"(?<!\d){re.escape(number)}(?:st|nd|rd|th)?\b", prose, re.IGNORECASE):
        if _date_like_number(prose, hit.start(), hit.end()):
            continue
        return hit.start(), hit.end()
    return None


def excerpt_around_ordinal(text: str, query: str, width: int) -> str:
    """问第 N 项时截到那个序号，避免清单头几条占满窗口。"""
    number = ordinal_index(query)
    if not number or width <= 0:
        return ""
    prose = " ".join((text or "").split())
    span = _ordinal_span(prose, number)
    if span is None:
        return ""
    if len(prose) <= width:
        return prose
    start = max(0, span[0] - width // 5)
    end = min(len(prose), start + width)
    start = max(0, end - width)
    snippet = prose[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(prose):
        snippet = snippet + "…"
    return snippet


def excerpt_keep_numbers(text: str, width: int) -> str:
    """长助手回复截断时留下带数字的句子，避免人数、金额落在尾部被切掉。"""
    prose = " ".join((text or "").split())
    if width <= 0 or len(prose) <= width:
        return prose
    half = max(80, width // 3)
    budget = max(40, width // 2)
    windows: list[str] = []
    for sent in _SENT_SPLIT_RE.split(prose):
        hit = _HAS_DIGIT_RE.search(sent)
        if hit is None:
            continue
        piece = sent.strip()
        if len(piece) > budget:
            start = max(0, hit.start() - budget // 3)
            piece = piece[start : start + budget].strip()
        windows.append(piece)
        if len(windows) >= 4:
            break
    mid = " ".join(windows)
    if len(mid) > width:
        mid = mid[:width].rstrip()
    return prose[:half].rstrip() + " … " + mid + " … " + prose[-half:].lstrip()


_PROPER_RE = re.compile(r"\b[A-Z]{2,}[A-Za-z0-9]*\b|\b[A-Z][a-z]{3,}\b")
_NAME_SKIP = frozenset(
    {
        "user",
        "assistant",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "that",
        "this",
        "what",
        "when",
        "where",
        "which",
        "there",
        "please",
        "thanks",
        "looking",
        "planning",
    }
)


def _extra_names(text: str, query_words: set[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _PROPER_RE.finditer(text or ""):
        key = match.group(0).lower()
        if key in query_words or key in _NAME_SKIP or key in seen:
            continue
        seen.add(key)
        found.append(key)
    return found


def sentence_has_extra_name(text: str, query_words: set[str]) -> bool:
    """句中有问句没写的专名，或有数量。高频词铺开时靠这个留下 Hawaii。"""
    if _extra_names(text, query_words):
        return True
    return states_a_value(text or "")


def prefer_named_lines(episodes: list[Episode], query: str, cap: int) -> list[Episode]:
    """每个专名先留一条，避免 Paris 把只出现几次的 Hawaii 抽掉。"""
    words = {t.lower() for t in query_tokens(query)}
    buckets: dict[str, list[Episode]] = {}
    rest: list[Episode] = []
    for ep in episodes:
        raw = ep["content"] if "content" in ep else ""
        names = _extra_names(raw, words)
        if not names:
            rest.append(ep)
            continue
        key = names[0]
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(ep)
    order = sorted(buckets, key=lambda name: (len(buckets[name]), name))
    kept: list[Episode] = []
    seen_ids: set[str] = set()
    while len(kept) < cap:
        progressed = False
        for name in order:
            if not buckets[name]:
                continue
            ep = buckets[name].pop(0)
            eid = ep["id"] if "id" in ep else ""
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            kept.append(ep)
            progressed = True
            if len(kept) >= cap:
                break
        if not progressed:
            break
    if len(kept) >= cap:
        return kept
    return kept + stride_keep(rest, cap - len(kept))


def _topic_hit_count(query: str, text: str) -> int:
    toks = [t for t in query_tokens(query) if len(t) >= 4]
    if not toks:
        return 0
    blob = (text or "").lower()
    return sum(1 for t in toks if token_in_text(t, blob))


def _speaker_stripped(raw: str) -> str:
    """去掉说话人前缀，避免 user_id 被当成主题词。"""
    body = (raw or "").lstrip()
    nl = body.find("\n")
    first = body if nl < 0 else body[:nl]
    if ":" not in first[:64]:
        return body
    head, rest = first.split(":", 1)
    key = head.strip()
    if not key or " " in key:
        return body
    tail = body[len(first) :]
    return (rest + tail).lstrip()


_HTML_DUMP_RE = re.compile(r"(?i)<(?:link|script|style|div|form|input|table|span|html|head|body|nav|ul|li|a)\b")
_TAG_RE = re.compile(r"<[^>]+>")
_STANDING_INSTR_RE = re.compile(r"(?i)^\s*(always|never|please always|do not ever|永远|千万不要|记住：)\b")


def _prose_without_fences(raw: str) -> str:
    return re.sub(r"\s+", " ", CODE_FENCE_RE.sub(" ", raw or "")).strip()


def _prose_without_markup(raw: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", _prose_without_fences(raw))).strip()


def _looks_like_code_dump(raw: str) -> bool:
    """几乎只剩粘贴块才当 dump；正文+代码示例仍是主题首次出现。"""
    body = raw or ""
    prose = _prose_without_markup(body)
    if len(_HTML_DUMP_RE.findall(body)) >= 3 and len(prose) < 120:
        return True
    if body.count("```") >= 2 and len(prose) < 80:
        return True
    if len(prose) >= 80:
        return False
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if len(lines) < 8:
        return False
    codeish = 0
    for ln in lines:
        if ln.startswith(("<", "def ", "class ", "import ", "from ", "{", "}", "#include")):
            codeish += 1
        elif ln.endswith((";", "{", "}")):
            codeish += 1
    return codeish >= 6


def looks_like_code_lead(raw: str) -> bool:
    """首 40 字是赋值/括号密度高的代码行，不能当阶段栏目。"""
    head = _prose_without_markup(_speaker_stripped(raw or ""))[:40]
    if len(head.strip()) < 8:
        return False
    dens = sum(1 for c in head if c in "={(")
    return dens >= 3


def _skip_order_noise(raw: str) -> bool:
    return bool(_STANDING_INSTR_RE.match(raw) or _looks_like_code_dump(raw) or looks_like_code_lead(raw))


def diversify_episodes(episodes: list[Episode], cap: int) -> list[Episode]:
    """目录卡跨会话取样：保留前两名向量命中，其余会话轮询。"""
    if len(episodes) <= cap:
        return list(episodes)
    clusters = cluster_episodes_by_time(episodes)
    if len(clusters) <= 1:
        return list(episodes[:cap])
    head = list(episodes[:2])
    seen = {str(e["id"]) for e in head if "id" in e}
    rest: list[list[Episode]] = []
    for cluster in clusters:
        rest.append([e for e in cluster if "id" in e and str(e["id"]) not in seen])
    picked = head
    i = 0
    while len(picked) < cap:
        progress = False
        for cluster in rest:
            if i < len(cluster):
                picked.append(cluster[i])
                progress = True
                if len(picked) >= cap:
                    break
        if not progress:
            break
        i += 1
    return picked[:cap]


def merge_episode_lists(
    primary: list[Episode],
    extras: list[Episode],
    *,
    prefer_extras: bool,
    limit: int,
) -> list[Episode]:
    """向量命中在前；给词面补条留位置，避免邻条把跨会话挤掉。"""
    seen: set[str] = set()
    out: list[Episode] = []

    def _add(ep: Episode) -> None:
        eid = str(ep["id"]) if "id" in ep else ""
        if not eid or eid in seen:
            return
        seen.add(eid)
        out.append(ep)

    if prefer_extras:
        for ep in primary[:_PRIMARY_KEEP]:
            _add(ep)
        for ep in extras:
            _add(ep)
        for ep in primary[_PRIMARY_KEEP:]:
            _add(ep)
    else:
        for ep in primary:
            _add(ep)
        for ep in extras:
            _add(ep)
    return out[:limit]


_STRONG_VALUE_RE = re.compile(
    r"\$\s?\d|\d+(?:\.\d+)?\s*%|\b\d{1,2}:\d{2}\b|"
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2}\b|\d{1,2}月\d{1,2}日|百分之\s?\d+",
    re.IGNORECASE,
)
_PLAIN_NUM_RE = re.compile(r"\b\d{1,4}\b")
_YEAR_ONLY_RE = re.compile(r"^(?:19|20)\d{2}$")
_NAMED_VALUE_RE = re.compile(r"\b(?:[A-Z]{2,}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_NAMED_VALUE_STOP = frozenset({"AI", "OK", "PC", "TV", "UK", "US", "USA"})


def _normalized_line(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip().lower())


def _states_named_entity(text: str) -> bool:
    """句中有大写专名或多词实体赋值，不要求出现阿拉伯数字。"""
    for sentence in _SENT_SPLIT_RE.split(text or ""):
        for match in _NAMED_VALUE_RE.finditer(sentence):
            token = match.group(0)
            if token in _NAMED_VALUE_STOP:
                continue
            if token.isupper() or match.start() > 0:
                return True
    return False


def states_a_value(text: str) -> bool:
    """原句里有数量、金额、比例、日期或具名实体。单独一个年份不算。"""
    if _STRONG_VALUE_RE.search(text or ""):
        return True
    for match in _PLAIN_NUM_RE.finditer(text or ""):
        if _YEAR_ONLY_RE.match(match.group(0)):
            continue
        return True
    return _states_named_entity(text)


def keep_sum_values(episodes: list[Episode], query: str, cap: int = 24) -> list[Episode]:
    """加总题保留主题词上每一笔带数字的原话，不按早中晚抽掉中段。"""
    topic = topic_pin_tokens(query, limit=1)
    tok = topic[0] if topic else ""
    kept: list[Episode] = []
    seen_line: set[str] = set()
    for ep in episodes:
        raw = ep["content"] or ""
        if _assistant_turn(raw) or not states_a_value(raw):
            continue
        key = _normalized_line(raw)
        if key in seen_line:
            continue
        seen_line.add(key)
        if tok and not token_in_text(tok, raw.lower()):
            continue
        kept.append(ep)
    kept.sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    return kept[:cap]


def spread_value_episodes(episodes: list[Episode], cap: int = 8) -> list[Episode]:
    """早、中、晚都留。只留最新几条会把中段那次更新丢掉。"""
    ordered = sorted(episodes, key=lambda ep: str(ep["valid_at"] if "valid_at" in ep else ""))
    uniq: list[Episode] = []
    seen: set[str] = set()
    for ep in ordered:
        eid = ep["id"] if "id" in ep else ""
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        uniq.append(ep)
    n = len(uniq)
    if cap <= 0 or n <= cap:
        return uniq
    want = {0, 1, n // 3, (2 * n) // 3, n - 4, n - 3, n - 2, n - 1}
    idxs = sorted(i for i in want if 0 <= i < n)[:cap]
    return [uniq[i] for i in idxs]


def render_value_timeline(episodes: list[Episode], query: str, budget: int) -> str:
    """赋值原句整段放进预算。缩行宽，不把较晚的那次截掉。"""
    if not episodes or budget < 80:
        return ""
    header = "【该事项的原话】"
    for width in (480, 280, 160):
        lines: list[str] = []
        for ep in episodes:
            raw = _speaker_stripped(ep["content"] or "")
            body = excerpt_around_tokens(raw, query, width)
            ts = str(ep["valid_at"] if "valid_at" in ep else "")[:19].replace("T", " ")
            stamp = f"[{ts}] " if ts else ""
            lines.append(f"{stamp}{body}")
        block = header + "\n" + "\n".join(lines)
        if len(block) <= budget:
            return block
    return ""


def stride_keep(episodes: list[Episode], cap: int) -> list[Episode]:
    """按时间铺开，头尾和中段都留。"""
    ordered = sorted(episodes, key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    n = len(ordered)
    if cap <= 0 or n <= cap:
        return ordered
    if cap == 1:
        return [ordered[n // 2]]
    idxs = {int(round(i * (n - 1) / (cap - 1))) for i in range(cap)}
    return [ordered[i] for i in sorted(idxs)]


def merge_strided_groups(groups: list[list[Episode]], cap: int) -> list[Episode]:
    """命中少的词先留全，再铺开高频词。混在一起抽样会把专名挤掉。"""
    ordered = sorted((g for g in groups if g), key=len)
    if not ordered or cap <= 0:
        return []
    per = max(8, cap // len(ordered))
    out: list[Episode] = []
    seen: set[str] = set()
    for group in ordered:
        for ep in stride_keep(group, per):
            eid = str(ep["id"]) if "id" in ep else ""
            if not eid or eid in seen:
                continue
            seen.add(eid)
            out.append(ep)
            if len(out) >= cap:
                return out
    return out


def _topic_word_in_piece(tok: str, piece_low: str) -> bool:
    # 主题词或其单复数/过去式/进行式，保持形态匹配对称
    if token_in_text(tok, piece_low):
        return True
    low = tok.lower().strip()
    if len(low) < 3 or not low.isascii() or " " in low:
        return False
    stem = low
    if low.endswith("ies") and len(low) > 4:
        stem = low[:-3] + "y"
    elif low.endswith("es") and len(low) > 4:
        stem = low[:-2]
    elif low.endswith("s") and not low.endswith("ss") and len(low) > 3:
        stem = low[:-1]
    if token_in_text(stem, piece_low):
        return True
    return re.search(rf"\b{re.escape(stem)}(?:e?s|e?d|ing)?\b", piece_low) is not None
