"""回灌时间戳：同日撞点依次 +1s，保持日历日、恢复会话内顺序。"""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta


def _naive(ts: datetime) -> datetime:
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
        cur = _naive(ts)
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
