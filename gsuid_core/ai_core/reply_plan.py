"""回复计划解析：模型输出里的 ``<reply #seq>`` 分组协议。

标签行单独一行 ``<reply #316>`` 或 ``<reply #316 #318>``（也接受逗号分隔）。
标签行之后到下一个标签行之间是该组正文；首段无标签用 ``default_seqs``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gsuid_core.ai_core.utils import is_silence_marker, remainder_after_protocol_tags
from gsuid_core.ai_core.angle_bracket_guard import _REPLY_TAG_RE

# 标签行：行首 <reply ...>，可独占一行或后接同组首句正文
_REPLY_LINE_RE = re.compile(
    r"^\s*<reply(?:\s+((?:#\s*\d+\s*[,，]?\s*)+))?\s*/?\s*>(?:\s+(.*))?$",
    re.IGNORECASE,
)
_REPLY_REF_RE = re.compile(r"#\s*(\d+)")


@dataclass(frozen=True, slots=True)
class ReplyGroup:
    seqs: tuple[int, ...]
    text: str


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    groups: tuple[ReplyGroup, ...]
    invalid_refs: tuple[int, ...]
    silence: bool


def _dedup(seqs: list[int]) -> tuple[int, ...]:
    out: list[int] = []
    seen: set[int] = set()
    for seq in seqs:
        if seq not in seen:
            seen.add(seq)
            out.append(seq)
    return tuple(out)


def parse_reply_plan(
    text: str,
    *,
    valid_seqs: frozenset[int],
    default_seqs: tuple[int, ...],
    max_targets: int,
) -> ReplyPlan:
    """把模型输出切成 (目标 seqs, 正文) 组；非法 #seq 记入 invalid_refs。"""
    chunks: list[tuple[tuple[int, ...] | None, list[str]]] = []
    cur_tag: tuple[int, ...] | None = None
    cur_lines: list[str] = []
    invalid: list[int] = []

    def flush() -> None:
        chunks.append((cur_tag, cur_lines))

    for line in text.splitlines():
        m = _REPLY_LINE_RE.match(line)
        if m is None:
            cur_lines.append(line)
            continue
        flush()
        refs = [int(x) for x in _REPLY_REF_RE.findall(m.group(1) or "")]
        cur_tag = _dedup(refs)
        tail = (m.group(2) or "").strip()
        cur_lines = [tail] if tail else []
    flush()

    body_no_tags = _REPLY_TAG_RE.sub("", text)
    silence = bool(body_no_tags.strip()) and is_silence_marker(body_no_tags.strip())

    groups: list[ReplyGroup] = []
    # max_targets 是整轮预算（一轮最多指向几名待回应用户），不是每组上限。
    remaining = max_targets
    for tag, lines in chunks:
        if tag is not None:
            keep = [s for s in tag if s in valid_seqs]
            for s in tag:
                if s not in valid_seqs:
                    invalid.append(s)
            seqs = tuple(keep) if keep else default_seqs
        else:
            seqs = default_seqs
        body = remainder_after_protocol_tags("\n".join(lines))
        body = _REPLY_TAG_RE.sub("", body).strip()
        if not body or is_silence_marker(body):
            continue
        if remaining <= 0:
            seqs = ()
        else:
            seqs = seqs[:remaining]
            remaining -= len(seqs)
        groups.append(ReplyGroup(seqs=seqs, text=body))

    if silence:
        groups = []
    return ReplyPlan(groups=tuple(groups), invalid_refs=_dedup(invalid), silence=silence)
