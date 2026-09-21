"""EO gold 短语抽取与 turn 对齐（零 LLM）。主路径是 source_chat_ids。"""

from __future__ import annotations

import os
import re
import json

_NUM_RE = re.compile(r"^\s*(?:\d+[\.\)]|[-*])\s*")
_MENTION_RE = re.compile(r"(?i)^(?:llm response should mention|the response should mention|should mention)\s*:\s*")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']{3,}")
_CHAT_ID_RE = re.compile(r"chat_id\s+(\d+(?:\s*,\s*\d+)*)", re.IGNORECASE)


def gold_phrases(standard_answer: str, rubric: list[str]) -> list[str]:
    """从 standard_answer / rubric 抽出 gold 项（一条一个方面）。"""
    items: list[str] = []
    for line in (standard_answer or "").splitlines():
        s = _NUM_RE.sub("", line.strip())
        if s:
            items.append(s)
    if items:
        return items
    for raw in rubric:
        s = _MENTION_RE.sub("", str(raw).strip())
        s = _NUM_RE.sub("", s)
        if s:
            items.append(s)
    return items


def phrase_tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def best_episode_id(gold: str, rows: list[dict[str, str]], *, min_ratio: float = 0.22) -> str:
    """词面重叠把 gold 项对齐到一条 user turn。对不上返回空串。"""
    gt = phrase_tokens(gold)
    if not gt or not rows:
        return ""
    best_id = ""
    best = 0.0
    for row in rows:
        eid = row["id"] if "id" in row else ""
        body = row["content"] if "content" in row else ""
        et = phrase_tokens(body)
        if not eid or not et:
            continue
        ratio = len(gt & et) / len(gt)
        if ratio > best:
            best = ratio
            best_id = eid
    return best_id if best >= min_ratio else ""


def gold_chat_ids(standard_answer: str) -> list[str]:
    """从 gold 原文抽出 ``(chat_id 24, 26, 28)``。仅作 source_chat_ids 缺失时的兜底。"""
    out: list[str] = []
    seen: set[str] = set()
    for match in _CHAT_ID_RE.finditer(standard_answer or ""):
        for part in match.group(1).split(","):
            cid = part.strip()
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out


def first_source_chat_ids(raw: object) -> list[str]:
    """嵌套取每组第一个 int，与 eo_gold_struct.ids 一致。"""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, list) and item:
            first = item[0]
            if isinstance(first, bool):
                continue
            if isinstance(first, (int, float)):
                out.append(str(int(first)))
            elif isinstance(first, str) and first.strip().isdigit():
                out.append(first.strip())
        elif isinstance(item, bool):
            continue
        elif isinstance(item, (int, float)):
            out.append(str(int(item)))
        elif isinstance(item, str) and item.strip().isdigit():
            out.append(item.strip())
    return out


def episode_id_for_turn(turn: dict[str, str], rows: list[dict[str, str]]) -> str:
    """用 turn 正文前缀对齐已摄入 episode；对不上再用前 120 字。"""
    body = (turn["content"] if "content" in turn else "").strip()
    if not body or not rows:
        return ""
    for n in (80, 120):
        head = body[:n]
        for row in rows:
            content = row["content"] if "content" in row else ""
            eid = row["id"] if "id" in row else ""
            if eid and head and content.startswith(head):
                return eid
    return best_episode_id(body[:120], rows, min_ratio=0.35)


def load_gold_struct_ids(path: str, conv: int, q: int) -> list[str]:
    """从 eo_gold_struct_*.json 取该题 ids（每组已压成一个 int）。"""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        raw = json.loads(f.read())
    if not isinstance(raw, list):
        return []
    for row in raw:
        if not isinstance(row, dict):
            continue
        if "conv" not in row or int(row["conv"]) != conv:
            continue
        if "q" not in row or int(row["q"]) != q:
            continue
        ids = row["ids"] if "ids" in row else []
        return first_source_chat_ids(ids)
    return []


def gold_struct_path(dest_dir: str, scale: str = "100k") -> str:
    return os.path.join(dest_dir, f"eo_gold_struct_{scale}.json")


def probe_source_chat_ids(
    probe: dict[str, object],
    *,
    dest_dir: str = "",
    conv: int = -1,
    q: int = -1,
    scale: str = "100k",
) -> list[str]:
    """主路径：probe.source_chat_ids；否则 gold_struct.ids。"""
    raw = probe["source_chat_ids"] if "source_chat_ids" in probe else None
    ids = first_source_chat_ids(raw)
    if ids:
        return ids
    if dest_dir and conv >= 0 and q >= 0:
        return load_gold_struct_ids(gold_struct_path(dest_dir, scale), conv, q)
    return []


def map_gold_episode_ids(
    standard_answer: str,
    rubric: list[str],
    turns: list[dict[str, str]],
    episodes: list[dict[str, str]],
    source_ids: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """source_chat_ids → parquet turn → episode。返回 (mapped, unmap_chat_ids)。"""
    _ = rubric
    by_tid: dict[str, dict[str, str]] = {}
    for turn in turns:
        tid = turn["turn_id"] if "turn_id" in turn else ""
        if tid:
            by_tid[tid] = turn
    ids = list(source_ids) if source_ids else gold_chat_ids(standard_answer)
    mapped: list[str] = []
    unmap: list[str] = []
    for cid in ids:
        if cid not in by_tid:
            unmap.append(cid)
            continue
        eid = episode_id_for_turn(by_tid[cid], episodes)
        if eid:
            mapped.append(eid)
        else:
            unmap.append(cid)
    return mapped, unmap
