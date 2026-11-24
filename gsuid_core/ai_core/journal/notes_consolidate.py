"""个人专题笔记库（autonomy/artifacts）离线聚类整编与冷归档清理。"""

from __future__ import annotations

import re
import json
import time
import hashlib
from typing import Sequence, TypedDict
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.ai_core.journal.storage import (
    ARCHIVE_RETENTION_MONTHS,
    _tz_dt,
    _append_jsonl_sync,
)
from gsuid_core.ai_core.journal.sanitizer import truncate_at_boundary
from gsuid_core.ai_core.configs.task_kinds import TASK_JOURNAL_COMPACT

MAX_ACTIVE_TOPIC_NOTES = 30
TOPIC_NOTE_MAX_CHARS = 3800
EPHEMERAL_NOTE_TTL_DAYS = 7
MAX_NOTE_CLUSTERS_PER_NIGHT = 2

_DATE_PREFIX_RE = re.compile(r"^(?:\d{4}[-_年]?\d{1,2}[-_月]?\d{0,2}|\d{1,2}月\d{1,2}日)")
_EPHEMERAL_TITLE_RE = re.compile(r"(?:速记|快照|日报|周报|临时记录)")
_NOTE_META_PREFIX_RE = re.compile(r"^(?:关于|有关|针对|围绕|日常|个人|近期|今日|本周)[_-]*")
_NOTE_META_SUFFIX_RE = re.compile(
    r"[_-]*(?:小抄|小笔记|小本子|(?:查证|核对|跟进|观察|调研|整理|汇总)?(?:笔记|小记|记录|纪要|总结|摘要|备忘)|调查|查证|核查|速记)(?:[_-]?\d+)?$"
)

_NOTE_MERGE_PROMPT = (
    "你正在整理自己的个人专题笔记库（Wiki Consolidation）。\n"
    "下面提供了同一主题下的多篇零散历史笔记（可能含已有的专题主稿）。\n"
    "请将它们去重整合为一篇结构清晰的最新专题笔记：\n"
    "1. 保留经过核实的事实数据、标准编号/出处日期、有效经验与避坑结论；\n"
    "2. 剔除重复寒暄、过期的单日临时臆测；若有前后矛盾，以较新且有明确出处的核查为准；\n"
    "3. 使用简洁的 Markdown 小标题与列表组织，总长度严格控制在 1500 字以内，保持角色自然口吻；\n"
    "4. 直接输出笔记正文，不要输出代码块外框或额外寒暄。"
)


class NoteArchiveRecord(TypedDict):
    archived_at: str
    title: str
    reason: str
    body: str
    scope_key: str
    owner_user_id: str


def _note_topic_stem(stem: str) -> str:
    """从散乱笔记标题提取归一化主题词干（纯结构剥离日期/引导前缀与笔记后缀，零业务词表）。"""
    s = _DATE_PREFIX_RE.sub("", stem).lstrip("_- ")
    s = _NOTE_META_PREFIX_RE.sub("", s).lstrip("_- ")
    candidate = _NOTE_META_SUFFIX_RE.sub("", s).strip("_- ")
    return candidate[:18] if len(candidate) >= 2 else ""


def _rule_merge_note_bodies(topic: str, docs: Sequence[tuple[str, str]]) -> str:
    """离线/降级时的确定性专题合并：保留各篇去重核心段并控制总长度。"""
    sections: list[str] = [f"# {topic}（整理专题）"]
    seen: set[str] = set()
    budget = TOPIC_NOTE_MAX_CHARS - 80
    used = 0
    for title, body in docs:
        snippet = truncate_at_boundary(body.strip(), max_chars=480)
        norm = re.sub(r"\s+", "", snippet)[:80]
        if not snippet or norm in seen:
            continue
        seen.add(norm)
        block = f"## {title}\n{snippet}"
        if used + len(block) > budget:
            break
        sections.append(block)
        used += len(block)
    return "\n\n".join(sections)


async def _llm_merge_notes(persona_name: str, topic: str, docs: Sequence[tuple[str, str]]) -> str | None:
    from gsuid_core.ai_core.configs.models import get_config_name_for_task
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data or not get_config_name_for_task("high", TASK_JOURNAL_COMPACT):
        return None
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.agent_errors import is_agent_error_text

    joined = "\n\n".join(f"### 原笔记：{title}\n{body[:1200]}" for title, body in docs[:8])
    user_prompt = f"专题名称：{topic}\n\n{joined}"
    agent = create_agent(
        system_prompt=_NOTE_MERGE_PROMPT,
        create_by="Journal_Compact",
        task_kind=TASK_JOURNAL_COMPACT,
        persona_name=persona_name,
        session_id=f"journal_note_wiki_{persona_name}_{int(time.time())}",
        is_subagent=True,
        task_level="high",
        dynamic_tools=False,
    )
    try:
        res = await agent.run(user_prompt, tools=[], return_mode="return", budget_gate=False)
    finally:
        agent._session_logger.close()
    text = res.strip() if isinstance(res, str) else ""
    if not text:
        return None
    if is_agent_error_text(text):
        logger.warning(t("log.ai.journal_compact_fail", persona_name=persona_name, e=text[:120]))
        return None
    return truncate_at_boundary(text, max_chars=TOPIC_NOTE_MAX_CHARS)


async def consolidate_persona_notes(
    persona_name: str,
    *,
    notes_dir: Path | None = None,
    now_ts: float | None = None,
    tz_name: str = "Asia/Shanghai",
    allow_llm: bool = True,
    max_llm_merges: int = MAX_NOTE_CLUSTERS_PER_NIGHT,
) -> tuple[int, int]:
    """Phase 3：按 scope 隔离整编个人笔记库（同 scope+同题才合并，严禁跨群/私聊混编）。"""
    from gsuid_core.ai_core.autonomy.tools import (
        artifact_dir,
        index_note_node,
        parse_note_scope_header,
    )
    from gsuid_core.ai_core.migrations.cross_group_isolation_v1 import ensure_isolation_watermark_ts

    clock = now_ts if now_ts is not None else time.time()
    folder = notes_dir if notes_dir is not None else artifact_dir(persona_name)
    if not folder.exists():
        return 0, 0

    now_local = _tz_dt(tz_name, clock)
    month_str = now_local.strftime("%Y-%m")
    raw_archive_file = folder / "archived_raw" / f"{month_str}.jsonl"
    archived_count = 0
    merged_topics = 0
    watermark_ts = await ensure_isolation_watermark_ts()

    all_files = sorted(
        [p for p in list(folder.glob("*.txt")) + list(folder.glob("*.md")) if p.is_file()],
        key=lambda p: p.stat().st_mtime,
    )
    if not all_files:
        return 0, 0

    async def _archive_and_unlink(p: Path, reason: str) -> None:
        nonlocal archived_count
        try:
            body = await to_thread(p.read_text)(encoding="utf-8")
        except OSError:
            return
        sk, uid = parse_note_scope_header(body)
        rec_obj: NoteArchiveRecord = {
            "archived_at": now_local.strftime("%Y-%m-%d %H:%M"),
            "title": p.stem,
            "reason": reason,
            "body": body,
            "scope_key": sk,
            "owner_user_id": uid,
        }
        await _append_jsonl_sync(raw_archive_file, json.dumps(rec_obj, ensure_ascii=False))
        try:
            await to_thread(p.unlink)(missing_ok=True)
            archived_count += 1
        except OSError:
            return

    # 1. 超过 7 天的单日时效快照归档下沉
    ephemeral_cutoff = clock - EPHEMERAL_NOTE_TTL_DAYS * 86400
    remaining: list[Path] = []
    for fpath in all_files:
        stem = fpath.stem
        if stem.startswith("topic_"):
            remaining.append(fpath)
            continue
        is_dated_ephemeral = _DATE_PREFIX_RE.match(stem) is not None or _EPHEMERAL_TITLE_RE.search(stem) is not None
        try:
            mtime = fpath.stat().st_mtime
        except OSError:
            continue
        if is_dated_ephemeral and mtime < ephemeral_cutoff:
            await _archive_and_unlink(fpath, "ephemeral_expired")
        else:
            remaining.append(fpath)

    # 2. 按 (scope_key, owner_user_id, topic_stem) 聚类，严禁跨群/私聊混编
    clusters: dict[tuple[str, str, str], list[Path]] = {}
    existing_topics: dict[tuple[str, str, str], Path] = {}
    for fpath in remaining:
        try:
            head_txt = await to_thread(fpath.read_text)(encoding="utf-8")
        except OSError:
            continue
        sk, uid = parse_note_scope_header(head_txt)
        if not sk and watermark_ts > 0.0:
            try:
                if fpath.stat().st_mtime < watermark_ts:
                    continue
            except OSError:
                continue
        if fpath.stem.startswith("topic_"):
            t_key = fpath.stem[len("topic_") :]
            if t_key:
                existing_topics[(sk, uid, t_key)] = fpath
            continue
        t_key = _note_topic_stem(fpath.stem)
        if t_key:
            ckey = (sk, uid, t_key)
            bucket = clusters[ckey] if ckey in clusters else []
            bucket.append(fpath)
            clusters[ckey] = bucket

    eligible: list[tuple[tuple[str, str, str], list[Path]]] = []
    for ckey, members in clusters.items():
        if len(members) >= 3 or (ckey in existing_topics and len(members) >= 1):
            eligible.append((ckey, members))
    eligible.sort(key=lambda item: len(item[1]), reverse=True)

    still_active_set = set(remaining)
    llm_merges_used = 0
    for ckey, members in eligible[:MAX_NOTE_CLUSTERS_PER_NIGHT]:
        c_scope, c_owner, t_key = ckey
        docs: list[tuple[str, str]] = []
        if ckey in existing_topics and existing_topics[ckey].exists():
            try:
                old_topic_body = await to_thread(existing_topics[ckey].read_text)(encoding="utf-8")
                docs.append((f"既有专题主稿_{t_key}", old_topic_body))
            except OSError:
                pass
        for m_path in members:
            try:
                m_body = await to_thread(m_path.read_text)(encoding="utf-8")
                docs.append((m_path.stem, m_body))
            except OSError:
                continue
        if len(docs) < 2:
            continue

        merged_body: str | None = None
        if allow_llm and llm_merges_used < max_llm_merges:
            llm_merges_used += 1
            merged_body = await _llm_merge_notes(persona_name, t_key, docs)
        if not merged_body:
            merged_body = _rule_merge_note_bodies(t_key, docs)

        if c_scope and not c_scope.startswith("self:"):
            shash = hashlib.sha1(f"{c_scope}|{c_owner}".encode("utf-8")).hexdigest()[:6]
            topic_stem = f"topic_{shash}_{t_key}"
        else:
            topic_stem = f"topic_{t_key}"
        topic_path = folder / f"{topic_stem}.md"
        await to_thread(topic_path.write_text)(merged_body, encoding="utf-8")
        await index_note_node(
            persona_name,
            topic_stem,
            merged_body,
            f"note:{persona_name}:{topic_stem}",
            scope_key=c_scope,
            owner_user_id=c_owner,
        )

        for m_path in members:
            await _archive_and_unlink(m_path, f"merged_into:{topic_stem}")
            still_active_set.discard(m_path)
        still_active_set.add(topic_path)
        merged_topics += 1

    # 3. 活跃文件总数硬封顶 MAX_ACTIVE_TOPIC_NOTES (LRU 淘汰最旧散稿)
    final_active = sorted(
        [p for p in still_active_set if p.exists()],
        key=lambda p: (1 if p.stem.startswith("topic_") else 0, p.stat().st_mtime),
    )
    if len(final_active) > MAX_ACTIVE_TOPIC_NOTES:
        overflow = len(final_active) - MAX_ACTIVE_TOPIC_NOTES
        for victim in final_active[:overflow]:
            await _archive_and_unlink(victim, "lru_capacity_cap")

    # 4. 清理超过 12 个月的笔记冷归档卷，防止 archived_raw 随年份无限增长
    raw_arch_dir = folder / "archived_raw"
    if raw_arch_dir.exists():
        min_ym = (now_local.year * 12 + now_local.month) - ARCHIVE_RETENTION_MONTHS
        for rfile in list(raw_arch_dir.glob("*.jsonl")):
            parts = rfile.stem.split("-")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                if int(parts[0]) * 12 + int(parts[1]) < min_ym:
                    await to_thread(rfile.unlink)(missing_ok=True)

    if merged_topics or archived_count:
        logger.info(
            t(
                "log.ai.journal_notes_consolidated",
                persona_name=persona_name,
                merged=merged_topics,
                archived=archived_count,
            )
        )
    return merged_topics, archived_count
