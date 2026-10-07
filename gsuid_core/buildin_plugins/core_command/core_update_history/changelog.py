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
# 当前版提交超过这个数时，recent 不再拼上一版，避免默认图过长。
_RECENT_PREV_MAX_COMMITS = 6

# `# 0.11.0（未发布）— 副标题` / `# 0.7.4 — 副标题` / `# 0.11.0 副标题`
# 分隔符放宽到 0~2 个：`-—–:：` 任一。写成 `0,1` 个会在 `0.1.0——标题` 上把 `—` 留给副标题。
_TITLE_RE = re.compile(
    r"^#\s+(?P<ver>[0-9][0-9A-Za-z.+]*)(?P<flag>（[^）]*）)?"
    r"\s*(?:[-—–:：]\s*){0,2}(?P<sub>.*?)\s*$"
)
_DATE_RE = re.compile(r"发布于\s*(?P<date>\d{4}-\d{2}-\d{2})")
_COMMITS_RE = re.compile(r"本版\s*(?P<num>\d+)\s*个提交")
# 只收顶层条目：缩进的 `  - xxx` 是上一条的续行，收进来会变成平级假条目。
_BULLET_RE = re.compile(r"^-\s+(?P<body>.+)$")
# emoji 与标签名之间允许没有空格，`**🐛修复**` 也要切出标签。
_TAG_RE = re.compile(r"^\*\*(?P<tag>[^*]+?)\*\*\s*(?P<rest>.*)$")
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

# 更新日志按 commit 逐条写，标签有 51 种（长尾还有「语境」「排版」这类一次性自定义），
# 直接按标签分组等于一个 commit 一个条目。这里再归一层「变更性质」，读者关心的是
# 这版动了几类东西。键是细标签，值是 (粗分类标签, 粗分类 emoji)。
_CATEGORY_EMOJI = {
    "新增": ("✨", "4ade80"),
    "修复": ("🐛", "f87171"),
    "调整": ("🎨", "c084fc"),
    "优化": ("⚡", "60a5fa"),
    "依赖": ("📦", "94a3b8"),
    "破坏性": ("💥", "ef4444"),
    "安全": ("🔒", "38bdf8"),
    "移除": ("🗑️", "94a3b8"),
    "其他": ("🧩", "94a3b8"),
}
_CATEGORY_LABELS = {
    # 新增：造出新东西
    "新增": "新增",
    "上线": "新增",
    "资源": "新增",
    "表情包": "新增",
    "记忆": "新增",
    "人格": "新增",
    "注入": "新增",
    "委派": "新增",
    # 修复：把坏的东西修好
    "修复": "修复",
    "修补": "修复",
    "纠错": "修复",
    "兼容": "修复",
    "登录": "修复",
    "i18n": "修复",
    "日志": "修复",
    "检索": "修复",
    "召回": "修复",
    "收发": "修复",
    "渲染": "修复",
    "排版": "修复",
    "行为": "修复",
    "资源占用": "修复",
    "内存": "修复",
    "默认值": "修复",
    "移除": "移除",
    # 调整：已有东西挪一挪
    "调整": "调整",
    "重构": "调整",
    "配置": "调整",
    "网页控制台": "调整",
    "前端": "调整",
    "实验性": "调整",
    "未完工": "调整",
    "Agent": "调整",
    "语境": "调整",
    "闸门": "调整",
    "工具": "调整",
    "MCP": "调整",
    "评测": "调整",
    "文档": "调整",
    "CI": "调整",
    "CI/检查": "调整",
    "运维": "调整",
    "部署": "调整",
    "数据库": "调整",
    # 优化：变快变小
    "性能": "优化",
    "内存占用": "优化",
    # 依赖与构建
    "依赖": "依赖",
    "打包": "依赖",
    "升版": "依赖",
    "锁版本": "依赖",
    # 破坏性变更单列，混进调整里会被当成普通调整
    "破坏性": "破坏性",
    "破坏性变更": "破坏性",
    # 安全单列：7 次改动散在别处，混进「修复」看不出安全影响面
    "安全": "安全",
}
_CATEGORY_FALLBACK = ("其他", "🎨", "c084fc")
# 组序：优化 > 新增 > 调整 > 安全 > 修复，其余追加在后。
_CATEGORY_ORDER = ("优化", "新增", "调整", "安全", "修复", "破坏性", "依赖", "移除", "其他")


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
class CommitAuthor:
    """一条 commit 的作者：显示名、GitHub 登录名、头像 data URI。"""

    name: str
    login: str
    avatar_uri: str


@dataclass(frozen=True, slots=True)
class ChangelogEntry:
    """一条变更：分类标签 + 正文 + 证据 commit。"""

    emoji: str
    label: str
    text: str
    commits: tuple[str, ...]
    authors: tuple[CommitAuthor, ...] = ()


@dataclass(frozen=True, slots=True)
class ChangelogGroup:
    """同一「变更性质」的归并：一个小标题 + 若干子条目。

    日志里标签有 51 种，直接按标签分组等于一个 commit 一个条目。这里按
    `_CATEGORY_LABELS` 归到 8 个粗分类，组内保持原顺序。
    """

    emoji: str
    accent: str
    label: str
    items: tuple[ChangelogEntry, ...]


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
    groups: tuple[ChangelogGroup, ...]
    authors: tuple[CommitAuthor, ...] = ()


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
    """`**⚡ 性能**` → `("⚡", "性能")`；`**🐛**` → `("🐛", "修复")`。

    不按字符盲切：`⬆️` 是双码位（U+2B06+U+FE0F），`✏️` 同理，盲切会把变体
    选择符留在标签里；而 `**修复**`（没有 emoji）盲切会切成「修」「复」。
    做法是先按已知 emoji 长度剥，剥不出再按码位猜一次。
    """
    cleaned = tag.strip().strip("*").strip()
    if not cleaned:
        return "", _FALLBACK_LABEL
    for known in sorted(_EMOJI_LABELS, key=len, reverse=True):
        if cleaned.startswith(known):
            return known, cleaned[len(known) :].strip() or _EMOJI_LABELS[known]
    # 未登记的 emoji：按首码位切，附带 U+FE0F 变体选择符
    head = cleaned[:2] if len(cleaned) > 1 and cleaned[1] == "️" else cleaned[:1]
    rest = cleaned[len(head) :].strip()
    # 首码位是文字就说明本条没写 emoji：ASCII 字母数字，或中日韩汉字
    if not rest or _is_text_head(head):
        return "", cleaned
    return head, rest


# 标签名可能整段没有 emoji（`**修复**`）。中日韩汉字也算文字，
# 否则「修」会被当 emoji 吃掉，「修复」切成「修」「复」。
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def _is_text_head(text: str) -> bool:
    """首码位像文字（ASCII 字母数字/标点，或汉字）时，按「没写 emoji」处理。"""
    head = text[0]
    if _HAN.match(head):
        return True
    return head.isascii() and (head.isalnum() or head in "#/._-")


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


def peek_emoji(ref: VersionRef) -> str:
    """索引行前面的代表 emoji：取该版本**条目最多**的那个粗分类。

    不走 `parse_version`：索引一次要画 12 行，整篇解析只为拿一个 emoji 太贵。
    这里只扫条目行的标签名过一遍 `_CATEGORY_LABELS`，不建对象、不抽 commit。
    """
    try:
        text = ref.path.read_text(encoding="utf-8")
    except OSError:
        return "📄"
    counts: dict[str, int] = {}
    for line in text.splitlines():
        bullet = _BULLET_RE.match(line.strip())
        if bullet is None:
            continue
        matched = _TAG_RE.match(_plain(bullet.group("body")))
        if matched is None:
            continue
        label = _split_tag(matched.group("tag"))[1]
        name = _category_of(label or _FALLBACK_LABEL)
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return "📄"
    # 并列时按 _CATEGORY_ORDER 取靠前的（优化优先于新增），别让 dict 顺序决定
    rank = {name: index for index, name in enumerate(_CATEGORY_ORDER)}
    dominant = min(counts, key=lambda name: (-counts[name], rank.get(name, len(rank))))
    return _style_of(dominant)[0]


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


def pick_recent(refs: Sequence[VersionRef]) -> tuple[VersionRef, ...]:
    """默认展示当前这段；提交不超过 6 个时再带上一个不同版本号。

    升版打开这一版，之后的提交仍是这个版本号。同号的「未发布」和「已发布」
    不能并排两张。当前版已经够长时不再拼上一版。
    """
    if not refs:
        return ()
    first = refs[0]
    if first.commit_count > _RECENT_PREV_MAX_COMMITS:
        return (first,)
    picked: list[VersionRef] = [first]
    for ref in refs[1:]:
        if ref.version != first.version:
            picked.append(ref)
            break
    return tuple(picked)


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


def _style_of(category: str) -> tuple[str, str]:
    """粗分类名 → (emoji, 强调色)。未登记的名字给中性样式。"""
    return _CATEGORY_EMOJI.get(category, ("🎨", "c084fc"))


def _category_of(label: str) -> str:
    """细标签 → 粗分类名。未登记的标签进「其他」。"""
    return _CATEGORY_LABELS.get(label, _CATEGORY_FALLBACK[0])


def group_entries(entries: Sequence[ChangelogEntry]) -> tuple[ChangelogGroup, ...]:
    """按「变更性质」归并成粗分类，组序固定，组内保持日志原顺序。"""
    buckets: dict[str, list[ChangelogEntry]] = {}
    for entry in entries:
        buckets.setdefault(_category_of(entry.label or _FALLBACK_LABEL), []).append(entry)
    ordered = [name for name in _CATEGORY_ORDER if name in buckets]
    ordered.extend(name for name in buckets if name not in _CATEGORY_ORDER)
    return tuple(
        ChangelogGroup(
            emoji=_style_of(name)[0],
            accent=_style_of(name)[1],
            label=name,
            items=tuple(buckets[name]),
        )
        for name in ordered
    )


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
        groups=group_entries(entries),
    )
