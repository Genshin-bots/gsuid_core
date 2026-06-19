"""
AI Session Logs APIs
提供 AI Agent 会话执行日志的 RESTful APIs

统一合并内存活跃会话 + 本地持久化日志，去重后提供给前端，
便于前端渲染 AI 调用历史栈，清晰展示每一步结果。
"""

import json
import time
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Literal, Optional, Sequence, TypedDict
from pathlib import Path
from datetime import datetime

from fastapi import Depends
from pydantic import Field, BaseModel
from fastapi.concurrency import run_in_threadpool

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import SessionLogEntry, LinkedAgentRecord, SessionLogFileData
from gsuid_core.ai_core.resource import AI_SESSION_LOGS_PATH, AI_SUBAGENT_LOGS_PATH
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.session_registry import get_ai_session_registry

if TYPE_CHECKING:
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.session_logger import AISessionLogger
    from gsuid_core.ai_core.session_registry import AISessionRegistry

# ─────────────────────────────────────────────
# TypedDict 定义
#
# 序列化基础结构（SessionLogEntry / LinkedAgentRecord / SessionLogFileData）统一
# 由 ``gsuid_core.ai_core.models`` 提供，与 AISessionLogger 落盘格式同源；
# 以下仅定义 webconsole 响应特有的派生类型。
# ─────────────────────────────────────────────


class LinkedAgentEnriched(LinkedAgentRecord, total=False):
    """Enriched 后的关联 Agent 条目（在 LinkedAgentRecord 基础上补聚合统计）"""

    entry_count: int
    type_counts: Dict[str, int]
    is_active: Optional[bool]
    created_at: float
    ended_at: Optional[float]
    source: Literal["memory", "disk", "unavailable"]


class SessionLogSummary(TypedDict):
    """Session 日志摘要"""

    session_id: str
    session_uuid: Optional[str]
    persona_name: Optional[str]
    create_by: Optional[str]
    is_subagent: bool
    created_at: float
    created_at_str: Optional[str]
    updated_at: float
    updated_at_str: Optional[str]
    ended_at: Optional[float]
    ended_at_str: Optional[str]
    duration_seconds: Optional[float]
    entry_count: int
    type_counts: Dict[str, int]
    is_active: bool
    source: str
    file_name: Optional[str]
    # 基础摘要里是原始 LinkedAgentRecord，enrich 后是 LinkedAgentEnriched（后者是前者
    # 的子类型）；字段按基类 LinkedAgentRecord 协变声明，两者皆可赋入（仅读取，不就地改）。
    linked_agents: Sequence[LinkedAgentRecord]
    linked_agent_count: int


class SessionLogDetail(SessionLogSummary, total=False):
    """Session 日志详情（含完整 entries）"""

    entries: List[SessionLogEntry]


# ─────────────────────────────────────────────
# Pydantic 请求模型
# ─────────────────────────────────────────────


class SessionLogsFilterRequest(BaseModel):
    """Session 日志筛选请求"""

    session_id: Optional[str] = Field(None, description="按 session_id 精确筛选")
    create_by: Optional[str] = Field(None, description="按创建来源筛选 (Chat/SubAgent/BuildPersona/LLM)")
    persona_name: Optional[str] = Field(None, description="按 Persona 名称筛选")
    is_active: Optional[bool] = Field(None, description="按是否活跃筛选 (true=仅活跃, false=仅已结束)")
    date_from: Optional[str] = Field(None, description="起始日期 YYYY-MM-DD")
    date_to: Optional[str] = Field(None, description="结束日期 YYYY-MM-DD")
    limit: int = Field(default=50, ge=1, le=200, description="返回数量限制")
    offset: int = Field(default=0, ge=0, description="偏移量")


# ─────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────


def _build_summary_from_memory(
    sid: str,
    session: "GsCoreAIAgent",
    index: Optional["LogIndex"] = None,
) -> SessionLogSummary:
    """从内存中的活跃 Session 构建日志摘要"""
    # _session_logger 是 GsCoreAIAgent 的已声明字段（恒非 None），按类型直接访问
    logger_obj: "AISessionLogger" = session._session_logger

    # 快照拷贝：本函数可能在线程池中执行，事件循环可能并发向 entries 追加，
    # list(...) 在 GIL 下原子拷贝，避免 Python for 循环遍历时“list changed size”。
    entries: List[SessionLogEntry] = list(logger_obj.entries)
    created_at: float = logger_obj.created_at
    updated_at: float = logger_obj.updated_at
    ended_at: Optional[float] = logger_obj.ended_at
    linked_agents: List[LinkedAgentRecord] = logger_obj.linked_agents
    file_name: Optional[str] = logger_obj._file_path.name or None

    # 计算运行时长
    duration: Optional[float] = None
    if ended_at and created_at:
        duration = ended_at - created_at
    elif created_at:
        duration = time.time() - created_at

    # 统计各类型条目数量
    type_counts: Dict[str, int] = {}
    for entry in entries:
        etype: str = entry["type"]
        type_counts[etype] = type_counts.get(etype, 0) + 1

    return {
        "session_id": sid,
        "session_uuid": logger_obj.session_uuid,
        "persona_name": logger_obj.persona_name,
        "create_by": logger_obj.create_by,
        "is_subagent": logger_obj.is_subagent,
        "created_at": created_at,
        "created_at_str": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S") if created_at else None,
        "updated_at": updated_at,
        "updated_at_str": datetime.fromtimestamp(updated_at).strftime("%Y-%m-%d %H:%M:%S") if updated_at else None,
        "ended_at": ended_at,
        "ended_at_str": datetime.fromtimestamp(ended_at).strftime("%Y-%m-%d %H:%M:%S") if ended_at else None,
        "duration_seconds": round(duration, 2) if duration else None,
        "entry_count": len(entries),
        "type_counts": type_counts,
        "is_active": ended_at is None,
        # 内存中尚有未落盘新数据则 source=memory，否则 disk
        "source": "memory" if logger_obj.has_unpersisted_data else "disk",
        "file_name": file_name,
        "linked_agents": _enrich_linked_agents_list(linked_agents, index),
        "linked_agent_count": len(linked_agents),
    }


# ─────────────────────────────────────────────
# 文件解析缓存 + 查找索引（消除重复解析与全目录扫描）
#
# 此前列表/概览接口每次请求都会：
#   ① 完整读取并 json 解析全部日志文件（含庞大的 entries 数组）；
#   ② 对每个 linked_agent 在磁盘上做一次全目录扫描查找目标文件，
#      复杂度 O(文件数 × linked_agent 数)。文件越多越慢，呈二次增长。
#   而 linked_agent 记录里的 log_file 是绝对路径，跨机器/迁移后必然失效，
#   使其总是落到最坏的全目录扫描分支。
#
# 优化：
#   * 按 (mtime, size) 缓存每个文件的“基础摘要”，已结束的日志不再变化，
#     首次解析后后续请求直接命中缓存，省去重复读取/解析/统计 entries。
#   * 构建 (session_id, session_uuid) -> 摘要 索引，linked_agent 改为
#     O(1) 索引查找，彻底消除全目录扫描。
# ─────────────────────────────────────────────

# 索引类型：((session_id, session_uuid) -> 摘要, session_id -> 最新摘要)
LogIndex = Tuple[Dict[Tuple[str, str], SessionLogSummary], Dict[str, SessionLogSummary]]

# path -> ((mtime, size), base_summary)
_BASE_SUMMARY_CACHE: Dict[str, Tuple[Tuple[float, int], SessionLogSummary]] = {}

# 列表/概览接口经 run_in_threadpool 执行，可能多线程并发读写上面的全局缓存；
# 用一把锁保护缓存的增删与遍历，避免 "dictionary changed size during iteration"。
_CACHE_LOCK = threading.Lock()

# 顶层 ``entries`` 键的字节标记：indent=2 下顶层键恒为换行+2 空格（更深层≥4 空格），
# 且串内换行被转义，故不会误命中；CRLF 多出的 ``\r`` 由 _read_log_header 内 rstrip 去除。
_ENTRIES_MARKER = b'\n  "entries":'
# 正常元数据头（不含 entries）只有几 KB；超过此上限（如异常巨大的 linked_agents）
# 放弃快速读取，回退完整解析。
_HEADER_MAX_BYTES = 1 << 20  # 1 MB


def _read_log_header(path: Path) -> Optional[Dict[str, Any]]:
    """只读取日志文件 ``entries`` 之前的元数据头，避免解析庞大的 entries 数组。

    新版 ``_build_data`` 把 ``entries`` 作为最后一个顶层字段写出，且预先持久化了
    ``type_counts``，因此 entries 之前已包含构建摘要所需的全部字段。本函数按字节扫描到
    顶层 ``"entries"`` 键即停止，把前缀重组为合法 JSON 解析，读取量为 O(头部大小) 而非
    O(文件大小)——这是 2G 级日志下列表/概览/分类接口提速的关键。

    返回 None 表示无法快速读取（旧格式 entries 非末位 / 头部过大 / 文件损坏），
    由调用方回退到完整 ``json.load``。返回的 dict 是否为新格式由调用方按 ``type_counts``
    是否存在判定（旧格式头部缺 type_counts 与 linked_agents）。
    """
    head: Optional[bytes] = None
    try:
        with open(path, "rb") as f:
            buf = b""
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break  # 读到文件尾仍未命中标记（旧格式 entries 非末位等）
                buf += chunk
                idx = buf.find(_ENTRIES_MARKER)
                if idx != -1:
                    head = buf[:idx]
                    break
                if len(buf) > _HEADER_MAX_BYTES:
                    return None
    except OSError:
        return None

    if not head:
        return None

    # head 形如 ``{\n  "a": 1,\n  "b": 2,``（以上一字段的逗号结尾，CRLF 时末尾还带 \r），
    # 去掉尾随空白与逗号、补 ``}`` 即为合法 JSON。
    text = head.rstrip()
    if text.endswith(b","):
        text = text[:-1]
    text += b"\n}"
    try:
        data = json.loads(text.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _parse_log_file_base(path: Path) -> Optional[SessionLogSummary]:
    """解析日志文件为基础摘要（linked_agents 保持原始未 enrich），按 mtime+size 缓存。

    已结束的磁盘日志内容不再变化，首次解析后缓存；后续请求若文件未变则直接复用，
    避免重复读取 / JSON 解析 / entries 统计（这是列表接口的主要耗时来源）。

    解析路径：优先 ``_read_log_header`` 头部快速读取（新格式：entries 末位 + 已持久化
    type_counts），只读几 KB 即可；旧格式（升级前落盘）头部缺 type_counts，回退到完整
    ``json.load`` 并遍历 entries 现算 type_counts，保证向后兼容（旧文件随 8 天日志保留
    自然淘汰后，全部走快速路径）。
    """
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        with _CACHE_LOCK:
            _BASE_SUMMARY_CACHE.pop(key, None)
        return None

    sig: Tuple[float, int] = (st.st_mtime, st.st_size)
    with _CACHE_LOCK:
        cached = _BASE_SUMMARY_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]

    # 1. 优先头部快速读取（新格式）：命中即拿到 type_counts / linked_agents，无需读 entries
    header = _read_log_header(path)
    data: Dict[str, Any]
    type_counts: Dict[str, int]
    if header is not None and "type_counts" in header and "session_id" in header and "created_at" in header:
        data = header
        tc = header.get("type_counts")
        type_counts = tc if isinstance(tc, dict) else {}
    else:
        # 2. 回退完整解析（旧格式 / 头部不可用）：遍历 entries 现算 type_counts
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        # 磁盘文件可能由历史版本写入、字段不全；按 _build_data 契约类型化读取，缺字段取默认
        if not isinstance(data, dict) or "session_id" not in data or "created_at" not in data:
            return None
        type_counts = {}
        for entry in data.get("entries", []):
            etype: str = entry.get("type", "unknown")
            type_counts[etype] = type_counts.get(etype, 0) + 1

    created_at: float = data.get("created_at", 0)
    ended_at: Optional[float] = data.get("ended_at")
    updated_at: float = data.get("updated_at", 0)

    duration: Optional[float] = (ended_at - created_at) if (ended_at and created_at) else None
    linked_agents: List[LinkedAgentRecord] = data.get("linked_agents", [])

    summary: SessionLogSummary = {
        "session_id": data.get("session_id", ""),
        "session_uuid": data.get("session_uuid"),
        "persona_name": data.get("persona_name"),
        "create_by": data.get("create_by"),
        "is_subagent": data.get("is_subagent", False),
        "created_at": created_at,
        "created_at_str": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S") if created_at else None,
        "updated_at": updated_at,
        "updated_at_str": datetime.fromtimestamp(updated_at).strftime("%Y-%m-%d %H:%M:%S") if updated_at else None,
        "ended_at": ended_at,
        "ended_at_str": datetime.fromtimestamp(ended_at).strftime("%Y-%m-%d %H:%M:%S") if ended_at else None,
        "duration_seconds": round(duration, 2) if duration else None,
        "entry_count": data.get("entry_count", 0),
        "type_counts": type_counts,
        "is_active": ended_at is None,
        "source": "disk",
        "file_name": path.name,
        # 原始 linked_agents（未 enrich），enrich 在构建列表时按索引 O(1) 完成
        "linked_agents": linked_agents,
        "linked_agent_count": len(linked_agents),
    }
    with _CACHE_LOCK:
        _BASE_SUMMARY_CACHE[key] = (sig, summary)
    return summary


def _index_from_bases(bases: List[SessionLogSummary]) -> LogIndex:
    """从一组基础摘要构建查找索引，供 linked_agent enrich 时 O(1) 命中。"""
    by_sid_uuid: Dict[Tuple[str, str], SessionLogSummary] = {}
    latest_by_sid: Dict[str, SessionLogSummary] = {}
    for b in bases:
        sid = b["session_id"]
        if not sid:
            continue
        uuid = b["session_uuid"]
        if uuid:
            by_sid_uuid[(sid, uuid)] = b
        existing = latest_by_sid.get(sid)
        if existing is None or b["updated_at"] > existing["updated_at"]:
            latest_by_sid[sid] = b
    return by_sid_uuid, latest_by_sid


def _prune_base_cache(valid_keys: set) -> None:
    """移除缓存中已不存在（被清理）的日志文件条目，避免长期运行内存无界增长。"""
    with _CACHE_LOCK:
        if len(_BASE_SUMMARY_CACHE) <= len(valid_keys):
            return
        # 先在锁内快照 key 列表再删除，避免遍历与并发写入同时进行触发 RuntimeError。
        stale_keys = [k for k in _BASE_SUMMARY_CACHE if k not in valid_keys]
        for k in stale_keys:
            _BASE_SUMMARY_CACHE.pop(k, None)


def _build_log_index() -> LogIndex:
    """扫描全部日志文件并构建查找索引（基础摘要带 mtime 缓存）。

    供单会话详情类接口即时使用；列表接口会复用一次性构建好的索引。
    """
    bases: List[SessionLogSummary] = []
    for path in _list_log_files():
        base = _parse_log_file_base(path)
        if base is not None:
            bases.append(base)
    return _index_from_bases(bases)


def _enrich_linked_agent(
    agent_record: LinkedAgentRecord,
    by_sid_uuid: Dict[Tuple[str, str], SessionLogSummary],
    latest_by_sid: Dict[str, SessionLogSummary],
    registry: "AISessionRegistry",
) -> LinkedAgentEnriched:
    """
    为 linked_agent 条目补充聚合统计信息（type_counts, entry_count, is_active 等）。

    查找顺序：内存活跃会话（实时数据） -> 磁盘索引（O(1)）。
    （此前会按 log_file 绝对路径查找，失败后对每个 agent 全目录扫描，开销为
    O(文件数 × agent 数)；现统一走索引，无全目录扫描。）
    """
    enriched: LinkedAgentEnriched = {**agent_record}  # 复制，避免修改原始数据

    # link_agent 落盘的记录理论上字段齐全，但磁盘历史文件可能缺字段，故防御性取值
    agent_session_id: str = agent_record.get("session_id", "")
    if not agent_session_id:
        return enriched

    # 1. 优先从内存查找（活跃会话的实时数据）
    session = registry.get_ai_session(agent_session_id)
    if session is not None:
        logger_obj = session._session_logger
        # 快照拷贝，避免线程池执行时与事件循环并发追加产生竞态（见 _build_summary_from_memory）
        entries: List[SessionLogEntry] = list(logger_obj.entries)
        type_counts: Dict[str, int] = {}
        for entry in entries:
            etype = entry["type"]
            type_counts[etype] = type_counts.get(etype, 0) + 1
        enriched["entry_count"] = len(entries)
        enriched["type_counts"] = type_counts
        enriched["is_active"] = logger_obj.ended_at is None
        enriched["created_at"] = logger_obj.created_at
        enriched["ended_at"] = logger_obj.ended_at
        enriched["source"] = "memory" if logger_obj.has_unpersisted_data else "disk"
        return enriched

    # 2. 从磁盘索引查找（O(1)）：优先 (session_id, uuid) 精确匹配，否则取该 sid 最新
    agent_uuid: str = agent_record.get("session_uuid", "")
    target: Optional[SessionLogSummary] = None
    if agent_uuid:
        target = by_sid_uuid.get((agent_session_id, agent_uuid))
    if target is None:
        target = latest_by_sid.get(agent_session_id)

    if target is not None:
        enriched["entry_count"] = target["entry_count"]
        enriched["type_counts"] = target["type_counts"]
        enriched["created_at"] = target["created_at"]
        # 未在内存 registry 中 → 视为已结束；磁盘 ended_at 为 null 时用 updated_at 近似
        _disk_ended_at = target["ended_at"]
        enriched["is_active"] = False
        enriched["ended_at"] = _disk_ended_at if _disk_ended_at is not None else target["updated_at"]
        enriched["source"] = "disk"
        return enriched

    # 3. 无法获取详情，添加空占位
    enriched.setdefault("entry_count", 0)
    enriched.setdefault("type_counts", {})
    enriched.setdefault("is_active", None)
    enriched.setdefault("source", "unavailable")
    return enriched


def _enrich_linked_agents_list(
    agents: Sequence[LinkedAgentRecord],
    index: Optional[LogIndex] = None,
    registry: Optional["AISessionRegistry"] = None,
) -> List[LinkedAgentEnriched]:
    """对 linked_agents 列表中的每个条目进行 enrichment（基于索引，O(1)/条）。

    Args:
        agents: 原始 linked_agent 记录列表
        index: 预构建的查找索引；为 None 时即时构建一次（单会话详情场景）
        registry: AI session registry；为 None 时即时获取
    """
    if not agents:
        return []
    if index is None:
        index = _build_log_index()
    if registry is None:
        registry = get_ai_session_registry()
    by_sid_uuid, latest_by_sid = index
    return [_enrich_linked_agent(a, by_sid_uuid, latest_by_sid, registry) for a in agents]


def _parse_log_file(path: Path) -> Optional[SessionLogSummary]:
    """解析单个日志 JSON 文件，返回摘要信息（linked_agents 已 enrich）。

    保留作兼容入口；列表构建走 ``_parse_log_file_base`` + 统一索引 enrich。
    """
    base = _parse_log_file_base(path)
    if base is None:
        return None
    item = base.copy()
    item["linked_agents"] = _enrich_linked_agents_list(base["linked_agents"])
    return item


def _load_log_detail(path: Path) -> Optional[SessionLogFileData]:
    """加载单个日志文件的完整内容（按 AISessionLogger._build_data 契约解析）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data: SessionLogFileData = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict) or "session_id" not in data or "created_at" not in data:
        return None
    return data


def _find_log_by_file_stem(file_stem: str) -> Optional[Dict[str, Any]]:
    """按文件名 stem 直接查找日志详情（高效 O(1)，避免全目录扫描）

    在主目录和 subagents 子目录中查找 ``{file_stem}.json`` 文件。
    找到后加载并补充 source / is_active / linked_agents 等字段。

    Args:
        file_stem: 日志文件名去掉 .json 后缀的部分
            （如 ``heartbeat_decision_早柚_xxx_c7b1408f_20260531_134144``）

    Returns:
        补充了 source/is_active/linked_agents 的日志数据字典，未找到返回 None
    """
    # 安全检查：防止目录遍历
    if ".." in file_stem or "/" in file_stem or "\\" in file_stem:
        return None

    registry = get_ai_session_registry()

    for base_path in (AI_SESSION_LOGS_PATH, AI_SUBAGENT_LOGS_PATH):
        candidate = base_path / f"{file_stem}.json"
        if candidate.exists() and candidate.is_file():
            file_data = _load_log_detail(candidate)
            if file_data is None:
                continue
            # 在原始文件结构上补充响应字段（source / is_active / 已 enrich 的 linked_agents）
            resp: Dict[str, Any] = dict(file_data)
            resp["source"] = "disk"
            # 修正 is_active：磁盘 ended_at 为 null 时需检查内存 registry
            disk_ended: Optional[float] = file_data.get("ended_at")
            real_sid: str = file_data.get("session_id", "")
            if disk_ended is None:
                in_mem = registry.get_ai_session(real_sid)
                resp["is_active"] = in_mem is not None
                if in_mem is None:
                    resp["ended_at"] = file_data.get("updated_at", 0)
            else:
                resp["is_active"] = False
            # Enrich linked_agents
            disk_linked = file_data.get("linked_agents", [])
            if disk_linked:
                resp["linked_agents"] = _enrich_linked_agents_list(disk_linked)
            return resp

    return None


def _list_log_files(include_subagents: bool = True) -> List[Path]:
    """列出所有日志文件

    Args:
        include_subagents: 是否包含 SubAgent 日志（session_logs/subagents/ 子目录）
    """
    files: List[Path] = []

    if AI_SESSION_LOGS_PATH.exists():
        files.extend([p for p in AI_SESSION_LOGS_PATH.iterdir() if p.is_file() and p.suffix == ".json"])

    if include_subagents and AI_SUBAGENT_LOGS_PATH.exists():
        files.extend([p for p in AI_SUBAGENT_LOGS_PATH.iterdir() if p.is_file() and p.suffix == ".json"])

    return files


def _build_unified_list() -> List[SessionLogSummary]:
    """
    构建统一的日志列表：合并内存活跃会话 + 磁盘持久化文件，按 session_uuid 去重

    去重规则：同一 session_uuid 在内存和磁盘中都存在时，优先使用内存版本（更新）
    """
    registry = get_ai_session_registry()
    sessions = registry.get_all_ai_sessions()
    memory_session_ids: set = set(sessions.keys())  # 内存中真正活跃的 session_id 集合

    # 1. 解析磁盘文件为基础摘要（带 mtime 缓存），并构建一次性查找索引
    #    （索引供 linked_agent enrich 做 O(1) 命中，避免逐 agent 全目录扫描）
    disk_files = _list_log_files()
    disk_bases: List[SessionLogSummary] = []
    for path in disk_files:
        base = _parse_log_file_base(path)
        if base is not None:
            disk_bases.append(base)
    _prune_base_cache({str(p) for p in disk_files})
    index: LogIndex = _index_from_bases(disk_bases)

    # 2. 收集内存中活跃 Session（linked_agents 复用同一索引 enrich）
    memory_map: Dict[str, SessionLogSummary] = {}  # session_uuid -> summary
    for sid, session in sessions.items():
        summary = _build_summary_from_memory(sid, session, index)
        uuid_val = summary["session_uuid"]
        memory_map[uuid_val or sid] = summary

    # 3. 磁盘摘要按 session_uuid 去重（同一 uuid 多份时取 updated_at 最新）
    disk_map: Dict[str, SessionLogSummary] = {}
    for base in disk_bases:
        uuid_val: Optional[str] = base["session_uuid"]
        key = uuid_val or base["file_name"] or ""
        existing = disk_map.get(key)
        if existing is None or base["updated_at"] > existing["updated_at"]:
            disk_map[key] = base

    # 4. 合并去重：先磁盘（含 is_active 校正 + linked_agents enrich），内存版本覆盖
    unified: Dict[str, SessionLogSummary] = {}
    for key, base in disk_map.items():
        item = base.copy()  # 浅拷贝，避免污染缓存中的基础摘要
        # 修正 is_active：磁盘上 ended_at 为 null 的 session 不一定仍活跃，
        # 只有在内存 registry 中真正存在的 session 才是活跃的。
        # （AISessionLogger 定时落盘不写 ended_at，只有 close() 才写；
        #  进程重启后旧 session 不在内存中，应视为已结束。）
        sid = item["session_id"] or ""
        if item["is_active"] and sid not in memory_session_ids:
            item["is_active"] = False
            ea = item["ended_at"]
            if ea is None:
                ea = item["updated_at"]
                item["ended_at"] = ea
                item["ended_at_str"] = datetime.fromtimestamp(ea).strftime("%Y-%m-%d %H:%M:%S") if ea else None
            created = item["created_at"]
            if ea and created:
                item["duration_seconds"] = round(ea - created, 2)
        # enrich linked_agents（O(1)/条，复用索引）
        item["linked_agents"] = _enrich_linked_agents_list(item["linked_agents"], index, registry)
        unified[key] = item

    # 内存版本覆盖磁盘版本（同一 session_uuid）
    for key, summary in memory_map.items():
        unified[key] = summary

    # 5. 按 created_at 倒序排列
    results = sorted(
        unified.values(),
        key=lambda x: x["created_at"],
        reverse=True,
    )

    return results


# 统一列表短期缓存：ai-history 一次加载会并发调用 list/overview/categories，各构建一次
# 全量列表。极短 TTL 合并为一次构建；活跃会话状态至多滞后数秒，对日志看板可接受。

_UNIFIED_CACHE_TTL: float = 3.0
# (build_time, results)
_unified_cache: Optional[Tuple[float, List[SessionLogSummary]]] = None
# 仅用于"同一时刻多线程并发构建"的去重，避免三个接口各跑一遍全量构建。
_UNIFIED_BUILD_LOCK = threading.Lock()


def _build_unified_list_cached() -> List[SessionLogSummary]:
    """带极短 TTL 的统一列表构建（供 list/overview/categories 共用）。

    命中 TTL 内的缓存直接复用；未命中时加锁构建，并发的其他请求在锁内二次检查后
    复用刚构建好的结果，确保一次页面加载只真正构建一次。返回结果只读共享（调用方
    只做筛选/统计，不就地修改条目），故多接口复用同一份安全。
    """
    global _unified_cache
    now = time.time()
    cache = _unified_cache
    if cache is not None and (now - cache[0]) < _UNIFIED_CACHE_TTL:
        return cache[1]
    with _UNIFIED_BUILD_LOCK:
        # 二次检查：可能已被并发请求构建好
        cache = _unified_cache
        if cache is not None and (time.time() - cache[0]) < _UNIFIED_CACHE_TTL:
            return cache[1]
        results = _build_unified_list()
        _unified_cache = (time.time(), results)
        return results


def _apply_filters(
    items: List[SessionLogSummary],
    session_id: Optional[str] = None,
    create_by: Optional[str] = None,
    persona_name: Optional[str] = None,
    is_active: Optional[bool] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[SessionLogSummary]:
    """对统一列表应用筛选条件"""
    results: List[SessionLogSummary] = []

    for item in items:
        if session_id and item["session_id"] != session_id:
            continue
        if create_by and item["create_by"] != create_by:
            continue
        if persona_name and item["persona_name"] != persona_name:
            continue
        if is_active is not None and item["is_active"] != is_active:
            continue

        if date_from or date_to:
            created_str: Optional[str] = item["created_at_str"]
            if created_str:
                date_part: str = created_str[:10]
                if date_from and date_part < date_from:
                    continue
                if date_to and date_part > date_to:
                    continue

        results.append(item)

    return results


def _find_log_by_session_id_and_uuid(
    session_id: str,
    session_uuid: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    根据 session_id + session_uuid 查找日志详情

    当 session_uuid 提供时，精确匹配到具体实例；
    当 session_uuid 为 None 或空字符串时，返回该 session_id 最新的实例（向后兼容）。

    查找优先级：
    1. 文件名 stem 精确匹配（O(1)，最高效）
    2. 内存活跃会话（实时数据）
    3. JSON 内 session_id 字段全目录扫描（兜底）

    当 session_id 实际上是文件名 stem（如 subagent 日志的
    ``heartbeat_decision_早柚_xxx_c7b1408f_20260531_134144``）时，
    文件名 stem 匹配能直接命中，无需全目录扫描。
    """
    # 规范化：空字符串视为未指定（与 None 等价）
    if session_uuid is not None and session_uuid.strip() == "":
        session_uuid = None

    # 1. 优先按文件名 stem 精确匹配（O(1)）
    #    subagent 日志的 session_id 常等于文件名 stem，直接命中
    file_stem_data = _find_log_by_file_stem(session_id)
    if file_stem_data is not None:
        # 如果指定了 uuid，需验证
        if session_uuid is None or file_stem_data.get("session_uuid") == session_uuid:
            return file_stem_data

    # 2. 从内存查找（活跃会话）
    registry = get_ai_session_registry()
    session = registry.get_ai_session(session_id)
    if session is not None:
        logger_obj = session._session_logger
        mem_uuid: str = logger_obj.session_uuid
        # 如果指定了 uuid，必须匹配；否则取内存中的
        if session_uuid is None or mem_uuid == session_uuid:
            mem_linked: List[LinkedAgentRecord] = logger_obj.linked_agents
            mem_ended_at: Optional[float] = logger_obj.ended_at
            mem_detail: Dict[str, Any] = {
                "session_id": session_id,
                "session_uuid": mem_uuid,
                "persona_name": logger_obj.persona_name,
                "create_by": logger_obj.create_by,
                "is_subagent": logger_obj.is_subagent,
                "created_at": logger_obj.created_at,
                "updated_at": logger_obj.updated_at,
                "ended_at": mem_ended_at,
                "is_active": mem_ended_at is None,
                "entry_count": len(logger_obj.entries),
                "entries": logger_obj.entries,
                "linked_agents": _enrich_linked_agents_list(mem_linked),
                "linked_agent_count": len(mem_linked),
                "source": "memory" if logger_obj.has_unpersisted_data else "disk",
            }
            return mem_detail

    # 3. 从磁盘文件查找（按 JSON 内 session_id 字段全目录扫描，兜底）
    best_file: Optional[SessionLogFileData] = None
    best_updated_at: float = 0.0
    for path in _list_log_files():
        data = _load_log_detail(path)
        if data is None:
            continue
        if data.get("session_id") != session_id:
            continue
        # 如果指定了 uuid，必须匹配
        if session_uuid is not None and data.get("session_uuid") != session_uuid:
            continue
        ua: float = data.get("updated_at", 0)
        if ua > best_updated_at:
            best_updated_at = ua
            best_file = data

    if best_file is None:
        return None

    # 在原始文件结构上补充响应字段（source / is_active / 已 enrich 的 linked_agents）
    resp: Dict[str, Any] = dict(best_file)
    resp["source"] = "disk"
    # 修正 is_active：磁盘 ended_at 为 null 时需检查内存 registry
    disk_ended: Optional[float] = best_file.get("ended_at")
    if disk_ended is None:
        in_mem = registry.get_ai_session(session_id)
        resp["is_active"] = in_mem is not None
        if in_mem is None:
            resp["ended_at"] = best_file.get("updated_at", 0)
    else:
        resp["is_active"] = False
    disk_linked = best_file.get("linked_agents", [])
    if disk_linked:
        resp["linked_agents"] = _enrich_linked_agents_list(disk_linked)
    return resp


# ─────────────────────────────────────────────
# 会话来源（create_by）分类目录
#
# 每个 AI 会话在创建 AISessionLogger 时都会带一个 create_by 标识其来源
# （Chat / MemCategorization / Heartbeat_Decision / Heartbeat_Output ...）。
# 此目录把原始 create_by 映射为前端可读的展示名、说明与分组，供分类筛选使用。
# 未在目录中的来源会按前缀规则或回退到 "other" 分组，不会报错。
# ─────────────────────────────────────────────

# create_by -> (前端展示名, 说明, 所属分组)
_CREATE_BY_CATALOG: Dict[str, Tuple[str, str, str]] = {
    "Chat": ("聊天对话", "用户与 AI 的常规聊天会话", "chat"),
    "Agent": ("Agent 对话", "Agent 模式下的多轮工具调用会话", "chat"),
    "LLM": ("通用 LLM", "未显式指定来源的通用 LLM 调用", "chat"),
    "SubAgent": ("子 Agent", "由主会话派生的子 Agent 执行", "agent"),
    "AutoPlanner": ("自动规划", "AutoPlanner 自动拆解并规划任务执行", "agent"),
    "CapabilityEvaluator": ("能力评估", "评估当前消息是否触发能力 Agent", "capability"),
    "CapabilityAgent": ("能力 Agent", "执行具体能力的 Agent 会话", "capability"),
    "ImageUnderstand": ("图片理解", "对图片内容进行理解分析", "image"),
    "ImageDescSummary": ("图片描述汇总", "汇总多张图片的描述信息", "image"),
    "BuildPersona": ("人格构建", "构建 / 更新 AI 人格设定", "persona"),
    "Heartbeat_Decision": ("心跳决策", "心跳机制判断是否主动发言", "heartbeat"),
    "Heartbeat_Output": ("心跳输出", "心跳机制生成主动发言内容", "heartbeat"),
    "MemeTagger": ("表情包打标", "为表情包生成描述与标签", "meme"),
    "MemCategorization": ("记忆分类", "对记忆进行分层图谱分类", "memory"),
    "MemGroupSummary": ("群记忆摘要", "生成群组级记忆摘要", "memory"),
    "MemEntityExtraction": ("记忆实体抽取", "从对话中抽取记忆实体", "memory"),
    "MemNodeSelection": ("记忆节点选择", "System-2 全局记忆节点选择", "memory"),
    "Kanban_Relay": ("看板中继", "看板任务的中继执行", "kanban"),
    "ScheduledTask_Exec": ("定时任务执行", "定时任务触发的 AI 执行", "scheduled"),
}

# 前缀匹配：create_by 动态拼接的来源（如 Proactive_heartbeat / Proactive_tool）
_CREATE_BY_PREFIX_CATALOG: Dict[str, Tuple[str, str, str]] = {
    "Proactive_": ("主动消息", "主动消息发送器触发的会话", "proactive"),
}


def _categorize_create_by(create_by: Optional[str]) -> Dict[str, str]:
    """把原始 create_by 归一化为带展示名/说明/分组的分类元数据。"""
    cb = create_by or "Unknown"
    meta = _CREATE_BY_CATALOG.get(cb)
    if meta is not None:
        label, desc, group = meta
        return {"create_by": cb, "label": label, "description": desc, "group": group}

    for prefix, (label, desc, group) in _CREATE_BY_PREFIX_CATALOG.items():
        if cb.startswith(prefix):
            suffix = cb[len(prefix) :]
            display = f"{label}({suffix})" if suffix else label
            return {"create_by": cb, "label": display, "description": desc, "group": group}

    return {"create_by": cb, "label": cb, "description": "未归类的会话来源", "group": "other"}


# ─────────────────────────────────────────────
# 1. 统一日志列表 API（合并内存 + 磁盘，去重）
# ─────────────────────────────────────────────


@app.get("/api/ai/session_logs")
async def list_session_logs(
    session_id: Optional[str] = None,
    create_by: Optional[str] = None,
    persona_name: Optional[str] = None,
    is_active: Optional[bool] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取 AI Session 日志列表（统一合并内存活跃 + 磁盘持久化，去重）

    返回综合列表，每个条目包含 source 字段标识来源（"memory" 或 "disk"），
    is_active 字段标识是否仍在运行。同一 session_uuid 在内存和磁盘中都存在时，
    优先使用内存版本（数据更新）。结果按创建时间倒序排列。

    Args:
        session_id: 按 session_id 精确筛选
        create_by: 按创建来源筛选
        persona_name: 按 Persona 名称筛选
        is_active: 按是否活跃筛选 (true=仅活跃, false=仅已结束)
        date_from: 起始日期 YYYY-MM-DD
        date_to: 结束日期 YYYY-MM-DD
        limit: 返回数量限制
        offset: 偏移量

    Returns:
        status: 0成功，1失败
        data: 日志列表及分页信息
    """
    try:
        # 同步阻塞（大量磁盘读取/JSON 解析）放线程池避免冻结事件循环；走极短 TTL 缓存把
        # 一次页面加载的 list/overview/categories 三接口并发构建合并为一次。
        unified = await run_in_threadpool(_build_unified_list_cached)
        filtered = _apply_filters(
            unified,
            session_id=session_id,
            create_by=create_by,
            persona_name=persona_name,
            is_active=is_active,
            date_from=date_from,
            date_to=date_to,
        )

        total: int = len(filtered)
        paginated = filtered[offset : offset + limit]

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "items": paginated,
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        }
    except Exception as e:
        logger.error(f"📝 [SessionLogsAPI] 获取日志列表失败: {e}")
        return {
            "status": 1,
            "msg": f"获取日志列表失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 2. 日志详情 API（按 session_id + session_uuid 查找，优先内存）
# ─────────────────────────────────────────────


def _handle_detail_request(
    session_id: str,
    session_uuid: Optional[str] = None,
) -> Dict:
    """日志详情请求的统一处理逻辑，供多个路由复用"""
    try:
        data = _find_log_by_session_id_and_uuid(session_id, session_uuid)
        if data is None:
            return {
                "status": 1,
                "msg": f"未找到 Session 日志: {session_id}/{session_uuid}",
                "data": None,
            }

        return {"status": 0, "msg": "ok", "data": data}
    except Exception as e:
        logger.error(f"📝 [SessionLogsAPI] 获取日志详情失败: {e}")
        return {
            "status": 1,
            "msg": f"获取日志详情失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/session_logs/detail")
async def get_session_log_detail_by_query(
    session_id: str,
    session_uuid: Optional[str] = None,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取指定 Session 实例的日志详情（查询参数版，推荐）

    通过查询参数传递 session_id 和 session_uuid，避免路径参数中特殊字符
    （如冒号、中文、连续斜杠）导致的路由匹配问题。

    优先从内存查找活跃会话的实时日志，若不存在则从磁盘文件查找。

    Args:
        session_id: Session ID（如 ws-onebot:onebot:bot_001:group:123456）
            或文件名 stem（如 heartbeat_decision_早柚_xxx_c7b1408f_20260531_134144）
        session_uuid: Session 实例 UUID（如 abc12345），可选；
            省略时返回该 session_id 最新的实例

    Returns:
        status: 0成功，1失败
        data: 完整日志数据
    """
    return _handle_detail_request(session_id, session_uuid)


@app.get("/api/ai/session_logs/{session_id}/detail")
async def get_session_log_detail(
    session_id: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取指定 Session 实例的日志详情（路径参数版）

    通过 session_id 精确定位。同一 session_id 可能有多个实例（不同
    session_uuid），返回最新的实例。

    Args:
        session_id: Session ID 或文件名 stem

    Returns:
        status: 0成功，1失败
        data: 完整日志数据
    """
    return _handle_detail_request(session_id, None)


@app.get("/api/ai/session_logs/{session_id}/{session_uuid}/detail")
async def get_session_log_detail_with_uuid(
    session_id: str,
    session_uuid: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取指定 Session 实例的日志详情（路径参数版，含 UUID）

    通过 session_id + session_uuid 精确定位到某个具体实例。

    Args:
        session_id: Session ID 或文件名 stem
        session_uuid: Session 实例 UUID

    Returns:
        status: 0成功，1失败
        data: 完整日志数据
    """
    return _handle_detail_request(session_id, session_uuid)


@app.get("/api/ai/session_logs/{rest:path}/detail")
async def get_session_log_detail_catch_all(
    rest: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    日志详情 catch-all 路由（处理 URL 中含连续斜杠等边缘情况）

    前端可能构造 ``/api/ai/session_logs/{file_stem}//detail`` 这样的 URL
    （session_uuid 为空时产生连续斜杠），此时标准路径参数路由无法匹配。
    本路由使用 ``:path`` 转换器兜底捕获，并手动解析 session_id / session_uuid。

    Args:
        rest: catch-all 路径段，如 ``file_stem/`` 或 ``file_stem/uuid``

    Returns:
        status: 0成功，1失败
        data: 完整日志数据
    """
    # 解析 rest 路径：按 "/" 拆分，第一段为 session_id，第二段（可选）为 session_uuid
    parts = rest.split("/", 1)
    session_id = parts[0]
    session_uuid = parts[1] if len(parts) > 1 else None
    # 规范化：空字符串视为未指定
    if session_uuid is not None and session_uuid.strip() == "":
        session_uuid = None

    return _handle_detail_request(session_id, session_uuid)


# ─────────────────────────────────────────────
# 3. 日志文件详情 API（按文件名查找，调试用）
# ─────────────────────────────────────────────


@app.get("/api/ai/session_logs/file/{file_name}")
async def get_session_log_by_file(
    file_name: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    按文件名获取单个持久化日志详情（调试用）

    直接从磁盘读取指定 JSON 文件。适用于需要查看特定历史实例的场景。

    Args:
        file_name: 日志文件名（含 .json 后缀）

    Returns:
        status: 0成功，1失败
        data: 完整日志数据
    """
    try:
        # 安全检查：防止目录遍历
        if ".." in file_name or "/" in file_name or "\\" in file_name:
            return {"status": 1, "msg": "非法文件名", "data": None}

        # 使用 _find_log_by_file_stem 按文件名 stem 查找（不含 .json 后缀）
        stem = file_name.removesuffix(".json")
        data = _find_log_by_file_stem(stem)
        if data is not None:
            return {"status": 0, "msg": "ok", "data": data}

        # 兜底：传统路径查找（兼容极端情况）
        path = AI_SESSION_LOGS_PATH / file_name
        if not path.exists():
            path = AI_SUBAGENT_LOGS_PATH / file_name
        if not path.exists():
            return {"status": 1, "msg": f"未找到日志文件: {file_name}", "data": None}

        file_data = _load_log_detail(path)
        if file_data is None:
            return {"status": 1, "msg": f"解析日志文件失败: {file_name}", "data": None}

        # 在原始文件结构上 enrich linked_agents（type_counts / entry_count / is_active）
        resp: Dict[str, Any] = dict(file_data)
        file_linked = file_data.get("linked_agents", [])
        if file_linked:
            resp["linked_agents"] = _enrich_linked_agents_list(file_linked)

        return {"status": 0, "msg": "ok", "data": resp}
    except Exception as e:
        logger.error(f"📝 [SessionLogsAPI] 获取日志文件失败: {e}")
        return {
            "status": 1,
            "msg": f"获取日志文件失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 4. 查询会话关联 Agent API（支持 agent_mesh 扩展）
# ─────────────────────────────────────────────


@app.get("/api/ai/session_logs/{session_id}/linked_agents")
async def get_session_linked_agents(
    session_id: str,
    agent_type: Optional[str] = None,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取指定 Session 关联的 Agent 列表

    返回与该 Session 关联的所有 Agent（SubAgent、PeerAgent、ParentAgent 等）。
    支持按 agent_type 过滤，为前端展示 Agent 关系图提供数据。

    Args:
        session_id: Session ID（如 ws-onebot:onebot:bot_001:group:123456）
        agent_type: 可选的关联类型过滤
                    * "sub_agent"    – 由本 Agent 创建的子 Agent
                    * "peer_agent"   – 同级/对等 Agent（预留）
                    * "parent_agent" – 父 Agent（预留）
                    * None           – 返回全部关联 Agent

    Returns:
        status: 0成功，1失败
        data: {
            "session_id": str,
            "session_uuid": str|None,
            "linked_agents": [{
                "agent_type": str,
                "session_id": str,
                "session_uuid": str,
                "persona_name": str|None,
                "create_by": str|None,
                "linked_at": float,
            }],
            "total": int,
            "by_type": {"sub_agent": int, "peer_agent": int, "parent_agent": int},
        }
    """
    try:
        registry = get_ai_session_registry()
        session = registry.get_ai_session(session_id)

        linked_agents: List[LinkedAgentRecord] = []
        session_uuid: Optional[str] = None

        # 1. 优先从内存获取
        if session is not None:
            logger_obj = session._session_logger
            session_uuid = logger_obj.session_uuid
            all_linked: List[LinkedAgentRecord] = logger_obj.linked_agents
            if agent_type:
                linked_agents = [a for a in all_linked if a.get("agent_type") == agent_type]
            else:
                linked_agents = list(all_linked)
        else:
            # 2. 从磁盘文件查找
            best_file: Optional[SessionLogFileData] = None
            best_updated_at: float = 0.0
            for path in _list_log_files():
                data = _load_log_detail(path)
                if data is None:
                    continue
                if data.get("session_id") != session_id:
                    continue
                ua: float = data.get("updated_at", 0)
                if ua > best_updated_at:
                    best_updated_at = ua
                    best_file = data
            if best_file is not None:
                session_uuid = best_file.get("session_uuid")
                disk_linked: List[LinkedAgentRecord] = best_file.get("linked_agents", [])
                if agent_type:
                    linked_agents = [a for a in disk_linked if a.get("agent_type") == agent_type]
                else:
                    linked_agents = list(disk_linked)

        # 按类型统计
        by_type: Dict[str, int] = {"sub_agent": 0, "peer_agent": 0, "parent_agent": 0}
        for agent in linked_agents:
            atype: str = agent.get("agent_type", "unknown")
            by_type[atype] = by_type.get(atype, 0) + 1

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "session_id": session_id,
                "session_uuid": session_uuid,
                "linked_agents": _enrich_linked_agents_list(linked_agents),
                "total": len(linked_agents),
                "by_type": by_type,
            },
        }
    except Exception as e:
        logger.error(f"📝 [SessionLogsAPI] 获取关联 Agent 失败: {e}")
        return {
            "status": 1,
            "msg": f"获取关联 Agent 失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 5. 日志统计 API
# ─────────────────────────────────────────────


@app.get("/api/ai/session_logs/stats/overview")
async def get_session_logs_overview(
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取 Session 日志统计概览

    返回日志总数、今日新增、活跃 Session 数、关联 Agent 数等统计信息。

    Returns:
        status: 0成功，1失败
        data: 统计概览
    """
    try:
        unified = await run_in_threadpool(_build_unified_list_cached)

        today_str: str = datetime.now().strftime("%Y-%m-%d")
        today_count: int = 0
        active_count: int = 0
        memory_count: int = 0
        disk_count: int = 0
        create_by_counts: Dict[str, int] = {}
        linked_agent_total: int = 0
        linked_agent_by_type: Dict[str, int] = {}

        for item in unified:
            created_str: Optional[str] = item["created_at_str"]
            if created_str and created_str.startswith(today_str):
                today_count += 1

            if item["is_active"]:
                active_count += 1

            source: str = item["source"]
            if source == "memory":
                memory_count += 1
            elif source == "disk":
                disk_count += 1

            cb: Optional[str] = item["create_by"]
            if cb:
                create_by_counts[cb] = create_by_counts.get(cb, 0) + 1

            # 统计关联 Agent
            for agent in item["linked_agents"]:
                linked_agent_total += 1
                atype: str = agent["agent_type"]
                linked_agent_by_type[atype] = linked_agent_by_type.get(atype, 0) + 1

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "total": len(unified),
                "today_count": today_count,
                "active_count": active_count,
                "memory_count": memory_count,
                "disk_count": disk_count,
                "create_by_distribution": create_by_counts,
                "linked_agent_total": linked_agent_total,
                "linked_agent_by_type": linked_agent_by_type,
                "log_path": str(AI_SESSION_LOGS_PATH),
            },
        }
    except Exception as e:
        logger.error(f"📝 [SessionLogsAPI] 获取日志统计失败: {e}")
        return {
            "status": 1,
            "msg": f"获取日志统计失败: {str(e)}",
            "data": None,
        }


# ─────────────────────────────────────────────
# 6. 日志分类 API（按会话来源 create_by 聚合）
# ─────────────────────────────────────────────


@app.get("/api/ai/session_logs/categories")
async def get_session_log_categories(
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取当前后台日志的会话分类（按来源 create_by 聚合）

    扫描内存活跃会话 + 磁盘持久化日志中实际出现的所有会话来源（create_by，
    如 Chat / MemCategorization / Heartbeat_Decision / Heartbeat_Output 等），
    为每个分类附带前端展示名、说明、所属分组与数量统计，供前端渲染分类筛选 Tab/Chip。
    返回的 create_by 可直接作为 /api/ai/session_logs 的 create_by 查询参数使用。

    Returns:
        status: 0成功，1失败
        data:
            categories: 分类列表（按 count 倒序），每项含:
                create_by:       原始来源标识，可直接用于列表接口筛选
                label:           前端展示名
                description:     分类说明
                group:           所属分组（chat/memory/heartbeat/...）
                count:           该来源会话总数
                active_count:    其中仍活跃的会话数
                subagent_count:  其中属于 SubAgent 的会话数
            groups: 分组维度的会话数量聚合 {group: count}
            total: 分类种类数
    """
    try:
        # 与列表/概览接口一致，走带极短 TTL 的缓存并放线程池执行，避免阻塞事件循环。
        unified = await run_in_threadpool(_build_unified_list_cached)

        stats: Dict[str, Dict[str, Any]] = {}
        group_counts: Dict[str, int] = {}

        for item in unified:
            cb: str = item["create_by"] or "Unknown"
            meta = _categorize_create_by(cb)
            entry = stats.get(cb)
            if entry is None:
                entry = {**meta, "count": 0, "active_count": 0, "subagent_count": 0}
                stats[cb] = entry
            entry["count"] += 1
            if item["is_active"]:
                entry["active_count"] += 1
            if item["is_subagent"]:
                entry["subagent_count"] += 1
            group_counts[meta["group"]] = group_counts.get(meta["group"], 0) + 1

        categories = sorted(stats.values(), key=lambda x: x["count"], reverse=True)

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "categories": categories,
                "groups": group_counts,
                "total": len(categories),
            },
        }
    except Exception as e:
        logger.error(f"📝 [SessionLogsAPI] 获取日志分类失败: {e}")
        return {
            "status": 1,
            "msg": f"获取日志分类失败: {str(e)}",
            "data": None,
        }
