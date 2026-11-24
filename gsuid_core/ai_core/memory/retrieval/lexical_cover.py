"""跨文档覆盖摘录与 HTML/文本表格平铺。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gsuid_core.ai_core.memory.retrieval.types import Episode
from gsuid_core.ai_core.memory.retrieval.lexical_query import (
    _ASK_FILLER,
    _TOPIC_SKIP,
    _HAS_DIGIT_RE,
    _DOC_TOKEN_SKIP,
    _QUERY_STOPWORDS,
    _OVERLAP_TOKEN_RE,
    token_in_text,
    strip_clock_lines,
)

# 表头命中后连带保留的数据行数。表头只有列名，跨文档比较要的是它下面的数字。
# 多留几行：同一栏目常有期初/变动/期末或多期并列，3 行容易只剩表头附近噪声。
_DATA_ROWS_AFTER_HEADER = 8


# 每个栏目词最多保留几条命中行（不再 top-1），预算由 _fit_cover_lines 收。
_COVER_HITS_PER_TOKEN = 3


# 覆盖摘录的抽取预算，不是注入预算：先按来源选行，注入再按 inject_memory_cap 收。
# 抽取侧必须比注入宽档大，否则均分行数会在注入截断之前就被饿死。
DOC_COVER_EXTRACT_BUDGET = 96000


_DOC_SOURCE_RE = re.compile(r"【文档(?:续)?】([^\n]+)")


def episode_source_key(content: str) -> str:
    """平行材料的来源键：文档标题，否则说话人前缀，否则开头若干字。"""
    raw = (content or "").strip()
    m = _DOC_SOURCE_RE.search(raw)
    if m:
        return m.group(1).strip()[:80]
    if ": " in raw[:96]:
        head, rest = raw.split(": ", 1)
        if len(head) <= 64 and "\n" not in head:
            raw = rest
            m = _DOC_SOURCE_RE.search(raw)
            if m:
                return m.group(1).strip()[:80]
    hm = re.search(r"^#\s+(.+)$", raw, re.M)
    if hm:
        return hm.group(1).strip()[:80]
    return raw[:48]


def is_ingested_document_episode(content: str) -> bool:
    return bool(_DOC_SOURCE_RE.search(content or ""))


def has_document_coverage_tokens(query: str) -> bool:
    """问句里有可当栏目的实词。没有则不必加载入库文档池。"""
    return bool(_coverage_tokens(query))


def _coverage_tokens(query: str) -> list[str]:
    """问句里的实词。不按出现顺序截断，否则后半句的栏目词进不了权重。"""
    body = strip_clock_lines(query or "")
    out: list[str] = []
    seen: set[str] = set()

    def add(piece: str) -> None:
        key = piece.lower()
        if not key or key in seen:
            return
        if key in _ASK_FILLER or key in _TOPIC_SKIP or key in _DOC_TOKEN_SKIP or key in _QUERY_STOPWORDS:
            return
        if piece.isascii() and piece.isdigit() and len(piece) < 4:
            return
        if piece.isascii() and len(piece) < 4 and not any(ch.isdigit() for ch in piece):
            return
        if not piece.isascii() and len(piece) < 2:
            return
        seen.add(key)
        out.append(piece)

    for match in _OVERLAP_TOKEN_RE.finditer(body):
        tok = match.group(0).replace("%", "").replace("\\", "")
        cjk = not tok.isascii() and "-" not in tok and not any(ch.isdigit() for ch in tok)
        if cjk and len(tok) > 2:
            if len(tok) <= 6:
                add(tok)
            for size in (3, 2):
                if len(tok) < size:
                    continue
                for i in range(len(tok) - size + 1):
                    add(tok[i : i + size])
            continue
        add(tok)
    if len(out) <= 64:
        return out
    longer = [tok for tok in out if (not tok.isascii() and len(tok) >= 3) or len(tok) >= 6]
    return longer if len(longer) >= 8 else out[:64]


def _text_token_score(text: str, weights: dict[str, float]) -> float:
    blob = (text or "").lower()
    total = 0.0
    for tok, weight in weights.items():
        if token_in_text(tok, blob):
            total += weight
    return total


def _is_table_header_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|") or stripped.count("|") < 3:
        return False
    letters = 0
    digits = 0
    for ch in stripped:
        if ch.isdigit():
            digits += 1
        elif ch.isalpha() or "一" <= ch <= "鿿":
            letters += 1
    return letters >= 4 and digits <= 2


def _is_spaced_table_header_line(line: str, focus: list[str]) -> bool:
    """空格对齐表头：没有 ``|``，列名成串、几乎没有独立数字。认不出就只留列名。"""
    stripped = line.strip()
    if not stripped or "|" in stripped:
        return False
    letters = sum(1 for ch in stripped if ch.isalpha() or "一" <= ch <= "鿿")
    if letters < 12:
        return False
    # 命中 focus 词才认，且要求「字母远多于数字」——数据行正好相反。
    if letters < sum(1 for ch in stripped if ch.isdigit()) * 4:
        return False
    return _focus_hit(stripped, focus, loose=False)


def _clip_data_row(line: str, label_chars: int = 90, max_numbers: int = 10) -> str:
    """展平表格的数据行 → 「行标签 + 前几个数」，去掉补齐用的长空格。

    空格对齐展平出来的行有 350+ 字符，一半以上是补空格；整行带进摘录会把同文档
    的其它栏目和后续来源全挤掉。行标签与若干数字才是比较要用的。
    """
    parts = [p for p in re.split(r"\s{2,}", line.strip()) if p]
    if len(parts) <= 1:
        return line.strip()[:280]
    label = parts[0][:label_chars]
    numbers: list[str] = []
    for part in parts[1:]:
        if any(ch.isdigit() for ch in part):
            numbers.append(part)
        if len(numbers) >= max_numbers:
            break
    return " | ".join([label, *numbers])


def _cover_focus(weights: dict[str, float]) -> list[str]:
    """比较题多留几个栏目词。最高词若是 report 这类套话，后面的栏目词仍要留下。"""
    ranked = sorted(weights.items(), key=lambda item: -item[1])
    if not ranked:
        return []
    floor = max(0.2, ranked[0][1] * 0.05)
    out: list[str] = []
    for tok, weight in ranked:
        if weight < floor:
            break
        out.append(tok)
        if len(out) >= 12:
            break
    return out


_MONTH_WORD: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


_SPAN_RE = re.compile(
    r"(20\d{2})[/.年-](\d{1,2})(?:月)?"
    r".{0,48}?(?:\bto\b|\bthrough\b|至|到)"
    r".{0,16}?(20\d{2})[/.年-](\d{1,2})",
    re.IGNORECASE,
)


_HTML_CELL_RE = re.compile(r"<t([dh])([^>]*)>(.*?)</t\1>", re.IGNORECASE | re.DOTALL)


_SPAN_ATTR_RE = re.compile(r"""(rowspan|colspan)\s*=\s*["']?(\d+)""", re.IGNORECASE)


_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)


@dataclass
class _CoverLine:
    text: str
    section: str
    score: float
    protected: bool
    year: int
    month: int


def _query_month_span(query: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """问句里的起止年月。表从年份开头排时，要的月份在后部。"""
    match = _SPAN_RE.search(query or "")
    if match is None:
        return None
    y1, m1, y2, m2 = (int(match.group(i)) for i in range(1, 5))
    if not (1 <= m1 <= 12 and 1 <= m2 <= 12):
        return None
    start, end = (y1, m1), (y2, m2)
    if start > end:
        start, end = end, start
    return start, end


def _focus_hit(text: str, focus: list[str], *, loose: bool) -> bool:
    blob = (text or "").lower()
    for tok in focus:
        if token_in_text(tok, blob):
            return True
        if not loose or not tok.isascii() or len(tok) < 6:
            continue
        stem = tok.lower()
        for suf in ("ing", "ed", "es", "s"):
            if stem.endswith(suf) and len(stem) - len(suf) >= 4:
                stem = stem[: -len(suf)]
                break
        head = stem[:6]
        if len(head) >= 5 and re.search(rf"\b{re.escape(head)}", blob):
            return True
    return False


def _window_focus(line: str, focus: list[str], limit: int = 800) -> str:
    if len(line) <= limit:
        return line
    low = line.lower()
    idx = -1
    for tok in focus:
        found = low.find(tok.lower())
        if found >= 0:
            idx = found
            break
    if idx < 0:
        return line[:limit]
    start = max(0, idx - limit // 5)
    return line[start : start + limit]


def _html_grid(table_html: str) -> list[list[str]]:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL)
    grid: list[list[str]] = []
    carry: dict[tuple[int, int], str] = {}
    for r_i, row_html in enumerate(rows):
        cells = list(_HTML_CELL_RE.finditer(row_html))
        row: list[str] = []
        col = 0
        seen = 0
        while seen < len(cells) or (r_i, col) in carry:
            if (r_i, col) in carry:
                row.append(carry.pop((r_i, col)))
                col += 1
                continue
            if seen >= len(cells):
                break
            matched = cells[seen]
            seen += 1
            text = re.sub(r"<[^>]+>", " ", matched.group(3))
            text = re.sub(r"\s+", " ", text).strip()
            colspan = 1
            rowspan = 1
            for attr in _SPAN_ATTR_RE.finditer(matched.group(2)):
                span_n = int(attr.group(2))
                if attr.group(1).lower() == "colspan":
                    colspan = max(1, span_n)
                else:
                    rowspan = max(1, span_n)
            for _off in range(colspan):
                while (r_i, col) in carry:
                    row.append(carry.pop((r_i, col)))
                    col += 1
                row.append(text)
                if rowspan > 1:
                    for rr in range(1, rowspan):
                        carry[(r_i + rr, col)] = text
                col += 1
        if any(cell for cell in row):
            grid.append(row)
    return grid


def _header_labels(grid: list[list[str]]) -> tuple[list[str], int]:
    n_header = 0
    for row in grid[:3]:
        letters = 0
        digits = 0
        for cell in row:
            for ch in cell:
                if ch.isdigit():
                    digits += 1
                elif ch.isalpha() or "\u4e00" <= ch <= "\u9fff":
                    letters += 1
        if letters >= 4 and digits <= 2:
            n_header += 1
            continue
        break
    if n_header == 0:
        return [], 0
    width = max(len(row) for row in grid[:n_header])
    labels: list[str] = []
    for col in range(width):
        parts: list[str] = []
        for row in grid[:n_header]:
            cell = row[col].strip() if col < len(row) else ""
            if cell and (not parts or parts[-1] != cell):
                parts.append(cell)
        labels.append(" ".join(parts))
    return labels, n_header


def _flatten_html_tables(text: str) -> str:
    """把 HTML 表摊成「表题 + 列名 + 数据行」，避免整表从开头被截掉。"""
    if "<table" not in (text or "").lower():
        return text
    parts: list[str] = []
    cursor = 0
    for match in _TABLE_RE.finditer(text):
        before = text[cursor : match.start()]
        plain = re.sub(r"<[^>]+>", " ", before)
        plain = re.sub(r"[ \t]+", " ", plain).strip()
        if plain:
            parts.append(plain)
        grid = _html_grid(match.group(0))
        labels, n_header = _header_labels(grid)
        if labels:
            parts.append(" | ".join(labels))
        for row in grid[n_header:]:
            width = max(len(labels), len(row))
            cells = [(row[i] if i < len(row) else "") for i in range(width)]
            line = " | ".join(cells).strip()
            if line.strip("| "):
                parts.append(line)
        cursor = match.end()
    tail = re.sub(r"<[^>]+>", " ", text[cursor:])
    tail = re.sub(r"[ \t]+", " ", tail).strip()
    if tail:
        parts.append(tail)
    return "\n".join(parts)


def _split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.split("|")]


def _year_marker(cells: list[str]) -> int | None:
    if not cells:
        return None
    matched = re.fullmatch(r"20\d{2}", cells[0].strip())
    if matched is None:
        return None
    rest = "".join(cells[1:])
    if _HAS_DIGIT_RE.search(rest):
        return None
    return int(matched.group(0))


def _month_of_cell(cell: str) -> int | None:
    word = re.match(r"[A-Za-z]+", cell.strip())
    if word is not None:
        return _MONTH_WORD[word.group(0).lower()] if word.group(0).lower() in _MONTH_WORD else None
    num = re.match(r"(\d{1,2})\s*月", cell.strip())
    if num is None:
        return None
    month = int(num.group(1))
    if 1 <= month <= 12:
        return month
    return None


def _section_matches(line: str, focus: list[str]) -> bool:
    """用较长的问句词判断这张表，避免 total 把每张表都打开。

    必须精确命中：loose 会把 universities 对上 University，题头几乎全亮。
    """
    long = [tok for tok in focus if len(tok) >= 6]
    return _focus_hit(line, long or focus, loose=False)


def _line_has_digit(line: str) -> bool:
    return any(ch.isdigit() for ch in line)


def _select_cover_lines(
    text: str,
    focus: list[str],
    weights: dict[str, float],
    span: tuple[tuple[int, int], tuple[int, int]] | None,
    *,
    section_focus: list[str] | None = None,
) -> list[_CoverLine]:
    """留下栏目行；问句有月份区间时，连同该区间的表行，不从第一行往下切。

    ``section_focus`` 是去掉「来源标题里普遍出现的词」后的栏目词。标题词
    （University / 公司简称）会出现在每份材料的题头，拿去开 section 会让
    ``section_on`` 整篇常亮，表头连带数据行把无关表塞满预算。
    """
    open_focus = section_focus if section_focus is not None else focus
    # 逐行命中仍用完整 focus；但若行只命中标题词、没命中栏目词，则丢掉。
    content_focus = open_focus if open_focus else focus
    content_set = set(content_focus)
    flat = _flatten_html_tables(text)
    raw_lines = [line.strip() for line in flat.splitlines() if line.strip()]
    chosen: list[_CoverLine] = []
    # 每个栏目词保留 top-k 行，避免只剩一条表头说明而丢掉同栏目的数值行。
    best_hit: dict[str, list[tuple[float, str, str]]] = {}
    section = ""
    section_on = False
    section_score = 0.0
    year = 0
    months_kept = 0
    header_kept = False
    header_rows_left = 0
    for line in raw_lines:
        if "【文档" in line:
            chosen.append(_CoverLine(line, "", 1.0, True, 0, 0))
            continue
        cells = _split_cells(line)
        marked = _year_marker(cells)
        if marked is not None:
            year = marked
            continue
        month = _month_of_cell(cells[0]) if cells else None
        heading = "|" not in line and sum(ch.isdigit() for ch in line) <= 2 and 8 <= len(line) <= 280
        table_label = bool(re.search(r"\btable\s+\d", line, re.IGNORECASE))
        if heading or table_label:
            section = line[:120]
            # 长问句/问卷题干不当栏目标题，否则会连带灌进无关问答。
            title_like = table_label or (heading and len(line) <= 100 and not line.rstrip().endswith("?"))
            section_on = bool(title_like and _section_matches(line, open_focus))
            section_score = _text_token_score(line, weights)
            header_kept = False
            if section_on and table_label:
                chosen.append(_CoverLine(line[:180], section, section_score, False, 0, 0))
            months_kept = 0
            continue
        # 月份行有自己的保留路径（按问句的月份区间筛），必须先于「表头连带数据行」
        # 判定——否则带月份的行会被当成普通数据行提前吃掉，月份区间就失效了。
        in_span = month is not None and year > 0 and span is not None and span[0] <= (year, month) <= span[1]
        if section_on and in_span and month is not None and months_kept < 36:
            chosen.append(_CoverLine(line[:500], section, section_score + 0.5, False, year, month))
            months_kept += 1
            continue
        headerish = "|" in line and month is None and sum(ch.isdigit() for ch in line) <= 2
        spaced_header = _is_spaced_table_header_line(line, open_focus)
        if section_on and not header_kept and (_is_table_header_line(line) or headerish or spaced_header):
            chosen.append(_CoverLine(line[:400], section, section_score, True, 0, 0))
            header_kept = True
            header_rows_left = _DATA_ROWS_AFTER_HEADER
            continue
        # 表头命中后连带数字行；空格对齐表没有 `|`，不能用管道符当数据行判据。
        if header_kept and header_rows_left > 0 and any(ch.isdigit() for ch in line):
            # 问句给了月份区间时，表头连带不能把区间外月份带进来。
            if month is not None and span is not None and not in_span:
                header_rows_left -= 1
                if header_rows_left == 0:
                    header_kept = False
                continue
            # 展平数据行很长，只留「行标签 + 前几个数」以免吃光预算。
            chosen.append(
                _CoverLine(
                    _clip_data_row(line) if month is None else line[:500],
                    section,
                    section_score + 0.25,
                    False,
                    year,
                    month or 0,
                )
            )
            header_rows_left -= 1
            if header_rows_left == 0:
                header_kept = False
            continue
        # 宽表行是栏目+数值所在；命中 focus 就截窗留下，不再按长度否决。
        if not _focus_hit(line, focus, loose=False):
            continue
        # 只命中标题词（校名/公司名）的行不是栏目行。
        if content_focus is not focus and not _focus_hit(line, content_focus, loose=False):
            continue
        digit = _line_has_digit(line)
        # 无数字的长说明/问句不进 best_hit。
        if not digit and (line.rstrip().endswith("?") or len(line) > 160):
            continue
        # 申请说明长句（单空格散文）丢掉；宽表是多空格对齐或带 |。
        if len(line) > 200 and line.count("|") < 2 and "  " not in line and line.count(" ") > 20:
            continue
        # 无数字的超长粘连行丢掉；带数字的空格对齐宽表行要留。
        if len(line) > 800 and not digit:
            continue
        score = _text_token_score(line, weights)
        # 表格式数值行加分，压过只点到一个栏目词的申请说明长句。
        if "|" in line and digit:
            score += max(0.35, 0.08 * len(content_set))
        low = line.lower()
        for tok in content_focus:
            if not token_in_text(tok, low):
                continue
            bucket = best_hit[tok] if tok in best_hit else []
            # 同分时优先带数字、更短的行。
            entry = (score, line, section)
            bucket.append(entry)
            bucket.sort(
                key=lambda item: (
                    -item[0],
                    0 if _line_has_digit(item[1]) else 1,
                    len(item[1]),
                )
            )
            # 去重：同一行只留一次。
            dedup: list[tuple[float, str, str]] = []
            seen_lines: set[str] = set()
            for item in bucket:
                key = item[1]
                if key in seen_lines:
                    continue
                seen_lines.add(key)
                dedup.append(item)
                if len(dedup) >= _COVER_HITS_PER_TOKEN:
                    break
            best_hit[tok] = dedup
    seen_hit: set[str] = set()
    for tok, entries in best_hit.items():
        for score, line, sec in entries:
            # 按该栏目词截窗，避免 focus 列表里靠前的标题词把窗口拉到校名说明段。
            clipped = _window_focus(line, [tok, *content_focus])
            if clipped in seen_hit:
                continue
            seen_hit.add(clipped)
            chosen.append(_CoverLine(clipped, sec, score + 1.0, False, 0, 0))
    if not any(line.score > 1.0 or line.month for line in chosen):
        return []
    # 每份至少保住权重最高的那一行，缩预算时不能把它删掉。
    best_i = -1
    best_s = -1.0
    for i, line in enumerate(chosen):
        if line.protected and "【文档" in line.text:
            continue
        if line.score >= best_s:
            best_s = line.score
            best_i = i
    if best_i >= 0:
        kept = chosen[best_i]
        chosen[best_i] = _CoverLine(kept.text, kept.section, kept.score, True, kept.year, kept.month)
    return chosen


def _fit_cover_lines(groups: list[list[_CoverLine]], budget: int) -> list[list[_CoverLine]]:
    def total() -> int:
        return sum(len("\n".join(line.text for line in group)) for group in groups)

    def per_group_cap() -> int:
        """每份材料的行数上限：先按份数均分预算，再留出余量给超长行。"""
        n = max(1, len(groups))
        return max(8, budget // n // 80)

    # 表头连带数据行后单份能出很多行；按份数均分行数上限，避免总量冲破预算。
    cap = per_group_cap()
    for group in groups:
        if len(group) > cap:
            # 保留文档头行 + 分数最高的若干行，其余从低到高丢。
            keep_idx = {i for i, line in enumerate(group) if "【文档" in line.text}
            rest = [(i, line) for i, line in enumerate(group) if i not in keep_idx]
            rest.sort(key=lambda item: -item[1].score)
            keep_idx.update(i for i, _ in rest[: max(3, cap - len(keep_idx))])
            group[:] = [line for i, line in enumerate(group) if i in keep_idx]

    guard = 0
    # 上限按「还要删的行数」给，别按固定 8000：guard 提前耗尽会放任超预算内容注入。
    limit = sum(len(group) for group in groups) + 16
    while total() > budget and guard < limit:
        guard += 1
        drop_g = -1
        drop_i = -1
        # 先删非月份、且不带数字的多余行，每份至少留一行，避免预算把后面的来源整份丢掉。
        drop_key = (2, 2, 10**9.0)
        for g_i, group in enumerate(groups):
            idxs = [i for i, line in enumerate(group) if "【文档" not in line.text]
            if len(idxs) <= 1:
                continue
            for i in idxs:
                line = group[i]
                if line.protected:
                    continue
                digit = 1 if _line_has_digit(line.text) else 0
                kind = 0 if line.month <= 0 else 1
                key = (digit, kind, line.score)
                if drop_g < 0 or key < drop_key:
                    drop_key = key
                    drop_g = g_i
                    drop_i = i
        if drop_g < 0:
            break
        del groups[drop_g][drop_i]
    return [group for group in groups if any("【文档" in line.text or line.score > 0 for line in group)]


def merge_coverage_with_metric_hits(
    covered: list[Episode],
    pool: list[Episode],
    *,
    max_metric_docs: int = 48,
    max_chat: int = 8,
) -> list[Episode]:
    """Cover 置顶；再保留池里带数字的文档命中与少量闲聊。

    栏目摘行会漏叙述句里的数字，S1 向量命中正好补上。
    """
    cover_ids = {str(ep["id"]) for ep in covered if "id" in ep}
    metric_docs: list[Episode] = []
    chat: list[Episode] = []
    seen_metric: set[str] = set()
    for ep in pool:
        eid = str(ep["id"]) if "id" in ep else ""
        if eid and eid in cover_ids:
            continue
        raw = ep["content"] or ""
        if is_ingested_document_episode(raw):
            if len(metric_docs) >= max_metric_docs:
                continue
            if not any(ch.isdigit() for ch in raw):
                continue
            sig = eid or raw[:96]
            if sig in seen_metric:
                continue
            seen_metric.add(sig)
            metric_docs.append(ep)
        elif len(chat) < max_chat:
            chat.append(ep)
    return list(covered) + metric_docs + chat


def cover_document_excerpts(
    episodes: list[Episode],
    query: str,
    *,
    char_budget: int = DOC_COVER_EXTRACT_BUDGET,
) -> list[Episode]:
    """每份入库文档只留下和问句重叠的段落，预算按来源分。

    拼全文再从开头截，会只剩封面，问到的表在后面。
    """
    groups: dict[str, list[Episode]] = {}
    order: list[str] = []
    for ep in episodes:
        raw = ep["content"] or ""
        if not is_ingested_document_episode(raw):
            continue
        key = episode_source_key(raw)
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ep)
    if len(order) < 2:
        return []
    tokens = _coverage_tokens(query)
    if not tokens:
        return []
    for key in order:
        groups[key].sort(key=lambda e: str(e["valid_at"] if "valid_at" in e else ""))
    # 多份材料都出现、但每份只出现少数次的词，才像「同一栏目」。
    doc_df = {tok: 0 for tok in tokens}
    chunk_df = {tok: 0 for tok in tokens}
    for key in order:
        seen_in_doc: set[str] = set()
        for ep in groups[key]:
            blob = (ep["content"] or "").lower()
            for tok in tokens:
                if not token_in_text(tok, blob):
                    continue
                chunk_df[tok] += 1
                seen_in_doc.add(tok)
        for tok in seen_in_doc:
            doc_df[tok] += 1
    weights: dict[str, float] = {}
    for tok in tokens:
        docs_n = doc_df[tok]
        chunks_n = chunk_df[tok]
        if docs_n < 2 or chunks_n <= 0:
            continue
        weights[tok] = (docs_n * docs_n) / chunks_n
    if len(weights) > 24:
        weights = dict(sorted(weights.items(), key=lambda item: -item[1])[:24])
    if not weights:
        return []
    focus = _cover_focus(weights)
    if not focus:
        return []
    # 来源标题里普遍出现的词打不开栏目，否则 section_on 整篇常亮。
    title_df = {tok: 0 for tok in focus}
    for key in order:
        low = key.lower()
        for tok in focus:
            if token_in_text(tok, low) or _focus_hit(low, [tok], loose=True):
                title_df[tok] += 1
    title_floor = max(2, (len(order) + 1) // 2)
    section_focus = [tok for tok in focus if title_df[tok] < title_floor]
    if not section_focus:
        section_focus = list(focus)
    budget = char_budget if char_budget > 0 else DOC_COVER_EXTRACT_BUDGET
    span = _query_month_span(query)
    built: list[tuple[str, str, list[_CoverLine]]] = []
    for key in order:
        chunks = groups[key]
        parts: list[str] = []
        for ep in chunks:
            raw = (ep["content"] or "").strip()
            marker = _DOC_SOURCE_RE.search(raw)
            body = raw[marker.end() :].lstrip() if marker else raw
            if body:
                parts.append(body)
        joined = "".join(parts) if any("<tr" in part.lower() for part in parts) else "\n".join(parts)
        if "【文档" not in joined:
            joined = f"【文档】{key}\n{joined}"
        lines = _select_cover_lines(joined, focus, weights, span, section_focus=section_focus)
        if not lines:
            continue
        anchor = chunks[0]
        valid_at = str(anchor["valid_at"] if "valid_at" in anchor else "")
        built.append((key, valid_at, lines))
    if len(built) < 2:
        return []
    _fit_cover_lines([lines for _key, _va, lines in built], budget)
    out: list[Episode] = []
    for key, valid_at, lines in built:
        if not lines:
            continue
        anchor = groups[key][0]
        text = "\n".join(line.text for line in lines).strip()
        if not text:
            continue
        if "【文档" not in text:
            text = f"【文档】{key}\n{text}"
        out.append(
            Episode(
                id=anchor["id"],
                content=text,
                valid_at=valid_at,
                scope_key=anchor["scope_key"] if "scope_key" in anchor else "",
                embedding=[],
            )
        )
    if len(out) < 2:
        return []
    out.insert(0, _source_manifest_episode(out))
    return out


def _source_manifest_episode(built: list[Episode]) -> Episode:
    """覆盖块的来源清单：共 N 份、来源名，并要求每一份都纳入。"""
    titles: list[str] = []
    for ep in built:
        m = _DOC_SOURCE_RE.search(ep["content"] or "")
        title = m.group(1).strip() if m else ""
        if title and title not in titles:
            titles.append(title)
    n = len(titles)
    head = (
        f"（下面按来源交错排列。不同文件或标题不是同一条记录的更新，不要只留最后一份。"
        f"每段是该来源里和问句重叠的摘录，不是文件开头。共{n}份，每一份都要纳入，"
        f"不要只拿其中几份下结论。来源：{'、'.join(titles)}）"
    )
    anchor = built[0]
    return Episode(
        id=str(anchor["id"]),
        content=head,
        valid_at=str(anchor["valid_at"]) if "valid_at" in anchor else "",
        scope_key=str(anchor["scope_key"]) if "scope_key" in anchor else "",
        embedding=[],
    )
