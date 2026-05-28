"""
AI Session Logs APIs
提供 AI Agent 会话执行日志的 RESTful APIs

统一合并内存活跃会话 + 本地持久化日志，去重后提供给前端，
便于前端渲染 AI 调用历史栈，清晰展示每一步结果。
"""

import json
import time
from typing import Any, Dict, List, Literal, Optional, TypedDict, cast
from pathlib import Path
from datetime import datetime

from fastapi import Depends
from pydantic import Field, BaseModel

from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import AI_SESSION_LOGS_PATH, AI_SUBAGENT_LOGS_PATH
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.session_registry import get_ai_session_registry

# ─────────────────────────────────────────────
# TypedDict 定义
# ─────────────────────────────────────────────


class SessionLogEntry(TypedDict):
    """单条日志条目"""

    type: str
    timestamp: float
    data: Dict[str, Any]  # type-specific payload: user_input, tool_call, tools_list, etc.


class LinkedAgentRecord(TypedDict):
    """关联 Agent 的基本信息（在 link_agent 时写入）"""

    agent_type: str
    session_id: str
    session_uuid: str
    persona_name: Optional[str]
    create_by: Optional[str]
    log_file: Optional[str]
    linked_at: float


class LinkedAgentEnriched(LinkedAgentRecord, total=False):
    """Enriched 后的关联 Agent 条目"""

    entry_count: int
    type_counts: Dict[str, int]
    is_active: bool
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
    linked_agents: List[LinkedAgentEnriched]
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


def _build_summary_from_memory(sid: str, session: Any) -> SessionLogSummary:
    """从内存中的活跃 Session 构建日志摘要"""
    logger_obj: Optional[Any] = getattr(session, "_session_logger", None)

    if logger_obj is not None:
        entries: List[Dict[str, Any]] = getattr(logger_obj, "entries", [])
        created_at: float = getattr(logger_obj, "created_at", 0)
        updated_at: float = getattr(logger_obj, "updated_at", 0)
        session_uuid: Optional[str] = getattr(logger_obj, "session_uuid", None)
        persona_name: Optional[str] = getattr(logger_obj, "persona_name", None)
        create_by: Optional[str] = getattr(logger_obj, "create_by", None)
        is_subagent: bool = getattr(logger_obj, "is_subagent", False)
        ended_at: Optional[float] = getattr(logger_obj, "ended_at", None)
        file_name: Optional[str] = str(getattr(logger_obj, "_file_path", Path("")).name) or None
        linked_agents: List[Dict[str, Any]] = getattr(logger_obj, "linked_agents", [])
        # 判断内存中是否有未落盘的新数据：有则 source=memory，否则 source=disk
        _has_unpersisted: bool = getattr(logger_obj, "has_unpersisted_data", True)
    else:
        entries = []
        created_at = 0
        updated_at = 0
        session_uuid = None
        persona_name = None
        create_by = None
        is_subagent = False
        ended_at = None
        file_name = None
        linked_agents = []
        _has_unpersisted = True

    # 计算运行时长
    duration: Optional[float] = None
    if ended_at and created_at:
        duration = ended_at - created_at
    elif created_at:
        duration = time.time() - created_at

    # 统计各类型条目数量
    type_counts: Dict[str, int] = {}
    for entry in entries:
        etype: str = entry.get("type", "unknown")
        type_counts[etype] = type_counts.get(etype, 0) + 1

    return {
        "session_id": sid,
        "session_uuid": session_uuid,
        "persona_name": persona_name,
        "create_by": create_by,
        "is_subagent": is_subagent,
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
        "source": "memory" if _has_unpersisted else "disk",
        "file_name": file_name,
        "linked_agents": _enrich_linked_agents_list(linked_agents),
        "linked_agent_count": len(linked_agents),
    }


def _enrich_linked_agent(agent_record: Dict[str, Any]) -> LinkedAgentEnriched:
    """
    为 linked_agent 条目补充聚合统计信息（type_counts, entry_count, is_active 等）。

    优先从内存（HistoryManager）读取，若不存在则从磁盘文件读取。
    若关联的 agent 日志不可读，返回原始记录（仅含基本信息）。
    """
    enriched = dict(agent_record)  # 复制，避免修改原始数据

    agent_session_id: Optional[str] = agent_record.get("session_id")
    if not agent_session_id:
        return cast(LinkedAgentEnriched, enriched)

    # 1. 优先从内存查找
    registry = get_ai_session_registry()
    session = registry.get_ai_session(agent_session_id)
    if session is not None:
        logger_obj = getattr(session, "_session_logger", None)
        if logger_obj is not None:
            entries: List[Dict[str, Any]] = getattr(logger_obj, "entries", [])
            type_counts: Dict[str, int] = {}
            for entry in entries:
                etype = entry.get("type", "unknown")
                type_counts[etype] = type_counts.get(etype, 0) + 1
            enriched["entry_count"] = len(entries)
            enriched["type_counts"] = type_counts
            enriched["is_active"] = getattr(logger_obj, "ended_at", None) is None
            enriched["created_at"] = getattr(logger_obj, "created_at", 0)
            enriched["ended_at"] = getattr(logger_obj, "ended_at", None)
            enriched["source"] = "memory" if getattr(logger_obj, "has_unpersisted_data", True) else "disk"
            return cast(LinkedAgentEnriched, enriched)

    # 2. 从磁盘文件查找（使用 log_file 路径或 session_id + session_uuid 匹配）
    log_file: Optional[str] = agent_record.get("log_file")
    if log_file:
        # 尝试直接使用 log_file 路径
        path = Path(log_file)
        if path.exists() and path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data: Dict[str, Any] = json.load(f)
                entries = data.get("entries", [])
                type_counts = {}
                for entry in entries:
                    etype = entry.get("type", "unknown")
                    type_counts[etype] = type_counts.get(etype, 0) + 1
                enriched["entry_count"] = len(entries)
                enriched["type_counts"] = type_counts
                # 磁盘上 ended_at 为 null 不代表仍活跃——可能只是定时落盘未写 ended_at。
                # 只有在内存 registry 中存在的 session 才是真正活跃的。
                _disk_ended_at = data.get("ended_at")
                if _disk_ended_at is None:
                    _agent_in_mem = get_ai_session_registry().get_ai_session(agent_session_id)
                    enriched["is_active"] = _agent_in_mem is not None
                    if _agent_in_mem is None:
                        enriched["ended_at"] = data.get("updated_at")
                    else:
                        enriched["ended_at"] = None
                else:
                    enriched["is_active"] = False
                    enriched["ended_at"] = _disk_ended_at
                enriched["created_at"] = data.get("created_at", 0)
                enriched["source"] = "disk"
                return cast(LinkedAgentEnriched, enriched)
            except Exception:
                pass

    # 3. 回退：通过 session_id + session_uuid 在磁盘目录中搜索
    agent_uuid: Optional[str] = agent_record.get("session_uuid")
    if agent_uuid:
        for p in _list_log_files():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("session_id") == agent_session_id and data.get("session_uuid") == agent_uuid:
                    entries = data.get("entries", [])
                    type_counts = {}
                    for entry in entries:
                        etype = entry.get("type", "unknown")
                        type_counts[etype] = type_counts.get(etype, 0) + 1
                    enriched["entry_count"] = len(entries)
                    enriched["type_counts"] = type_counts
                    # 同上：磁盘 ended_at 为 null 时需检查内存 registry
                    _disk_ended_at = data.get("ended_at")
                    if _disk_ended_at is None:
                        _agent_in_mem = get_ai_session_registry().get_ai_session(agent_session_id)
                        enriched["is_active"] = _agent_in_mem is not None
                        if _agent_in_mem is None:
                            enriched["ended_at"] = data.get("updated_at")
                        else:
                            enriched["ended_at"] = None
                    else:
                        enriched["is_active"] = False
                        enriched["ended_at"] = _disk_ended_at
                    enriched["created_at"] = data.get("created_at", 0)
                    enriched["source"] = "disk"
                    return cast(LinkedAgentEnriched, enriched)
            except Exception:
                continue

    # 无法获取详情，添加空占位
    enriched.setdefault("entry_count", 0)
    enriched.setdefault("type_counts", {})
    enriched.setdefault("is_active", None)
    enriched.setdefault("source", "unavailable")
    return cast(LinkedAgentEnriched, enriched)


def _enrich_linked_agents_list(
    agents: List[Dict[str, Any]],
) -> List[LinkedAgentEnriched]:
    """对 linked_agents 列表中的每个条目进行 enrichment"""
    return [_enrich_linked_agent(a) for a in agents]


def _parse_log_file(path: Path) -> Optional[SessionLogSummary]:
    """解析单个日志 JSON 文件，返回摘要信息"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data: Dict[str, Any] = json.load(f)

        created_at: float = data.get("created_at", 0)
        ended_at: Optional[float] = data.get("ended_at")
        entry_count: int = data.get("entry_count", 0)
        linked_agents: List[Dict[str, Any]] = data.get("linked_agents", [])

        # 计算运行时长
        duration: Optional[float] = None
        if ended_at and created_at:
            duration = ended_at - created_at
        elif created_at:
            duration = time.time() - created_at

        # 统计各类型条目数量
        type_counts: Dict[str, int] = {}
        for entry in data.get("entries", []):
            etype: str = entry.get("type", "unknown")
            type_counts[etype] = type_counts.get(etype, 0) + 1

        return cast(
            SessionLogSummary,
            {
                "session_id": data.get("session_id"),
                "session_uuid": data.get("session_uuid"),
                "persona_name": data.get("persona_name"),
                "create_by": data.get("create_by"),
                "is_subagent": data.get("is_subagent", False),
                "created_at": created_at,
                "created_at_str": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S")
                if created_at
                else None,
                "updated_at": data.get("updated_at"),
                "updated_at_str": (
                    datetime.fromtimestamp(data.get("updated_at", 0)).strftime("%Y-%m-%d %H:%M:%S")
                    if data.get("updated_at")
                    else None
                ),
                "ended_at": ended_at,
                "ended_at_str": datetime.fromtimestamp(ended_at).strftime("%Y-%m-%d %H:%M:%S") if ended_at else None,
                "duration_seconds": round(duration, 2) if duration else None,
                "entry_count": entry_count,
                "type_counts": type_counts,
                "is_active": ended_at is None,
                "source": "disk",
                "file_name": path.name,
                "linked_agents": _enrich_linked_agents_list(linked_agents),
                "linked_agent_count": len(linked_agents),
            },
        )
    except Exception:
        return None


def _load_log_detail(path: Path) -> Optional[Dict[str, Any]]:
    """加载单个日志文件的完整内容（保持 Dict[str, Any] 因为是原始 JSON）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data: Dict[str, Any] = json.load(f)
        return data
    except Exception:
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
    # 1. 收集内存中活跃 Session
    memory_map: Dict[str, SessionLogSummary] = {}  # session_uuid -> summary
    memory_session_id_map: Dict[str, str] = {}  # session_id -> session_uuid (用于快速查找)

    registry = get_ai_session_registry()
    sessions = registry.get_all_ai_sessions()

    for sid, session in sessions.items():
        summary = _build_summary_from_memory(sid, session)
        uuid_val = summary.get("session_uuid")
        if uuid_val:
            memory_map[uuid_val] = summary
            memory_session_id_map[sid] = uuid_val
        else:
            # 没有 uuid 的兜底：用 session_id 作为 key
            memory_map[sid] = summary

    # 2. 收集磁盘持久化文件
    disk_map: Dict[str, SessionLogSummary] = {}  # session_uuid -> summary
    for path in _list_log_files():
        info = _parse_log_file(path)
        if info is None:
            continue
        uuid_val: Optional[str] = info.get("session_uuid")
        if uuid_val:
            # 同一 uuid 可能有多份文件（异常情况），取最新的
            existing = disk_map.get(uuid_val)
            if existing is None or info.get("updated_at", 0) > existing.get("updated_at", 0):
                disk_map[uuid_val] = info  # type: ignore
        else:
            # 没有 uuid 的兜底：用 file_name 作为 key
            disk_map[path.name] = info  # type: ignore

    # 3. 合并去重：内存优先
    unified: Dict[str, SessionLogSummary] = {}

    # 先加入磁盘文件
    for key, info in disk_map.items():
        unified[key] = info

    # 内存版本覆盖磁盘版本（同一 session_uuid）
    for key, info in memory_map.items():
        unified[key] = info

    # 4. 修正 is_active：磁盘上 ended_at 为 null 的 session 不一定仍活跃，
    #    只有在内存 registry 中真正存在的 session 才是活跃的。
    #    （AISessionLogger 定时落盘不写 ended_at，只有 close() 才写；
    #    进程重启后旧 session 不在内存中，应视为已结束。）
    memory_session_ids: set = set(sessions.keys())  # 内存中真正活跃的 session_id 集合
    for key, info in unified.items():
        if info.get("is_active") and info.get("source") == "disk":
            sid = info.get("session_id", "")
            if sid not in memory_session_ids:
                info["is_active"] = False
                # 用 updated_at 近似作为 ended_at（最后一次活动时间）
                if info.get("ended_at") is None:
                    info["ended_at"] = info.get("updated_at")
                    ea = info["ended_at"]
                    info["ended_at_str"] = datetime.fromtimestamp(ea).strftime("%Y-%m-%d %H:%M:%S") if ea else None

    # 5. 按 created_at 倒序排列
    results = sorted(
        unified.values(),
        key=lambda x: x.get("created_at", 0),
        reverse=True,
    )

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
        if session_id and item.get("session_id") != session_id:
            continue
        if create_by and item.get("create_by") != create_by:
            continue
        if persona_name and item.get("persona_name") != persona_name:
            continue
        if is_active is not None and item.get("is_active") != is_active:
            continue

        if date_from or date_to:
            created_str: Optional[str] = item.get("created_at_str")
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
) -> Optional[SessionLogDetail]:
    """
    根据 session_id + session_uuid 查找日志详情

    当 session_uuid 提供时，精确匹配到具体实例；
    当 session_uuid 为 None 时，返回该 session_id 最新的实例（向后兼容）。

    优先从内存查找（活跃会话），其次从磁盘文件查找。
    """
    # 1. 优先从内存查找
    registry = get_ai_session_registry()
    session = registry.get_ai_session(session_id)
    if session is not None:
        logger_obj: Optional[Any] = getattr(session, "_session_logger", None)
        if logger_obj is not None:
            mem_uuid: Optional[str] = getattr(logger_obj, "session_uuid", None)
            # 如果指定了 uuid，必须匹配；否则取内存中的
            if session_uuid is None or mem_uuid == session_uuid:
                linked_agents: List[Dict[str, Any]] = getattr(logger_obj, "linked_agents", [])
                _mem_ended_at = getattr(logger_obj, "ended_at", None)
                return cast(
                    SessionLogDetail,
                    {
                        "session_id": session_id,
                        "session_uuid": mem_uuid,
                        "persona_name": getattr(logger_obj, "persona_name", None),
                        "create_by": getattr(logger_obj, "create_by", None),
                        "is_subagent": getattr(logger_obj, "is_subagent", False),
                        "created_at": getattr(logger_obj, "created_at", 0),
                        "updated_at": getattr(logger_obj, "updated_at", 0),
                        "ended_at": _mem_ended_at,
                        "is_active": _mem_ended_at is None,
                        "entry_count": len(getattr(logger_obj, "entries", [])),
                        "entries": getattr(logger_obj, "entries", []),
                        "linked_agents": _enrich_linked_agents_list(linked_agents),
                        "linked_agent_count": len(linked_agents),
                        "source": "memory" if getattr(logger_obj, "has_unpersisted_data", True) else "disk",
                    },
                )

    # 2. 从磁盘文件查找（按 JSON 内 session_id 字段匹配）
    best_data: Optional[Dict[str, Any]] = None
    best_updated_at: float = 0

    for path in _list_log_files():
        data = _load_log_detail(path)
        if data is None:
            continue
        if data.get("session_id") != session_id:
            continue
        # 如果指定了 uuid，必须匹配
        if session_uuid is not None and data.get("session_uuid") != session_uuid:
            continue

        updated_at: float = data.get("updated_at", 0)
        if updated_at > best_updated_at:
            best_updated_at = updated_at
            best_data = data
            best_data["source"] = "disk"  # type: ignore
            # 修正 is_active：磁盘 ended_at 为 null 时需检查内存 registry
            _disk_ended = best_data.get("ended_at")
            if _disk_ended is None:
                _in_mem = registry.get_ai_session(session_id)
                best_data["is_active"] = _in_mem is not None
                if _in_mem is None:
                    best_data["ended_at"] = best_data.get("updated_at")
            else:
                best_data["is_active"] = False
            # Enrich linked_agents with type_counts, entry_count, is_active
            if best_data.get("linked_agents"):
                best_data["linked_agents"] = _enrich_linked_agents_list(best_data["linked_agents"])

    # 3. 回退：按文件名 stem 匹配
    #    前端可能把 file_name（如 xxx_uuid_20260529_040912.json）的 stem
    #    当作 session_id 传入，此时按 JSON 字段匹配不到，需要按文件名匹配。
    if best_data is None:
        target_stem = session_id  # 前端传入的可能是文件名 stem
        for path in _list_log_files():
            if path.stem == target_stem:
                data = _load_log_detail(path)
                if data is None:
                    continue
                # 如果指定了 uuid，也需匹配
                if session_uuid is not None and data.get("session_uuid") != session_uuid:
                    continue
                data["source"] = "disk"  # type: ignore
                # 修正 is_active
                _disk_ended = data.get("ended_at")
                _real_sid: str = data.get("session_id", "")
                if _disk_ended is None:
                    _in_mem = registry.get_ai_session(_real_sid)
                    data["is_active"] = _in_mem is not None
                    if _in_mem is None:
                        data["ended_at"] = data.get("updated_at")
                else:
                    data["is_active"] = False
                # Enrich linked_agents
                if data.get("linked_agents"):
                    data["linked_agents"] = _enrich_linked_agents_list(data["linked_agents"])
                best_data = data
                break  # 文件名精确匹配，最多一个

    return cast(Optional[SessionLogDetail], best_data)


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
        unified = _build_unified_list()
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


@app.get("/api/ai/session_logs/{session_id}/detail")
@app.get("/api/ai/session_logs/{session_id}/{session_uuid}/detail")
async def get_session_log_detail(
    session_id: str,
    session_uuid: Optional[str] = None,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取指定 Session 实例的日志详情

    通过 session_id + session_uuid 精确定位到某个具体实例。
    同一 session_id 可能有多个实例（不同 session_uuid），用于区分
    同一会话的不同运行记录。

    优先从内存查找活跃会话的实时日志，若不存在则从磁盘文件查找。

    Args:
        session_id: Session ID（如 ws-onebot:onebot:bot_001:group:123456）
        session_uuid: Session 实例 UUID（如 abc12345），可选；
            省略时返回该 session_id 最新的实例

    Returns:
        status: 0成功，1失败
        data: 完整日志数据
    """
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

        # 优先从主目录查找，再从 subagents 子目录查找
        path = AI_SESSION_LOGS_PATH / file_name
        if not path.exists():
            path = AI_SUBAGENT_LOGS_PATH / file_name
        if not path.exists():
            return {"status": 1, "msg": f"未找到日志文件: {file_name}", "data": None}

        data = _load_log_detail(path)
        if data is None:
            return {"status": 1, "msg": f"解析日志文件失败: {file_name}", "data": None}

        # Enrich linked_agents with type_counts, entry_count, is_active
        if data.get("linked_agents"):
            data["linked_agents"] = _enrich_linked_agents_list(data["linked_agents"])

        return {"status": 0, "msg": "ok", "data": data}
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

        linked_agents: List[Dict[str, Any]] = []
        session_uuid: Optional[str] = None

        # 1. 优先从内存获取
        if session is not None:
            logger_obj: Optional[Any] = getattr(session, "_session_logger", None)
            if logger_obj is not None:
                session_uuid = getattr(logger_obj, "session_uuid", None)
                all_linked: List[Dict[str, Any]] = getattr(logger_obj, "linked_agents", [])
                if agent_type:
                    linked_agents = [a for a in all_linked if a.get("agent_type") == agent_type]
                else:
                    linked_agents = list(all_linked)
        else:
            # 2. 从磁盘文件查找
            best_data: Optional[Dict[str, Any]] = None
            best_updated_at: float = 0
            for path in _list_log_files():
                data = _load_log_detail(path)
                if data is None:
                    continue
                if data.get("session_id") != session_id:
                    continue
                updated_at: float = data.get("updated_at", 0)
                if updated_at > best_updated_at:
                    best_updated_at = updated_at
                    best_data = data
            if best_data is not None:
                session_uuid = best_data.get("session_uuid")
                disk_linked: List[Dict[str, Any]] = best_data.get("linked_agents", [])
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
        unified = _build_unified_list()

        today_str: str = datetime.now().strftime("%Y-%m-%d")
        today_count: int = 0
        active_count: int = 0
        memory_count: int = 0
        disk_count: int = 0
        create_by_counts: Dict[str, int] = {}
        linked_agent_total: int = 0
        linked_agent_by_type: Dict[str, int] = {}

        for item in unified:
            created_str: Optional[str] = item.get("created_at_str")
            if created_str and created_str.startswith(today_str):
                today_count += 1

            if item.get("is_active"):
                active_count += 1

            source: Optional[str] = item.get("source")
            if source == "memory":
                memory_count += 1
            elif source == "disk":
                disk_count += 1

            cb: Optional[str] = item.get("create_by")
            if cb:
                create_by_counts[cb] = create_by_counts.get(cb, 0) + 1

            # 统计关联 Agent
            for agent in item.get("linked_agents", []):
                linked_agent_total += 1
                atype: str = agent.get("agent_type", "unknown")
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
