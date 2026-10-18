"""在插件目录里找出包含这次回执原文的源码片段。"""

from __future__ import annotations

import re
from pathlib import Path

_SKIP_DIRS = frozenset({".git", "__pycache__", "resource", "resources", "node_modules"})
_MAX_FILES = 800
_MAX_FILE_BYTES = 256_000
_EXCERPT_LINES = 24
_STEP_MARKS = ("先", "然后", "接着", "再发送")
_NOT_COMMANDS = frozenset({"或者", "或", "以及"})


def receipt_needles(receipt: str) -> list[str]:
    found: list[str] = []
    for line in receipt.splitlines():
        stripped = line.strip()
        if len(stripped) >= 6:
            found.append(stripped)
        for part in re.findall(r"[\u4e00-\u9fff]{4,}", stripped):
            found.append(part)
    ordered: list[str] = []
    for needle in sorted(set(found), key=len, reverse=True):
        if needle not in ordered:
            ordered.append(needle)
    return ordered[:8]


def command_stems(receipt: str) -> list[str]:
    """回执方括号里的命令。去掉末尾示例数字和开头的英文前缀，方便对上源码。"""
    stems: list[str] = []
    for item in re.findall(r"\[([^\[\]\n]{2,80})\]", receipt):
        cut = re.sub(r"\d+$", "", item).strip()
        bare = re.sub(r"^[A-Za-z]+", "", cut)
        for stem in (cut, bare):
            if len(stem) >= 2 and stem not in stems:
                stems.append(stem)
    return stems


def find_receipt_excerpts(root: Path, receipt: str, entry_rel: str = "") -> list[tuple[str, str]]:
    needles = receipt_needles(receipt)
    root_resolved = root.resolve()
    files = _py_texts(root_resolved)
    hits: list[tuple[str, str]] = []
    # 先用最长原文。短中文片段会命中无关文件。
    for needle in needles:
        matched: list[tuple[str, str]] = []
        for rel, text in files:
            if needle not in text:
                continue
            matched.append((rel, _window(text, needle)))
            if len(matched) >= 2:
                break
        if matched:
            hits = matched
            break
    if not hits and entry_rel:
        entry = (root_resolved / entry_rel).resolve()
        if entry.is_file() and entry.is_relative_to(root_resolved) and entry.stat().st_size <= _MAX_FILE_BYTES:
            text = entry.read_text(encoding="utf-8", errors="replace")
            rel = entry.relative_to(root_resolved).as_posix()
            hits = [(rel, "\n".join(text.splitlines()[:_EXCERPT_LINES]))]
    skip = {rel for rel, _ in hits}
    hits.extend(_followup_excerpts(files, command_stems(receipt), skip))
    return hits


def _followup_excerpts(
    files: list[tuple[str, str]],
    stems: list[str],
    skip: set[str],
) -> list[tuple[str, str]]:
    """回执命令旁边用「先/然后」串着的另一步。只取一处。"""
    if not stems:
        return []
    for rel, text in files:
        if rel in skip:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not any(stem in line for stem in stems):
                continue
            start = max(0, index - 2)
            end = min(len(lines), index + 6)
            window = "\n".join(lines[start:end])
            if not any(mark in window for mark in _STEP_MARKS):
                continue
            if _other_commands(window, stems):
                return [(rel, window)]
    return []


def _other_commands(window: str, stems: list[str]) -> list[str]:
    found: list[str] = []
    for raw in re.findall(r"[【\[]([^】\]\n]{2,30})[】\]]", window):
        command = raw.strip()
        if any(ch in command for ch in "[]【】"):
            continue
        if command in _NOT_COMMANDS or len(command) < 2:
            continue
        if not any("\u4e00" <= ch <= "\u9fff" for ch in command):
            continue
        if any(stem in command or command in stem for stem in stems):
            continue
        if command not in found:
            found.append(command)
    return found


def _py_texts(root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in root.rglob("*.py"):
        if len(found) >= _MAX_FILES:
            break
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            continue
        if resolved.stat().st_size > _MAX_FILE_BYTES:
            continue
        found.append((resolved.relative_to(root).as_posix(), resolved.read_text(encoding="utf-8", errors="replace")))
    return found


def _window(text: str, needle: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if needle in line:
            start = max(0, index - 4)
            end = min(len(lines), index + _EXCERPT_LINES)
            return "\n".join(lines[start:end])
    return "\n".join(lines[:_EXCERPT_LINES])
