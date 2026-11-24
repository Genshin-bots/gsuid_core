"""离线做梦引擎（Sleep-Time Compute / Dreaming）。

执行四步夜间记忆巩固：
1. 系统真值对账（AIAgentTask / AIAutonomyActivity，消除僵尸未完结幻觉）；
2. 按月单文件无损 JSONL 归档 + 12 个月 TTL 与 60 天休眠群自动维护；
3. 个人笔记库（autonomy/artifacts）同题聚类合并为 topic_*.md（上限 30 篇）；
4. 三级成本阀门驱动的 high 档多尺度覆写提炼（持久 / 当月 / 昨日）。
"""

from __future__ import annotations

import re
import json
import time
from typing import TYPE_CHECKING, Dict, List, Tuple, Literal, Sequence, TypedDict
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.ai_core.persona.config import persona_config_manager
from gsuid_core.ai_core.journal.storage import (
    SELF_SHARD_ID,
    SELF_MONTHLY_MAX_CHARS,
    SELF_MONTHLY_MAX_ITEMS,
    SELF_TIMELINE_RING_CAP,
    GROUP_MONTHLY_MAX_CHARS,
    GROUP_MONTHLY_MAX_ITEMS,
    GROUP_TIMELINE_RING_CAP,
    ARCHIVE_RETENTION_MONTHS,
    SELF_YESTERDAY_MAX_CHARS,
    SELF_YESTERDAY_MAX_ITEMS,
    GROUP_YESTERDAY_MAX_CHARS,
    GROUP_YESTERDAY_MAX_ITEMS,
    SELF_PERSISTENT_MAX_CHARS,
    SELF_PERSISTENT_MAX_ITEMS,
    GROUP_PERSISTENT_MAX_CHARS,
    GROUP_PERSISTENT_MAX_ITEMS,
    _tz_dt,
    _safe_token,
    _archives_dir,
    _persona_root,
    _user_shard_id,
    _get_shard_lock,
    cap_bullet_slot,
    _load_full_state,
    _load_shard_sync,
    _save_shard_sync,
    _append_jsonl_sync,
    _empty_state_entry,
    parse_timeline_line,
    _is_placeholder_bullet,
    get_all_journal_personas,
    _list_user_shard_ids_sync,
    _commit_compact_state_sync,
    _list_group_shard_ids_sync,
    _migrate_legacy_root_file_sync,
)
from gsuid_core.ai_core.journal.sanitizer import truncate_at_boundary
from gsuid_core.ai_core.configs.task_kinds import TASK_JOURNAL_COMPACT
from gsuid_core.ai_core.persona.config_rules import read_autonomy_runtime, is_within_active_hours
from gsuid_core.ai_core.journal.notes_consolidate import (
    MAX_NOTE_CLUSTERS_PER_NIGHT,
    consolidate_persona_notes,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.journal.storage import ShardDocument

DREAM_HEAVY_MIN_ENTRIES = 3
DREAM_LIGHT_CARRY_MAX = 3
DREAM_MAX_HEAVY_SHARDS_PER_NIGHT = 20
DORMANT_GROUP_DAYS = 60

_ARCHIVE_KEY_RE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?$")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_GROUP_DREAM_PROMPT = (
    "你正在执行夜间离线记忆巩固（Dreaming），负责维护你在当前群聊的四层定长驻群手账。\n"
    "手账用于未来跨会话唤醒时快速恢复语境，各分区均为【原地覆写定长槽位】，严禁无限累加：\n"
    "1. persistent（本群长期默契与职责基调，1~3 条，总计 ≤150 字）："
    "仅保留跨周稳定成立的相处默契、本群固定职责、反复验证过的避坑/排版/口径经验。"
    "单日临时数据严禁写入。\n"
    "2. monthly（本群当月主线与持续议题，1~4 条，总计 ≤200 字）："
    "记录本月跨天推进的专题脉络与阶段成果。必须对照【系统真值对账】："
    "凡已在「已完结交付」出现或超过一天无后续的一次性查询，必须标为「已完成…」或淘汰，"
    "严禁继续写成「还在慢慢核/仍在整理」的未完假象！瞬时单日价格下次可重查，勿硬背。\n"
    "3. yesterday（本群昨日记忆摘要，1~3 条，总计 ≤150 字）："
    "以第一人称自然回顾昨日在本群办成的关键事项、得出的核心结论与留下的互动话头。\n"
    "硬性格式要求：\n"
    "- 保持角色自身语气与自称，严禁暴露内部工具函数名（如 web_search_tool）、群号标签或协议符号。\n"
    "- 若保留已有句柄（如 note:xxx 或 sa_xxx），原样保留在括号内。\n"
    '- 严格仅输出合法 JSON 对象：{"persistent": ["..."], "monthly": ["..."], "yesterday": ["..."]}，'
    "每个数组元素是不带 `- ` 前缀的单行纯文本。"
)

_SELF_DREAM_PROMPT = (
    "你正在执行夜间离线记忆巩固（Dreaming），负责维护你个人的三层定长自我手账。\n"
    "各分区均为【原地覆写定长槽位】，用于沉淀跨会话自我连续性：\n"
    "1. persistent（持久自我认知与通用经验，1~4 条，总计 ≤200 字）："
    "记录长期职责认知、跨场景通用的核查原则与踩坑验证过的经验规律，满额时新陈代谢淘汰琐事。\n"
    "2. monthly（近期个人探索与习惯，1~4 条，总计 ≤200 字）："
    "记录近期自主探索的兴趣课题、生活小习惯及活跃专题笔记句柄（如 note:角色:topic_xxx），"
    "对照【系统真值对账】及时销账已完结事项。\n"
    "3. yesterday（昨日自主回顾，1~2 条，总计 ≤110 字）："
    "简述昨日闲暇自主探索或全局周期事务的收获。\n"
    "隐私硬红线：严禁将任何特定群聊的专属任务、私聊话题或特定用户个人事务写入自我手账。\n"
    '- 严格仅输出合法 JSON 对象：{"persistent": ["..."], "monthly": ["..."], "yesterday": ["..."]}，'
    "每个数组元素是不带 `- ` 前缀的单行纯文本，严禁输出额外解释。"
)


class ArchiveRecord(TypedDict):
    date: str
    shard: str
    kind: str
    entries: list[str]
    ts: float


class DreamSlots(TypedDict):
    persistent: list[str]
    monthly: list[str]
    yesterday: list[str]


@dataclass(frozen=True)
class GroundTruthDigest:
    completed: tuple[str, ...] = ()
    in_flight: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    autonomy_facts: tuple[str, ...] = ()

    def to_prompt_block(self) -> str:
        parts: list[str] = []
        if self.completed:
            parts.append(f"- 已完结交付（严禁再写成未完成/还在查）：{'；'.join(self.completed[:12])}")
        if self.in_flight:
            parts.append(f"- 真实仍在途任务：{'；'.join(self.in_flight[:6])}")
        else:
            parts.append("- 真实仍在途任务：无（所有早期一次性委派均已收尾）")
        if self.failed:
            parts.append(f"- 已终止/失败项：{'；'.join(self.failed[:4])}")
        if self.autonomy_facts:
            parts.append(f"- 近期自主核实记录：{'；'.join(self.autonomy_facts[:6])}")
        return "\n".join(parts)


def logical_sleep_date(tz_name: str, now_ts: float) -> str:
    """计算睡眠周期归属自然日（减 6 小时对齐午夜前后同一休息窗，根除 24h 漂移）。"""
    shifted = _tz_dt(tz_name, now_ts) - timedelta(hours=6)
    return shifted.strftime("%Y-%m-%d")


def current_month_str(tz_name: str, now_ts: float) -> str:
    return _tz_dt(tz_name, now_ts).strftime("%Y-%m")


async def collect_ground_truth(
    persona_name: str,
    shard_id: str,
    now_ts: float,
) -> GroundTruthDigest:
    """Phase 1：从 AIAgentTask 与 AIAutonomyActivity 查询近 48h 确定性终态。"""
    from sqlmodel import col, select

    from gsuid_core.ai_core.database.models import AIAutonomyActivity
    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.utils.database.base_models import async_maker

    cutoff_dt = datetime.fromtimestamp(now_ts - 48 * 3600)
    cutoff_24h = datetime.fromtimestamp(now_ts - 24 * 3600)
    completed: list[str] = []
    in_flight: list[str] = []
    failed: list[str] = []
    auto_facts: list[str] = []

    async with async_maker() as session:
        stmt = select(
            col(AIAgentTask.display_name),
            col(AIAgentTask.status),
            col(AIAgentTask.updated_at),
            col(AIAgentTask.recurring_trigger),
        ).where(col(AIAgentTask.updated_at) >= cutoff_dt)
        if shard_id.startswith("user:"):
            uid_only = shard_id[len("user:") :].strip()
            stmt = stmt.where(
                col(AIAgentTask.owner_user_id) == uid_only,
                (col(AIAgentTask.group_id).is_(None)) | (col(AIAgentTask.group_id) == ""),
            )
        elif shard_id != "__self__":
            stmt = stmt.where(
                col(AIAgentTask.group_id) == shard_id,
                (col(AIAgentTask.persona_name).is_(None))
                | (col(AIAgentTask.persona_name) == "")
                | (col(AIAgentTask.persona_name) == persona_name),
            )
        else:
            # 自我核严禁拉取特定用户的私聊任务，只对账无属主的全局自主任务
            stmt = stmt.where(
                col(AIAgentTask.persona_name) == persona_name,
                (col(AIAgentTask.group_id).is_(None)) | (col(AIAgentTask.group_id) == ""),
                col(AIAgentTask.owner_user_id) == "",
            )
        rows = (await session.execute(stmt)).all()
        for disp_raw, status_raw, upd_raw, rec_raw in rows:
            disp = str(disp_raw or "").strip()
            if not disp or rec_raw:
                continue
            status = str(status_raw or "")
            if status == "completed" and disp not in completed:
                completed.append(disp)
            elif status == "running" or (
                status in ("pending", "waiting_approval") and isinstance(upd_raw, datetime) and upd_raw >= cutoff_24h
            ):
                if disp not in in_flight:
                    in_flight.append(disp)
            elif status in ("failed", "cancelled") and disp not in failed:
                failed.append(disp)

        if shard_id == "__self__":
            act_stmt = (
                select(col(AIAutonomyActivity.summary), col(AIAutonomyActivity.drive_kind))
                .where(
                    col(AIAutonomyActivity.persona_name) == persona_name,
                    col(AIAutonomyActivity.drive_kind) == "self_explore",
                    col(AIAutonomyActivity.created_at) >= now_ts - 24 * 3600,
                )
                .order_by(col(AIAutonomyActivity.created_at).desc())
                .limit(8)
            )
            for summary_raw, _drive_raw in (await session.execute(act_stmt)).all():
                s_txt = str(summary_raw or "").strip()
                if s_txt and s_txt not in auto_facts:
                    auto_facts.append(truncate_at_boundary(s_txt, max_chars=70))

    return GroundTruthDigest(
        completed=tuple(completed),
        in_flight=tuple(in_flight),
        failed=tuple(failed),
        autonomy_facts=tuple(auto_facts),
    )


async def archive_shard_entries(
    archives_dir: Path,
    *,
    date_str: str,
    shard_id: str,
    kind: str,
    entries: Sequence[str],
    now_ts: float,
    tz_name: str = "Asia/Shanghai",
) -> Path | None:
    """Phase 2：按月单文件追加无损归档（避免每日每群产生海量小文件）。"""
    cleaned = [e.strip() for e in entries if e.strip()]
    if not cleaned:
        return None
    month_key = (
        date_str[:7] if _ARCHIVE_KEY_RE.match(date_str) is not None else _tz_dt(tz_name, now_ts).strftime("%Y-%m")
    )
    target = archives_dir / f"{month_key}.jsonl"
    rec: ArchiveRecord = {
        "date": date_str,
        "shard": shard_id,
        "kind": kind,
        "entries": cleaned,
        "ts": now_ts,
    }
    await _append_jsonl_sync(target, json.dumps(rec, ensure_ascii=False))
    return target


@to_thread
def read_journal_archive_sync(
    archives_dir: Path,
    shard_id: str,
    date_or_month: str,
) -> str:
    """从按月 JSONL 归档中检索指定分片与日期/月份的历史流水。"""
    key = date_or_month.strip()
    if _ARCHIVE_KEY_RE.match(key) is None or not archives_dir.exists():
        return ""
    month_key = key[:7]
    target = archives_dir / f"{month_key}.jsonl"
    if not target.exists():
        return ""
    matched_blocks: list[str] = []
    with target.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            rec_shard = parsed["shard"] if "shard" in parsed and isinstance(parsed["shard"], str) else ""
            rec_date = parsed["date"] if "date" in parsed and isinstance(parsed["date"], str) else ""
            rec_kind = parsed["kind"] if "kind" in parsed and isinstance(parsed["kind"], str) else "daily"
            if rec_kind == "legacy_self_v1_archive":
                continue
            rec_entries = parsed["entries"] if "entries" in parsed and isinstance(parsed["entries"], list) else []
            if shard_id and rec_shard != shard_id:
                continue
            if len(key) == 7 or rec_date == key:
                header = f"### {rec_date} ({'月度汇总' if rec_kind == 'monthly_rollup' else '当日流水'})"
                body = "\n".join(str(x) for x in rec_entries if isinstance(x, str))
                if body:
                    matched_blocks.append(f"{header}\n{body}")
    return "\n\n".join(matched_blocks)


@to_thread
def run_storage_gc_sync(persona_root: Path, now_ts: float, tz_name: str = "Asia/Shanghai") -> tuple[int, int]:
    """365 天零膨胀维护：将 >60 天未活跃群移入休眠目录，清理 >12 个月的旧归档卷。"""
    dormant_moved = 0
    purged_archives = 0

    groups_dir = persona_root / "groups"
    dormant_dir = persona_root / "dormant_groups"
    if groups_dir.exists():
        cutoff_dormant = now_ts - DORMANT_GROUP_DAYS * 86400
        for gfile in list(groups_dir.glob("*.md")):
            try:
                if gfile.stat().st_mtime < cutoff_dormant:
                    dormant_dir.mkdir(parents=True, exist_ok=True)
                    dest = dormant_dir / gfile.name
                    gfile.replace(dest)
                    dormant_moved += 1
            except OSError:
                continue

    archives_dir = persona_root / "archives"
    if archives_dir.exists():
        dt_now = _tz_dt(tz_name, now_ts)
        min_year_month = (dt_now.year * 12 + dt_now.month) - ARCHIVE_RETENTION_MONTHS
        for afile in list(archives_dir.glob("*.jsonl")):
            stem = afile.stem
            parts = stem.split("-")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                ym = int(parts[0]) * 12 + int(parts[1])
                if ym < min_year_month:
                    try:
                        afile.unlink(missing_ok=True)
                        purged_archives += 1
                    except OSError:
                        continue
    return dormant_moved, purged_archives


def _parse_dream_slots_output(text: str) -> DreamSlots | None:
    """解析做梦模型输出的 JSON（兼容代码块包裹与分段兜底）。"""
    raw = text.strip()
    if not raw:
        return None
    candidates: list[str] = []
    m = _JSON_FENCE_RE.search(raw)
    if m is not None:
        candidates.append(m.group(1))
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidates.append(raw[brace_start : brace_end + 1])

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        pers = obj["persistent"] if "persistent" in obj and isinstance(obj["persistent"], list) else []
        mon = obj["monthly"] if "monthly" in obj and isinstance(obj["monthly"], list) else []
        yest = obj["yesterday"] if "yesterday" in obj and isinstance(obj["yesterday"], list) else []
        p_clean = [str(x).strip().lstrip("-*• ") for x in pers if isinstance(x, str) and str(x).strip()]
        m_clean = [str(x).strip().lstrip("-*• ") for x in mon if isinstance(x, str) and str(x).strip()]
        y_clean = [str(x).strip().lstrip("-*• ") for x in yest if isinstance(x, str) and str(x).strip()]
        if p_clean or m_clean or y_clean:
            return {"persistent": p_clean, "monthly": m_clean, "yesterday": y_clean}
    return None


DreamLLMOutcome = DreamSlots | Literal["transient_error"] | None


async def synthesize_shard_with_llm(
    persona_name: str,
    doc: "ShardDocument",
    *,
    ground_truth: GroundTruthDigest,
    prev_month_rollover: Sequence[str] = (),
) -> DreamLLMOutcome:
    """Phase 4：调用 high 档模型执行多尺度手账提炼与状态销账。"""
    from gsuid_core.ai_core.configs.models import get_config_name_for_task
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data or not get_config_name_for_task("high", TASK_JOURNAL_COMPACT):
        return None

    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.agent_errors import is_agent_error_text
    from gsuid_core.ai_core.persona.persona import Persona
    from gsuid_core.ai_core.persona.resource import extract_compact_persona

    card = ""
    persona_obj = Persona(persona_name)
    if persona_obj.exists():
        raw_card = await persona_obj.load_content()
        card = extract_compact_persona(raw_card) or raw_card

    is_self = doc.shard_id == "__self__"
    guide = _SELF_DREAM_PROMPT if is_self else _GROUP_DREAM_PROMPT
    system_prompt = f"{card}\n\n{guide}" if card else guide

    pers_block = "\n".join(doc.persistent) if doc.persistent else "（暂无）"
    mon_block = "\n".join(doc.monthly) if doc.monthly else "（暂无）"
    rollover_section = ""
    if prev_month_rollover:
        rollover_section = (
            "\n\n【上月已翻篇主线（仅将具有长期价值的经验晋升入 persistent，其余归档淘汰）】\n"
            + "\n".join(prev_month_rollover)
        )
    timeline_block = "\n".join(doc.timeline[-60:])

    user_prompt = (
        f"【原持久层记忆】\n{pers_block}\n\n"
        f"【原当月/近期层记忆】\n{mon_block}"
        f"{rollover_section}\n\n"
        f"【系统真值对账】\n{ground_truth.to_prompt_block()}\n\n"
        f"【待整理的今日行动流水】\n{timeline_block}"
    )

    agent = create_agent(
        system_prompt=system_prompt,
        create_by="Journal_Compact",
        task_kind=TASK_JOURNAL_COMPACT,
        persona_name=persona_name,
        session_id=f"journal_dream_{persona_name}_{doc.shard_id}_{int(time.time())}",
        is_subagent=True,
        task_level="high",
        dynamic_tools=False,
    )
    try:
        result = await agent.run(user_prompt, tools=[], return_mode="return", budget_gate=False)
    finally:
        agent._session_logger.close()

    text = result if isinstance(result, str) else ""
    if not text:
        return None
    if is_agent_error_text(text):
        logger.warning(t("log.ai.journal_compact_fail", persona_name=persona_name, e=text[:120]))
        return "transient_error"
    return _parse_dream_slots_output(text)


def rule_rollover_shard(
    doc: "ShardDocument",
    *,
    persona_name: str,
    sleep_date: str,
    current_month: str,
    ground_truth: GroundTruthDigest,
) -> DreamSlots:
    """轻度分片零 LLM 规则跨日（或离线降级兜底）：保留持久与当月脉络，将流水凝练入昨日摘要。"""
    from gsuid_core.ai_core.journal.storage import parse_timeline_line, _is_placeholder_bullet

    pers = [ln.lstrip("-*• ").strip() for ln in doc.persistent if ln.strip() and not _is_placeholder_bullet(ln)]
    # 跨月时旧当月主线已封存，新月从空起步；同月保留并标记已完结任务
    if doc.month_label and doc.month_label != current_month:
        mon: list[str] = []
    else:
        mon = []
        for raw_ln in doc.monthly:
            body = raw_ln.lstrip("-*• ").strip()
            if not body or _is_placeholder_bullet(body):
                continue
            for done_name in ground_truth.completed:
                if done_name and done_name in body and "已完结" not in body and "已完成" not in body:
                    body = f"[已完结] {body}"
                    break
            mon.append(body)

    # 从今日流水提取前 2 条实质项 + 统计概览写入昨日记忆
    yest: list[str] = []
    tags: list[str] = []
    substantive_bodies: list[str] = []
    for line in doc.timeline:
        parsed = parse_timeline_line(line)
        if parsed is not None:
            if parsed.tag:
                tags.append(parsed.tag)
            if not parsed.is_silent and parsed.text:
                substantive_bodies.append(truncate_at_boundary(parsed.text, max_chars=65))
        else:
            stripped = line.lstrip("-*• ").strip()
            if stripped:
                substantive_bodies.append(truncate_at_boundary(stripped, max_chars=65))

    for item in substantive_bodies[:2]:
        yest.append(item)
    remaining = len(doc.timeline) - len(yest)
    if remaining > 0 or not yest:
        uniq_tags = list(dict.fromkeys(tags))[:4]
        tag_desc = f"（含 {', '.join(uniq_tags)} 等）" if uniq_tags else ""
        yest.append(f"{sleep_date} 共完成了 {len(doc.timeline)} 项日常与委派行动{tag_desc}。")

    if not pers:
        pers = ["日常跟进群聊协作、查证与委派交付，未知信息核实后再作答。"]
    return {"persistent": pers, "monthly": mon, "yesterday": yest}


_JOB_ID: str = "ai_journal_hourly_compact"


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

        # 若 self.md 内混有 (群:gid) 或私聊 (替:uid) 流水，做梦前分流至对应群/私聊分片防广播
        if shard_id == SELF_SHARD_ID:
            stray_by_group: Dict[str, List[str]] = {}
            stray_by_user: Dict[str, List[str]] = {}
            kept_self: List[str] = []
            for ln in doc.timeline:
                p = parse_timeline_line(ln)
                if p is not None and p.gid:
                    bucket = stray_by_group[p.gid] if p.gid in stray_by_group else []
                    bucket.append(ln)
                    stray_by_group[p.gid] = bucket
                elif p is not None and p.who:
                    ubucket = stray_by_user[p.who] if p.who in stray_by_user else []
                    ubucket.append(ln)
                    stray_by_user[p.who] = ubucket
                else:
                    kept_self.append(ln)
            if stray_by_group or stray_by_user:
                doc.timeline = kept_self
                for gid, glines in stray_by_group.items():
                    gdoc = await to_thread(_load_shard_sync)(persona_name, gid, create_if_missing=True)
                    if gdoc is not None:
                        gdoc.timeline.extend(glines)
                        gdoc.timeline = gdoc.timeline[-GROUP_TIMELINE_RING_CAP:]
                        await to_thread(_save_shard_sync)(persona_name, gdoc, ts_val=now_ts)
                for req_uid, ulines in stray_by_user.items():
                    usid = _user_shard_id(req_uid)
                    udoc = await to_thread(_load_shard_sync)(persona_name, usid, create_if_missing=True)
                    if udoc is not None:
                        udoc.timeline.extend(ulines)
                        udoc.timeline = udoc.timeline[-SELF_TIMELINE_RING_CAP:]
                        await to_thread(_save_shard_sync)(persona_name, udoc, ts_val=now_ts)
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

        user_sids = {_user_shard_id(uid) for uid in await to_thread(_list_user_shard_ids_sync)(key)}
        all_gids = set(st_entry["dirty_shards"]) | set(await to_thread(_list_group_shard_ids_sync)(key)) | user_sids
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
