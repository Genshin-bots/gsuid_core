"""独立人格分片式生活手账存储层（路径、分片解析/渲染、分片与状态读写）。"""

from __future__ import annotations

import re
import json
import time
import asyncio
import threading
from typing import Dict, List, Tuple, Mapping, Sequence, TypedDict
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import field, dataclass

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.data_store import AI_CORE_PATH
from gsuid_core.ai_core.persona.config import persona_config_manager
from gsuid_core.ai_core.journal.sanitizer import (
    SILENT_CHECK_SUMMARY,
    is_silent_payload,
    truncate_at_boundary,
)
from gsuid_core.ai_core.persona.config_rules import is_persona_enabled

JOURNAL_DIR: Path = AI_CORE_PATH / "journals"


SELF_SHARD_ID: str = "__self__"


USER_SHARD_PREFIX: str = "user:"


_JOURNAL_STATE_FILE: str = ".journal_state.json"

ARCHIVE_RETENTION_MONTHS = 12


def _resolve_tz(tz_name: str) -> ZoneInfo:
    zone_str = (tz_name or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(zone_str)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Shanghai")


def _tz_dt(tz_name: str, now_ts: float) -> datetime:
    return datetime.fromtimestamp(now_ts, tz=_resolve_tz(tz_name))


@to_thread
def _append_jsonl_sync(path: Path, line_json: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line_json + "\n")


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


_SHARD_TITLE_UID_RE = re.compile(r"\(用户:(?P<uid>[^)]+)\)\s*$")


_ARCHIVE_MONTH_STEM_RE = re.compile(r"^(?:legacy_v1_)?\d{4}-\d{2}$")


_ARCHIVE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    schema_version: int


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


class UserShardPreview(TypedDict):
    user_id: str
    shard_id: str
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
    user_shards: list[UserShardPreview]
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


def _user_shard_id(user_id: str) -> str:
    return f"{USER_SHARD_PREFIX}{user_id.strip()}"


def _is_user_shard(shard_id: str) -> bool:
    return shard_id.startswith(USER_SHARD_PREFIX) and bool(shard_id[len(USER_SHARD_PREFIX) :].strip())


def _user_shard_path(persona_name: str, user_id: str) -> Path:
    udir = _persona_root(persona_name) / "users"
    udir.mkdir(parents=True, exist_ok=True)
    raw_uid = user_id[len(USER_SHARD_PREFIX) :] if user_id.startswith(USER_SHARD_PREFIX) else user_id
    return udir / f"{_safe_token(raw_uid, 'user')}.md"


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
    if _is_user_shard(doc.shard_id):
        raw_uid = doc.shard_id[len(USER_SHARD_PREFIX) :].strip()
        m_hdr = f"## 私聊当月主线 ({doc.month_label})" if doc.month_label else "## 私聊当月主线"
        y_hdr = f"## 私聊昨日记忆摘要 ({doc.yesterday_label})" if doc.yesterday_label else "## 私聊昨日记忆摘要"
        t_hdr = f"## 私聊今日行动流水 ({doc.today_label})" if doc.today_label else "## 私聊今日行动流水"
        lines = [
            f"# {persona_name}的私聊手账 (用户:{raw_uid})",
            "",
            "## 私聊长期默契",
            *(pers or ["- 日常回应对方私聊提问与个人委托。"]),
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
    elif _is_user_shard(shard_id):
        path = _user_shard_path(persona_name, shard_id)
        if not path.exists():
            if not create_if_missing:
                return None
            init_doc = ShardDocument(shard_id=shard_id)
            path.write_text(render_shard_markdown(persona_name, init_doc), encoding="utf-8")
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
    if doc.shard_id == SELF_SHARD_ID:
        path = _self_shard_path(key)
    elif _is_user_shard(doc.shard_id):
        path = _user_shard_path(key, doc.shard_id)
    else:
        path = _group_shard_path(key, doc.shard_id)
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


def _get_journal_state_path() -> Path:
    return _ensure_journal_dir() / _JOURNAL_STATE_FILE


def _empty_state_entry(*, schema_version: int = 2) -> PersonaJournalStateEntry:
    return {
        "last_compact_ts": 0.0,
        "last_dream_date": "",
        "dirty_shards": [],
        "light_carry": {},
        "last_gc_month": "",
        "schema_version": schema_version,
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
        # 旧版 state 文件缺 schema_version 时按 1 处理，触发一次无损迁移
        entry = _empty_state_entry(schema_version=1)
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
            if "schema_version" in v:
                sv_raw = v["schema_version"]
                if isinstance(sv_raw, int) and not isinstance(sv_raw, bool):
                    entry["schema_version"] = sv_raw
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
        if is_persona_enabled(cfg):
            active.append(name)
    return active or ["default"]


def _is_placeholder_bullet(line: str) -> bool:
    return (
        "暂无新增自主探索专题" in line
        or "昨日无额外独立探索记录" in line
        or "昨日暂无归档摘要" in line
        or "本月日常话题平稳推进中" in line
        or "日常回应对方私聊提问与个人委托" in line
    )


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


def _list_user_shard_ids_sync(persona_name: str) -> List[str]:
    udir = _persona_root(persona_name) / "users"
    if not udir.exists():
        return []
    files = sorted(udir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: List[str] = []
    for f in files:
        if not f.is_file() or not f.stem:
            continue
        real_uid = f.stem
        try:
            with f.open("r", encoding="utf-8") as fp:
                first_line = fp.readline().strip()
            m_uid = _SHARD_TITLE_UID_RE.search(first_line)
            if m_uid is not None and m_uid.group("uid").strip():
                real_uid = m_uid.group("uid").strip()
        except OSError:
            pass
        if real_uid not in out:
            out.append(real_uid)
    return out


def _list_archive_months_sync(persona_name: str) -> List[str]:
    adir = _archives_dir(persona_name)
    if not adir.exists():
        return []
    stems = [p.stem for p in adir.glob("*.jsonl") if _ARCHIVE_MONTH_STEM_RE.match(p.stem) is not None]
    regular = sorted([s for s in stems if not s.startswith("legacy_v1_")], reverse=True)
    legacy = sorted([s for s in stems if s.startswith("legacy_v1_")], reverse=True)
    return regular + legacy


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
