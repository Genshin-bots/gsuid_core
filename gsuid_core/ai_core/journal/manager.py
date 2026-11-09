"""独立人格生活手账管理器（Per-Persona Self Journal）。

用于记录与检索 AI 人格在被动对话、主动巡检、长任务与自主行动中的真实经历。
遵循三层隐私隔离与前缀缓存设计。
"""

from __future__ import annotations

import re
import asyncio
from typing import Dict, List, Tuple, Sequence
from pathlib import Path
from dataclasses import dataclass

from gsuid_core.data_store import AI_CORE_PATH
from gsuid_core.ai_core.persona.config import persona_config_manager

JOURNAL_DIR: Path = AI_CORE_PATH / "journals"

_DEFAULT_SUMMARY_MAX_CHARS = 300
_MAX_TIMELINE_ENTRIES = 20
_LINE_PARSER = re.compile(
    r"^- \[(?P<time>[^\]|]+)(?: \| (?P<tag>[^\]]+))?\] (?P<text>.*?)"
    r"(?: \(群:(?P<gid>\d+)\))?(?: \(句柄: (?P<hid>[^)]+)\))?(?: \((?P<cnt>×\d+)\))?$"
)
_JOURNAL_LOCKS: Dict[str, asyncio.Lock] = {}
_LAST_UPDATE_TS: Dict[str, float] = {}


def _get_persona_lock(persona_name: str) -> asyncio.Lock:
    key = persona_name.strip() or "default"
    if key not in _JOURNAL_LOCKS:
        _JOURNAL_LOCKS[key] = asyncio.Lock()
    return _JOURNAL_LOCKS[key]


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


def _ensure_journal_dir() -> Path:
    if not JOURNAL_DIR.exists():
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return JOURNAL_DIR


def get_journal_path(persona_name: str) -> Path:
    """获取指定人格的手账 markdown 路径。"""
    safe_name = re.sub(r'[\\/*?:"<>|]', "_", persona_name.strip() or "default")
    return _ensure_journal_dir() / f"{safe_name}.md"


def _init_journal_file_if_missing(path: Path, persona_name: str) -> None:
    if path.exists():
        return
    template = (
        f"# {persona_name}的生活手账\n\n"
        "## 常驻状态与近期小结\n"
        "- 目前一切正常，日常处理群聊交互与各项委派。\n\n"
        "## 今日行动流水\n"
    )
    path.write_text(template, encoding="utf-8")


def _read_sections(content: str) -> Tuple[str, List[str]]:
    """拆分手账的上半区（常驻状态小结）与下半区（今日行动流水行）。"""
    standing_part = ""
    timeline_lines: List[str] = []
    standing_header = "## 常驻状态与近期小结"
    timeline_header = "## 今日行动流水"

    if standing_header in content:
        split_standing = content.split(standing_header, 1)[1]
        if timeline_header in split_standing:
            standing_part = split_standing.split(timeline_header, 1)[0].strip()
            rest = split_standing.split(timeline_header, 1)[1]
            timeline_lines = [line.strip() for line in rest.splitlines() if line.strip().startswith("- [")]
        else:
            standing_part = split_standing.strip()
    elif timeline_header in content:
        rest = content.split(timeline_header, 1)[1]
        timeline_lines = [line.strip() for line in rest.splitlines() if line.strip().startswith("- [")]

    return standing_part, timeline_lines


def get_journal_content(persona_name: str) -> str:
    """读取手账全文（供 WebConsole 或内部查看）。"""
    path = get_journal_path(persona_name)
    _init_journal_file_if_missing(path, persona_name)
    return path.read_text(encoding="utf-8")


def save_journal_content(persona_name: str, content: str) -> None:
    """保存手账全文。"""
    path = get_journal_path(persona_name)
    path.write_text(content, encoding="utf-8")


def _format_entry_line(entry: JournalEntry) -> str:
    parts: List[str] = [f"- [{entry.time_str}"]
    if entry.tag:
        parts.append(f" | {entry.tag}")
    parts.append(f"] {entry.summary}")
    if entry.group_id:
        parts.append(f" (群:{entry.group_id})")
    if entry.handle_id:
        parts.append(f" (句柄: {entry.handle_id})")
    return "".join(parts)


def _append_to_persona_journal(path: Path, persona_name: str, entry: JournalEntry) -> None:
    _init_journal_file_if_missing(path, persona_name)
    content = path.read_text(encoding="utf-8")
    standing_part, timeline_lines = _read_sections(content)

    # 静默巡检合并：若同 tag 且为静默巡检，则累加计数
    if entry.is_silent_check and timeline_lines:
        last_line = timeline_lines[-1]
        m = _LINE_PARSER.match(last_line)
        if m and m.group("tag") == entry.tag:
            cnt_str = m.group("cnt")
            cnt = int(cnt_str.replace("×", "")) if cnt_str else 1
            new_cnt = cnt + 1
            start_time = m.group("time").split("~")[0]
            new_time = f"{start_time}~{entry.time_str}"
            merged_entry = JournalEntry(
                timestamp=entry.timestamp,
                date_str=entry.date_str,
                time_str=new_time,
                tag=entry.tag,
                summary=entry.summary,
                handle_id=entry.handle_id or (m.group("hid") or ""),
                group_id=entry.group_id or (m.group("gid") or ""),
                is_silent_check=True,
            )
            timeline_lines[-1] = f"{_format_entry_line(merged_entry)} (×{new_cnt})"
            _write_back(path, persona_name, standing_part, timeline_lines)
            return

    new_line = _format_entry_line(entry)
    timeline_lines.append(new_line)
    _write_back(path, persona_name, standing_part, timeline_lines)


def _write_back(path: Path, persona_name: str, standing: str, timeline: Sequence[str]) -> None:
    lines: List[str] = [
        f"# {persona_name}的生活手账",
        "",
        "## 常驻状态与近期小结",
        standing or "- 目前运转正常。",
        "",
        "## 今日行动流水",
    ]
    lines.extend(timeline)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def get_active_personas() -> List[str]:
    """获取所有激活的人格列表。"""
    active: List[str] = []
    for name in persona_config_manager.list_available():
        cfg = persona_config_manager.get_config(name)
        if cfg.get_config("scope").data != "disabled":
            active.append(name)
    return active or ["default"]


async def record_journal_entry(
    entry: JournalEntry,
    *,
    target_personas: Sequence[str] | None = None,
    fanout_all_active: bool = False,
) -> None:
    """写入生活手账。若 fanout_all_active=True 则广播至所有激活人格。"""
    if fanout_all_active:
        targets = get_active_personas()
    elif target_personas:
        targets = list(target_personas)
    else:
        targets = ["default"]

    for name in targets:
        key = name.strip() or "default"
        async with _get_persona_lock(key):
            path = get_journal_path(key)
            _append_to_persona_journal(path, key, entry)
            _LAST_UPDATE_TS[key] = entry.timestamp


def read_journal_for_context(
    persona_name: str,
    *,
    current_group_id: str | None = None,
    since_ts: float = 0.0,
) -> Tuple[str, float]:
    """读取手账供上下文装配。

    since_ts == 0 时返回完整背景小结与今日行动流水（用于 history[0] 锚点）；
    since_ts > 0 时仅返回在此之后发生的增量流水（用于会话中途提醒）。
    """
    key = persona_name.strip() or "default"
    path = get_journal_path(key)
    if not path.exists():
        return "", 0.0
    content = path.read_text(encoding="utf-8")
    standing_part, timeline_lines = _read_sections(content)

    visible_lines: List[str] = []
    for line in timeline_lines:
        m = _LINE_PARSER.match(line)
        if m:
            gid = m.group("gid")
            # 隐私隔离：若指定了群，且本行标有其他群的标签，则过滤
            if gid and current_group_id and gid != current_group_id:
                continue
            visible_lines.append(line)

    file_mtime = path.stat().st_mtime
    latest_ts = _LAST_UPDATE_TS[key] if key in _LAST_UPDATE_TS else file_mtime

    if since_ts > 0.0:
        # 会话中途增量模式：仅当最新更新时间晚于锚点时间时输出最新一行
        if visible_lines and latest_ts > since_ts:
            from gsuid_core.ai_core.control.lines import journal_delta_line

            recent = visible_lines[-1]
            return journal_delta_line(recent.lstrip("- ")), latest_ts
        return "", latest_ts

    parts: List[str] = []
    if standing_part:
        parts.append(f"【常驻状态与近期小结】\n{standing_part}")
    if visible_lines:
        timeline_block = "\n".join(visible_lines[-_MAX_TIMELINE_ENTRIES:])
        parts.append(f"【今日行动流水】\n{timeline_block}")

    return "\n\n".join(parts), latest_ts


async def compact_journal(persona_name: str) -> bool:
    """自动整理手账：精炼旧流水合入常驻小结，裁切过长流水。"""
    key = persona_name.strip() or "default"
    async with _get_persona_lock(key):
        path = get_journal_path(key)
        if not path.exists():
            return False
        content = path.read_text(encoding="utf-8")
        standing_part, timeline_lines = _read_sections(content)
        if len(timeline_lines) <= _MAX_TIMELINE_ENTRIES:
            return False

        # 保留最近的新鲜流水，超出的旧流水合并至小结
        overflow = timeline_lines[:-_MAX_TIMELINE_ENTRIES]
        kept = timeline_lines[-_MAX_TIMELINE_ENTRIES:]
        tags = {line.split("|")[1].split("]")[0].strip() for line in overflow if "|" in line}
        addendum = f"\n- 前期完成了 {len(overflow)} 项日常事项（含 {', '.join(list(tags)[:4])} 等）。"
        new_standing = (standing_part + addendum)[:_DEFAULT_SUMMARY_MAX_CHARS]

        _write_back(path, key, new_standing, kept)
        return True
