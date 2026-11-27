"""相对发生时间：陈述时刻 + 原文里的 today / N weeks ago 等。

生产事实边与评测共用。不解析 bare ``recently``（回指，不是新事件）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from datetime import date, datetime, timezone, timedelta

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.retrieval.types import Episode

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
    r"(days?|weeks?|months?|years?)\s+ago\b",
    re.IGNORECASE,
)
_PAST_SPAN_RE = re.compile(
    r"\b(?:in the |over the )?past\s+"
    r"(?:(a couple of|a few|an|a|one|two|three|four|five|six|seven|eight|nine|ten|few|\d+)\s+)?"
    r"(days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_CN_AGO_RE = re.compile(r"(一|两|二|三|四|五|六|七|八|九|十|\d+)\s*(?:个)?([天周月年])前")
_CN_TODAY_RE = re.compile(r"今天|今晚|今早|刚刚|刚才")
_CN_YESTERDAY_RE = re.compile(r"昨天|昨晚|昨儿")
_CN_DAY_BEFORE_YESTERDAY_RE = re.compile(r"前天")
_CN_THREE_DAYS_AGO_RE = re.compile(r"大前天")
_CN_LAST_WEEKDAY_RE = re.compile(r"上周([一二三四五六日天])")
_CN_LAST_WEEK_RE = re.compile(r"上周(?![一二三四五六日天])")
_CN_LAST_MONTH_RE = re.compile(r"上个月|上月")
_CN_YMD_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
_CN_MD_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_CN_AGO_NUM: dict[str, int] = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_CN_WEEKDAY_INDEX: dict[str, int] = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}
_EN_MONTH_MAP: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_EN_MONTH_DAY_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(\d{4}))?\b",
    re.IGNORECASE,
)
_TODAY_RE = re.compile(r"\b(today|this morning|this afternoon|this evening|tonight)\b", re.IGNORECASE)
_DAY_BEFORE_YESTERDAY_RE = re.compile(r"\bthe day before yesterday\b", re.IGNORECASE)
_YESTERDAY_RE = re.compile(r"(?<!before )\b(yesterday|last night)\b", re.IGNORECASE)
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
    if u.startswith("year"):
        return said - timedelta(days=365 * n)
    return said - timedelta(days=30 * n)


def _last_weekday(said: datetime, name: str) -> datetime:
    wd = _WEEKDAY_INDEX[name.lower()] if name.lower() in _WEEKDAY_INDEX else said.weekday()
    delta = (said.weekday() - wd) % 7
    if delta == 0:
        delta = 7
    return said - timedelta(days=delta)


def _last_cn_weekday(said: datetime, ch: str) -> datetime:
    wd = _CN_WEEKDAY_INDEX[ch] if ch in _CN_WEEKDAY_INDEX else said.weekday()
    delta = (said.weekday() - wd) % 7
    if delta == 0:
        delta = 7
    return said - timedelta(days=delta)


def _safe_date(year: int, month: int, day: int, ref: datetime) -> datetime | None:
    try:
        return datetime(year, month, day, ref.hour, ref.minute, ref.second, tzinfo=ref.tzinfo)
    except ValueError:
        return None


def _explicit_dates_in_text(raw: str, said_at: datetime) -> list[datetime]:
    out: list[datetime] = []
    used_spans: list[tuple[int, int]] = []
    for m in _CN_YMD_RE.finditer(raw):
        dt = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), said_at)
        if dt is not None:
            out.append(dt)
            used_spans.append(m.span())
    for m in _CN_MD_RE.finditer(raw):
        if any(s <= m.start() and m.end() <= e for s, e in used_spans):
            continue
        year = said_at.year
        dt = _safe_date(year, int(m.group(1)), int(m.group(2)), said_at)
        if dt is not None:
            if dt.date() > said_at.date() + timedelta(days=7):
                dt = _safe_date(year - 1, int(m.group(1)), int(m.group(2)), said_at) or dt
            out.append(dt)
    for m in _EN_MONTH_DAY_RE.finditer(raw):
        m_key = m.group(1).lower()
        month = _EN_MONTH_MAP[m_key] if m_key in _EN_MONTH_MAP else 0
        if not month:
            continue
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else said_at.year
        dt = _safe_date(year, month, day, said_at)
        if dt is not None:
            if not m.group(3) and dt.date() > said_at.date() + timedelta(days=7):
                dt = _safe_date(year - 1, month, day, said_at) or dt
            out.append(dt)
    return out


def event_at_from_text(text: str, said_at: datetime) -> datetime:
    """发生时刻：原文显式或相对日期；没有则退回陈述时刻。"""
    times = event_times_in_text(text, said_at)
    return min(times) if times else said_at


def event_times_in_text(text: str, said_at: datetime) -> list[datetime]:
    """从用户话里抽出中英文显式/相对发生时间。不解析 recently。"""
    raw = text or ""
    blob = raw.lower().replace("’", "'")
    out: list[datetime] = []
    out.extend(_explicit_dates_in_text(raw, said_at))
    for m in _AGO_RE.finditer(blob):
        out.append(_shift_ago(said_at, _ago_count(m.group(1)), m.group(2)))
    for m in _CN_AGO_RE.finditer(raw):
        out.append(_shift_ago(said_at, _cn_ago_count(m.group(1)), _cn_unit_to_en(m.group(2))))
    for m in _LAST_WEEKDAY_RE.finditer(blob):
        out.append(_last_weekday(said_at, m.group(1)))
    for m in _CN_LAST_WEEKDAY_RE.finditer(raw):
        out.append(_last_cn_weekday(said_at, m.group(1)))
    if _DAY_BEFORE_YESTERDAY_RE.search(blob) or _CN_DAY_BEFORE_YESTERDAY_RE.search(raw):
        out.append(said_at - timedelta(days=2))
    if _CN_THREE_DAYS_AGO_RE.search(raw):
        out.append(said_at - timedelta(days=3))
    if _YESTERDAY_RE.search(blob) or _CN_YESTERDAY_RE.search(raw):
        out.append(said_at - timedelta(days=1))
    if _LAST_WEEK_RE.search(blob) or _CN_LAST_WEEK_RE.search(raw):
        out.append(said_at - timedelta(days=7))
    if _LAST_MONTH_RE.search(blob) or _CN_LAST_MONTH_RE.search(raw):
        out.append(said_at - timedelta(days=30))
    if _TODAY_RE.search(blob) or _CN_TODAY_RE.search(raw):
        out.append(said_at)
    if not out and _JUST_DONE_RE.search(blob):
        out.append(said_at)
    return out


def effective_event_at(ep: "Episode") -> str:
    """Episode 真实发生时间排序键（优先显式 event_at，再按原文相对/显式日期折算）。"""
    if "event_at" in ep and ep["event_at"].strip():
        return ep["event_at"].strip().replace("T", " ")[:19]
    valid_raw = (ep["valid_at"] if "valid_at" in ep else "").strip()
    if not valid_raw:
        return ""
    norm = valid_raw.replace("T", " ")[:19]
    said: datetime | None = None
    for fmt, slen in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        if len(norm) < slen:
            continue
        try:
            said = datetime.strptime(norm[:slen], fmt)
            break
        except ValueError:
            continue
    if said is None:
        return norm
    ev = event_at_from_text(ep["content"] if "content" in ep else "", said)
    if ev.date() != said.date():
        return ev.strftime("%Y-%m-%d %H:%M:%S")
    return norm


def has_relative_time_span(query: str) -> bool:
    """问句在用相对日定位事件。不含裸 today，避免和 currently 撞车。"""
    raw = query or ""
    blob = raw.lower().replace("’", "'")
    if _AGO_RE.search(blob) or _PAST_SPAN_RE.search(blob):
        return True
    if _LAST_WEEKDAY_RE.search(blob) or _YESTERDAY_RE.search(blob) or _DAY_BEFORE_YESTERDAY_RE.search(blob):
        return True
    if _LAST_WEEK_RE.search(blob) or _LAST_MONTH_RE.search(blob):
        return True
    if (
        _CN_AGO_RE.search(raw)
        or _CN_YESTERDAY_RE.search(raw)
        or _CN_DAY_BEFORE_YESTERDAY_RE.search(raw)
        or _CN_THREE_DAYS_AGO_RE.search(raw)
        or _CN_LAST_WEEKDAY_RE.search(raw)
        or _CN_LAST_MONTH_RE.search(raw)
    ):
        return True
    return bool(_CN_LAST_WEEK_RE.search(raw))


def strip_relative_time_spans(query: str) -> str:
    """去掉 last Saturday / N days ago，留给珠宝、厨电这类事件名词。"""
    body = query or ""
    for pat in (
        _PAST_SPAN_RE,
        _AGO_RE,
        _LAST_WEEKDAY_RE,
        _LAST_WEEK_RE,
        _LAST_MONTH_RE,
        _DAY_BEFORE_YESTERDAY_RE,
        _YESTERDAY_RE,
        _CN_AGO_RE,
        _CN_DAY_BEFORE_YESTERDAY_RE,
        _CN_THREE_DAYS_AGO_RE,
        _CN_YESTERDAY_RE,
        _CN_LAST_WEEKDAY_RE,
        _CN_LAST_WEEK_RE,
        _CN_LAST_MONTH_RE,
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
    if unit == "年":
        return "year"
    return "month"


def _query_anchor_times(query: str, clock: datetime) -> list[datetime]:
    raw = query or ""
    blob = raw.lower().replace("’", "'")
    out: list[datetime] = []
    for m in _AGO_RE.finditer(blob):
        out.append(_shift_ago(clock, _ago_count(m.group(1)), m.group(2)))
    for m in _LAST_WEEKDAY_RE.finditer(blob):
        out.append(_last_weekday(clock, m.group(1)))
    for m in _CN_LAST_WEEKDAY_RE.finditer(raw):
        out.append(_last_cn_weekday(clock, m.group(1)))
    if _DAY_BEFORE_YESTERDAY_RE.search(blob) or _CN_DAY_BEFORE_YESTERDAY_RE.search(raw):
        out.append(clock - timedelta(days=2))
    if _CN_THREE_DAYS_AGO_RE.search(raw):
        out.append(clock - timedelta(days=3))
    if _YESTERDAY_RE.search(blob) or _CN_YESTERDAY_RE.search(raw):
        out.append(clock - timedelta(days=1))
    if _LAST_WEEK_RE.search(blob) or _CN_LAST_WEEK_RE.search(raw):
        out.append(clock - timedelta(days=7))
    if _LAST_MONTH_RE.search(blob) or _CN_LAST_MONTH_RE.search(raw):
        out.append(clock - timedelta(days=30))
    for m in _CN_AGO_RE.finditer(raw):
        out.append(_shift_ago(clock, _cn_ago_count(m.group(1)), _cn_unit_to_en(m.group(2))))
    return out


def _window_slack_days(query: str) -> int:
    raw = query or ""
    blob = raw.lower()
    m = _AGO_RE.search(blob)
    if m is None:
        m = _PAST_SPAN_RE.search(blob)
    if m is not None:
        unit = m.group(2).lower()
        if unit.startswith("week"):
            return 3
        if unit.startswith("month"):
            return 7
        if unit.startswith("year"):
            return 30
        return 1
    cn_m = _CN_AGO_RE.search(raw)
    if cn_m is not None:
        unit = cn_m.group(2)
        if unit == "周":
            return 3
        if unit == "月":
            return 7
        if unit == "年":
            return 30
        return 1
    if _LAST_WEEK_RE.search(blob) or _CN_LAST_WEEK_RE.search(raw):
        return 4
    if _LAST_MONTH_RE.search(blob) or _CN_LAST_MONTH_RE.search(raw):
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


# ── 回灌时间戳展开（同日撞点依次 +1s，保持日历日）────────────────


def _naive_utc(ts: datetime) -> datetime:
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def spread_datetimes(timestamps: list[datetime | None]) -> list[datetime | None]:
    """同一日历日里后一条不早于前一条；跨日重置。None 原样留下。"""
    out: list[datetime | None] = []
    last: datetime | None = None
    last_day: date | None = None
    for ts in timestamps:
        if ts is None:
            out.append(None)
            continue
        cur = _naive_utc(ts)
        day = cur.date()
        if last_day != day:
            last = None
            last_day = day
        if last is not None and cur <= last:
            nxt = last + timedelta(seconds=1)
            if nxt.date() != day:
                nxt = last + timedelta(microseconds=1)
            if nxt.date() != day:
                nxt = last
            cur = nxt
        last = cur
        out.append(cur)
    return out


# ── 一次性相对日程与 24h 过期判断 ────────────────────────────────

_RELATIVE_WORD_RE = re.compile(
    r"(?:大后天|今天|明天|后天|昨天|前天|昨晚|今晚|明早|明晚|刚才|下周|本周|这周|下个月|这周末)"
)
_STANDING_RE = re.compile(r"(?:一般|通常|每天|总是|习惯|以后|每次|不要|别在|禁止|指|理解|时区|叫做|称为|算作|意思是)")


def looks_like_one_off_relative_schedule(text: str) -> bool:
    """正文是某一次相对时间的行程，不是习惯，也不是时间词的释义。"""
    body = (text or "").strip()
    if not body or _STANDING_RE.search(body):
        return False
    return _RELATIVE_WORD_RE.search(body) is not None


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def preference_written_on(created_at: datetime) -> str:
    """偏好写入日，用本机本地日历，避免把 UTC 日期直接标给用户。"""
    return _as_utc(created_at).astimezone().strftime("%Y.%m.%d")


def one_off_schedule_expired(created_at: datetime, *, now: datetime) -> bool:
    """写入时刻到 now 是否已超过 24 小时。两边都折成 UTC 再减。"""
    return (_as_utc(now) - _as_utc(created_at)).total_seconds() > 86400
