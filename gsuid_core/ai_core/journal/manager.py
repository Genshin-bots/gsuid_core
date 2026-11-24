"""独立人格分片式生活手账读写视图与旧版迁移接口。"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Tuple, Sequence
from pathlib import Path
from datetime import datetime as _dt

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.ai_core.journal import storage
from gsuid_core.ai_core.journal.dream import ArchiveRecord, read_journal_archive_sync
from gsuid_core.ai_core.persona.config import (
    is_persona_enabled,
    persona_config_manager,
)
from gsuid_core.ai_core.journal.storage import (
    _CACHE_LOCK,
    _STATE_LOCK,
    SELF_SHARD_ID,
    _ARCHIVE_DATE_RE,
    _RECENT_ENTRY_TS,
    _SHARD_UPDATE_TS,
    SILENT_SCAN_WINDOW,
    DELTA_LINE_MAX_CHARS,
    _ARCHIVE_MONTH_STEM_RE,
    ANCHOR_TOTAL_MAX_CHARS,
    SELF_TIMELINE_RING_CAP,
    GROUP_TIMELINE_RING_CAP,
    ANCHOR_RECENT_TIMELINE_CAP,
    JournalEntry,
    ShardDocument,
    UserShardPreview,
    GroupShardPreview,
    ParsedTimelineLine,
    PersonaJournalStructured,
    _safe_token,
    _archives_dir,
    _persona_root,
    _user_shard_id,
    _get_shard_lock,
    _load_shard_sync,
    _save_shard_sync,
    _self_shard_path,
    _group_shard_path,
    _empty_state_entry,
    _ensure_journal_dir,
    get_active_personas,
    parse_timeline_line,
    parse_shard_markdown,
    render_shard_markdown,
    _is_placeholder_bullet,
    _mark_shard_dirty_sync,
    _list_archive_months_sync,
    _list_user_shard_ids_sync,
    _load_full_state_unlocked,
    _save_full_state_unlocked,
    _list_group_shard_ids_sync,
    _migrate_legacy_root_file_sync,
)
from gsuid_core.ai_core.journal.sanitizer import (
    SILENT_CHECK_SUMMARY,
    clean_journal_tag,
    is_silent_payload,
    truncate_at_boundary,
    is_rejected_journal_body,
    sanitize_journal_summary,
)


def get_journal_path(persona_name: str, group_id: str = "") -> Path:
    """获取指定人格（及可选群分片）的主手账路径；首次访问自动迁移旧版单文件。"""
    _migrate_legacy_root_file_sync(persona_name)
    if group_id.strip():
        return _group_shard_path(persona_name, group_id.strip())
    return _self_shard_path(persona_name)


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
            if is_persona_enabled(cfg):
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
    if entry.group_id and entry.group_id.strip():
        shard_id = entry.group_id.strip()
    elif entry.requested_by and entry.requested_by.strip():
        # 私聊流水独立落盘到 users/{uid}.md，严禁混入公共 self.md
        shard_id = _user_shard_id(entry.requested_by.strip())
    else:
        shard_id = SELF_SHARD_ID
    for name in targets:
        key = _safe_token(name)
        async with _get_shard_lock(key, shard_id):
            try:
                await _write_entry_sync(key, shard_id, entry)
            except OSError as e:
                logger.warning(t("log.ai.journal_compact_save_state_fail", e=e))


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


def _line_end_minutes(line: str) -> int | None:
    parsed = parse_timeline_line(line)
    if parsed is None:
        return None
    end_hhmm = parsed.time_str.split("~")[-1].strip()
    if len(end_hhmm) == 5 and end_hhmm[2] == ":" and end_hhmm[:2].isdigit() and end_hhmm[3:].isdigit():
        return int(end_hhmm[:2]) * 60 + int(end_hhmm[3:])
    return None


def _is_future_timeline_line(line: str, today_label: str, now_ts: float) -> bool:
    """检查行时间套上 today_label 后是否晚于当前墙上时钟超 5 分钟（跨日残留旧行）。"""
    if len(today_label) != 10:
        return False
    parsed = parse_timeline_line(line)
    if parsed is None:
        return False
    end_hhmm = parsed.time_str.split("~")[-1].strip()
    if len(end_hhmm) != 5 or end_hhmm[2] != ":":
        return False
    try:
        line_min_ts = _dt.strptime(f"{today_label} {end_hhmm}", "%Y-%m-%d %H:%M").timestamp()
    except ValueError:
        return False
    return line_min_ts > now_ts + 300.0


def _today_timeline_tail(lines: Sequence[str], *, today_label: str = "") -> List[str]:
    """仅保留末段当日有效流水：遇跨日回跳（前条比后条晚超 60 分钟）截断，并剔除未来时间戳残留。"""
    if not lines:
        return []
    kept: List[str] = []
    later_min: int | None = None
    now_ts = time.time()
    for ln in reversed(lines):
        cur_min = _line_end_minutes(ln)
        if cur_min is not None:
            if later_min is not None and cur_min > later_min + 60:
                break
            later_min = cur_min
        if today_label and _is_future_timeline_line(ln, today_label, now_ts):
            continue
        kept.append(ln)
    kept.reverse()
    return kept


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
                # 晚于当前墙上时钟超 5 分钟必为跨日残留旧流水，避免套上今日日期后恒判为最新
                if line_min_ts > time.time() + 300.0:
                    return False
                return line_min_ts >= (int(since_ts) // 60) * 60
            except ValueError:
                pass
    return True


def _self_timeline_line_visible(ln: str, *, gid: str, uid: str) -> bool:
    """判定 self.md 中单行流水的隐私可见性：他群行与私聊 (替:uid) 绝不跨群或跨人泄露。"""
    parsed = parse_timeline_line(ln)
    if parsed is None:
        return True
    if parsed.gid:
        if gid:
            return parsed.gid == gid
        return bool(uid) and parsed.who == uid
    if parsed.who:
        if gid:
            return False
        return bool(uid) and parsed.who == uid
    return True


def read_journal_for_context(
    persona_name: str,
    *,
    current_group_id: str | None = None,
    current_user_id: str | None = None,
    since_ts: float = 0.0,
    turn_target_uids: Sequence[str] | None = None,
) -> Tuple[str, float]:
    """按分片读取生活手账供上下文装配（物理级群隔离 + O(1) 严格 ≤880 字锚点预算）。"""
    key = _safe_token(persona_name)
    gid = (current_group_id or "").strip()
    uid = (current_user_id or "").strip()
    if not uid and turn_target_uids and len(turn_target_uids) == 1:
        uid = (turn_target_uids[0] or "").strip()

    user_sid = _user_shard_id(uid) if (not gid and uid) else ""

    # 极速增量短路：若本会话关联的分片内存时间戳均未推进且已缓存，零磁盘 I/O 直接返回
    if since_ts > 0.0:
        with _CACHE_LOCK:
            ts_self_mem = _SHARD_UPDATE_TS[(key, SELF_SHARD_ID)] if (key, SELF_SHARD_ID) in _SHARD_UPDATE_TS else -1.0
            ts_grp_mem = _SHARD_UPDATE_TS[(key, gid)] if (gid and (key, gid) in _SHARD_UPDATE_TS) else -1.0
            ts_usr_mem = (
                _SHARD_UPDATE_TS[(key, user_sid)] if (user_sid and (key, user_sid) in _SHARD_UPDATE_TS) else -1.0
            )
        if ts_self_mem > 0.0 and (not gid or ts_grp_mem > 0.0) and (not user_sid or ts_usr_mem > 0.0):
            best_mem = max(ts_self_mem, ts_grp_mem, ts_usr_mem)
            if best_mem <= since_ts:
                return "", best_mem

    self_doc = _load_shard_sync(key, SELF_SHARD_ID, create_if_missing=False)
    grp_doc = _load_shard_sync(key, gid, create_if_missing=False) if gid else None
    usr_doc = _load_shard_sync(key, user_sid, create_if_missing=False) if user_sid else None

    # 过滤 self.md 中的 (群:xxx) 与私聊 (替:uid) 行，严防跨群与跨人泄露
    self_visible_timeline: List[str] = []
    if self_doc is not None:
        for ln in self_doc.timeline:
            if _self_timeline_line_visible(ln, gid=gid, uid=uid):
                self_visible_timeline.append(ln)

    ts_self = _latest_ts_for_scope(key, SELF_SHARD_ID, self_doc, _persona_root(key) / "self.md")
    ts_grp = (
        _latest_ts_for_scope(key, gid, grp_doc, _persona_root(key) / "groups" / f"{_safe_token(gid, 'group')}.md")
        if gid
        else 0.0
    )
    ts_usr = (
        _latest_ts_for_scope(key, user_sid, usr_doc, _persona_root(key) / "users" / f"{_safe_token(uid, 'user')}.md")
        if user_sid
        else 0.0
    )
    latest_ts = max(ts_self, ts_grp, ts_usr)

    if since_ts > 0.0:
        if latest_ts <= since_ts:
            return "", latest_ts
        from gsuid_core.ai_core.control.lines import journal_delta_line

        candidates: List[Tuple[str, str, str]] = []
        if grp_doc is not None and ts_grp > since_ts:
            for ln in _today_timeline_tail(grp_doc.timeline, today_label=grp_doc.today_label):
                candidates.append((gid, grp_doc.today_label, ln))
        if usr_doc is not None and ts_usr > since_ts:
            for ln in _today_timeline_tail(usr_doc.timeline, today_label=usr_doc.today_label):
                candidates.append((user_sid, usr_doc.today_label, ln))
        if self_doc is not None and self_visible_timeline and ts_self > since_ts:
            for ln in _today_timeline_tail(self_visible_timeline, today_label=self_doc.today_label):
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
    elif usr_doc is not None:
        usr_standing = [
            x for x in [*usr_doc.persistent[:2], *usr_doc.monthly[:3]] if x.strip() and not _is_placeholder_bullet(x)
        ]
        if usr_standing:
            blocks.append("【私聊默契与近期主线】\n" + "\n".join(usr_standing))
        usr_yest = [x for x in usr_doc.yesterday[:3] if x.strip() and not _is_placeholder_bullet(x)]
        if usr_yest:
            blocks.append("【私聊昨日记忆】\n" + "\n".join(usr_yest))

    combined_timeline: List[str] = []
    if self_doc is not None and self_visible_timeline:
        combined_timeline.extend(_today_timeline_tail(self_visible_timeline, today_label=self_doc.today_label)[-3:])
    if usr_doc is not None and usr_doc.timeline:
        combined_timeline.extend(_today_timeline_tail(usr_doc.timeline, today_label=usr_doc.today_label))
    if grp_doc is not None and grp_doc.timeline:
        combined_timeline.extend(_today_timeline_tail(grp_doc.timeline, today_label=grp_doc.today_label))

    if combined_timeline:
        tl_text = _compact_timeline_for_anchor(combined_timeline, recent_cap=ANCHOR_RECENT_TIMELINE_CAP)
        if tl_text:
            blocks.append(f"【今日行动流水】\n{tl_text}")

    full_text = "\n\n".join(blocks).strip()
    if len(full_text) > ANCHOR_TOTAL_MAX_CHARS:
        full_text = truncate_at_boundary(full_text, max_chars=ANCHOR_TOTAL_MAX_CHARS)
    return full_text, latest_ts


def _archive_single_legacy_self_sync(persona_name: str, now_ts: float) -> bool:
    """无损归档旧版 self.md 并清空可能含跨群任务的活跃槽位；幂等受 schema_version>=2 保护。"""
    from gsuid_core.ai_core.migrations.cross_group_isolation_v1 import get_isolation_watermark_ts

    key = _safe_token(persona_name)
    with _STATE_LOCK:
        state = _load_full_state_unlocked()
        has_state = key in state
        if has_state and state[key]["schema_version"] >= 2:
            return False

    legacy_root_path = storage.JOURNAL_DIR / f"{key}.md"
    migrated_from_v0 = legacy_root_path.exists() and legacy_root_path.is_file()
    if migrated_from_v0:
        _migrate_legacy_root_file_sync(persona_name)

    self_path = _self_shard_path(persona_name)
    watermark_ts = get_isolation_watermark_ts()
    # 升级后新建人格（非 v0 迁入且 self.md 晚于水位线）仅含隔离后内容，直接标 v2 不清库
    if (
        not migrated_from_v0
        and not has_state
        and watermark_ts > 0.0
        and self_path.exists()
        and self_path.stat().st_mtime > watermark_ts
    ):
        with _STATE_LOCK:
            state = _load_full_state_unlocked()
            entry = state[key] if key in state else _empty_state_entry(schema_version=2)
            entry["schema_version"] = 2
            state[key] = entry
            _save_full_state_unlocked(state)
        return False

    archived = False
    if self_path.exists() and self_path.is_file():
        raw = self_path.read_text(encoding="utf-8")
        doc = parse_shard_markdown(raw, SELF_SHARD_ID)
        legacy_lines: list[str] = []
        for item in [*doc.persistent, *doc.monthly, *doc.yesterday, *doc.timeline]:
            cleaned = item.strip()
            if cleaned and not _is_placeholder_bullet(cleaned):
                legacy_lines.append(cleaned)
        if legacy_lines:
            # 1) 保留原始备份 2) 写独立 legacy_v1 归档卷（不被 read_handle 回读）3) 清空活跃槽位
            bak_path = _persona_root(persona_name) / "self.legacy_v1.bak"
            if not bak_path.exists():
                bak_path.write_text(raw, encoding="utf-8")
            adir = _archives_dir(persona_name)
            adir.mkdir(parents=True, exist_ok=True)
            month_key = _dt.fromtimestamp(now_ts).strftime("%Y-%m")
            date_str = _dt.fromtimestamp(now_ts).strftime("%Y-%m-%d")
            rec = {
                "date": date_str,
                "shard": SELF_SHARD_ID,
                "kind": "legacy_self_v1_archive",
                "entries": legacy_lines,
                "ts": now_ts,
            }
            with (adir / f"legacy_v1_{month_key}.jsonl").open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
            clean_doc = ShardDocument(
                shard_id=SELF_SHARD_ID,
                month_label=doc.month_label,
                yesterday_label=doc.yesterday_label,
                today_label=doc.today_label,
            )
            _save_shard_sync(persona_name, clean_doc, bump_ts=True, ts_val=now_ts)
            archived = True

    with _STATE_LOCK:
        state = _load_full_state_unlocked()
        entry = state[key] if key in state else _empty_state_entry()
        entry["schema_version"] = 2
        state[key] = entry
        _save_full_state_unlocked(state)
    return archived


def _discover_persona_names_sync() -> list[str]:
    names: list[str] = []
    jdir = _ensure_journal_dir()
    personas_root = jdir / "personas"
    if personas_root.exists():
        for child in sorted(personas_root.iterdir()):
            if child.is_dir() and child.name and child.name not in names:
                names.append(child.name)
    for legacy_file in sorted(jdir.glob("*.md")):
        if legacy_file.is_file() and legacy_file.stem and legacy_file.stem not in names:
            names.append(legacy_file.stem)
    return names


async def archive_and_reset_legacy_self_shards() -> list[str]:
    """扫描所有已有手账目录及旧版单文件的人格，加分片锁执行零删除无损迁移。"""
    now_ts = time.time()
    names = await to_thread(_discover_persona_names_sync)()
    archived_names: list[str] = []
    for pname in names:
        async with _get_shard_lock(pname, SELF_SHARD_ID):
            if await to_thread(_archive_single_legacy_self_sync)(pname, now_ts):
                archived_names.append(pname)
    return archived_names


def _non_silent_lines(lines: Sequence[str]) -> List[str]:
    out: List[str] = []
    for ln in lines:
        parsed = parse_timeline_line(ln)
        if parsed is None or not parsed.is_silent:
            out.append(ln)
    return out


def read_life_journal_view(
    persona_name: str,
    *,
    group_id: str | None = None,
    user_id: str = "",
    recent_limit: int = 12,
    allowed_group_ids: Sequence[str] | None = None,
) -> str:
    """按严格隐私边界读取实时生活手账：群聊看 self+本群，私聊看 self+跨群替本人办的事。"""
    key = _safe_token(persona_name)
    gid = (group_id or "").strip()
    uid = (user_id or "").strip()
    cap = max(4, min(recent_limit, 24))

    self_doc = _load_shard_sync(key, SELF_SHARD_ID, create_if_missing=False)
    sections: List[str] = []
    if self_doc is not None:
        self_parts: List[str] = []
        pers = [x for x in self_doc.persistent if x.strip() and not _is_placeholder_bullet(x)]
        mon = [x for x in self_doc.monthly if x.strip() and not _is_placeholder_bullet(x)]
        yest = [x for x in self_doc.yesterday if x.strip() and not _is_placeholder_bullet(x)]
        tl_raw = _non_silent_lines(self_doc.timeline)
        tl = [ln for ln in tl_raw if _self_timeline_line_visible(ln, gid=gid, uid=uid)]
        if pers:
            self_parts.append("【持久自我认知】\n" + "\n".join(pers))
        if mon:
            self_parts.append("【近期个人探索与习惯】\n" + "\n".join(mon))
        if yest:
            self_parts.append("【昨日自主回顾】\n" + "\n".join(yest))
        if tl:
            self_parts.append("【今日个人探索流水】\n" + "\n".join(tl[-cap:]))
        if self_parts:
            sections.append("== 个人生活手账 ==\n" + "\n\n".join(self_parts))

    if gid:
        grp_doc = _load_shard_sync(key, gid, create_if_missing=False)
        if grp_doc is not None:
            grp_parts: List[str] = []
            pers = [x for x in grp_doc.persistent if x.strip() and not _is_placeholder_bullet(x)]
            mon = [x for x in grp_doc.monthly if x.strip() and not _is_placeholder_bullet(x)]
            yest = [x for x in grp_doc.yesterday if x.strip() and not _is_placeholder_bullet(x)]
            tl = _non_silent_lines(grp_doc.timeline)
            if pers:
                grp_parts.append("【本群长期默契】\n" + "\n".join(pers))
            if mon:
                grp_parts.append("【本群当月主线】\n" + "\n".join(mon))
            if yest:
                grp_parts.append("【本群昨日记忆】\n" + "\n".join(yest))
            if tl:
                grp_parts.append("【本群今日行动流水】\n" + "\n".join(tl[-cap:]))
            if grp_parts:
                sections.append(f"== 本群驻群手账 (群:{gid}) ==\n" + "\n\n".join(grp_parts))
    elif uid:
        # 1) 读本人独立私聊分片 users/{uid}.md（完整展示四层记忆）
        usr_doc = _load_shard_sync(key, _user_shard_id(uid), create_if_missing=False)
        if usr_doc is not None:
            usr_parts: List[str] = []
            u_pers = [x for x in usr_doc.persistent if x.strip() and not _is_placeholder_bullet(x)]
            u_mon = [x for x in usr_doc.monthly if x.strip() and not _is_placeholder_bullet(x)]
            u_yest = [x for x in usr_doc.yesterday if x.strip() and not _is_placeholder_bullet(x)]
            u_tl = _non_silent_lines(usr_doc.timeline)
            if u_pers:
                usr_parts.append("【私聊长期默契】\n" + "\n".join(u_pers))
            if u_mon:
                usr_parts.append("【私聊当月主线】\n" + "\n".join(u_mon))
            if u_yest:
                usr_parts.append("【私聊昨日记忆】\n" + "\n".join(u_yest))
            if u_tl:
                usr_parts.append("【私聊今日行动流水】\n" + "\n".join(u_tl[-cap:]))
            if usr_parts:
                sections.append("== 私聊手账 ==\n" + "\n\n".join(usr_parts))
        # 2) 私聊跨群聚合：allowed_group_ids 非空时按所在群剪枝，为空则安全扫全部分片并严格按 parsed.who==uid 过滤
        personal_rows: List[tuple[str, str, str]] = []
        allowed_set = (
            {g.strip() for g in allowed_group_ids if g and g.strip()} if allowed_group_ids is not None else None
        )
        shard_ids = _list_group_shard_ids_sync(key)
        if allowed_set:
            shard_ids = [g_shard for g_shard in shard_ids if g_shard in allowed_set]
        for g_shard in shard_ids:
            gdoc = _load_shard_sync(key, g_shard, create_if_missing=False)
            if gdoc is None:
                continue
            doc_date = gdoc.today_label or ""
            for ln in gdoc.timeline:
                parsed = parse_timeline_line(ln)
                if parsed is None or parsed.is_silent:
                    continue
                if parsed.who == uid:
                    personal_rows.append((doc_date, parsed.time_str, f"[群:{g_shard}] {ln.lstrip('- ').strip()}"))
        if personal_rows:
            personal_rows.sort(key=lambda r: (r[0], r[1]))
            sections.append("== 近期跨群替你办过的事 ==\n" + "\n".join(r[2] for r in personal_rows[-cap:]))

    if not sections:
        return "目前生活手账里还没有新的记录。"
    return "\n\n".join(sections)


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
    users_out: List[UserShardPreview] = []
    for uid in _list_user_shard_ids_sync(persona_name)[:32]:
        usid = _user_shard_id(uid)
        udoc = _load_shard_sync(persona_name, usid, create_if_missing=False)
        if udoc is None:
            continue
        users_out.append(
            {
                "user_id": uid,
                "shard_id": usid,
                "content": render_shard_markdown(persona_name, udoc),
                "persistent": [x for x in udoc.persistent if not _is_placeholder_bullet(x)],
                "monthly": [x for x in udoc.monthly if not _is_placeholder_bullet(x)],
                "yesterday": [x for x in udoc.yesterday if not _is_placeholder_bullet(x)],
                "month_label": udoc.month_label,
                "yesterday_label": udoc.yesterday_label,
                "timeline": list(udoc.timeline),
                "timeline_count": len(udoc.timeline),
            }
        )
    months = _list_archive_months_sync(persona_name)
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
        "user_shards": users_out,
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


def get_journal_archive_records(
    persona_name: str,
    *,
    month: str = "",
    shard_id: str = "",
    date: str = "",
) -> Tuple[List[ArchiveRecord], List[str], str]:
    """供 WebConsole 按月/分片/日期读取无损 JSONL 归档流水（含昨日流水与历史冷归档）。"""
    adir = _archives_dir(persona_name)
    available = _list_archive_months_sync(persona_name)
    if not available:
        return [], [], ""

    req_month = month.strip()
    req_date = date.strip()
    req_shard = shard_id.strip()

    if req_date and _ARCHIVE_DATE_RE.match(req_date) is None:
        return [], available, ""
    if req_month and _ARCHIVE_MONTH_STEM_RE.match(req_month) is None:
        return [], available, ""

    target_month = req_month
    if not target_month:
        target_month = req_date[:7] if req_date else available[0]

    target_file = adir / f"{target_month}.jsonl"
    if not target_file.exists():
        return [], available, target_month

    records: List[ArchiveRecord] = []
    with target_file.open("r", encoding="utf-8", errors="replace") as fp:
        for raw_line in fp:
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            rec_date = parsed["date"] if "date" in parsed and isinstance(parsed["date"], str) else ""
            rec_shard = parsed["shard"] if "shard" in parsed and isinstance(parsed["shard"], str) else ""
            rec_kind = parsed["kind"] if "kind" in parsed and isinstance(parsed["kind"], str) else "daily"
            raw_entries = parsed["entries"] if "entries" in parsed and isinstance(parsed["entries"], list) else []
            entries = [str(item) for item in raw_entries if isinstance(item, str) and item.strip()]
            raw_ts = parsed["ts"] if "ts" in parsed else 0.0
            rec_ts = float(raw_ts) if isinstance(raw_ts, (int, float)) else 0.0
            if not entries:
                continue
            if req_shard and rec_shard != req_shard:
                continue
            if req_date and rec_date != req_date:
                continue
            records.append(
                {
                    "date": rec_date,
                    "shard": rec_shard,
                    "kind": rec_kind,
                    "entries": entries,
                    "ts": rec_ts,
                }
            )
    records.sort(key=lambda r: (r["date"], r["ts"]), reverse=True)
    return records, available, target_month


async def read_archived_journal_handle(
    persona_name: str,
    shard_id: str,
    date_or_month: str,
) -> str:
    """供 read_handle 读取指定分片的月度/单日无损归档。"""
    return await read_journal_archive_sync(_archives_dir(persona_name), shard_id, date_or_month)
