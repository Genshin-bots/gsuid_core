"""长文本按句切块。表格在空行处换表头，避免下一张表贴上上一张的列名。"""

import re

# bge-small 约 512 token；900 字一块，句子边界不会把嵌入切碎。
INGEST_CHUNK_CHARS = 900

_SENT_SPLIT_RE = re.compile(r"(?<=[。.!?！？\n])\s+")


def _is_md_table_row(piece: str) -> bool:
    stripped = piece.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _is_html_table_row(piece: str) -> bool:
    low = piece.lower()
    return "<tr" in low or "</tr>" in low


def _table_header_prefix(header: str) -> str:
    head = header.strip()
    return f"{head}\n" if head else ""


def chunk_text(text: str, target: int = INGEST_CHUNK_CHARS) -> list[str]:
    """按句子边界打包成不超过 target 的块；表格行带上当前表的列名。"""
    text = text.strip()
    if len(text) <= target:
        return [text] if text else []

    pieces: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            # 空行是两张表的分界，不能把上一张的列名带到下一张。
            if pieces and pieces[-1] != "":
                pieces.append("")
            continue
        if _is_md_table_row(stripped) or _is_html_table_row(stripped):
            pieces.append(stripped)
            continue
        parts = [p.strip() for p in _SENT_SPLIT_RE.split(stripped) if p.strip()]
        pieces.extend(parts or [stripped])

    chunks: list[str] = []
    cur = ""
    md_header = ""
    html_header = ""
    for piece in pieces:
        if piece == "":
            md_header = ""
            html_header = ""
            if cur:
                chunks.append(cur)
                cur = ""
            continue
        if _is_md_table_row(piece):
            compact = piece.replace(" ", "")
            if compact and set(compact) <= set("|:-"):
                continue
            if not md_header:
                md_header = piece
        elif not _is_html_table_row(piece):
            md_header = ""
            html_header = ""
        if "<tr" in piece.lower() and "<th" in piece.lower():
            html_header = piece
        if cur and len(cur) + 1 + len(piece) > target:
            chunks.append(cur)
            cur = ""
        prefix = ""
        if not cur:
            if _is_md_table_row(piece) and md_header and piece != md_header:
                prefix = _table_header_prefix(md_header)
            elif _is_html_table_row(piece) and html_header and piece != html_header:
                prefix = _table_header_prefix(html_header)
        payload = f"{prefix}{piece}" if prefix else piece
        if len(payload) > target:
            for i in range(0, len(payload), target):
                chunks.append(payload[i : i + target])
            continue
        joiner = "\n" if (_is_md_table_row(piece) or _is_html_table_row(piece)) else " "
        cur = f"{cur}{joiner}{payload}" if cur else payload
    if cur:
        chunks.append(cur)
    return chunks
