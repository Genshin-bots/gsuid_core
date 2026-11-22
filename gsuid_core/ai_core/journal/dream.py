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
from typing import TYPE_CHECKING, Literal, Sequence, TypedDict
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.ai_core.journal.sanitizer import truncate_at_boundary
from gsuid_core.ai_core.configs.task_kinds import TASK_JOURNAL_COMPACT

if TYPE_CHECKING:
    from gsuid_core.ai_core.journal.manager import ShardDocument

DREAM_HEAVY_MIN_ENTRIES = 3
DREAM_LIGHT_CARRY_MAX = 3
DREAM_MAX_HEAVY_SHARDS_PER_NIGHT = 20
ARCHIVE_RETENTION_MONTHS = 12
DORMANT_GROUP_DAYS = 60
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

_NOTE_MERGE_PROMPT = (
    "你正在整理自己的个人专题笔记库（Wiki Consolidation）。\n"
    "下面提供了同一主题下的多篇零散历史笔记（可能含已有的专题主稿）。\n"
    "请将它们去重整合为一篇结构清晰的最新专题笔记：\n"
    "1. 保留经过核实的事实数据、标准编号/出处日期、有效经验与避坑结论；\n"
    "2. 剔除重复寒暄、过期的单日临时臆测；若有前后矛盾，以较新且有明确出处的核查为准；\n"
    "3. 使用简洁的 Markdown 小标题与列表组织，总长度严格控制在 1500 字以内，保持角色自然口吻；\n"
    "4. 直接输出笔记正文，不要输出代码块外框或额外寒暄。"
)


class ArchiveRecord(TypedDict):
    date: str
    shard: str
    kind: str
    entries: list[str]
    ts: float


class NoteArchiveRecord(TypedDict):
    archived_at: str
    title: str
    reason: str
    body: str
    scope_key: str
    owner_user_id: str


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


def _resolve_tz(tz_name: str) -> ZoneInfo:
    zone_str = (tz_name or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(zone_str)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Shanghai")


def _tz_dt(tz_name: str, now_ts: float) -> datetime:
    return datetime.fromtimestamp(now_ts, tz=_resolve_tz(tz_name))


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


@to_thread
def _append_jsonl_sync(path: Path, line_json: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line_json + "\n")


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

    from gsuid_core.ai_core.utils import is_agent_error_text
    from gsuid_core.ai_core.gs_agent import create_agent
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
    from gsuid_core.ai_core.journal.manager import parse_timeline_line, _is_placeholder_bullet

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
    from gsuid_core.ai_core.utils import is_agent_error_text
    from gsuid_core.ai_core.gs_agent import create_agent

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
    import hashlib

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
