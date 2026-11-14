"""相对发生时间：陈述时刻 + 原文里的 today / N weeks ago 等。

生产事实边与评测共用。不解析 bare ``recently``（回指，不是新事件）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_AGO_NUM_WORDS: dict[str, int] = {
    "a": 1,
    "an": 1,
    "a few": 3,
    "a couple of": 2,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "few": 3,
}
_AGO_RE = re.compile(
    r"\b(a couple of|a few|an|a|one|two|three|four|five|six|seven|eight|nine|ten|few|\d+)\s+"
    r"(days?|weeks?|months?)\s+ago\b",
    re.IGNORECASE,
)
_PAST_SPAN_RE = re.compile(
    r"\b(?:in the |over the )?past\s+"
    r"(?:(a couple of|a few|an|a|one|two|three|four|five|six|seven|eight|nine|ten|few|\d+)\s+)?"
    r"(days?|weeks?|months?)\b",
    re.IGNORECASE,
)
_CN_AGO_RE = re.compile(r"(两|三|四|五|六|七|十|\d+)\s*([天周月])前")
_CN_YESTERDAY_RE = re.compile(r"昨天|昨晚")
_CN_LAST_WEEK_RE = re.compile(r"上周")
_CN_AGO_NUM: dict[str, int] = {
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "十": 10,
}
_TODAY_RE = re.compile(r"\b(today|this morning|this afternoon|this evening|tonight)\b", re.IGNORECASE)
_YESTERDAY_RE = re.compile(r"\b(yesterday|last night)\b", re.IGNORECASE)
_LAST_WEEK_RE = re.compile(r"\blast week\b", re.IGNORECASE)
_LAST_MONTH_RE = re.compile(r"\blast month\b", re.IGNORECASE)
_LAST_WEEKDAY_RE = re.compile(
    r"\blast (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_JUST_DONE_RE = re.compile(
    r"\bjust (got back|got home|finished|fixed|did|completed|returned)\b",
    re.IGNORECASE,
)
_WEEKDAY_INDEX: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _ago_count(raw: str) -> int:
    key = raw.lower().strip()
    if key in _AGO_NUM_WORDS:
        return _AGO_NUM_WORDS[key]
    if raw.isdigit():
        return int(raw)
    return 1


def _shift_ago(said: datetime, n: int, unit: str) -> datetime:
    u = unit.lower()
    if u.startswith("day"):
        return said - timedelta(days=n)
    if u.startswith("week"):
        return said - timedelta(days=7 * n)
    return said - timedelta(days=30 * n)


def _last_weekday(said: datetime, name: str) -> datetime:
    wd = _WEEKDAY_INDEX[name.lower()] if name.lower() in _WEEKDAY_INDEX else said.weekday()
    delta = (said.weekday() - wd) % 7
    if delta == 0:
        delta = 7
    return said - timedelta(days=delta)


def event_at_from_text(text: str, said_at: datetime) -> datetime:
    """发生时刻：原文相对语；没有则退回陈述时刻。"""
    times = event_times_in_text(text, said_at)
    return min(times) if times else said_at


def event_times_in_text(text: str, said_at: datetime) -> list[datetime]:
    """从用户话里抽出相对发生时间。不解析 recently。"""
    blob = (text or "").lower().replace("’", "'")
    out: list[datetime] = []
    for m in _AGO_RE.finditer(blob):
        out.append(_shift_ago(said_at, _ago_count(m.group(1)), m.group(2)))
    for m in _LAST_WEEKDAY_RE.finditer(blob):
        out.append(_last_weekday(said_at, m.group(1)))
    if _YESTERDAY_RE.search(blob):
        out.append(said_at - timedelta(days=1))
    if _LAST_WEEK_RE.search(blob):
        out.append(said_at - timedelta(days=7))
    if _LAST_MONTH_RE.search(blob):
        out.append(said_at - timedelta(days=30))
    if _TODAY_RE.search(blob):
        out.append(said_at)
    if not out and _JUST_DONE_RE.search(blob):
        out.append(said_at)
    return out


def has_relative_time_span(query: str) -> bool:
    """问句在用相对日定位事件。不含裸 today，避免和 currently 撞车。"""
    blob = (query or "").lower().replace("’", "'")
    if _AGO_RE.search(blob) or _PAST_SPAN_RE.search(blob):
        return True
    if _LAST_WEEKDAY_RE.search(blob) or _YESTERDAY_RE.search(blob):
        return True
    if _LAST_WEEK_RE.search(blob) or _LAST_MONTH_RE.search(blob):
        return True
    if _CN_AGO_RE.search(query or "") or _CN_YESTERDAY_RE.search(query or ""):
        return True
    return bool(_CN_LAST_WEEK_RE.search(query or ""))


def strip_relative_time_spans(query: str) -> str:
    """去掉 last Saturday / N days ago，留给珠宝、厨电这类事件名词。"""
    body = query or ""
    for pat in (
        _PAST_SPAN_RE,
        _AGO_RE,
        _LAST_WEEKDAY_RE,
        _LAST_WEEK_RE,
        _LAST_MONTH_RE,
        _YESTERDAY_RE,
        _CN_AGO_RE,
        _CN_YESTERDAY_RE,
        _CN_LAST_WEEK_RE,
    ):
        body = pat.sub(" ", body)
    return _WS_RE.sub(" ", body).strip()


def _cn_ago_count(raw: str) -> int:
    if raw.isdigit():
        return int(raw)
    return _CN_AGO_NUM[raw] if raw in _CN_AGO_NUM else 1


def _cn_unit_to_en(unit: str) -> str:
    if unit == "天":
        return "day"
    if unit == "周":
        return "week"
    return "month"


def _query_anchor_times(query: str, clock: datetime) -> list[datetime]:
    blob = (query or "").lower().replace("’", "'")
    out: list[datetime] = []
    for m in _AGO_RE.finditer(blob):
        out.append(_shift_ago(clock, _ago_count(m.group(1)), m.group(2)))
    for m in _LAST_WEEKDAY_RE.finditer(blob):
        out.append(_last_weekday(clock, m.group(1)))
    if _YESTERDAY_RE.search(blob) or _CN_YESTERDAY_RE.search(query or ""):
        out.append(clock - timedelta(days=1))
    if _LAST_WEEK_RE.search(blob) or _CN_LAST_WEEK_RE.search(query or ""):
        out.append(clock - timedelta(days=7))
    if _LAST_MONTH_RE.search(blob):
        out.append(clock - timedelta(days=30))
    for m in _CN_AGO_RE.finditer(query or ""):
        out.append(_shift_ago(clock, _cn_ago_count(m.group(1)), _cn_unit_to_en(m.group(2))))
    return out


def _window_slack_days(query: str) -> int:
    blob = (query or "").lower()
    m = _AGO_RE.search(blob)
    if m is None:
        m = _PAST_SPAN_RE.search(blob)
    if m is not None:
        unit = m.group(2).lower()
        if unit.startswith("week"):
            return 3
        if unit.startswith("month"):
            return 7
        return 1
    if _LAST_WEEK_RE.search(blob) or _CN_LAST_WEEK_RE.search(query or ""):
        return 4
    if _LAST_MONTH_RE.search(blob):
        return 7
    return 1


def query_time_window(query: str, clock: datetime) -> tuple[datetime, datetime] | None:
    """问句相对日相对墙上时钟换成 [start, end]。无相对语则 None。"""
    blob = (query or "").lower().replace("’", "'")
    span = _PAST_SPAN_RE.search(blob)
    if span is not None:
        n = _ago_count(span.group(1)) if span.group(1) else 1
        start = _shift_ago(clock, n, span.group(2))
        return start, clock + timedelta(days=1)
    times = _query_anchor_times(query, clock)
    if not times:
        return None
    slack = timedelta(days=_window_slack_days(query))
    return min(times) - slack, max(times) + slack + timedelta(hours=23, minutes=59)


# 分隔符两侧允许空格：中文正文常写成「2024 年 1 月 1 日」，不容忍空格时整句匹配不到，
# 于是带枚举词也照样不收窄时间窗。
_QUERY_DATE_RE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?")
_WS_RE = re.compile(r"\s+")


def query_explicit_time_range(query: str) -> tuple[datetime, datetime] | None:
    """query 含 ≥2 个显式日期 → [最早, 最晚+1天]。"""
    dates: list[datetime] = []
    for m in _QUERY_DATE_RE.finditer(query or ""):
        try:
            dates.append(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    if len(set(dates)) < 2:
        return None
    return min(dates), max(dates) + timedelta(days=1)


def temporal_search_query(query: str) -> str:
    """去掉显式日期字符串，留给分桶语义检索的主题词；剥空时退回原问句。"""
    body = _QUERY_DATE_RE.sub(" ", query or "")
    body = _WS_RE.sub(" ", body).strip(" ,.?!")
    return body or (query or "").strip()


_ONES: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_ITEM_CAP_RE = re.compile(
    r"\b(?:(\d+)|(one|two|three|four|five|six|seven|eight|nine|ten))\s+"
    r"(?:(?:main|key|major|different)\s+)?(?:items?|aspects?|stages?|steps?|phases?|topics?|events?)\b|"
    r"(?:列出|举出|说出|恰好|只要)\s*(\d+)\s*[条个项阶段方面步]",
    re.IGNORECASE,
)


def query_only_item_cap(query: str) -> int | None:
    """「list 3 items / 列出5条」→ 数量上限；没有则 None。"""
    m = _ITEM_CAP_RE.search(query or "")
    if m is None:
        return None
    if m.group(1):
        n = int(m.group(1))
        return n if 1 <= n <= 20 else None
    word = (m.group(2) or "").lower()
    if word in _ONES:
        return _ONES[word]
    if m.group(3):
        n = int(m.group(3))
        return n if 1 <= n <= 20 else None
    return None
