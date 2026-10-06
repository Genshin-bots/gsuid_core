"""`changelogs/` 目录的定位、版本枚举与单版本记录解析。

仓库的更新日志是「一版本一文件」的 Markdown，本模块把它读成渲染层能直接
用的 dataclass，避免 HTML 模板去处理 Markdown 语法。
"""

from __future__ import annotations

import re
from typing import Sequence
from pathlib import Path
from dataclasses import dataclass

from gsuid_core.version import __version__

_INDEX_FILE = "CHANGELOG.md"
_UPWARD_DEPTH = 6
_UNRELEASED_SUFFIX = "-unreleased"

# `# 0.11.0（未发布）— 副标题` / `# 0.7.4 — 副标题` / `# 0.11.0 副标题`
_TITLE_RE = re.compile(r"^#\s+(?P<ver>[0-9][0-9A-Za-z.+]*)(?P<flag>（[^）]*）)?\s*(?:[—–\-:：]\s*)?(?P<sub>.*?)\s*$")
_DATE_RE = re.compile(r"发布于\s*(?P<date>\d{4}-\d{2}-\d{2})")
_COMMITS_RE = re.compile(r"本版\s*(?P<num>\d+)\s*个提交")
_BULLET_RE = re.compile(r"^-\s+(?P<body>.+)$")
_TAG_RE = re.compile(r"^\*\*(?P<tag>[^*]+)\*\*\s*(?P<rest>.*)$")
_SHA_RE = re.compile(r"\[`(?P<sha>[0-9a-f]{7,40})`\]\(\s*https://github\.com/[^)]*\)")
_LINK_RE = re.compile(r"\[(?P<label>[^\]]*)\]\([^)]*\)")
# 索引表行：`| [`0.11.0`](0.11.0.md) | 一句话 | 2026-09-30 | 45 |`
_ROW_RE = re.compile(r"^\|\s*\[`(?P<ver>[^`]+)`\]\((?P<file>[^)]+)\)\s*\|(?P<rest>.*)$")

# 仓库 commit emoji 约定里的类别名，用于 `**🐛**` 这种只有 emoji 的条目
_EMOJI_LABELS: dict[str, str] = {
    "✨": "新增",
    "🐛": "修复",
    "🎨": "调整",
    "⚡": "性能",
    "💥": "破坏性",
    "🔒": "安全",
    "⬆️": "依赖",
    "♻": "重构",
    "🔧": "工具",
    "📦": "打包",
    "🍱": "配置",
    "📝": "文档",
    "🚨": "CI",
    "🧪": "评测",
    "👽": "未完工",
    "🌐": "i18n",
    "💻": "前端",
    "💚": "修补",
    "🔖": "升版",
    "✏️": "笔误",
    "🚀": "上线",
    "📌": "锁版本",
    "⚗️": "数据库",
    "🔌": "MCP",
    "🔥": "紧急",
}
_FALLBACK_LABEL = "变更"


@dataclass(frozen=True, slots=True)
class VersionRef:
    """索引里的一个版本：文件路径 + 索引表给的摘要。"""

    version: str
    released: bool
    path: Path
    summary: str
    date: str
    commit_count: int


@dataclass(frozen=True, slots=True)
class ChangelogEntry:
    """一条变更：分类标签 + 正文 + 证据 commit。"""

    emoji: str
    label: str
    text: str
    commits: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChangelogVersion:
    """一个版本的完整记录，来自 `changelogs/<版本>.md`。"""

    version: str
    released: bool
    subtitle: str
    date: str
    commit_count: int
    lead: tuple[str, ...]
    entries: tuple[ChangelogEntry, ...]


def changelog_dir() -> Path | None:
    """向上找含 `changelogs/CHANGELOG.md` 的仓库根；pip 安装态没有，返回 None。"""
    for parent in Path(__file__).resolve().parents[:_UPWARD_DEPTH]:
        candidate = parent / "changelogs"
        if (candidate / _INDEX_FILE).is_file():
            return candidate
    return None


def _join_lines(lines: Sequence[str]) -> str:
    """中文按行首尾相接，拉丁文之间补空格。"""
    out = ""
    for line in lines:
        if not out:
            out = line
        elif out[-1] > "⺀":
            out += line
        else:
            out += f" {line}"
    return out


def _split_tag(tag: str) -> tuple[str, str]:
    """`**⚡ 性能**` → `("⚡", "性能")`；`**🐛**` → `("🐛", "修复")`。"""
    cleaned = tag.strip()
    cut = 0
    for ch in cleaned:
        if ch.isspace() or (ch.isascii() and (ch.isalnum() or ch == "#")):
            break
        cut += 1
    emoji = cleaned[:cut].strip()
    label = cleaned[cut:].strip()
    if not label:
        label = _EMOJI_LABELS.get(emoji, _FALLBACK_LABEL)
    return emoji, label


def _parse_entry(body: str) -> ChangelogEntry:
    commits = tuple(m.group("sha")[:8] for m in _SHA_RE.finditer(body))
    plain = _plain(body)
    matched = _TAG_RE.match(plain)
    if matched is None:
        emoji, label = "•", _FALLBACK_LABEL
        text = plain.strip()
    else:
        emoji, label = _split_tag(matched.group("tag"))
        text = matched.group("rest").strip()
    return ChangelogEntry(emoji=emoji, label=label, text=text, commits=commits)


def _parse_lead(lines: Sequence[str]) -> tuple[str, ...]:
    """按空行分段，返回引言段落。"""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(_plain(line.strip()))
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return tuple(_join_lines(block) for block in blocks)


def _parse_meta(line: str) -> tuple[str, int]:
    date_match = _DATE_RE.search(line)
    count_match = _COMMITS_RE.search(line)
    date = date_match.group("date") if date_match is not None else ""
    count = int(count_match.group("num")) if count_match is not None else 0
    return date, count


def _plain(text: str) -> str:
    """去掉 commit 链接与普通 Markdown 链接，只留可读文字。"""
    return _LINK_RE.sub(lambda m: m.group("label"), _SHA_RE.sub("", text)).strip()


def _read_index_rows(index_text: str) -> dict[str, tuple[str, str, int]]:
    """索引表 → {文件名: (一句话, 日期, 提交数)}，省掉逐版本读文件。"""
    rows: dict[str, tuple[str, str, int]] = {}
    for line in index_text.splitlines():
        matched = _ROW_RE.match(line)
        if matched is None:
            continue
        # 不用 ^…$ 切单元格：没有 MULTILINE 时锚点只认整个字符串，跨不过中间的 |
        cells = [cell.strip() for cell in matched.group("rest").split("|")]
        summary = cells[0] if len(cells) > 0 else ""
        date = cells[1] if len(cells) > 1 else ""
        digits = "".join(ch for ch in (cells[2] if len(cells) > 2 else "") if ch.isdigit())
        rows[matched.group("file")] = (summary, date, int(digits) if digits else 0)
    return rows


def _version_key(version: str, released: bool) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    # 未发布段是同版本的更新内容，同版本内排最前
    return (*parts, 0 if released else 1)


def list_versions() -> tuple[VersionRef, ...]:
    """按版本号倒序列出全部版本（含未发布段）。找不到目录时返回空元组。"""
    root = changelog_dir()
    if root is None:
        return ()
    index_path = root / _INDEX_FILE
    rows: dict[str, tuple[str, str, int]] = {}
    if index_path.is_file():
        rows = _read_index_rows(index_path.read_text(encoding="utf-8"))

    refs: list[VersionRef] = []
    for path in root.glob("*.md"):
        if path.name == _INDEX_FILE:
            continue
        stem = path.stem
        released = not stem.endswith(_UNRELEASED_SUFFIX)
        version = stem.removesuffix(_UNRELEASED_SUFFIX) if not released else stem
        summary, date, commit_count = rows.get(path.name, ("", "", 0))
        refs.append(
            VersionRef(
                version=version,
                released=released,
                path=path,
                summary=summary,
                date=date,
                commit_count=commit_count,
            )
        )
    refs.sort(key=lambda ref: _version_key(ref.version, ref.released), reverse=True)
    return tuple(refs)


def is_current(ref: VersionRef) -> bool:
    """是否就是当前运行中的 core 版本。"""
    return ref.released and ref.version == __version__


def pick_default(refs: Sequence[VersionRef]) -> VersionRef | None:
    """默认展示当前 core 版本；该版本没有记录文件时退回最新一版。"""
    if not refs:
        return None
    for ref in refs:
        if is_current(ref):
            return ref
    return refs[0]


def resolve_query(query: str, refs: Sequence[VersionRef]) -> VersionRef | None:
    """`0.10.8` / `v0.10.8` / `0.10` 都能命中；写 `-unreleased` 才要未发布段。"""
    key = query.strip().lower().lstrip("v")
    if not key:
        return None
    want_unreleased = key.endswith(_UNRELEASED_SUFFIX)
    if want_unreleased:
        key = key.removesuffix(_UNRELEASED_SUFFIX)
    for ref in refs:
        if ref.version.lower() == key and ref.released is not want_unreleased:
            return ref
    for ref in refs:
        if ref.version.lower() == key:
            return ref
    for ref in refs:
        if ref.version.lower().startswith(key):
            return ref
    return None


def parse_version(ref: VersionRef) -> ChangelogVersion:
    """读单个版本文件，解析成结构化记录。"""
    lines = ref.path.read_text(encoding="utf-8").splitlines()

    subtitle = ref.summary
    date = ref.date
    commit_count = ref.commit_count

    lead_start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index == 0 and stripped.startswith("# "):
            title = _TITLE_RE.match(stripped)
            if title is not None and title.group("sub"):
                subtitle = title.group("sub")
            lead_start = index + 1
            continue
        if stripped.startswith("发布于"):
            parsed_date, parsed_count = _parse_meta(stripped)
            if parsed_date:
                date = parsed_date
            if parsed_count:
                commit_count = parsed_count
            lead_start = index + 1
            continue
        if stripped.startswith("- ") or stripped.startswith("##"):
            break

    body_start = len(lines)
    for index in range(lead_start, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("- ") or stripped.startswith("##"):
            body_start = index
            break

    entries: list[ChangelogEntry] = []
    for line in lines[body_start:]:
        matched = _BULLET_RE.match(line.strip())
        if matched is not None:
            entries.append(_parse_entry(matched.group("body")))

    return ChangelogVersion(
        version=ref.version,
        released=ref.released,
        subtitle=subtitle,
        date=date,
        commit_count=commit_count,
        lead=_parse_lead(lines[lead_start:body_start]),
        entries=tuple(entries),
    )
