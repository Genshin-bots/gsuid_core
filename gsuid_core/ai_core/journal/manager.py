"""独立人格生活手账管理器（Per-Persona Self Journal）。

用于记录与检索 AI 人格在被动对话、主动巡检、长任务与自主行动中的真实经历。
遵循三层隐私隔离与前缀缓存设计。
"""

from __future__ import annotations

import re
import json
import time
import asyncio
from typing import Dict, List, Tuple, Optional, Sequence
from pathlib import Path
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import AI_CORE_PATH
from gsuid_core.ai_core.persona.config import (
    read_autonomy_runtime,
    is_within_active_hours,
    persona_config_manager,
)
from gsuid_core.ai_core.configs.task_kinds import TASK_JOURNAL_COMPACT

JOURNAL_DIR: Path = AI_CORE_PATH / "journals"
_JOURNAL_STATE_FILE: str = ".journal_state.json"
_JOB_ID: str = "ai_journal_hourly_compact"

_DEFAULT_SUMMARY_MAX_CHARS = 300
_MAX_TIMELINE_ENTRIES = 20
_LINE_PARSER = re.compile(
    r"^- \[(?P<time>[^\]|]+)(?: \| (?P<tag>[^\]]+))?\] (?P<text>.*?)"
    r"(?: \(群:(?P<gid>\d+)\))?(?: \(句柄: (?P<hid>[^)]+)\))?"
    r"(?: \(替:(?P<who>[^)]+)\))?(?: \((?P<cnt>×\d+)\))?$"
)
_JOURNAL_LOCKS: Dict[str, asyncio.Lock] = {}
_LAST_UPDATE_TS: Dict[str, float] = {}

_DREAM_SYSTEM_PROMPT = (
    "你正在进行夜间记忆整理（做梦与手账提炼）。\n"
    "根据提供的【原常驻状态与近期小结】和【今日行动流水】，提炼生成更新后的【常驻状态与近期小结】。\n"
    "要求：\n"
    "1. 严格使用第一人称或角色自称，以简洁的 Markdown 无序列表（`- ...`）输出 1~3 条小结。\n"
    "2. 总结当前处于持续中的重要工作状态、责任职责、近期习惯偏好或关键发现，淘汰已失效的琐碎细节。\n"
    "3. 严禁出现机械技术标记（如句柄 sa_xxx、群号 群:xxx、工具名 search_stock、API等）。\n"
    "4. 总长度严格控制在 250 字以内，语言自然亲切。\n"
    "5. 仅输出以 `- ` 开头的列表项，严禁输出任何标题、前言或额外解释。"
)


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
    requested_by: str = ""  # 触发该工具条目的说话人 uid（§4.7 F15）


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
    if entry.requested_by:
        parts.append(f" (替:{entry.requested_by})")
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
    turn_target_uids: Sequence[str] | None = None,
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

            # 替本轮目标以外的人跑的工具条目不注入（§4.7 F15）。
            for recent in reversed(visible_lines):
                m = _LINE_PARSER.match(recent)
                who = m.group("who") if m else None
                if who and turn_target_uids is not None and who not in turn_target_uids:
                    continue
                return journal_delta_line(recent.lstrip("- ")), latest_ts
        return "", latest_ts

    parts: List[str] = []
    if standing_part:
        parts.append(f"【常驻状态与近期小结】\n{standing_part}")
    if visible_lines:
        timeline_block = "\n".join(visible_lines[-_MAX_TIMELINE_ENTRIES:])
        parts.append(f"【今日行动流水】\n{timeline_block}")

    return "\n\n".join(parts), latest_ts


def _get_journal_state_path() -> Path:
    return _ensure_journal_dir() / _JOURNAL_STATE_FILE


def _load_journal_state() -> dict[str, float]:
    path = _get_journal_state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    res: dict[str, float] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, (int, float)):
            res[k] = float(v)
    return res


def _save_last_compact_ts(persona_name: str, ts: float) -> None:
    state = _load_journal_state()
    key = persona_name.strip() or "default"
    state[key] = ts
    path = _get_journal_state_path()
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning(t("log.ai.journal_compact_save_state_fail", e=e))


def get_last_compact_ts(persona_name: str) -> float:
    """获取指定人格上次记忆整理的时间戳（未整理过返回 0.0）。"""
    key = persona_name.strip() or "default"
    state = _load_journal_state()
    if key in state:
        return state[key]
    return 0.0


def get_all_journal_personas() -> list[str]:
    """获取所有需要维护手账的人格列表（包含激活人格与磁盘已有手账的人格）。"""
    names = set(get_active_personas())
    if JOURNAL_DIR.exists():
        for file in JOURNAL_DIR.glob("*.md"):
            if file.stem and not file.name.startswith("."):
                names.add(file.stem)
    return sorted(names)


def _rule_based_summary(standing_part: str, timeline_lines: Sequence[str]) -> str:
    """规则备用提炼：当大模型不可用时保证手账精炼收敛。"""
    tags: list[str] = []
    for line in timeline_lines:
        m = _LINE_PARSER.match(line)
        if m:
            tag = m.group("tag")
            if tag:
                tags.append(tag)
    unique_tags = list(dict.fromkeys(tags))
    tag_str = f"（含 {', '.join(unique_tags[:4])} 等）" if unique_tags else ""
    addendum = f"- 前期完成了 {len(timeline_lines)} 项日常与委派事务{tag_str}。"

    if not standing_part or standing_part.strip() == "- 目前一切正常，日常处理群聊交互与各项委派。":
        return f"{addendum}\n- 目前各项日常事务正常推进中。"
    base_lines = [line.strip() for line in standing_part.splitlines() if line.strip().startswith("-")]
    base = base_lines[0] if base_lines else "- 目前日常运转正常。"
    combined = f"{base}\n{addendum}"
    return combined[:_DEFAULT_SUMMARY_MAX_CHARS]


async def _summarize_journal_with_llm(
    persona_name: str,
    standing_part: str,
    timeline_lines: Sequence[str],
) -> Optional[str]:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return None

    from gsuid_core.ai_core.utils import is_agent_error_text
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.persona.resource import load_persona, extract_compact_persona

    card = ""
    try:
        raw_card = await load_persona(persona_name)
        card = extract_compact_persona(raw_card) or raw_card
    except Exception:
        card = ""

    system_prompt = f"{card}\n\n{_DREAM_SYSTEM_PROMPT}" if card else _DREAM_SYSTEM_PROMPT
    timeline_block = "\n".join(timeline_lines[-100:])
    user_prompt = f"【原常驻状态与近期小结】\n{standing_part or '（暂无）'}\n\n【今日行动流水】\n{timeline_block}"

    agent = create_agent(
        system_prompt=system_prompt,
        create_by="Journal_Compact",
        task_kind=TASK_JOURNAL_COMPACT,
        persona_name=persona_name,
        session_id=f"journal_compact_{persona_name}_{int(time.time())}",
        is_subagent=True,
        task_level="low",
        dynamic_tools=False,
    )
    try:
        result = await agent.run(user_prompt, tools=[], return_mode="return", budget_gate=False)
    except Exception as e:
        logger.warning(t("log.ai.journal_compact_fail", persona_name=persona_name, e=e))
        return None
    finally:
        agent._session_logger.close()

    text = result if isinstance(result, str) else ""
    if not text or is_agent_error_text(text):
        return None

    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("【"):
            continue
        if stripped.startswith(("-", "*", "•")):
            lines.append(f"- {stripped.lstrip('-*• ')}")
        else:
            lines.append(f"- {stripped}")

    if not lines:
        return None

    return "\n".join(lines[:3])[:_DEFAULT_SUMMARY_MAX_CHARS]


async def compact_journal(persona_name: str, *, force: bool = False) -> bool:
    """自动整理手账：精炼旧流水合入常驻小结，清空今日行动流水。"""
    key = persona_name.strip() or "default"
    async with _get_persona_lock(key):
        path = get_journal_path(key)
        if not path.exists():
            return False
        content = path.read_text(encoding="utf-8")
        standing_part, timeline_lines = _read_sections(content)
        if not timeline_lines:
            return False

        llm_summary = await _summarize_journal_with_llm(key, standing_part, timeline_lines)
        new_standing = llm_summary or _rule_based_summary(standing_part, timeline_lines)

        _write_back(path, key, new_standing, [])
        now_ts = time.time()
        _LAST_UPDATE_TS[key] = now_ts
        _save_last_compact_ts(key, now_ts)
        logger.info(t("log.ai.journal_compact_success", persona_name=key, count=len(timeline_lines)))
        return True


async def check_and_compact_all_journals() -> None:
    """每小时定时巡检：检查不在活动时间内且 24h 内未整理的人格，执行做梦整理。"""
    now = time.time()
    for persona_name in get_all_journal_personas():
        key = persona_name.strip() or "default"
        cfg = persona_config_manager.get_config(key)
        runtime = read_autonomy_runtime(cfg)
        within_active = is_within_active_hours(list(runtime.active_hours), runtime.timezone)
        if within_active:
            continue

        last_compact = get_last_compact_ts(key)
        if last_compact > 0.0 and (now - last_compact) < 86400.0:
            continue

        try:
            await compact_journal(key)
        except Exception as e:
            logger.exception(t("log.ai.journal_compact_fail", persona_name=key, e=e))


async def init_journal_scheduler() -> None:
    """注册每小时手账整理定时任务。"""
    from gsuid_core.aps import scheduler

    scheduler.add_job(
        func=check_and_compact_all_journals,
        trigger="interval",
        hours=1,
        id=_JOB_ID,
        name="生活手账定时整理",
        replace_existing=True,
    )
    logger.info(t("log.ai.journal_scheduler_start"))


async def stop_journal_scheduler() -> None:
    """停止生活手账定时整理任务。"""
    from gsuid_core.aps import scheduler

    job = scheduler.get_job(_JOB_ID)
    if job is not None:
        scheduler.remove_job(_JOB_ID)
