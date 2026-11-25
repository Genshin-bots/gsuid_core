"""定位每个勾选项要处理的数据：scope 键、会话、文件、行数与备份预估。"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass

from gsuid_core.models import Event
from gsuid_core.ai_core.memory.database import bulk_ops
from gsuid_core.ai_core.persona_reset.types import ResetItemKey

GROUP_PREFIXES: tuple[str, ...] = ("group:", "user_in_group:")
USER_PREFIXES: tuple[str, ...] = ("user_global:",)
GROUP_PROFILE_SCOPE = "__gscore_group_profile__"
SELF_MODEL_KEY = "self_model"

# 备份预估：流式导出含 gzip，取约 1.5 万行/s 的一半作安全系数。
_ROWS_PER_SEC = 7500.0
_BYTES_PER_ROW = 400
_FILE_BYTES_PER_SEC = 50 * 1024 * 1024
_ZIP_RATIO = 0.35


@dataclass
class SelfScopes:
    keys: list[str]
    legacy_keys: list[str]


async def self_scopes(persona_name: str) -> SelfScopes:
    """``self:{bot}:{persona}`` 全部 bot；该人格是迁移前旧键归属人时，带上 ``self:{bot}``。"""
    from gsuid_core.ai_core.migrations.persona_scope_v1 import get_legacy_owner_persona

    all_self = await bulk_ops.list_scope_keys(["self:"])
    own: list[str] = []
    legacy: list[str] = []
    is_owner = persona_name == get_legacy_owner_persona()
    for key in all_self:
        parts = key.split(":", 2)
        if len(parts) == 3 and parts[2] == persona_name:
            own.append(key)
        elif len(parts) == 2 and is_owner:
            legacy.append(key)
    return SelfScopes(keys=own + legacy, legacy_keys=legacy)


async def shared_scopes(item: ResetItemKey) -> list[str]:
    if item == "global_group":
        return await bulk_ops.list_scope_keys(GROUP_PREFIXES, include_preferences=False)
    if item == "global_user":
        return await bulk_ops.list_scope_keys(USER_PREFIXES, include_preferences=False)
    return []


async def self_model_scopes(persona_name: str) -> SelfScopes:
    """``AIPersistentState`` 中归属该人格的 ``self_model`` scope（含迁移前旧键归属）。"""
    from gsuid_core.ai_core.state_store.store import state_list_scopes_with_key
    from gsuid_core.ai_core.migrations.persona_scope_v1 import get_legacy_owner_persona

    all_self = await state_list_scopes_with_key(SELF_MODEL_KEY, scope_prefix="self:")
    own: list[str] = []
    legacy: list[str] = []
    is_owner = persona_name == get_legacy_owner_persona()
    for key in sorted(set(all_self)):
        parts = key.split(":", 2)
        if len(parts) == 3 and parts[2] == persona_name:
            own.append(key)
        elif len(parts) == 2 and is_owner:
            legacy.append(key)
    return SelfScopes(keys=own + legacy, legacy_keys=legacy)


async def shared_self_model_scopes(item: ResetItemKey) -> list[str]:
    """``AIPersistentState`` 中属于全局群聊或全局用户的 ``self_model`` scope。"""
    from gsuid_core.ai_core.state_store.store import state_list_scopes_with_key

    prefixes: tuple[str, ...] = ()
    if item == "global_group":
        prefixes = GROUP_PREFIXES
    elif item == "global_user":
        prefixes = USER_PREFIXES
    found: set[str] = set()
    for prefix in prefixes:
        found.update(await state_list_scopes_with_key(SELF_MODEL_KEY, scope_prefix=prefix))
    return sorted(found)


def _session_persona(session_id: str) -> str | None:
    from gsuid_core.ai_core.persona.config import persona_config_manager

    parts = session_id.split(":", 4)
    if len(parts) != 5 or parts[3] not in ("group", "private"):
        return None
    return persona_config_manager.get_persona_for_session(session_id)


@dataclass
class PersonaSessions:
    history_events: list[Event]
    session_ids: list[str]


def persona_sessions(persona_name: str) -> PersonaSessions:
    """当前解析到该人格的会话：消息历史里的 + AI 会话注册表里的（两边取并集）。"""
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    events: list[Event] = []
    ids: list[str] = []
    for ev in get_history_manager().list_sessions():
        sid = ev.session_id
        if _session_persona(sid) == persona_name:
            events.append(ev)
            ids.append(sid)
    for sid, agent in get_ai_session_registry().get_all_ai_sessions().items():
        if agent.persona_name == persona_name and sid not in ids:
            ids.append(sid)
    return PersonaSessions(history_events=events, session_ids=ids)


def journal_paths(persona_name: str) -> tuple[Path, Path, Path]:
    """（手账目录，自主探索笔记目录，迁移前的旧单文件）。"""
    from gsuid_core.ai_core.autonomy.tools import artifact_dir
    from gsuid_core.ai_core.journal.storage import legacy_journal_file, persona_journal_dir

    return persona_journal_dir(persona_name), artifact_dir(persona_name), legacy_journal_file(persona_name)


def scan_files_sync(paths: tuple[Path, ...]) -> tuple[int, int]:
    """（文件数，总字节数）。"""
    count = 0
    size = 0
    for path in paths:
        if path.is_file():
            count += 1
            size += path.stat().st_size
        elif path.is_dir():
            for sub in path.rglob("*"):
                if sub.is_file():
                    count += 1
                    size += sub.stat().st_size
    return count, size


def estimate_rows(rows: int) -> tuple[float, int]:
    return rows / _ROWS_PER_SEC + 0.2, rows * _BYTES_PER_ROW


def estimate_files(size_bytes: int, files: int) -> tuple[float, int]:
    return size_bytes / _FILE_BYTES_PER_SEC + files * 0.002 + 0.1, int(size_bytes * _ZIP_RATIO)
