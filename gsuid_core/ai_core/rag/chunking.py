"""送进嵌入之前的统一切分。

嵌入接口一串文本对一个向量，超限的正文必须在调用嵌入之前切开。
切法编号和模型最大 token 一起折进哈希。模型上限变了会重切。
还没盖章、新切法仍然是一片的旧点不重嵌。
"""

from __future__ import annotations

import re
from typing import Sequence
from dataclasses import dataclass

# 未知模型按 512 token 留 32 个特殊 token，得到 480 字。已知模型用它自己的上限。
CHUNKER_ID = "embed-v1"
_FALLBACK_MAX_TOKENS = 512
_TOKEN_MARGIN = 32
EMBED_CHAR_BUDGET = _FALLBACK_MAX_TOKENS - _TOKEN_MARGIN
DEFAULT_CHUNK_SIZE = EMBED_CHAR_BUDGET
DEFAULT_CHUNK_OVERLAP = 60
MIN_CHUNK_SIZE = 50
MAX_CHUNK_SIZE = EMBED_CHAR_BUDGET

_HEADING_MARK = re.compile(r"^#{1,6} ")
# 句末才断。英文句号只在后面是空白时断开，避免把 3.14 切成两截。
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])|(?<=[.?!])(?=\s)")


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def current_max_input_tokens() -> int:
    """当前嵌入模型声明的最大输入 token。模型还没加载时按 512。"""
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        return _FALLBACK_MAX_TOKENS
    tokens = embedding_provider.max_input_tokens
    if isinstance(tokens, int) and 8 <= tokens <= 32768:
        return tokens
    return _FALLBACK_MAX_TOKENS


def embed_char_budget(max_tokens: int | None = None) -> int:
    """把 token 上限换成字符预算。中文大约一字一 token，并留出特殊 token。"""
    tokens = max_tokens if max_tokens is not None and max_tokens > 0 else current_max_input_tokens()
    if tokens > 32768:
        tokens = 32768
    room = tokens - _TOKEN_MARGIN
    if room < MIN_CHUNK_SIZE:
        return MIN_CHUNK_SIZE
    return room


def chunker_stamp() -> str:
    """写入 SQL 的切法标记。模型 token 上限变了，标记也变。"""
    return f"{CHUNKER_ID}@{current_max_input_tokens()}"


def clamp_chunk_size(value: int, max_tokens: int | None = None) -> int:
    """调用方可以要更小的片，但不能超过当前模型的预算。"""
    return _clamp(int(value), MIN_CHUNK_SIZE, embed_char_budget(max_tokens))


@dataclass(frozen=True)
class EmbedPiece:
    """正文片，以及真正送进嵌入模型的整串（含标题、标签前缀）。"""

    body: str
    embed_text: str


def embed_prefix(title: str, tags: Sequence[str]) -> str:
    parts: list[str] = []
    if title:
        parts.append(f"标题：{title}")
    if tags:
        parts.append(f"标签：{' '.join(tags)}")
    return "\n".join(parts)


def shrink_prefix(prefix: str, budget: int) -> str:
    """前缀把预算吃光时缩短，给正文留位置。"""
    if not prefix:
        return ""
    min_body = min(MIN_CHUNK_SIZE, max(budget // 2, 1))
    if len(prefix) + 1 + min_body <= budget:
        return prefix
    cap = min(max(budget // 4, 1), max(budget - 1, 0))
    return prefix[:cap]


def chunk_method_hash(content_hash: str, max_tokens: int | None = None) -> str:
    from gsuid_core.ai_core.rag.base import calculate_hash

    tokens = max_tokens if max_tokens is not None and max_tokens > 0 else current_max_input_tokens()
    return calculate_hash({"_chunker": CHUNKER_ID, "_max_tokens": tokens, "_content": content_hash})


def should_skip_rebuild(
    stored_hash: str,
    stored_count: int,
    content_hash: str,
    piece_count: int,
) -> bool:
    """已按当前切法入库，或旧的一片文档新切法仍然是一片。"""
    if piece_count < 1 or stored_count < 1 or not stored_hash:
        return False
    if stored_hash == chunk_method_hash(content_hash) and stored_count == piece_count:
        return True
    return piece_count == 1 and stored_count == 1 and stored_hash == content_hash


def logical_chunk_ids(parent_id: str, count: int) -> list[str]:
    """一片沿用原文 id，多片才加序号，避免短文换 id。"""
    if count <= 1:
        return [parent_id]
    return [f"{parent_id}#{idx}" for idx in range(count)]


def stored_hash_uniform(hashes: Sequence[str]) -> str:
    if not hashes:
        return ""
    first = hashes[0]
    for item in hashes:
        if item != first:
            return ""
    return first


def keep_ids_after_rebuild(
    kept: set[str],
    *,
    old_ids: Sequence[str],
    new_ids: Sequence[str],
    vectors: Sequence[Sequence[float] | None],
) -> bool:
    """新片全部嵌入成功才收下新 id；否则保住旧点，下次同步再试。"""
    ready = bool(vectors) and all(vec is not None for vec in vectors)
    if ready:
        kept.update(new_ids)
        return True
    kept.update(old_ids)
    return False


def _hard_window(text: str, room: int, overlap: int) -> list[str]:
    """只在无法按结构断开时定长重叠。重叠只发生在这个窗口里。"""
    room = max(room, 1)
    step = max(room - max(overlap, 0), 1)
    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        pieces.append(text[start : start + room])
        if start + room >= length:
            break
        start += step
    return [piece for piece in pieces if piece]


def _sentences(text: str) -> list[str]:
    parts = [part for part in _SENT_SPLIT_RE.split(text) if part]
    merged: list[str] = []
    for part in parts:
        if not part.strip():
            if merged:
                merged[-1] = merged[-1] + part
            continue
        merged.append(part)
    return [part.strip() for part in merged if part.strip()]


def _join_fit(left: str, right: str, room: int) -> str | None:
    if left.endswith((" ", "\n")) or right.startswith((" ", "\n")):
        joined = f"{left}{right}"
    elif left.endswith((".", "?", "!")):
        joined = f"{left} {right}"
    else:
        joined = f"{left}{right}"
    if len(joined) <= room:
        return joined
    return None


def _atoms(text: str) -> list[str]:
    """代码围栏整段保留；围栏外按空行分成段落。"""
    lines = text.splitlines()
    atoms: list[str] = []
    buf: list[str] = []
    in_fence = False

    def flush_prose() -> None:
        raw = "\n".join(buf).strip()
        buf.clear()
        if not raw:
            return
        for para in re.split(r"\n\s*\n", raw):
            piece = para.strip()
            if piece:
                atoms.append(piece)

    for line in lines:
        if line.lstrip().startswith("```"):
            if not in_fence:
                flush_prose()
                buf.append(line)
                in_fence = True
            else:
                buf.append(line)
                fence = "\n".join(buf).strip()
                if fence:
                    atoms.append(fence)
                buf.clear()
                in_fence = False
            continue
        buf.append(line)
    if in_fence:
        fence = "\n".join(buf).strip()
        if fence:
            atoms.append(fence)
    else:
        flush_prose()
    return atoms


def _split_atom(atom: str, room: int, overlap: int) -> list[str]:
    if atom.lstrip().startswith("```"):
        return _hard_window(atom, room, overlap)
    pieces: list[str] = []
    buf = ""
    for sent in _sentences(atom):
        if len(sent) > room:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.extend(_hard_window(sent, room, overlap))
            continue
        if not buf:
            buf = sent
            continue
        joined = _join_fit(buf, sent, room)
        if joined is None:
            pieces.append(buf)
            buf = sent
        else:
            buf = joined
    if buf:
        pieces.append(buf)
    return pieces


def _pack_prose(text: str, room: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= room:
        return [text]
    pieces: list[str] = []
    buf = ""
    for atom in _atoms(text):
        if len(atom) > room:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.extend(_split_atom(atom, room, overlap))
            continue
        newline_joined = f"{buf}\n{atom}"
        if len(newline_joined) <= room:
            buf = newline_joined
            continue
        pieces.append(buf)
        buf = atom
    if buf:
        pieces.append(buf)
    return pieces


def _is_heading_line(stripped: str) -> bool:
    return _HEADING_MARK.match(stripped) is not None


def _heading_sections(text: str) -> list[tuple[str, str]]:
    """围栏外的 Markdown 标题各成一节。标题行本身不进正文。"""
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_fence = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            sections[-1][1].append(line)
            continue
        if not in_fence and _is_heading_line(stripped):
            heading = stripped
            current_heading, current_lines = sections[-1]
            if current_heading or any(item.strip() for item in current_lines):
                sections.append((heading, []))
            else:
                sections[-1] = (heading, [])
            continue
        sections[-1][1].append(line)
    out: list[tuple[str, str]] = []
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if heading or body:
            out.append((heading, body))
    return out


def chunk_body(text: str, *, room: int, overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """把正文切到每片不超过 ``room``。标题会抄到后续片上。"""
    text = text.strip()
    if not text:
        return []
    room = max(int(room), 1)
    overlap = 0 if room <= 1 else _clamp(int(overlap), 0, room // 2)
    if len(text) <= room:
        return [text]
    pieces: list[str] = []
    for heading, body in _heading_sections(text):
        carry = ""
        if heading and len(heading) <= max(room // 2, 1) and len(heading) + 1 < room:
            carry = heading
        if carry:
            inner = room - len(carry) - 1
            packed = _pack_prose(body, inner, overlap) if body else []
            if not packed:
                pieces.append(carry)
                continue
            for part in packed:
                piece = f"{carry}\n{part}" if part else carry
                if len(piece) <= room:
                    pieces.append(piece)
                else:
                    pieces.extend(_hard_window(piece, room, overlap))
            continue
        whole = f"{heading}\n{body}".strip() if heading else body
        pieces.extend(_pack_prose(whole, room, overlap))
    out: list[str] = []
    for piece in pieces:
        cleaned = piece.strip()
        if not cleaned:
            continue
        if len(cleaned) <= room:
            out.append(cleaned)
        else:
            out.extend(_hard_window(cleaned, room, overlap))
    return out


def pieces_for_embed(
    body: str,
    *,
    title: str = "",
    tags: Sequence[str] = (),
    budget: int | None = None,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    max_tokens: int | None = None,
) -> list[EmbedPiece]:
    """返回送进嵌入的片。每片整串长度不超过当前模型预算。"""
    cap = embed_char_budget(max_tokens)
    budget = cap if budget is None else _clamp(int(budget), MIN_CHUNK_SIZE, cap)
    overlap = _clamp(int(overlap), 0, budget // 2)
    prefix = shrink_prefix(embed_prefix(title, tags), budget)
    room = budget - len(prefix) - (1 if prefix else 0)
    if room < 1:
        room = 1
    bodies = chunk_body(body, room=room, overlap=overlap)
    if not bodies and prefix:
        return [EmbedPiece(body="", embed_text=prefix[:budget])]
    pieces: list[EmbedPiece] = []
    for part in bodies:
        embed = f"{prefix}\n{part}" if prefix else part
        if len(embed) > budget:
            embed = embed[:budget]
        pieces.append(EmbedPiece(body=part, embed_text=embed))
    return pieces


def document_bodies(
    *,
    full_text: str,
    sections: Sequence[str],
    title: str,
    tags: Sequence[str],
    budget: int,
    overlap: int,
    max_tokens: int | None = None,
) -> list[str]:
    """整篇和调用方预先切开的小节走同一把刀，再摊平。"""
    usable = [section.strip() for section in sections if section.strip()]
    if usable:
        bodies: list[str] = []
        for section in usable:
            bodies.extend(
                piece.body
                for piece in pieces_for_embed(
                    section,
                    title=title,
                    tags=tags,
                    budget=budget,
                    overlap=overlap,
                    max_tokens=max_tokens,
                )
                if piece.body
            )
        return bodies
    return [
        piece.body
        for piece in pieces_for_embed(
            full_text,
            title=title,
            tags=tags,
            budget=budget,
            overlap=overlap,
            max_tokens=max_tokens,
        )
        if piece.body
    ]


def split_text(
    text: str,
    max_chars: int | None = None,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    max_tokens: int | None = None,
) -> list[str]:
    """把长文切成不超过当前模型预算的片。``max_chars`` 更大时会被夹回预算。"""
    cap = embed_char_budget(max_tokens)
    chosen = cap if max_chars is None else max_chars
    return document_bodies(
        full_text=text,
        sections=(),
        title="",
        tags=(),
        budget=chosen,
        overlap=overlap,
        max_tokens=max_tokens,
    )
