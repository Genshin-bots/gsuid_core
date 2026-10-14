"""一次性相对日程。习惯句和对「明天」的释义规则不在这里。"""

import re
from datetime import datetime, timezone

# 大后天放在后天前，避免短词先吃掉长词（search 只关心是否命中）。
_RELATIVE_WORD_RE = re.compile(
    r"(?:大后天|今天|明天|后天|昨天|前天|昨晚|今晚|明早|明晚|刚才|下周|本周|这周|下个月|这周末)"
)
# 长期习惯，或在解释「明天」这个词本身。
_STANDING_RE = re.compile(r"(?:一般|通常|每天|总是|习惯|以后|每次|不要|别在|禁止|指|理解|时区|叫做|称为|算作|意思是)")


def looks_like_one_off_relative_schedule(text: str) -> bool:
    """正文是某一次相对时间的行程，不是习惯，也不是时间词的释义。"""
    body = (text or "").strip()
    if not body or _STANDING_RE.search(body):
        return False
    return _RELATIVE_WORD_RE.search(body) is not None


def _as_utc(moment: datetime) -> datetime:
    """无时区的库存值按 UTC 钟面读。本列 default_factory 写的就是 UTC。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def preference_written_on(created_at: datetime) -> str:
    """偏好写入日，用本机本地日历，避免把 UTC 日期直接标给用户。"""
    return _as_utc(created_at).astimezone().strftime("%Y.%m.%d")


def one_off_schedule_expired(created_at: datetime, *, now: datetime) -> bool:
    """写入时刻到 now 是否已超过 24 小时。两边都折成 UTC 再减。"""
    return (_as_utc(now) - _as_utc(created_at)).total_seconds() > 86400
