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
_CLOCK_PARSE_RE = re.compile(
    r"(?:当前时间[：:]\s*|\[当前时间[：:]\s*)"
    r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
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


def parse_query_clock(text: str) -> datetime | None:
    """问句里的 inject_date / [当前时间]；没有就不猜墙上时钟。"""
    if not text:
        return None
    m = _CLOCK_PARSE_RE.search(text)
    if m is None:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4)) if m.group(4) else 12
    mm = int(m.group(5)) if m.group(5) else 0
    ss = int(m.group(6)) if m.group(6) else 0
    try:
        return datetime(y, mo, d, hh, mm, ss)
    except ValueError:
        return None


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
    return re.sub(r"\s+", " ", body).strip()


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
