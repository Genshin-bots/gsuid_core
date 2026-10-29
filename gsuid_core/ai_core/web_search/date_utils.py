"""网页搜索结果发布日期归一化工具。"""

from __future__ import annotations

import re
from email.utils import parsedate_to_datetime

_DATE_PREFIX_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_DIRTY_DATE_VALUES = frozenset({"null", "none", "unknown", "n/a", "undefined", "0000-00-00"})


def normalize_published_date(raw: object) -> str:
    """归一化页面发布日期为 YYYY-MM-DD 格式。无效或无法解析时返回空串。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s.lower() in _DIRTY_DATE_VALUES:
        return ""
    m = _DATE_PREFIX_RE.match(s)
    if m:
        try:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"
        except ValueError:
            pass
    try:
        dt = parsedate_to_datetime(s)
        if 1990 <= dt.year <= 2100:
            return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    return ""
