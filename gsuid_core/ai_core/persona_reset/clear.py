"""各勾选项的删除动作。调用方负责先冻结写入（``guard.freeze``）。"""

from __future__ import annotations

import shutil
import asyncio
from pathlib import Path
from collections.abc import Callable, Sequence

from gsuid_core.pool import to_thread
from gsuid_core.ai_core.memory.database import bulk_ops
from gsuid_core.ai_core.persona_reset.targets import (
    SELF_MODEL_KEY,
    GROUP_PROFILE_SCOPE,
    journal_paths,
    scan_files_sync,
    persona_sessions,
)

_INGEST_IDLE_TIMEOUT = 200.0
_LLM_IDLE_TIMEOUT = 20.0


async def quiesce_writers() -> None:
    """等在途的摄入 flush / 交互回合收尾，再丢掉冻结 scope 的积压。

    冻结后新写入都会被拒；这里只处理冻结前已经开工、稍后才落库的那部分。
    """
    from gsuid_core.ai_core.handle_ai import interactive_llm_in_use
    from gsuid_core.ai_core.memory.startup import get_ingestion_worker

    worker = get_ingestion_worker()
    if worker is not None:
        # 超时上限须大于 extract_cluster 的 180s 调用时限，否则 flush 永远等不完。
        if not await worker.wait_frozen_flush_idle(_INGEST_IDLE_TIMEOUT):
            raise RuntimeError("记忆摄入仍在写入，等待超时，请稍后重试")
        worker.drop_frozen_buffers()
    waited = 0.0
    while interactive_llm_in_use() and waited < _LLM_IDLE_TIMEOUT:
        await asyncio.sleep(0.5)
        waited += 0.5
    if worker is not None:
        worker.drop_frozen_buffers()


async def clear_sessions(persona_name: str) -> int:
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.session_registry import get_ai_session_registry
    from gsuid_core.ai_core.http_agent.runtime import cancel_session_runs

    found = persona_sessions(persona_name)
    history = get_history_manager()
    registry = get_ai_session_registry()
    for ev in found.history_events:
        history.clear_history(ev)
        history.delete_session(ev)
    for sid in found.session_ids:
        if sid.startswith("HTTP_AGENT:"):
            await cancel_session_runs(sid)
        registry.remove_ai_session(sid)
    return len(found.session_ids)


def _remove_paths_sync(paths: Sequence[Path]) -> None:
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()


async def clear_journal(persona_name: str) -> int:
    """手账目录 + 自主探索笔记 + 旧单文件 + 手账状态条目 + 自主行动流水。返回删掉的文件数。"""
    from gsuid_core.ai_core.database.models import AIAutonomyActivity
    from gsuid_core.ai_core.journal.storage import pop_journal_state_sync, forget_persona_cache_sync

    paths = journal_paths(persona_name)
    files, _size = await to_thread(scan_files_sync)(paths)
    await to_thread(_remove_paths_sync)(paths)
    await to_thread(forget_persona_cache_sync)(persona_name)
    await to_thread(pop_journal_state_sync)(persona_name)
    await AIAutonomyActivity.delete_by_persona(persona_name)
    return files


async def clear_memory(
    scope_keys: Sequence[str],
    on_progress: Callable[[int], None],
    *,
    include_preferences: bool = True,
) -> int:
    # 先清向量再清 SQL：若向量删除失败，SQL 仍留着 scope_keys，重试可完整重跑不留孤儿向量。
    await bulk_ops.delete_vectors_by_scope_keys(scope_keys)
    counts = await bulk_ops.delete_by_scope_keys(scope_keys, on_progress, include_preferences=include_preferences)
    return bulk_ops.counts_total(counts)


async def clear_user_preferences(on_progress: Callable[[int], None]) -> int:
    """清空所有非 ``self:*`` 的全局共享用户偏好规则（SQL-only）。"""
    return await bulk_ops.delete_shared_preferences(on_progress)


async def clear_self_models(scopes: Sequence[str]) -> int:
    """清空指定 scope 下的 ``AIPersistentState(state_key="self_model")`` 行。"""
    from gsuid_core.ai_core.state_store import state_delete_value

    removed = 0
    for scope in scopes:
        if await state_delete_value(scope, SELF_MODEL_KEY):
            removed += 1
    return removed


async def clear_relationship(persona_name: str) -> int:
    from gsuid_core.ai_core.database.models import UserFavorability

    return await UserFavorability.delete_by_persona(persona_name)


async def clear_persona_state(persona_name: str) -> int:
    """心情（内存）+ state_store ``persona:{name}`` 下的全部键（homeostasis / presence / micro_arcs）。"""
    from gsuid_core.ai_core.state_store import state_list_keys, state_delete_value
    from gsuid_core.ai_core.persona.mood import clear_mood_states
    from gsuid_core.ai_core.persona.homeostasis import persona_scope

    removed = clear_mood_states(persona_name)
    scope = persona_scope(persona_name)
    for key in await state_list_keys(scope):
        if await state_delete_value(scope, key):
            removed += 1
    return removed


async def clear_group_profiles() -> int:
    from gsuid_core.ai_core.state_store import state_list_keys, state_delete_value

    removed = 0
    for key in await state_list_keys(GROUP_PROFILE_SCOPE):
        if await state_delete_value(GROUP_PROFILE_SCOPE, key):
            removed += 1
    return removed


async def cancel_scheduled_tasks(persona_name: str) -> list[tuple[str, str]]:
    """移出调度器并标记已取消；返回（task_id，原状态）供备份还原。"""
    from gsuid_core.aps import scheduler
    from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

    cancelled: list[tuple[str, str]] = []
    for task in await AIScheduledTask.list_live_by_persona(persona_name):
        if scheduler.get_job(task.task_id):
            scheduler.remove_job(task.task_id)
        await AIScheduledTask.set_status(task.task_id, "cancelled")
        cancelled.append((task.task_id, task.status))
    return cancelled
