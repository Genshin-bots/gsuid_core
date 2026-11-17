"""独立人格分片式定长生活手账管理器（Sharded & Ring-Bounded Persona Journal）。

存储架构（面向超多大型群聊与 365 天零膨胀运行）：
- ``personas/{persona}/self.md``：角色全局自我核（持久认知 ≤4 条 / 个人探索 ≤4 条 / 昨日 ≤2 条 / 今日 ≤24 条）
- ``personas/{persona}/groups/{gid}.md``：懒创建驻群分片（长期默契 ≤3 条 / 当月主线 ≤4 条 / 昨日 ≤3 条 / 今日 ≤50 条）
- ``personas/{persona}/archives/{YYYY-MM}.jsonl``：按月单文件无损冷归档（12 个月滚动清理）
"""

from __future__ import annotations

import re
import json
import time
import asyncio
import threading
from typing import Dict, List, Tuple, Mapping, Sequence, TypedDict
from pathlib import Path
from datetime import datetime as _dt
from dataclasses import field, dataclass

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.data_store import AI_CORE_PATH
from gsuid_core.ai_core.journal.dream import (
    DREAM_LIGHT_CARRY_MAX,
    DREAM_HEAVY_MIN_ENTRIES,
    MAX_NOTE_CLUSTERS_PER_NIGHT,
    DREAM_MAX_HEAVY_SHARDS_PER_NIGHT,
    DreamSlots,
    DreamLLMOutcome,
    GroundTruthDigest,
    current_month_str,
    logical_sleep_date,
    rule_rollover_shard,
    run_storage_gc_sync,
    collect_ground_truth,
    archive_shard_entries,
    consolidate_persona_notes,
    read_journal_archive_sync,
    synthesize_shard_with_llm,
)
from gsuid_core.ai_core.persona.config import (
    read_autonomy_runtime,
    is_within_active_hours,
    persona_config_manager,
)
from gsuid_core.ai_core.journal.sanitizer import (
    SILENT_CHECK_SUMMARY,
    clean_journal_tag,
    is_silent_payload,
    truncate_at_boundary,
    is_rejected_journal_body,
    sanitize_journal_summary,
)

JOURNAL_DIR: Path = AI_CORE_PATH / "journals"
SELF_SHARD_ID: str = "__self__"
_JOURNAL_STATE_FILE: str = ".journal_state.json"
_JOB_ID: str = "ai_journal_hourly_compact"

# 定长容量硬约束（保证 365 天后单文件字节数恒定 O(1)）
SELF_PERSISTENT_MAX_ITEMS = 4
SELF_PERSISTENT_MAX_CHARS = 220
SELF_MONTHLY_MAX_ITEMS = 4
SELF_MONTHLY_MAX_CHARS = 220
SELF_YESTERDAY_MAX_ITEMS = 2
SELF_YESTERDAY_MAX_CHARS = 160
SELF_TIMELINE_RING_CAP = 24

GROUP_PERSISTENT_MAX_ITEMS = 3
GROUP_PERSISTENT_MAX_CHARS = 160
GROUP_MONTHLY_MAX_ITEMS = 4
GROUP_MONTHLY_MAX_CHARS = 220
GROUP_YESTERDAY_MAX_ITEMS = 3
GROUP_YESTERDAY_MAX_CHARS = 210
GROUP_TIMELINE_RING_CAP = 50

ANCHOR_TOTAL_MAX_CHARS = 880
ANCHOR_RECENT_TIMELINE_CAP = 8
DELTA_LINE_MAX_CHARS = 90
SILENT_SCAN_WINDOW = 12

_LINE_PARSER = re.compile(
    r"^- \[(?P<time>[^\]|]+)(?: \| (?P<tag>[^\]]+))?\] (?P<text>.*?)"
    r"(?: \(群:(?P<gid>[^)]+)\))?(?: \(句柄: (?P<hid>[^)]+)\))?"
    r"(?: \(替:(?P<who>[^)]+)\))?(?: \((?P<cnt>×\d+)\))?$"
)
_HEADER_DATE_RE = re.compile(r"\((?P<label>\d{4}-\d{2}(?:-\d{2})?)\)")
_GROUP_HEADER_RE = re.compile(r"^###\s+驻群分片\s+\[群:(?P<gid>[^\]]+)\]")
_SHARD_TITLE_GID_RE = re.compile(r"\(群:(?P<gid>[^)]+)\)\s*$")
_SAFE_NAME_RE = re.compile(r'[\\/*?:"<>|]')

_SHARD_LOCKS: Dict[Tuple[str, str], asyncio.Lock] = {}
_SHARD_UPDATE_TS: Dict[Tuple[str, str], float] = {}
_RECENT_ENTRY_TS: Dict[Tuple[str, str], List[Tuple[float, str]]] = {}
_SHARD_CACHE: Dict[Path, Tuple[int, int, "ShardDocument"]] = {}
_CACHE_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class JournalEntry:
    """一条生活手账流水项。"""

    timestamp: float
    date_str: str
    time_str: str
    tag: str
    summary: str
    handle_id: str = ""
    group_id: str = ""
    is_silent_check: bool = False
    requested_by: str = ""


@dataclass(frozen=True)
class ParsedTimelineLine:
    time_str: str
    tag: str
    text: str
    gid: str
    hid: str
    who: str
    count: int
    is_silent: bool


@dataclass
class ShardDocument:
    """单个分片（self.md 或 groups/{gid}.md）的内存结构。"""

    shard_id: str
    persistent: List[str] = field(default_factory=list)
    monthly: List[str] = field(default_factory=list)
    yesterday: List[str] = field(default_factory=list)
    timeline: List[str] = field(default_factory=list)
    month_label: str = ""
    yesterday_label: str = ""
    today_label: str = ""


class PersonaJournalStateEntry(TypedDict):
    last_compact_ts: float
    last_dream_date: str
    dirty_shards: list[str]
    light_carry: dict[str, int]
    last_gc_month: str


class GroupShardPreview(TypedDict):
    group_id: str
    content: str
    persistent: list[str]
    monthly: list[str]
    yesterday: list[str]
    month_label: str
    yesterday_label: str
    timeline: list[str]
    timeline_count: int


class PersonaJournalStructured(TypedDict):
    self_content: str
    self_persistent: list[str]
    self_monthly: list[str]
    self_yesterday: list[str]
    self_yesterday_label: str
    self_timeline: list[str]
    persistent_summary: str
    monthly_summary: str
    yesterday_summary: str
    group_shards: list[GroupShardPreview]
    archive_months: list[str]


def _safe_token(raw: str, fallback: str = "default") -> str:
    cleaned = _SAFE_NAME_RE.sub("_", (raw or "").strip()).strip(". ")
    if not cleaned or cleaned in (".", "..") or ".." in cleaned:
        return fallback
    return cleaned


def _clone_shard_doc(doc: ShardDocument) -> ShardDocument:
    return ShardDocument(
        shard_id=doc.shard_id,
        persistent=list(doc.persistent),
        monthly=list(doc.monthly),
        yesterday=list(doc.yesterday),
        timeline=list(doc.timeline),
        month_label=doc.month_label,
        yesterday_label=doc.yesterday_label,
        today_label=doc.today_label,
    )


def _ensure_journal_dir() -> Path:
    if not JOURNAL_DIR.exists():
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return JOURNAL_DIR


def _persona_root(persona_name: str) -> Path:
    return _ensure_journal_dir() / "personas" / _safe_token(persona_name)


def _self_shard_path(persona_name: str) -> Path:
    root = _persona_root(persona_name)
    root.mkdir(parents=True, exist_ok=True)
    return root / "self.md"


def _group_shard_path(persona_name: str, group_id: str) -> Path:
    gdir = _persona_root(persona_name) / "groups"
    gdir.mkdir(parents=True, exist_ok=True)
    return gdir / f"{_safe_token(group_id, 'group')}.md"


def _dormant_group_path(persona_name: str, group_id: str) -> Path:
    return _persona_root(persona_name) / "dormant_groups" / f"{_safe_token(group_id, 'group')}.md"


def _archives_dir(persona_name: str) -> Path:
    return _persona_root(persona_name) / "archives"


def _get_shard_lock(persona_name: str, shard_id: str) -> asyncio.Lock:
    key = (_safe_token(persona_name), shard_id or SELF_SHARD_ID)
    if key not in _SHARD_LOCKS:
        _SHARD_LOCKS[key] = asyncio.Lock()
    return _SHARD_LOCKS[key]


def get_journal_path(persona_name: str, group_id: str = "") -> Path:
    """获取指定人格（及可选群分片）的主手账路径；首次访问自动迁移旧版单文件。"""
    _migrate_legacy_root_file_sync(persona_name)
    if group_id.strip():
        return _group_shard_path(persona_name, group_id.strip())
    return _self_shard_path(persona_name)


def parse_timeline_line(line: str) -> ParsedTimelineLine | None:
    m = _LINE_PARSER.match(line.strip())
    if m is None:
        return None
    cnt_raw = m.group("cnt") or ""
    cnt = int(cnt_raw.replace("×", "")) if cnt_raw.startswith("×") and cnt_raw[1:].isdigit() else 1
    txt = (m.group("text") or "").strip()
    is_silent = txt == SILENT_CHECK_SUMMARY or is_silent_payload(txt)
    return ParsedTimelineLine(
        time_str=(m.group("time") or "").strip(),
        tag=(m.group("tag") or "").strip(),
        text=txt,
        gid=(m.group("gid") or "").strip(),
        hid=(m.group("hid") or "").strip(),
        who=(m.group("who") or "").strip(),
        count=cnt,
        is_silent=is_silent,
    )


_TRAILING_HANDLE_RE = re.compile(
    r"(\s*(?:（归档:\s*[^）]+）|\(归档:\s*[^)]+\)|（句柄:\s*[^）]+）|\(句柄:\s*[^)]+\))[。.]?)$"
)


def cap_bullet_slot(lines: Sequence[str], *, max_items: int, max_chars: int) -> List[str]:
    """定长槽位裁剪器：去重、单行化、保护尾部句柄完整性，限制条数与总字数。"""
    if max_items <= 0 or max_chars <= 0:
        return []
    result: List[str] = []
    seen_norm: set[str] = set()
    used_chars = 0
    per_item_cap = max(45, min(120, max_chars // max(1, min(max_items, 2))))

    for raw in lines:
        body = " ".join(str(raw).splitlines()).strip()
        if not body or body.startswith("#") or body.startswith("【"):
            continue
        body = body.lstrip("-*• ").strip()
        if not body:
            continue
        m_tail = _TRAILING_HANDLE_RE.search(body)
        tail_suffix = m_tail.group(1) if m_tail is not None else ""
        head_body = body[: m_tail.start()].rstrip("。.") if m_tail is not None else body
        if tail_suffix:
            head_cap = max(14, per_item_cap - len(tail_suffix))
            body = f"{truncate_at_boundary(head_body, max_chars=head_cap).rstrip('。.')}{tail_suffix}"
        else:
            body = truncate_at_boundary(head_body, max_chars=per_item_cap)

        norm = re.sub(r"[\s，。；,;：:！!？?()（）\[\]]+", "", body).lower()
        if not norm or norm in seen_norm:
            continue
        bullet = f"- {body}"
        if used_chars + len(bullet) > max_chars:
            remain = max_chars - used_chars - 2
            if tail_suffix and remain > len(tail_suffix) + 10:
                short_head = truncate_at_boundary(head_body, max_chars=remain - len(tail_suffix)).rstrip("。.")
                bullet = f"- {short_head}{tail_suffix}"
            elif not tail_suffix and remain >= 24 and not result:
                bullet = f"- {truncate_at_boundary(body, max_chars=remain)}"
            elif tail_suffix and result:
                # 连续回收前置条目空间，确保末尾归档句柄完整且总长严格 <= max_chars
                while result and (max_chars - used_chars - 2) < len(tail_suffix) + 10:
                    popped = result.pop()
                    used_chars -= len(popped) + 1
                remain = max_chars - used_chars - 2
                head_allow = max(8, remain - len(tail_suffix))
                short_head = truncate_at_boundary(head_body, max_chars=head_allow).rstrip("。.")
                bullet = f"- {short_head}{tail_suffix}"
            elif tail_suffix and not result:
                head_allow = max(6, remain - len(tail_suffix))
                short_head = truncate_at_boundary(head_body, max_chars=head_allow).rstrip("。.")
                bullet = f"- {short_head}{tail_suffix}"
            else:
                break
        seen_norm.add(norm)
        result.append(bullet)
        used_chars += len(bullet) + 1
        if len(result) >= max_items:
            break
    return result


def parse_shard_markdown(content: str, shard_id: str = SELF_SHARD_ID) -> ShardDocument:
    """解析四层分片 Markdown（同时完整向后兼容旧版两层『常驻状态与近期小结 + 今日行动流水』）。"""
    doc = ShardDocument(shard_id=shard_id)
    current_slot = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            h_low = line[3:].strip()
            m_lbl = _HEADER_DATE_RE.search(h_low)
            lbl = m_lbl.group("label") if m_lbl is not None else ""
            if "持久" in h_low or "长期默契" in h_low or "常驻状态" in h_low:
                current_slot = "persistent"
            elif "当月" in h_low or "个人探索" in h_low or "近期主线" in h_low:
                current_slot = "monthly"
                if lbl:
                    doc.month_label = lbl[:7]
            elif "昨日" in h_low:
                current_slot = "yesterday"
                if lbl:
                    doc.yesterday_label = lbl
            elif "今日" in h_low or "行动流水" in h_low:
                current_slot = "timeline"
                if lbl:
                    doc.today_label = lbl
            else:
                current_slot = ""
            continue
        if line.startswith("#"):
            continue
        if current_slot == "timeline":
            if line.startswith("- ["):
                doc.timeline.append(line)
        elif current_slot == "persistent" and line.startswith(("-", "*", "•")):
            doc.persistent.append(f"- {line.lstrip('-*• ').strip()}")
        elif current_slot == "monthly" and line.startswith(("-", "*", "•")):
            if not _is_placeholder_bullet(line):
                doc.monthly.append(f"- {line.lstrip('-*• ').strip()}")
        elif current_slot == "yesterday" and line.startswith(("-", "*", "•")):
            if not _is_placeholder_bullet(line):
                doc.yesterday.append(f"- {line.lstrip('-*• ').strip()}")
    return doc


def render_shard_markdown(persona_name: str, doc: ShardDocument) -> str:
    """将分片渲染为定长 Markdown 文本。"""
    is_self = doc.shard_id == SELF_SHARD_ID
    if is_self:
        pers = cap_bullet_slot(doc.persistent, max_items=SELF_PERSISTENT_MAX_ITEMS, max_chars=SELF_PERSISTENT_MAX_CHARS)
        mon = cap_bullet_slot(doc.monthly, max_items=SELF_MONTHLY_MAX_ITEMS, max_chars=SELF_MONTHLY_MAX_CHARS)
        yest = cap_bullet_slot(doc.yesterday, max_items=SELF_YESTERDAY_MAX_ITEMS, max_chars=SELF_YESTERDAY_MAX_CHARS)
        y_hdr = f"## 昨日自主回顾 ({doc.yesterday_label})" if doc.yesterday_label else "## 昨日自主回顾"
        t_hdr = f"## 今日行动流水 ({doc.today_label})" if doc.today_label else "## 今日行动流水"
        lines: List[str] = [
            f"# {persona_name}的自我手账",
            "",
            "## 持久自我认知与通用经验",
            *(pers or ["- 日常处理群聊交互、资料核实与委派协作。"]),
            "",
            "## 近期个人探索与习惯",
            *(mon or ["- 暂无新增自主探索专题。"]),
            "",
            y_hdr,
            *(yest or ["- 昨日无额外独立探索记录。"]),
            "",
            t_hdr,
            *doc.timeline[-SELF_TIMELINE_RING_CAP:],
            "",
        ]
        return "\n".join(lines)

    pers = cap_bullet_slot(doc.persistent, max_items=GROUP_PERSISTENT_MAX_ITEMS, max_chars=GROUP_PERSISTENT_MAX_CHARS)
    mon = cap_bullet_slot(doc.monthly, max_items=GROUP_MONTHLY_MAX_ITEMS, max_chars=GROUP_MONTHLY_MAX_CHARS)
    yest = cap_bullet_slot(doc.yesterday, max_items=GROUP_YESTERDAY_MAX_ITEMS, max_chars=GROUP_YESTERDAY_MAX_CHARS)
    m_hdr = f"## 本群当月主线与持续议题 ({doc.month_label})" if doc.month_label else "## 本群当月主线与持续议题"
    y_hdr = f"## 本群昨日记忆摘要 ({doc.yesterday_label})" if doc.yesterday_label else "## 本群昨日记忆摘要"
    t_hdr = f"## 本群今日行动流水 ({doc.today_label})" if doc.today_label else "## 本群今日行动流水"
    lines = [
        f"# {persona_name}的驻群手账 (群:{doc.shard_id})",
        "",
        "## 本群长期默契与职责基调",
        *(pers or ["- 日常回应本群问答、核查数据并跟进委派事务。"]),
        "",
        m_hdr,
        *(mon or ["- 本月日常话题平稳推进中。"]),
        "",
        y_hdr,
        *(yest or ["- 昨日暂无归档摘要。"]),
        "",
        t_hdr,
        *doc.timeline[-GROUP_TIMELINE_RING_CAP:],
        "",
    ]
    return "\n".join(lines)


def _attach_group_tag_for_view(line: str, gid: str) -> str:
    """在全景聚合流水里为驻群分片条目补上 (群:gid)，供控制台直观区分来源群。"""
    if not gid or f"(群:{gid})" in line:
        return line
    p = parse_timeline_line(line)
    if p is None or p.gid:
        return line
    parts: List[str] = [f"- [{p.time_str}"]
    if p.tag:
        parts.append(f" | {p.tag}")
    parts.append(f"] {p.text} (群:{gid})")
    if p.hid:
        parts.append(f" (句柄: {p.hid})")
    if p.who:
        parts.append(f" (替:{p.who})")
    if p.count > 1:
        parts.append(f" (×{p.count})")
    return "".join(parts)


def _read_sections(content: str) -> Tuple[str, List[str]]:
    """兼容接口：返回（持久+当月+昨日合并摘要, 全部今日流水列表）。"""
    if "### 驻群分片 [群:" in content:
        head_part, _, rest_groups = content.partition("### 驻群分片 [群:")
        self_doc = parse_shard_markdown(head_part, SELF_SHARD_ID)
        standing_blocks: List[str] = []
        all_timeline: List[str] = list(self_doc.timeline)
        if self_doc.persistent:
            standing_blocks.append("【持久自我认知】\n" + "\n".join(self_doc.persistent))
        if self_doc.monthly:
            standing_blocks.append("【近期个人探索】\n" + "\n".join(self_doc.monthly))
        if self_doc.yesterday:
            standing_blocks.append("【昨日自主回顾】\n" + "\n".join(self_doc.yesterday))
        chunks = ("### 驻群分片 [群:" + rest_groups).split("### 驻群分片 [群:")
        for chunk in chunks:
            if not chunk.strip():
                continue
            gid, _, body = chunk.partition("]")
            clean_gid = gid.strip()
            gdoc = parse_shard_markdown(body, clean_gid)
            all_timeline.extend(_attach_group_tag_for_view(ln, clean_gid) for ln in gdoc.timeline)
            if gdoc.monthly or gdoc.yesterday:
                g_lines = [*gdoc.monthly[:2], *gdoc.yesterday[:2]]
                if g_lines:
                    standing_blocks.append(f"【群 {gid.strip()} 近况】\n" + "\n".join(g_lines))
        return "\n\n".join(standing_blocks).strip(), all_timeline

    doc = parse_shard_markdown(content, SELF_SHARD_ID)
    parts: List[str] = []
    for bucket in (doc.persistent, doc.monthly, doc.yesterday):
        for item in bucket:
            if (
                item not in parts
                and "暂无新增自主探索专题" not in item
                and "昨日无额外独立探索记录" not in item
                and "昨日暂无归档摘要" not in item
            ):
                parts.append(item)
    return "\n".join(parts).strip(), list(doc.timeline)


def _load_shard_sync(persona_name: str, shard_id: str, *, create_if_missing: bool = False) -> ShardDocument | None:
    _migrate_legacy_root_file_sync(persona_name)
    if shard_id == SELF_SHARD_ID:
        path = _self_shard_path(persona_name)
        if not path.exists():
            if not create_if_missing:
                return None
            init_doc = ShardDocument(
                shard_id=SELF_SHARD_ID,
                persistent=["- 目前运转正常，日常处理群聊交互与各项委派。"],
            )
            text = render_shard_markdown(persona_name, init_doc)
            path.write_text(text, encoding="utf-8")
    else:
        gdir = _persona_root(persona_name) / "groups"
        path = gdir / f"{_safe_token(shard_id, 'group')}.md"
        if not path.exists():
            # 若该群此前因超 60 天静默被移入休眠目录，有访问/写入需求时无缝唤醒
            dormant = _dormant_group_path(persona_name, shard_id)
            if dormant.exists():
                gdir.mkdir(parents=True, exist_ok=True)
                try:
                    dormant.replace(path)
                except OSError:
                    path = dormant
            elif create_if_missing:
                gdir.mkdir(parents=True, exist_ok=True)
                init_doc = ShardDocument(shard_id=shard_id)
                path.write_text(render_shard_markdown(persona_name, init_doc), encoding="utf-8")
            else:
                return None

    try:
        st = path.stat()
    except OSError:
        return None
    with _CACHE_LOCK:
        cached = _SHARD_CACHE[path] if path in _SHARD_CACHE else None
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return _clone_shard_doc(cached[2])

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    doc = parse_shard_markdown(raw, shard_id)
    with _CACHE_LOCK:
        _SHARD_CACHE[path] = (st.st_mtime_ns, st.st_size, _clone_shard_doc(doc))
    return doc


def _save_shard_sync(persona_name: str, doc: ShardDocument, *, bump_ts: bool = True, ts_val: float = 0.0) -> Path:
    key = _safe_token(persona_name)
    path = _self_shard_path(key) if doc.shard_id == SELF_SHARD_ID else _group_shard_path(key, doc.shard_id)
    rendered = render_shard_markdown(persona_name, doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    try:
        st = path.stat()
        with _CACHE_LOCK:
            _SHARD_CACHE[path] = (st.st_mtime_ns, st.st_size, _clone_shard_doc(doc))
            if bump_ts:
                _SHARD_UPDATE_TS[(key, doc.shard_id)] = max(ts_val, time.time(), st.st_mtime)
    except OSError:
        if bump_ts:
            with _CACHE_LOCK:
                _SHARD_UPDATE_TS[(key, doc.shard_id)] = max(ts_val, time.time())
    return path


def _migrate_legacy_root_file_sync(persona_name: str) -> None:
    """将旧版单文件 journals/{persona}.md 无损拆迁至 self.md + groups/{gid}.md。"""
    safe_name = _safe_token(persona_name)
    legacy_path = JOURNAL_DIR / f"{safe_name}.md"
    if not legacy_path.exists() or not legacy_path.is_file():
        return
    try:
        raw = legacy_path.read_text(encoding="utf-8")
    except OSError:
        return

    legacy_doc = parse_shard_markdown(raw, SELF_SHARD_ID)
    self_path = _self_shard_path(persona_name)
    self_doc = (
        parse_shard_markdown(self_path.read_text(encoding="utf-8"), SELF_SHARD_ID)
        if self_path.exists()
        else ShardDocument(shard_id=SELF_SHARD_ID)
    )
    if legacy_doc.persistent and not self_doc.persistent:
        self_doc.persistent = cap_bullet_slot(
            legacy_doc.persistent,
            max_items=SELF_PERSISTENT_MAX_ITEMS,
            max_chars=SELF_PERSISTENT_MAX_CHARS,
        )

    by_group: Dict[str, List[str]] = {}
    for line in legacy_doc.timeline:
        parsed = parse_timeline_line(line)
        if parsed is not None and parsed.gid:
            bucket = by_group[parsed.gid] if parsed.gid in by_group else []
            bucket.append(line)
            by_group[parsed.gid] = bucket
        else:
            self_doc.timeline.append(line)

    _save_shard_sync(persona_name, self_doc)
    for gid, glines in by_group.items():
        gpath = _group_shard_path(persona_name, gid)
        gdoc = (
            parse_shard_markdown(gpath.read_text(encoding="utf-8"), gid)
            if gpath.exists()
            else ShardDocument(shard_id=gid)
        )
        gdoc.timeline.extend(glines)
        gdoc.timeline = gdoc.timeline[-GROUP_TIMELINE_RING_CAP:]
        _save_shard_sync(persona_name, gdoc)

    backup_path = _persona_root(persona_name) / "legacy_v1.migrated.bak"
    try:
        legacy_path.replace(backup_path)
    except OSError:
        legacy_path.unlink(missing_ok=True)


def _format_entry_line(entry: JournalEntry, *, include_group_tag: bool = False) -> str:
    tag = clean_journal_tag(entry.tag)
    summary = SILENT_CHECK_SUMMARY if entry.is_silent_check else sanitize_journal_summary(entry.summary)
    parts: List[str] = [f"- [{entry.time_str}"]
    if tag:
        parts.append(f" | {tag}")
    parts.append(f"] {summary}")
    if include_group_tag and entry.group_id:
        parts.append(f" (群:{entry.group_id})")
    if entry.handle_id:
        parts.append(f" (句柄: {entry.handle_id})")
    if entry.requested_by and not entry.is_silent_check:
        parts.append(f" (替:{entry.requested_by})")
    return "".join(parts)


def _coalesce_ring_overflow(timeline: List[str], cap: int) -> List[str]:
    """当单日流水突破环形上限时，将前半区高频同标签旧流水原地折叠为 1 行统计，保证严格 <= cap。"""
    items = list(timeline)
    while len(items) > cap and len(items) >= 4:
        half = max(2, len(items) // 2)
        tag_indices: Dict[str, List[int]] = {}
        for idx in range(half):
            parsed = parse_timeline_line(items[idx])
            t_key = parsed.tag if parsed is not None and parsed.tag else "日常行动"
            bucket = tag_indices[t_key] if t_key in tag_indices else []
            bucket.append(idx)
            tag_indices[t_key] = bucket

        best_tag = ""
        best_idxs: List[int] = []
        for t_key, idxs in tag_indices.items():
            if len(idxs) > len(best_idxs):
                best_tag = t_key
                best_idxs = idxs

        if len(best_idxs) >= 2:
            p_first = parse_timeline_line(items[best_idxs[0]])
            p_last = parse_timeline_line(items[best_idxs[-1]])
            t_start = (p_first.time_str.split("~")[0] if p_first else "早先").strip()
            t_end = (p_last.time_str.split("~")[-1] if p_last else "稍早").strip()
            total_cnt = 0
            for i in best_idxs:
                pi = parse_timeline_line(items[i])
                total_cnt += pi.count if pi is not None else 1
            folded = (
                f"- [{t_start}~{t_end} | {best_tag}] 早先连续完成了 {total_cnt} 项「{best_tag}」相关行动 (×{total_cnt})"
            )
            idx_set = set(best_idxs)
            last_idx = best_idxs[-1]
            rebuilt: List[str] = []
            for i, line in enumerate(items):
                if i == last_idx:
                    rebuilt.append(folded)
                elif i not in idx_set:
                    rebuilt.append(line)
            items = rebuilt
        else:
            p0 = parse_timeline_line(items[0])
            p1 = parse_timeline_line(items[1])
            t0 = (p0.time_str.split("~")[0] if p0 else "早先").strip()
            t1 = (p1.time_str.split("~")[-1] if p1 else "稍早").strip()
            c_sum = (p0.count if p0 else 1) + (p1.count if p1 else 1)
            merged = f"- [{t0}~{t1} | 早先汇总] 合并早先 {c_sum} 项已完成行动 (×{c_sum})"
            items = [merged, *items[2:]]
    return items


def _append_entry_to_doc(doc: ShardDocument, entry: JournalEntry) -> Tuple[bool, bool, str]:
    """向分片文档追加一条记录（含跨行静默折叠、去错拦截与环形定长控制）。返回 (是否写入, 是否静默项, 落盘行文本)。"""
    silent = entry.is_silent_check or is_silent_payload(entry.summary)
    if not silent and is_rejected_journal_body(entry.summary):
        return False, silent, ""
    clean_sum = SILENT_CHECK_SUMMARY if silent else sanitize_journal_summary(entry.summary)
    if not clean_sum:
        return False, silent, ""

    clean_tag = clean_journal_tag(entry.tag)
    normalized = JournalEntry(
        timestamp=entry.timestamp,
        date_str=entry.date_str,
        time_str=entry.time_str,
        tag=clean_tag,
        summary=clean_sum,
        handle_id=entry.handle_id,
        group_id=entry.group_id,
        is_silent_check=silent,
        requested_by=entry.requested_by,
    )
    if entry.date_str:
        doc.today_label = entry.date_str
        if doc.shard_id != SELF_SHARD_ID and not doc.month_label:
            doc.month_label = entry.date_str[:7]

    # 跨行静默巡检合并：扫描最近 SILENT_SCAN_WINDOW 行同 tag 的静默项原位累加，不要求严格相邻
    if silent and doc.timeline:
        scan_floor = max(0, len(doc.timeline) - SILENT_SCAN_WINDOW)
        for idx in range(len(doc.timeline) - 1, scan_floor - 1, -1):
            parsed = parse_timeline_line(doc.timeline[idx])
            if parsed is not None and parsed.is_silent and parsed.tag == clean_tag:
                new_cnt = parsed.count + 1
                start_t = parsed.time_str.split("~")[0]
                merged_entry = JournalEntry(
                    timestamp=entry.timestamp,
                    date_str=entry.date_str,
                    time_str=f"{start_t}~{entry.time_str}",
                    tag=clean_tag,
                    summary=SILENT_CHECK_SUMMARY,
                    handle_id=entry.handle_id or parsed.hid,
                    group_id=entry.group_id or parsed.gid,
                    is_silent_check=True,
                )
                merged_line = f"{_format_entry_line(merged_entry)} (×{new_cnt})"
                del doc.timeline[idx]
                doc.timeline.append(merged_line)
                return True, True, merged_line

    formatted = _format_entry_line(normalized)
    doc.timeline.append(formatted)
    cap = SELF_TIMELINE_RING_CAP if doc.shard_id == SELF_SHARD_ID else GROUP_TIMELINE_RING_CAP
    if len(doc.timeline) > cap:
        doc.timeline = _coalesce_ring_overflow(doc.timeline, cap)
    return True, silent, formatted


def _get_journal_state_path() -> Path:
    return _ensure_journal_dir() / _JOURNAL_STATE_FILE


def _empty_state_entry() -> PersonaJournalStateEntry:
    return {
        "last_compact_ts": 0.0,
        "last_dream_date": "",
        "dirty_shards": [],
        "light_carry": {},
        "last_gc_month": "",
    }


def _load_full_state_unlocked() -> Dict[str, PersonaJournalStateEntry]:
    path = _get_journal_state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, PersonaJournalStateEntry] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        entry = _empty_state_entry()
        if isinstance(v, (int, float)):
            entry["last_compact_ts"] = float(v)
        elif isinstance(v, dict):
            if "last_compact_ts" in v and isinstance(v["last_compact_ts"], (int, float)):
                entry["last_compact_ts"] = float(v["last_compact_ts"])
            if "last_dream_date" in v and isinstance(v["last_dream_date"], str):
                entry["last_dream_date"] = v["last_dream_date"]
            if "dirty_shards" in v and isinstance(v["dirty_shards"], list):
                entry["dirty_shards"] = [str(s) for s in v["dirty_shards"] if isinstance(s, str) and s]
            if "light_carry" in v and isinstance(v["light_carry"], dict):
                entry["light_carry"] = {
                    str(sk): int(sv)
                    for sk, sv in v["light_carry"].items()
                    if isinstance(sk, str) and isinstance(sv, int)
                }
            if "last_gc_month" in v and isinstance(v["last_gc_month"], str):
                entry["last_gc_month"] = v["last_gc_month"]
        out[k] = entry
    return out


def _save_full_state_unlocked(state: Mapping[str, PersonaJournalStateEntry]) -> None:
    path = _get_journal_state_path()
    try:
        path.write_text(json.dumps(dict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning(t("log.ai.journal_compact_save_state_fail", e=e))


def _load_full_state() -> Dict[str, PersonaJournalStateEntry]:
    with _STATE_LOCK:
        return _load_full_state_unlocked()


def _save_full_state(state: Mapping[str, PersonaJournalStateEntry]) -> None:
    with _STATE_LOCK:
        _save_full_state_unlocked(state)


def _mark_shard_dirty_sync(persona_name: str, shard_id: str) -> None:
    key = _safe_token(persona_name)
    with _STATE_LOCK:
        state = _load_full_state_unlocked()
        entry = state[key] if key in state else _empty_state_entry()
        if shard_id not in entry["dirty_shards"]:
            entry["dirty_shards"].append(shard_id)
        state[key] = entry
        _save_full_state_unlocked(state)


def _commit_compact_state_sync(
    persona_key: str,
    *,
    cleared_shards: Sequence[str],
    still_dirty: Sequence[str],
    updated_carries: Mapping[str, int],
    gc_month: str = "",
    compact_ts: float | None = None,
    dream_date: str | None = None,
) -> None:
    """原子合入单人格做梦状态，绝不覆盖并发写入的其他人格或执行期间新增的 dirty_shards。"""
    with _STATE_LOCK:
        state = _load_full_state_unlocked()
        entry = state[persona_key] if persona_key in state else _empty_state_entry()
        cleared_set = set(cleared_shards)
        merged_dirty = [s for s in entry["dirty_shards"] if s not in cleared_set]
        for s in still_dirty:
            if s and s not in merged_dirty:
                merged_dirty.append(s)
        entry["dirty_shards"] = merged_dirty
        for sk, sv in updated_carries.items():
            entry["light_carry"][sk] = sv
        if gc_month:
            entry["last_gc_month"] = gc_month
        if compact_ts is not None:
            entry["last_compact_ts"] = compact_ts
        if dream_date is not None and not merged_dirty:
            entry["last_dream_date"] = dream_date
        state[persona_key] = entry
        _save_full_state_unlocked(state)


def get_last_compact_ts(persona_name: str) -> float:
    """获取指定人格上次记忆整理的时间戳（未整理过返回 0.0）。"""
    key = _safe_token(persona_name)
    state = _load_full_state()
    return state[key]["last_compact_ts"] if key in state else 0.0


def get_active_personas() -> List[str]:
    """获取所有激活的人格列表。"""
    active: List[str] = []
    for name in persona_config_manager.list_available():
        cfg = persona_config_manager.get_config(name)
        if cfg.get_config("scope").data != "disabled":
            active.append(name)
    return active or ["default"]


def resolve_persona_for_group_or_session(
    *,
    persona_name: str | None = None,
    session_id: str = "",
    group_id: str | None = None,
) -> str | None:
    """解析后台子任务/事件归属的人格，防止未显式传参时误落 default 或幽灵目录。"""
    if persona_name and persona_name.strip():
        cand = persona_name.strip()
        if cand in persona_config_manager.list_available():
            cfg = persona_config_manager.get_config(cand)
            if cfg.get_config("scope").data != "disabled":
                return cand

    sid = session_id.strip()
    if sid:
        parts = sid.split(":", 4)
        if len(parts) == 5 and parts[3] in ("group", "private"):
            resolved = persona_config_manager.get_persona_for_session(sid)
            if resolved:
                return resolved

    gid = (group_id or "").strip()
    if gid:
        resolved = persona_config_manager.get_persona_for_session(f"ws:bot:self:group:{gid}")
        if resolved:
            return resolved
    return None


@to_thread
def _write_entry_sync(persona_key: str, shard_id: str, entry: JournalEntry) -> bool:
    doc = _load_shard_sync(persona_key, shard_id, create_if_missing=True)
    if doc is None:
        return False
    wrote, is_silent, formatted = _append_entry_to_doc(doc, entry)
    if not wrote:
        return False
    eff_ts = max(entry.timestamp, time.time())
    _save_shard_sync(persona_key, doc, bump_ts=not is_silent, ts_val=eff_ts)
    if not is_silent and formatted:
        with _CACHE_LOCK:
            ring = (
                list(_RECENT_ENTRY_TS[(persona_key, shard_id)]) if (persona_key, shard_id) in _RECENT_ENTRY_TS else []
            )
            ring.append((eff_ts, formatted))
            _RECENT_ENTRY_TS[(persona_key, shard_id)] = ring[-16:]
    _mark_shard_dirty_sync(persona_key, shard_id)
    return True


async def record_journal_entry(
    entry: JournalEntry,
    *,
    target_personas: Sequence[str] | None = None,
    fanout_all_active: bool = False,
) -> None:
    """写入分片式生活手账（分片锁 + 异步线程落盘，群聊写入 groups/{gid}.md，全局写入 self.md）。"""
    if fanout_all_active:
        targets = get_active_personas()
    elif target_personas:
        targets = [n for n in target_personas if n and n.strip()]
    elif entry.group_id:
        inferred = resolve_persona_for_group_or_session(group_id=entry.group_id)
        targets = [inferred] if inferred else []
    else:
        targets = ["default"]

    if not targets:
        return
    shard_id = entry.group_id.strip() if entry.group_id and entry.group_id.strip() else SELF_SHARD_ID
    for name in targets:
        key = _safe_token(name)
        async with _get_shard_lock(key, shard_id):
            try:
                await _write_entry_sync(key, shard_id, entry)
            except OSError as e:
                logger.warning(t("log.ai.journal_compact_save_state_fail", e=e))


def _is_placeholder_bullet(line: str) -> bool:
    return (
        "暂无新增自主探索专题" in line
        or "昨日无额外独立探索记录" in line
        or "昨日暂无归档摘要" in line
        or "本月日常话题平稳推进中" in line
    )


def _compact_timeline_for_anchor(lines: Sequence[str], recent_cap: int = ANCHOR_RECENT_TIMELINE_CAP) -> str:
    """将当日流水压缩为『早先概览 1 行 + 最近 recent_cap 行』，兼顾全天不失忆与锚点定长。"""
    if not lines:
        return ""
    if len(lines) <= recent_cap:
        return "\n".join(lines)
    older = lines[:-recent_cap]
    recent = lines[-recent_cap:]
    tags: List[str] = []
    total_older = 0
    for ln in older:
        p = parse_timeline_line(ln)
        if p is not None:
            total_older += p.count
            if p.tag and p.tag not in tags:
                tags.append(p.tag)
        else:
            total_older += 1
    tag_hint = f"（涉及 {', '.join(tags[:4])} 等）" if tags else ""
    head_line = f"- [早先概览] 今日更早已完成 {total_older} 项行动{tag_hint}"
    return "\n".join([head_line, *recent])


def _latest_ts_for_scope(persona_key: str, shard_id: str, doc: ShardDocument | None, path: Path) -> float:
    with _CACHE_LOCK:
        mem_ts = _SHARD_UPDATE_TS[(persona_key, shard_id)] if (persona_key, shard_id) in _SHARD_UPDATE_TS else 0.0
    if doc is None or not path.exists():
        return mem_ts
    try:
        return max(mem_ts, path.stat().st_mtime)
    except OSError:
        return mem_ts


def _is_entry_newer_than_since(
    persona_key: str,
    shard_id: str,
    today_label: str,
    line: str,
    parsed: ParsedTimelineLine | None,
    since_ts: float,
) -> bool:
    """过滤早于 since_ts 的历史旧行，避免因静默巡检或单条新增把旧流水重复拼入增量提醒。"""
    with _CACHE_LOCK:
        ring = _RECENT_ENTRY_TS[(persona_key, shard_id)] if (persona_key, shard_id) in _RECENT_ENTRY_TS else None
        if ring is not None:
            for ets, eline in reversed(ring):
                if eline == line:
                    return ets > since_ts
    if since_ts < 1_000_000_000.0:
        return True
    if parsed is not None and len(today_label) == 10:
        end_hhmm = parsed.time_str.split("~")[-1].strip()
        if len(end_hhmm) == 5 and end_hhmm[2] == ":":
            try:
                line_min_ts = _dt.strptime(f"{today_label} {end_hhmm}", "%Y-%m-%d %H:%M").timestamp()
                return line_min_ts >= (int(since_ts) // 60) * 60
            except ValueError:
                pass
    return True


def read_journal_for_context(
    persona_name: str,
    *,
    current_group_id: str | None = None,
    since_ts: float = 0.0,
    turn_target_uids: Sequence[str] | None = None,
) -> Tuple[str, float]:
    """按分片读取生活手账供上下文装配（物理级群隔离 + O(1) 严格 ≤880 字锚点预算）。"""
    key = _safe_token(persona_name)
    gid = (current_group_id or "").strip()

    # 极速增量短路：若本会话关联的两个分片内存时间戳均未推进且已缓存，零磁盘 I/O 直接返回
    if since_ts > 0.0:
        with _CACHE_LOCK:
            ts_self_mem = _SHARD_UPDATE_TS[(key, SELF_SHARD_ID)] if (key, SELF_SHARD_ID) in _SHARD_UPDATE_TS else -1.0
            ts_grp_mem = _SHARD_UPDATE_TS[(key, gid)] if (gid and (key, gid) in _SHARD_UPDATE_TS) else -1.0
        if ts_self_mem > 0.0 and (not gid or ts_grp_mem > 0.0):
            best_mem = max(ts_self_mem, ts_grp_mem)
            if best_mem <= since_ts:
                return "", best_mem

    self_doc = _load_shard_sync(key, SELF_SHARD_ID, create_if_missing=False)
    grp_doc = _load_shard_sync(key, gid, create_if_missing=False) if gid else None

    # 兼容直接往 self.md 手写带 (群:xxx) 行时的隐私过滤
    self_visible_timeline: List[str] = []
    if self_doc is not None:
        for ln in self_doc.timeline:
            p = parse_timeline_line(ln)
            if p is not None and p.gid:
                if gid and p.gid == gid:
                    self_visible_timeline.append(ln)
            else:
                self_visible_timeline.append(ln)

    ts_self = _latest_ts_for_scope(key, SELF_SHARD_ID, self_doc, _persona_root(key) / "self.md")
    ts_grp = (
        _latest_ts_for_scope(key, gid, grp_doc, _persona_root(key) / "groups" / f"{_safe_token(gid, 'group')}.md")
        if gid
        else 0.0
    )
    latest_ts = max(ts_self, ts_grp)

    if since_ts > 0.0:
        if latest_ts <= since_ts:
            return "", latest_ts
        from gsuid_core.ai_core.control.lines import journal_delta_line

        candidates: List[Tuple[str, str, str]] = []
        if grp_doc is not None and ts_grp > since_ts:
            for ln in grp_doc.timeline:
                candidates.append((gid, grp_doc.today_label, ln))
        if self_doc is not None and self_visible_timeline and ts_self > since_ts:
            for ln in self_visible_timeline:
                candidates.append((SELF_SHARD_ID, self_doc.today_label, ln))

        matched: List[str] = []
        for cand_sid, cand_day, recent in reversed(candidates):
            p = parse_timeline_line(recent)
            if p is not None and p.is_silent:
                continue
            if not _is_entry_newer_than_since(key, cand_sid, cand_day, recent, p, since_ts):
                continue
            who = p.who if p is not None else ""
            if who and turn_target_uids is not None and who not in turn_target_uids:
                continue
            matched.append(recent.lstrip("- ").strip())
            if len(matched) >= 2:
                break
        if not matched:
            return "", latest_ts
        joined = "；".join(reversed(matched))
        clipped = truncate_at_boundary(joined, max_chars=DELTA_LINE_MAX_CHARS)
        return journal_delta_line(clipped), latest_ts

    # 首轮会话锚点（history[0]）：组装 Self 核 + 当前群专属四层记忆，总长卡死 <= ANCHOR_TOTAL_MAX_CHARS
    blocks: List[str] = []
    if self_doc is not None:
        self_items = [
            x for x in [*self_doc.persistent[:3], *self_doc.monthly[:2]] if x.strip() and not _is_placeholder_bullet(x)
        ]
        if not gid and self_doc.yesterday:
            self_items.extend(x for x in self_doc.yesterday[:2] if x.strip() and not _is_placeholder_bullet(x))
        if self_items:
            blocks.append("【自我认知与近况】\n" + "\n".join(self_items))

    if grp_doc is not None:
        grp_standing = [
            x for x in [*grp_doc.persistent[:2], *grp_doc.monthly[:3]] if x.strip() and not _is_placeholder_bullet(x)
        ]
        if grp_standing:
            blocks.append("【本群默契与当月主线】\n" + "\n".join(grp_standing))
        grp_yest = [x for x in grp_doc.yesterday[:3] if x.strip() and not _is_placeholder_bullet(x)]
        if grp_yest:
            blocks.append("【本群昨日记忆】\n" + "\n".join(grp_yest))

    combined_timeline: List[str] = []
    if self_visible_timeline:
        combined_timeline.extend(self_visible_timeline[-3:])
    if grp_doc is not None and grp_doc.timeline:
        combined_timeline.extend(grp_doc.timeline)

    if combined_timeline:
        tl_text = _compact_timeline_for_anchor(combined_timeline, recent_cap=ANCHOR_RECENT_TIMELINE_CAP)
        if tl_text:
            blocks.append(f"【今日行动流水】\n{tl_text}")

    full_text = "\n\n".join(blocks).strip()
    if len(full_text) > ANCHOR_TOTAL_MAX_CHARS:
        full_text = truncate_at_boundary(full_text, max_chars=ANCHOR_TOTAL_MAX_CHARS)
    return full_text, latest_ts


def _list_group_shard_ids_sync(persona_name: str) -> List[str]:
    gdir = _persona_root(persona_name) / "groups"
    if not gdir.exists():
        return []
    files = sorted(gdir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: List[str] = []
    for f in files:
        if not f.is_file() or not f.stem:
            continue
        real_gid = f.stem
        try:
            with f.open("r", encoding="utf-8") as fp:
                first_line = fp.readline().strip()
            m_gid = _SHARD_TITLE_GID_RE.search(first_line)
            if m_gid is not None and m_gid.group("gid").strip():
                real_gid = m_gid.group("gid").strip()
        except OSError:
            pass
        if real_gid not in out:
            out.append(real_gid)
    return out


def get_journal_content(persona_name: str) -> str:
    """读取指定人格手账全景视图（self.md + 最近活跃的驻群分片，兼容 WebConsole 单页展示）。"""
    _migrate_legacy_root_file_sync(persona_name)
    self_doc = _load_shard_sync(persona_name, SELF_SHARD_ID, create_if_missing=True)
    assert self_doc is not None
    parts: List[str] = [render_shard_markdown(persona_name, self_doc).rstrip()]

    for gid in _list_group_shard_ids_sync(persona_name)[:8]:
        gdoc = _load_shard_sync(persona_name, gid, create_if_missing=False)
        if gdoc is None:
            continue
        body_lines = render_shard_markdown(persona_name, gdoc).splitlines()[1:]
        parts.append(f"\n### 驻群分片 [群:{gid}]\n" + "\n".join(body_lines).strip())
    return "\n".join(parts) + "\n"


def get_journal_structured(persona_name: str) -> PersonaJournalStructured:
    """返回结构化分层手账元数据（供 WebConsole 多尺度卡片与按群分片切换消费）。"""
    _migrate_legacy_root_file_sync(persona_name)
    self_doc = _load_shard_sync(persona_name, SELF_SHARD_ID, create_if_missing=True)
    assert self_doc is not None
    self_md = render_shard_markdown(persona_name, self_doc)
    groups_out: List[GroupShardPreview] = []
    for gid in _list_group_shard_ids_sync(persona_name)[:32]:
        gdoc = _load_shard_sync(persona_name, gid, create_if_missing=False)
        if gdoc is None:
            continue
        groups_out.append(
            {
                "group_id": gid,
                "content": render_shard_markdown(persona_name, gdoc),
                "persistent": [x for x in gdoc.persistent if not _is_placeholder_bullet(x)],
                "monthly": [x for x in gdoc.monthly if not _is_placeholder_bullet(x)],
                "yesterday": [x for x in gdoc.yesterday if not _is_placeholder_bullet(x)],
                "month_label": gdoc.month_label,
                "yesterday_label": gdoc.yesterday_label,
                "timeline": list(gdoc.timeline),
                "timeline_count": len(gdoc.timeline),
            }
        )
    adir = _archives_dir(persona_name)
    months = sorted([p.stem for p in adir.glob("*.jsonl")], reverse=True) if adir.exists() else []
    self_pers = [x for x in self_doc.persistent if not _is_placeholder_bullet(x)]
    self_mon = [x for x in self_doc.monthly if not _is_placeholder_bullet(x)]
    self_yest = [x for x in self_doc.yesterday if not _is_placeholder_bullet(x)]
    return {
        "self_content": self_md,
        "self_persistent": self_pers,
        "self_monthly": self_mon,
        "self_yesterday": self_yest,
        "self_yesterday_label": self_doc.yesterday_label,
        "self_timeline": list(self_doc.timeline),
        "persistent_summary": "\n".join(self_doc.persistent),
        "monthly_summary": "\n".join(self_doc.monthly),
        "yesterday_summary": "\n".join(self_doc.yesterday),
        "group_shards": groups_out,
        "archive_months": months,
    }


def save_journal_content(persona_name: str, content: str, *, shard_id: str = "") -> None:
    """保存编辑后的手账文本（支持按指定 shard_id 单分片写回或全景分片写回）。"""
    target_sid = shard_id.strip()
    if target_sid:
        eff_sid = SELF_SHARD_ID if target_sid == SELF_SHARD_ID else target_sid
        doc = parse_shard_markdown(content, eff_sid)
        _save_shard_sync(persona_name, doc)
        return

    if "### 驻群分片 [群:" in content:
        head_part, _, rest_groups = content.partition("### 驻群分片 [群:")
        self_doc = parse_shard_markdown(head_part, SELF_SHARD_ID)
        _save_shard_sync(persona_name, self_doc)
        for chunk in ("### 驻群分片 [群:" + rest_groups).split("### 驻群分片 [群:"):
            if not chunk.strip():
                continue
            gid, _, body = chunk.partition("]")
            if gid.strip():
                gdoc = parse_shard_markdown(body, gid.strip())
                _save_shard_sync(persona_name, gdoc)
        return

    doc = parse_shard_markdown(content, SELF_SHARD_ID)
    _save_shard_sync(persona_name, doc)


async def save_journal_content_async(persona_name: str, content: str, *, shard_id: str = "") -> None:
    """带分片互斥锁的异步手账保存入口（供 WebConsole PUT 路由调用，防止并发写覆盖）。"""
    key = _safe_token(persona_name)
    target_sid = shard_id.strip()
    if target_sid:
        eff_sid = SELF_SHARD_ID if target_sid == SELF_SHARD_ID else target_sid
        async with _get_shard_lock(key, eff_sid):
            await to_thread(save_journal_content)(key, content, shard_id=eff_sid)
        return

    if "### 驻群分片 [群:" in content:
        head_part, _, rest_groups = content.partition("### 驻群分片 [群:")
        async with _get_shard_lock(key, SELF_SHARD_ID):
            await to_thread(save_journal_content)(key, head_part, shard_id=SELF_SHARD_ID)
        for chunk in ("### 驻群分片 [群:" + rest_groups).split("### 驻群分片 [群:"):
            if not chunk.strip():
                continue
            gid, _, body = chunk.partition("]")
            if gid.strip():
                async with _get_shard_lock(key, gid.strip()):
                    await to_thread(save_journal_content)(key, body, shard_id=gid.strip())
        return

    async with _get_shard_lock(key, SELF_SHARD_ID):
        await to_thread(save_journal_content)(key, content, shard_id=SELF_SHARD_ID)


async def read_archived_journal_handle(
    persona_name: str,
    shard_id: str,
    date_or_month: str,
) -> str:
    """供 read_handle 读取指定分片的月度/单日无损归档。"""
    return await read_journal_archive_sync(_archives_dir(persona_name), shard_id, date_or_month)


def get_all_journal_personas() -> List[str]:
    """获取所有需要维护手账的人格列表（激活人格 ∪ 磁盘已有分片或旧手账的人格）。"""
    names = set(get_active_personas())
    if JOURNAL_DIR.exists():
        for legacy in JOURNAL_DIR.glob("*.md"):
            if legacy.stem and not legacy.name.startswith("."):
                names.add(legacy.stem)
        p_dir = JOURNAL_DIR / "personas"
        if p_dir.exists():
            for sub in p_dir.iterdir():
                if sub.is_dir() and not sub.name.startswith("."):
                    names.add(sub.name)
    return sorted(names)


async def _summarize_journal_with_llm(
    persona_name: str,
    doc: ShardDocument,
    ground_truth: GroundTruthDigest,
    prev_month_rollover: Sequence[str] = (),
) -> DreamLLMOutcome:
    """委托做梦引擎调用 high 档模型生成三层覆写槽位。"""
    return await synthesize_shard_with_llm(
        persona_name,
        doc,
        ground_truth=ground_truth,
        prev_month_rollover=prev_month_rollover,
    )


def _apply_slots_to_doc(
    doc: ShardDocument,
    slots: DreamSlots,
    *,
    persona_name: str,
    sleep_date: str,
    current_month: str,
) -> None:
    is_self = doc.shard_id == SELF_SHARD_ID
    doc.persistent = cap_bullet_slot(
        slots["persistent"] or doc.persistent,
        max_items=SELF_PERSISTENT_MAX_ITEMS if is_self else GROUP_PERSISTENT_MAX_ITEMS,
        max_chars=SELF_PERSISTENT_MAX_CHARS if is_self else GROUP_PERSISTENT_MAX_CHARS,
    )
    doc.monthly = cap_bullet_slot(
        slots["monthly"],
        max_items=SELF_MONTHLY_MAX_ITEMS if is_self else GROUP_MONTHLY_MAX_ITEMS,
        max_chars=SELF_MONTHLY_MAX_CHARS if is_self else GROUP_MONTHLY_MAX_CHARS,
    )
    yest_raw = [x for x in slots["yesterday"] if x and x.strip()]
    archive_handle = f"journal:{persona_name}:{doc.shard_id}:{sleep_date}"
    if yest_raw and archive_handle not in yest_raw[-1]:
        yest_raw[-1] = f"{yest_raw[-1].rstrip('。.')}（归档: {archive_handle}）。"
    doc.yesterday = cap_bullet_slot(
        yest_raw,
        max_items=SELF_YESTERDAY_MAX_ITEMS if is_self else GROUP_YESTERDAY_MAX_ITEMS,
        max_chars=SELF_YESTERDAY_MAX_CHARS if is_self else GROUP_YESTERDAY_MAX_CHARS,
    )
    doc.month_label = "" if is_self else current_month
    doc.yesterday_label = sleep_date
    doc.today_label = ""
    doc.timeline = []


async def _dream_one_shard(
    persona_name: str,
    shard_id: str,
    *,
    sleep_date: str,
    current_month: str,
    tz_name: str = "Asia/Shanghai",
    now_ts: float,
    allow_heavy_llm: bool,
    light_carry_count: int,
    force: bool,
) -> Tuple[int, str, int]:
    """整理单个分片。返回 (归并流水条数, 模式 'llm'|'rule'|'none', 更新后的 light_carry)。"""
    async with _get_shard_lock(persona_name, shard_id):
        doc = await to_thread(_load_shard_sync)(persona_name, shard_id, create_if_missing=False)
        if doc is None or not doc.timeline:
            return 0, "none", light_carry_count

        # 若 self.md 内混有手写的 (群:gid) 流水，立即分流落盘至对应群分片并保存 self.md
        if shard_id == SELF_SHARD_ID:
            stray_by_group: Dict[str, List[str]] = {}
            kept_self: List[str] = []
            for ln in doc.timeline:
                p = parse_timeline_line(ln)
                if p is not None and p.gid:
                    bucket = stray_by_group[p.gid] if p.gid in stray_by_group else []
                    bucket.append(ln)
                    stray_by_group[p.gid] = bucket
                else:
                    kept_self.append(ln)
            if stray_by_group:
                doc.timeline = kept_self
                for gid, glines in stray_by_group.items():
                    gdoc = await to_thread(_load_shard_sync)(persona_name, gid, create_if_missing=True)
                    if gdoc is not None:
                        gdoc.timeline.extend(glines)
                        gdoc.timeline = gdoc.timeline[-GROUP_TIMELINE_RING_CAP:]
                        await to_thread(_save_shard_sync)(persona_name, gdoc, ts_val=now_ts)
                await to_thread(_save_shard_sync)(persona_name, doc, ts_val=now_ts)
                if not doc.timeline:
                    return 0, "none", 0

        entry_count = len(doc.timeline)
        gt = await collect_ground_truth(persona_name, shard_id, now_ts)
        archives_dir = _archives_dir(persona_name)

        # 跨月边界：先将上月主线封存进月卷，把上月条目交给做梦模型提取持久精华后清空新月槽位
        prev_month_rollover: List[str] = []
        if doc.month_label and doc.month_label != current_month and doc.monthly:
            prev_month_rollover = [x for x in doc.monthly if not _is_placeholder_bullet(x)]
            if prev_month_rollover:
                await archive_shard_entries(
                    archives_dir,
                    date_str=doc.month_label,
                    shard_id=shard_id,
                    kind="monthly_rollup",
                    entries=prev_month_rollover,
                    now_ts=now_ts,
                    tz_name=tz_name,
                )

        is_heavy = (
            force
            or entry_count >= DREAM_HEAVY_MIN_ENTRIES
            or bool(gt.completed)
            or bool(prev_month_rollover)
            or light_carry_count >= DREAM_LIGHT_CARRY_MAX
        )

        slots: DreamSlots | None = None
        mode = "rule"
        if allow_heavy_llm and is_heavy:
            outcome = await _summarize_journal_with_llm(
                persona_name,
                doc,
                gt,
                prev_month_rollover=prev_month_rollover,
            )
            if outcome == "transient_error":
                if not force:
                    return 0, "none", light_carry_count
            elif outcome is not None:
                slots = outcome
                mode = "llm"

        if slots is None:
            slots = rule_rollover_shard(
                doc,
                persona_name=persona_name,
                sleep_date=sleep_date,
                current_month=current_month,
                ground_truth=gt,
            )

        await archive_shard_entries(
            archives_dir,
            date_str=sleep_date,
            shard_id=shard_id,
            kind="daily",
            entries=doc.timeline,
            now_ts=now_ts,
            tz_name=tz_name,
        )
        _apply_slots_to_doc(
            doc,
            slots,
            persona_name=persona_name,
            sleep_date=sleep_date,
            current_month=current_month,
        )
        await to_thread(_save_shard_sync)(persona_name, doc)
        new_carry = 0 if mode == "llm" else (light_carry_count + 1)
        logger.info(
            t(
                "log.ai.journal_dream_shard_done",
                persona_name=persona_name,
                shard=shard_id,
                mode=mode,
                count=entry_count,
            )
        )
        return entry_count, mode, new_carry


async def compact_journal(
    persona_name: str,
    *,
    force: bool = False,
    target_shard: str | None = None,
    max_llm_shards: int | None = None,
) -> bool:
    """对指定人格执行做梦整理（支持 target_shard 单分片速整与 max_llm_shards 配额防阻塞）。"""
    key = _safe_token(persona_name)
    await to_thread(_migrate_legacy_root_file_sync)(key)

    now_ts = time.time()
    cfg = persona_config_manager.get_config(key)
    runtime = read_autonomy_runtime(cfg)
    sleep_date = logical_sleep_date(runtime.timezone, now_ts)
    cur_month = current_month_str(runtime.timezone, now_ts)
    eff_llm_cap = max_llm_shards if max_llm_shards is not None else DREAM_MAX_HEAVY_SHARDS_PER_NIGHT

    state_all = await to_thread(_load_full_state)()
    st_entry = state_all[key] if key in state_all else _empty_state_entry()

    candidate_shards: List[Tuple[str, int]] = []
    seen_sids: set[str] = set()
    req_sid = target_shard.strip() if target_shard and target_shard.strip() else ""

    if req_sid:
        eff_req = SELF_SHARD_ID if req_sid == SELF_SHARD_ID else req_sid
        sdoc = await to_thread(_load_shard_sync)(key, eff_req, create_if_missing=False)
        if sdoc is not None and sdoc.timeline:
            candidate_shards.append((eff_req, len(sdoc.timeline)))
            seen_sids.add(eff_req)
        note_llm_budget = (
            MAX_NOTE_CLUSTERS_PER_NIGHT if max_llm_shards is None else max(0, eff_llm_cap - len(candidate_shards))
        )
        notes_merged, notes_archived = (
            await consolidate_persona_notes(
                key,
                now_ts=now_ts,
                tz_name=runtime.timezone,
                allow_llm=note_llm_budget > 0,
                max_llm_merges=note_llm_budget,
            )
            if eff_req == SELF_SHARD_ID
            else (0, 0)
        )
        gc_ran = False
    else:
        # 收集所有待检查分片：脏标记 ∪ 磁盘上有非空 timeline 的分片（兼容外部编辑与测试直写）
        self_doc = await to_thread(_load_shard_sync)(key, SELF_SHARD_ID, create_if_missing=False)
        if self_doc is not None and self_doc.timeline:
            candidate_shards.append((SELF_SHARD_ID, len(self_doc.timeline)))
            seen_sids.add(SELF_SHARD_ID)

        all_gids = set(st_entry["dirty_shards"]) | set(await to_thread(_list_group_shard_ids_sync)(key))
        all_gids.discard(SELF_SHARD_ID)
        for gid in all_gids:
            gdoc = await to_thread(_load_shard_sync)(key, gid, create_if_missing=False)
            if gdoc is not None and gdoc.timeline and gid not in seen_sids:
                candidate_shards.append((gid, len(gdoc.timeline)))
                seen_sids.add(gid)

        # Phase 3：整编个人散乱笔记库（受总重度 LLM 配额统一约束）
        note_llm_budget = (
            MAX_NOTE_CLUSTERS_PER_NIGHT if max_llm_shards is None else max(0, eff_llm_cap - len(candidate_shards))
        )
        notes_merged, notes_archived = await consolidate_persona_notes(
            key,
            now_ts=now_ts,
            tz_name=runtime.timezone,
            allow_llm=note_llm_budget > 0,
            max_llm_merges=note_llm_budget,
        )

        # 月度存储维护 GC（休眠群归档 + 过期月卷清理）
        gc_ran = False
        if st_entry["last_gc_month"] != cur_month:
            dormant_cnt, purged_cnt = await run_storage_gc_sync(_persona_root(key), now_ts, runtime.timezone)
            gc_ran = True
            if dormant_cnt or purged_cnt:
                logger.info(t("log.ai.journal_gc_done", dormant=dormant_cnt, purged=purged_cnt))

    if not candidate_shards:
        if notes_merged or notes_archived:
            await to_thread(_commit_compact_state_sync)(
                key,
                cleared_shards=(),
                still_dirty=(),
                updated_carries={},
                gc_month=cur_month if gc_ran else "",
                compact_ts=now_ts if not req_sid else None,
                dream_date=sleep_date if not req_sid else None,
            )
            return True
        if gc_ran:
            await to_thread(_commit_compact_state_sync)(
                key,
                cleared_shards=(),
                still_dirty=(),
                updated_carries={},
                gc_month=cur_month,
            )
        return False

    # 按当日行动数降序排列，SELF 优先，重度 LLM 调用受 eff_llm_cap 配额保护
    candidate_shards.sort(key=lambda item: (1 if item[0] == SELF_SHARD_ID else 0, item[1]), reverse=True)

    # 若在 self_doc 分流后产生了新的群分片，动态追加进队列
    total_merged_entries = 0
    llm_used = min(notes_merged, note_llm_budget) if max_llm_shards is not None else 0
    cleared_sids: List[str] = []
    still_dirty: List[str] = []
    updated_carries: Dict[str, int] = {}

    idx = 0
    processed_this_run: set[str] = set()
    while idx < len(candidate_shards):
        sid, _ = candidate_shards[idx]
        idx += 1
        if sid in processed_this_run:
            continue
        processed_this_run.add(sid)

        carry = st_entry["light_carry"][sid] if sid in st_entry["light_carry"] else 0
        allow_llm = llm_used < eff_llm_cap
        merged_n, mode, new_carry = await _dream_one_shard(
            key,
            sid,
            sleep_date=sleep_date,
            current_month=cur_month,
            tz_name=runtime.timezone,
            now_ts=now_ts,
            allow_heavy_llm=allow_llm,
            light_carry_count=carry,
            force=force,
        )
        if sid == SELF_SHARD_ID and not req_sid:
            # 检查 self.md 拆分后是否有新落入的群分片需要紧接着整理
            for new_gid in await to_thread(_list_group_shard_ids_sync)(key):
                if new_gid not in processed_this_run and new_gid not in seen_sids:
                    gdoc = await to_thread(_load_shard_sync)(key, new_gid, create_if_missing=False)
                    if gdoc is not None and gdoc.timeline:
                        candidate_shards.append((new_gid, len(gdoc.timeline)))
                        seen_sids.add(new_gid)

        if merged_n > 0:
            total_merged_entries += merged_n
            cleared_sids.append(sid)
            updated_carries[sid] = new_carry
            if mode == "llm":
                llm_used += 1
        else:
            # 若因 LLM 临时故障保留了流水，维持 dirty 标记以便下一窗口续跑
            rem_doc = await to_thread(_load_shard_sync)(key, sid, create_if_missing=False)
            if rem_doc is not None and rem_doc.timeline:
                still_dirty.append(sid)
            else:
                cleared_sids.append(sid)

    did_work = total_merged_entries > 0 or notes_merged > 0 or notes_archived > 0
    full_run_done = did_work and not still_dirty and not req_sid
    await to_thread(_commit_compact_state_sync)(
        key,
        cleared_shards=cleared_sids,
        still_dirty=still_dirty,
        updated_carries=updated_carries,
        gc_month=cur_month if gc_ran else "",
        compact_ts=now_ts if full_run_done else None,
        dream_date=sleep_date if full_run_done else None,
    )
    if did_work:
        logger.info(t("log.ai.journal_compact_success", persona_name=key, count=total_merged_entries))
        return True
    return False


async def check_and_compact_all_journals() -> None:
    """定时巡检：在人格非活跃休息窗口内，按睡眠自然日（无 24h 漂移）执行每日做梦巩固。"""
    now = time.time()
    state_all = await to_thread(_load_full_state)()

    for persona_name in get_all_journal_personas():
        key = _safe_token(persona_name)
        cfg = persona_config_manager.get_config(key)
        runtime = read_autonomy_runtime(cfg)
        if is_within_active_hours(list(runtime.active_hours), runtime.timezone):
            continue

        sleep_date = logical_sleep_date(runtime.timezone, now)
        st_entry = state_all[key] if key in state_all else _empty_state_entry()
        if st_entry["last_dream_date"] == sleep_date:
            continue
        if st_entry["last_compact_ts"] > 0.0 and (now - st_entry["last_compact_ts"]) < 43200.0:
            continue

        try:
            await compact_journal(key, force=False)
        except Exception as e:
            logger.exception(t("log.ai.journal_compact_fail", persona_name=key, e=e))


async def init_journal_scheduler() -> None:
    """注册每 30 分钟生活手账休息窗做梦巡检任务。"""
    from gsuid_core.aps import scheduler

    scheduler.add_job(
        func=check_and_compact_all_journals,
        trigger="interval",
        minutes=30,
        id=_JOB_ID,
        name="生活手账做梦整理",
        replace_existing=True,
    )
    logger.info(t("log.ai.journal_scheduler_start"))


async def stop_journal_scheduler() -> None:
    """停止生活手账定时整理任务。"""
    from gsuid_core.aps import scheduler

    job = scheduler.get_job(_JOB_ID)
    if job is not None:
        scheduler.remove_job(_JOB_ID)
