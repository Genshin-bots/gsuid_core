"""人格自身复读提示：最近几条人格发言里反复出现、群友这段时间没说过的片段。

只看字面（汉字二字片段跨条出现的次数），不收任何人格的口癖词表（AGENTS §1.9）。
提示以「（系统：」开头，入史前由 ``_relean_user_turn`` 剥掉，不进持久 history。
"""

import re
from typing import Sequence
from collections import Counter

from gsuid_core.message_history import MessageRecord
from gsuid_core.ai_core.history_format import is_persona_speech

_PERSONA_WINDOW = 8
_MIN_PERSONA_LINES = 4
_MIN_REPEATS = 3
# 窗口前紧邻的少量用户发言也算「群友说过」：复述当前话题词不算复读。
_LEAD_RECORDS = 6
_MAX_FRAGMENTS = 3
_FRAGMENT_MAX = 10

_NOISE_RE = re.compile(r"\[[^\]]*\]|【[^】]*】|<[^>]*>|@\S+")
_HAN_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_HAN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_CLAUSE_SPLIT_RE = re.compile(r"[，。！？!?；;：:…、,~～\s]+")
# 现代汉语高频虚词/代词/轻动词闭类表（不含任何角色口癖或业务词，AGENTS §1.9）
_STOP_CHARS = frozenset(
    "的了是我你他她它这那在有就也还都吧呢啦呀吗嘛喔欸唔呼哼哈唉哦噢"
    "不没别很真太更最要能会可以去来着过个只条次点些好大少多上下里外中前后"
    "再又才并而且但和与或把被让给对向从到比跟同说看听想做干弄吃喝拿用叫问答觉得知道"
)


def _han_bigrams(text: str) -> set[str]:
    grams: set[str] = set()
    for run in _HAN_RUN_RE.findall(_NOISE_RE.sub(" ", text)):
        grams.update(run[i : i + 2] for i in range(len(run) - 1))
    return grams


def _han_content_unigrams(text: str) -> set[str]:
    cleaned = _NOISE_RE.sub(" ", text)
    return {ch for ch in _HAN_CHAR_RE.findall(cleaned) if ch not in _STOP_CHARS}


def _fragment_around(clause: str, gram: str) -> str:
    if len(clause) <= _FRAGMENT_MAX:
        return clause
    start = clause.index(gram) - (_FRAGMENT_MAX - len(gram)) // 2
    start = max(0, min(start, len(clause) - _FRAGMENT_MAX))
    return clause[start : start + _FRAGMENT_MAX]


def _repeated_fragments(persona_lines: Sequence[str], heard: Sequence[str]) -> list[str]:
    """近几条人格发言里 ≥3 条都有、``heard`` 里没有的片段；按出现次数排，至多 3 个。"""
    lines = [s for s in (_NOISE_RE.sub(" ", x).strip() for x in persona_lines) if s][-_PERSONA_WINDOW:]
    if len(lines) < _MIN_PERSONA_LINES:
        return []
    counts: Counter[str] = Counter()
    uni_counts: Counter[str] = Counter()
    for line in lines:
        counts.update(_han_bigrams(line))
        uni_counts.update(_han_content_unigrams(line))
    heard_grams: set[str] = set()
    heard_unis: set[str] = set()
    for text in heard:
        heard_grams |= _han_bigrams(text)
        heard_unis |= _han_content_unigrams(text)
    hot_bigrams = [g for g, n in counts.most_common() if n >= _MIN_REPEATS and g not in heard_grams]
    hot_unis = [ch for ch, n in uni_counts.most_common() if n >= _MIN_REPEATS and ch not in heard_unis]
    hot = hot_bigrams + hot_unis
    clauses = [c for line in reversed(lines) for c in _CLAUSE_SPLIT_RE.split(line) if c]
    picked: list[str] = []
    for gram in hot:
        if any(gram in frag for frag in picked):
            continue
        holders = [c for c in clauses if gram in c]
        if not holders:
            continue
        picked.append(_fragment_around(min(holders, key=len), gram))
        if len(picked) >= _MAX_FRAGMENTS:
            break
    return picked


def self_repetition_hint(
    records: Sequence[MessageRecord],
    *,
    query: str,
    exempt_terms: Sequence[str],
) -> str:
    """会话近窗里人格的复读提示；没有复读返回空串。

    ``exempt_terms``：人格名、唤醒词、主人称呼这类固定称呼，重复出现不算复读。
    """
    persona_at = [i for i, r in enumerate(records) if is_persona_speech(r) and r.content.strip()]
    window = persona_at[-_PERSONA_WINDOW:]
    if len(window) < _MIN_PERSONA_LINES:
        return ""
    heard: list[str] = [query, *exempt_terms]
    for rec in records[max(0, window[0] - _LEAD_RECORDS) :]:
        if rec.role != "user":
            continue
        heard.append(rec.content)
        if rec.user_name:
            heard.append(rec.user_name)
    fragments = _repeated_fragments([records[i].content for i in window], heard)
    if not fragments:
        return ""
    from gsuid_core.ai_core.control.lines import repetition_line

    quoted = "".join(f"「{f}」" for f in fragments)
    return repetition_line(quoted)
