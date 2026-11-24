"""人格重置 / 备份 / 还原的编排入口（webconsole 只调这里）。

先冻结写入 → 等在途写入收尾 →（可选）备份 → 删除；还原采用先清同类数据再导入的
覆盖语义，SQL 导入完即解冻，向量在后台重建并可断点续跑。
"""

from __future__ import annotations

import json
import time
import asyncio
from typing import TypedDict
from pathlib import Path
from collections.abc import Sequence, Coroutine

from pydantic import JsonValue

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.ai_core.persona_reset import jobs, clear, guard, archive, targets
from gsuid_core.ai_core.memory.database import bulk_ops
from gsuid_core.ai_core.persona_reset.types import (
    ALL_ITEMS,
    ITEM_UNITS,
    GLOBAL_ITEMS,
    MEMORY_ITEMS,
    DEFAULT_OFF_ITEMS,
    UNBACKUPABLE_ITEMS,
    JobView,
    StepKey,
    JobStatus,
    BackupInfo,
    ResetItemKey,
    ResetPreview,
    VectorCursor,
    RestorePreview,
    ResetItemPreview,
    parse_items,
)

_EXPORT_BATCH = 1000
_IMPORT_BATCH = 500
_VECTOR_BATCH = 64
_DEFAULT_TPS = 40.0
_SAMPLE_TEXTS = 8
_TPS_CACHE_TTL_S = 600.0
_COG_BUNDLE_NAME = "cognition.json"

_bg_tasks: set[asyncio.Task[None]] = set()
# 按当前嵌入模型缓存实测吞吐，避免每次打开还原弹窗重复调用远程嵌入 API。
_tps_cache: dict[str, tuple[float, float]] = {}


class StartResult(TypedDict):
    job_id: str
    error: str


def _spawn(job: jobs.Job, coro: Coroutine[None, None, None]) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(lambda task: _on_job_done(job, task))


def _on_job_done(job: jobs.Job, task: asyncio.Task[None]) -> None:
    """后台任务统一收口：函数内不再 try 兜底，异常在这里落成 failed/resumable。"""
    _bg_tasks.discard(task)
    if job.status != "running":
        return
    if task.cancelled():
        jobs.finish_job(job, "failed", "cancelled")
        return
    exc = task.exception()
    if exc is None:
        return
    step = job.running_step()
    status: JobStatus = "resumable" if step == "vectors" else "failed"
    if job.kind == "reset":
        logger.error(t("log.ai.persona_reset_fail", persona=job.persona, e=exc), exc_info=exc)
    elif step == "vectors":
        logger.error(t("log.ai.persona_restore_vectors_fail", persona=job.persona, e=exc), exc_info=exc)
    else:
        logger.error(t("log.ai.persona_restore_fail", persona=job.persona, e=exc), exc_info=exc)
    jobs.finish_job(job, status, str(exc) or type(exc).__name__)


def persona_exists(persona_name: str) -> bool:
    from gsuid_core.ai_core.persona.config import persona_config_manager

    return bool(persona_name) and persona_name in persona_config_manager.get_all_configs()


def _other_personas(persona_name: str) -> list[str]:
    from gsuid_core.ai_core.persona.config import persona_config_manager

    return sorted(name for name in persona_config_manager.get_all_configs() if name != persona_name)


# ───────────────────────────── 统计 / 预览 ─────────────────────────────


class _ItemStat(TypedDict):
    count: int
    seconds: float
    bytes: int


async def _item_stats(persona: str, items: Sequence[ResetItemKey]) -> dict[ResetItemKey, _ItemStat]:
    from gsuid_core.ai_core.state_store import state_list_keys
    from gsuid_core.ai_core.persona.mood import count_mood_states
    from gsuid_core.ai_core.database.models import UserFavorability, AIAutonomyActivity
    from gsuid_core.ai_core.persona.homeostasis import persona_scope
    from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

    stats: dict[ResetItemKey, _ItemStat] = {}
    for item in items:
        count = 0
        seconds = 0.0
        size = 0
        if item == "self_memory":
            scopes = await targets.self_scopes(persona)
            count = bulk_ops.counts_total(await bulk_ops.count_by_scope_keys(scopes.keys))
            seconds, size = targets.estimate_rows(count)
        elif item == "global_group":
            count = bulk_ops.counts_total(await bulk_ops.count_by_prefixes(targets.GROUP_PREFIXES))
            seconds, size = targets.estimate_rows(count)
        elif item == "global_user":
            count = bulk_ops.counts_total(await bulk_ops.count_by_prefixes(targets.USER_PREFIXES))
            seconds, size = targets.estimate_rows(count)
        elif item == "journal":
            files, file_bytes = await to_thread(targets.scan_files_sync)(targets.journal_paths(persona))
            rows = await AIAutonomyActivity.count_by_persona(persona)
            count = files
            f_sec, f_bytes = targets.estimate_files(file_bytes, files)
            r_sec, r_bytes = targets.estimate_rows(rows)
            seconds, size = f_sec + r_sec, f_bytes + r_bytes
        elif item == "relationship":
            count = await UserFavorability.count_by_persona(persona)
            seconds, size = targets.estimate_rows(count)
        elif item == "persona_state":
            count = len(await state_list_keys(persona_scope(persona))) + count_mood_states(persona)
            seconds, size = 0.1, 4096
        elif item == "session_history":
            count = len(targets.persona_sessions(persona).session_ids)
        elif item == "scheduled_tasks":
            count = len(await AIScheduledTask.list_live_by_persona(persona))
            seconds, size = 0.05, 512
        stats[item] = {"count": count, "seconds": round(seconds, 2), "bytes": size}
    return stats


async def preview_reset(persona: str) -> ResetPreview:
    stats = await _item_stats(persona, ALL_ITEMS)
    items: list[ResetItemPreview] = []
    for item in ALL_ITEMS:
        backupable = item not in UNBACKUPABLE_ITEMS
        items.append(
            {
                "key": item,
                "group": "global" if item in GLOBAL_ITEMS else "persona",
                "default_checked": item not in DEFAULT_OFF_ITEMS,
                "count": stats[item]["count"],
                "unit": ITEM_UNITS[item],
                "backup_supported": backupable,
                "backup_seconds": stats[item]["seconds"] if backupable else 0.0,
                "backup_bytes": stats[item]["bytes"] if backupable else 0,
            }
        )
    scopes = await targets.self_scopes(persona)
    return {
        "persona": persona,
        "items": items,
        "other_personas": _other_personas(persona),
        "legacy_self_key": bool(scopes.legacy_keys),
        "active_job": await active_job_view(persona),
    }


# ───────────────────────────── 重置 ─────────────────────────────


async def start_reset(
    persona: str, raw_items: list[str], confirm_name: str, backup: bool, confirm_global: bool
) -> StartResult:
    if not persona_exists(persona):
        return {"job_id": "", "error": "人格不存在"}
    if confirm_name != persona:
        return {"job_id": "", "error": "确认名称与人格名不一致"}
    items = parse_items(raw_items)
    if not items:
        return {"job_id": "", "error": "请至少选择一项"}
    if any(i in GLOBAL_ITEMS for i in items) and not confirm_global:
        return {"job_id": "", "error": "清空全局图谱需要二次确认"}
    stats = await _item_stats(persona, items)
    backup_items: list[ResetItemKey] = [i for i in items if i not in UNBACKUPABLE_ITEMS] if backup else []
    mem_rows = sum(stats[i]["count"] for i in items if i in MEMORY_ITEMS)
    plan: list[tuple[StepKey, float]] = [("prepare", 3.0)]
    if backup_items:
        plan.append(("backup", sum(stats[i]["seconds"] for i in backup_items) + 0.5))
    if mem_rows or any(i in MEMORY_ITEMS for i in items):
        plan.append(("memory", mem_rows / 2000.0 + 1.0))
    persona_steps: tuple[StepKey, ...] = ("journal", "relationship", "persona_state", "scheduled_tasks")
    for key in persona_steps:
        if key in items:
            plan.append((key, 0.5))
    plan.append(("finish", 0.1))
    job = jobs.begin_job("reset", persona, plan)
    if job is None:
        return {"job_id": "", "error": "已有重置或还原任务在运行，请稍后再试"}
    _spawn(job, _run_reset(job, items, backup_items))
    return {"job_id": job.job_id, "error": ""}


async def _scope_map(persona: str, items: Sequence[ResetItemKey]) -> tuple[dict[ResetItemKey, list[str]], list[str]]:
    """（item → scope 键，需要显式冻结的旧 self 键）。"""
    result: dict[ResetItemKey, list[str]] = {}
    legacy: list[str] = []
    if "self_memory" in items:
        scopes = await targets.self_scopes(persona)
        result["self_memory"] = scopes.keys
        legacy = scopes.legacy_keys
    for item in ("global_group", "global_user"):
        if item in items:
            result[item] = await targets.shared_scopes(item)
    return result, legacy


async def _run_reset(job: jobs.Job, items: list[ResetItemKey], backup_items: list[ResetItemKey]) -> None:
    from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

    persona = job.persona
    shared = any(i in GLOBAL_ITEMS for i in items)
    legacy: list[str] = []
    frozen = False
    writer: archive.BackupWriter | None = None
    logger.info(t("log.ai.persona_reset_start", persona=persona, items=",".join(items)))
    # 后台任务边界：异常由 task done-callback 收口为 failed/resumable（见 _spawn）。
    try:
        job.start("prepare")
        await to_thread(archive.sweep_stale_sync)()
        scope_map, legacy = await _scope_map(persona, items)
        guard.freeze([persona], shared=shared, scope_keys=legacy)
        frozen = True
        await clear.quiesce_writers()
        # 列清单到冻结之间新建的 scope 也要纳入删除范围。
        scope_map, _ = await _scope_map(persona, items)
        live_tasks = await AIScheduledTask.list_live_by_persona(persona) if "scheduled_tasks" in items else []
        job.end("prepare")

        if job.has_step("backup"):
            writer = archive.BackupWriter(persona)
            writer.tasks = [{"task_id": task.task_id, "status": task.status} for task in live_tasks]
            await _write_backup(job, writer, backup_items, scope_map)
            path = await writer.finalize()
            writer = None
            job.backup_file = path.name
            job.end("backup")

        # 备份成功后才执行任何破坏性删除，防止备份失败时会话已被清空。
        if "session_history" in items:
            job.add_count("session_history", await clear.clear_sessions(persona))

        if job.has_step("memory"):
            await to_thread(_drop_persona_cursors_sync)(persona)
            mem_items: list[ResetItemKey] = [i for i in items if i in MEMORY_ITEMS]
            total = 0
            for item in mem_items:
                total += bulk_ops.counts_total(await bulk_ops.count_by_scope_keys(scope_map[item]))
            job.start("memory", total)
            for item in mem_items:
                job.add_count(item, await clear.clear_memory(scope_map[item], lambda n: job.advance("memory", n)))
            if "global_group" in items:
                job.add_count("global_group", await clear.clear_group_profiles())
            job.end("memory")

        if "journal" in items:
            job.start("journal")
            job.add_count("journal", await clear.clear_journal(persona))
            job.end("journal")
        if "relationship" in items:
            job.start("relationship")
            job.add_count("relationship", await clear.clear_relationship(persona))
            job.end("relationship")
        if "persona_state" in items:
            job.start("persona_state")
            job.add_count("persona_state", await clear.clear_persona_state(persona))
            job.end("persona_state")
        if "scheduled_tasks" in items:
            job.start("scheduled_tasks")
            job.add_count("scheduled_tasks", len(await clear.cancel_scheduled_tasks(persona)))
            job.end("scheduled_tasks")

        job.start("finish")
        jobs.finish_job(job, "done")
        logger.info(t("log.ai.persona_reset_done", persona=persona, counts=json.dumps(job.counts)))
    finally:
        if frozen:
            guard.unfreeze([persona], shared=shared, scope_keys=legacy)
        if writer is not None:
            await writer.discard()


async def _write_backup(
    job: jobs.Job,
    writer: archive.BackupWriter,
    items: list[ResetItemKey],
    scope_map: dict[ResetItemKey, list[str]],
) -> None:
    from gsuid_core.ai_core.state_store import state_get_value, state_list_keys
    from gsuid_core.ai_core.cognition.nodes import AICogNode
    from gsuid_core.ai_core.database.models import UserFavorability, AIAutonomyActivity
    from gsuid_core.ai_core.journal.storage import read_journal_state_sync
    from gsuid_core.ai_core.persona.homeostasis import persona_scope

    persona = job.persona
    stats = await _item_stats(persona, items)
    job.start("backup", sum(stats[i]["count"] for i in items))
    writer.items = list(items)
    for item in items:
        if item in MEMORY_ITEMS:
            keys = scope_map[item]
            writer.scope_keys[item] = keys
            episode_ids: list[str] = []
            for model in bulk_ops.MEMORY_MODELS:
                table = model.__name__.lower()
                async for rows in bulk_ops.iter_rows(model, keys, _EXPORT_BATCH):
                    if model.__name__ == "AIMemEpisode":
                        episode_ids.extend(r["id"] for r in rows if isinstance(r["id"], str))
                    await writer.append_rows(item, table, rows)
                    job.advance("backup", len(rows))
            for start in range(0, len(episode_ids), _EXPORT_BATCH * 5):
                mentions = await bulk_ops.list_mentions(episode_ids[start : start + _EXPORT_BATCH * 5])
                await writer.append_rows(item, bulk_ops.MENTIONS_TABLE, mentions)
            cog_nodes, cog_edges, cog_atts = await AICogNode.export_by_scope_keys(keys)
            if cog_nodes or cog_edges or cog_atts:
                await writer.write_json(
                    f"items/{item}/{_COG_BUNDLE_NAME}",
                    json.loads(json.dumps({"nodes": cog_nodes, "edges": cog_edges, "attachments": cog_atts})),
                )
                writer.counts[f"{item}/aicognode"] = len(cog_nodes)
                job.advance("backup", len(cog_nodes))
            if item == "global_group":
                profiles: dict[str, JsonValue] = {}
                for key in await state_list_keys(targets.GROUP_PROFILE_SCOPE):
                    profiles[key] = await state_get_value(targets.GROUP_PROFILE_SCOPE, key)
                await writer.write_json("items/global_group/group_profiles.json", profiles)
        elif item == "journal":
            tree, notes, legacy = targets.journal_paths(persona)
            files = await writer.copy_tree(tree, "items/journal/tree")
            files += await writer.copy_tree(notes, "items/journal/notes")
            files += await writer.copy_tree(legacy, "items/journal/legacy.md")
            writer.counts["journal/files"] = files
            job.advance("backup", files)
            entry = await to_thread(read_journal_state_sync)(persona)
            if entry is not None:
                await writer.write_json("items/journal/state.json", json.loads(json.dumps(entry)))
            acts = [row.model_dump(mode="json") for row in await AIAutonomyActivity.list_by_persona(persona)]
            await writer.append_rows("journal", "aiautonomyactivity", acts)
        elif item == "relationship":
            rels = [row.model_dump(mode="json") for row in await UserFavorability.list_by_persona(persona)]
            await writer.append_rows("relationship", "userfavorability", rels)
            job.advance("backup", len(rels))
        elif item == "persona_state":
            scope = persona_scope(persona)
            values: dict[str, JsonValue] = {}
            for key in await state_list_keys(scope):
                values[key] = await state_get_value(scope, key)
            await writer.write_json("items/persona_state/state.json", values)
            writer.counts["persona_state/keys"] = len(values)
            job.advance("backup", len(values))
        elif item == "scheduled_tasks":
            writer.counts["scheduled_tasks/tasks"] = len(writer.tasks)
            job.advance("backup", len(writer.tasks))


# ───────────────────────────── 备份列表 ─────────────────────────────


async def list_backups(persona: str) -> list[BackupInfo]:
    return await to_thread(archive.list_backups_sync)(persona)


def backup_file_path(persona: str, file_name: str) -> Path | None:
    return archive.resolve_backup(persona, file_name)


async def delete_backup(persona: str, file_name: str) -> bool:
    path = archive.resolve_backup(persona, file_name)
    if path is None:
        return False
    await to_thread(path.unlink)()
    return True


# ───────────────────────────── 还原 ─────────────────────────────


def _item_import_counts(manifest: archive.Manifest, items: Sequence[ResetItemKey]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in manifest["counts"].items():
        item, _, _table = key.partition("/")
        if item in items:
            out[item] = (out[item] if item in out else 0) + value
    if "scheduled_tasks" in items:
        out["scheduled_tasks"] = len(manifest["tasks"])
    return out


def _vector_texts(manifest: archive.Manifest, items: Sequence[ResetItemKey]) -> int:
    total = 0
    for key, value in manifest["counts"].items():
        item, _, table = key.partition("/")
        if item not in items or item not in MEMORY_ITEMS:
            continue
        if table == "aimemepisode" or table == "aimemedge":
            total += value
        elif table == "aimementity":
            total += value * 2
    return total


def _sample_texts_sync(path: Path) -> list[str]:
    import gzip
    import zipfile

    texts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not name.endswith("aimemepisode.jsonl.gz"):
                continue
            with zf.open(name) as raw, gzip.open(raw, "rt", encoding="utf-8") as fp:
                for line in fp:
                    row = archive.parse_row(line)
                    if row is not None and "content" in row and isinstance(row["content"], str):
                        texts.append(row["content"])
                    if len(texts) >= _SAMPLE_TEXTS:
                        return texts
    return texts


async def _measure_tps(path: Path) -> float:
    """用少量样本实测嵌入吞吐（含 sparse 折算），按模型缓存 10 分钟以减少远程 API 费用。"""
    from gsuid_core.ai_core.rag.base import embedding_provider
    from gsuid_core.ai_core.memory.vector.ops import embed_texts_dense

    if embedding_provider is None:
        return 0.0
    model_key = archive.current_embedding_model()
    now = time.monotonic()
    if model_key in _tps_cache:
        cached_tps, cached_at = _tps_cache[model_key]
        if now - cached_at < _TPS_CACHE_TTL_S:
            return cached_tps
    samples = await to_thread(_sample_texts_sync)(path)
    if not samples:
        return 0.0
    started = time.perf_counter()
    try:
        await embed_texts_dense(samples)
    except (RuntimeError, OSError, ValueError) as e:
        logger.warning(t("log.ai.persona_reset_tps_fail", e=e))
        return 0.0
    elapsed = max(time.perf_counter() - started, 1e-3)
    tps = round(len(samples) / elapsed / 1.3, 2)
    _tps_cache[model_key] = (tps, now)
    return tps


async def preview_restore(persona: str, file_name: str) -> RestorePreview | str:
    path = archive.resolve_backup(persona, file_name)
    if path is None:
        return "备份文件不存在"
    manifest = await to_thread(archive.read_manifest_sync)(path)
    if manifest is None:
        return "备份文件已损坏或版本不兼容"
    if manifest["persona"] != persona:
        return "备份不属于该人格"
    items: list[ResetItemKey] = [i for i in parse_items(manifest["items"]) if i not in UNBACKUPABLE_ITEMS]
    stats = await _item_stats(persona, items)
    texts = _vector_texts(manifest, items)
    tps = await _measure_tps(path) if texts else 0.0
    preview: RestorePreview = {
        "file": path.name,
        "items": items,
        "overwrite_counts": {i: (0 if i == "scheduled_tasks" else stats[i]["count"]) for i in items},
        "import_counts": _item_import_counts(manifest, items),
        "vector_texts": texts,
        "vector_eta_seconds": int(texts / tps) + 1 if tps > 0 and texts else 0,
        "throughput_per_sec": tps,
        "embedding_model": archive.current_embedding_model(),
        "active_job": await active_job_view(persona),
    }
    return preview


async def start_restore(persona: str, file_name: str, raw_items: list[str], confirm_name: str) -> StartResult:
    if not persona_exists(persona):
        return {"job_id": "", "error": "人格不存在"}
    if confirm_name != persona:
        return {"job_id": "", "error": "确认名称与人格名不一致"}
    path = archive.resolve_backup(persona, file_name)
    if path is None:
        return {"job_id": "", "error": "备份文件不存在"}
    manifest = await to_thread(archive.read_manifest_sync)(path)
    if manifest is None:
        return {"job_id": "", "error": "备份文件已损坏或版本不兼容"}
    if manifest["persona"] != persona:
        return {"job_id": "", "error": "备份不属于该人格"}
    in_backup = set(parse_items(manifest["items"]))
    items: list[ResetItemKey] = [i for i in parse_items(raw_items) if i in in_backup and i not in UNBACKUPABLE_ITEMS]
    if not items:
        return {"job_id": "", "error": "请至少选择一项备份中包含的数据"}
    imports = sum(_item_import_counts(manifest, items).values())
    texts = _vector_texts(manifest, items)
    plan: list[tuple[StepKey, float]] = [
        ("prepare", 3.0),
        ("clear", 2.0),
        ("import", imports / 3000.0 + 1.0),
        ("files", 1.0),
    ]
    if texts:
        plan.append(("vectors", texts / _DEFAULT_TPS))
    plan.append(("finish", 0.1))
    job = jobs.begin_job("restore", persona, plan)
    if job is None:
        return {"job_id": "", "error": "已有重置或还原任务在运行，请稍后再试"}
    _spawn(job, _run_restore(job, path, manifest, items))
    return {"job_id": job.job_id, "error": ""}


async def _import_table(job: jobs.Job, src: Path, item: ResetItemKey, table: str) -> int:
    from gsuid_core.ai_core.database.models import UserFavorability, AIAutonomyActivity

    reader = archive.GzRowReader(src / "items" / item / f"{table}.jsonl.gz")
    done = 0
    try:
        while True:
            rows = await to_thread(reader.next_batch_sync)(_IMPORT_BATCH)
            if not rows:
                break
            if table == bulk_ops.MENTIONS_TABLE:
                await bulk_ops.insert_mentions(rows)
            elif table == "userfavorability":
                await UserFavorability.restore_rows([UserFavorability.model_validate(r) for r in rows])
            elif table == "aiautonomyactivity":
                await AIAutonomyActivity.restore_rows([AIAutonomyActivity.model_validate(r) for r in rows])
            elif table in bulk_ops.TABLE_NAMES:
                await bulk_ops.insert_rows(bulk_ops.TABLE_NAMES[table], rows)
            done += len(rows)
            job.advance("import", len(rows))
    finally:
        await to_thread(reader.close_sync)()
    return done


def _sql_import_total(manifest: archive.Manifest, items: Sequence[ResetItemKey]) -> int:
    total = 0
    for key, value in manifest["counts"].items():
        item, _, table = key.partition("/")
        if item not in items:
            continue
        if table not in ("files", "keys", "tasks"):
            total += value
    return total


async def _restore_cognition_bundle(src: Path, item: ResetItemKey) -> int:
    from pydantic import TypeAdapter

    from gsuid_core.ai_core.cognition.nodes import AICogNode, CogAttExportRow, CogEdgeExportRow, CogNodeExportRow

    bundle_path = src / "items" / item / _COG_BUNDLE_NAME
    if not bundle_path.is_file():
        return 0
    raw = await to_thread(archive.read_json_object_sync)(bundle_path)
    nodes = TypeAdapter(list[CogNodeExportRow]).validate_python(raw["nodes"] if "nodes" in raw else [])
    edges = TypeAdapter(list[CogEdgeExportRow]).validate_python(raw["edges"] if "edges" in raw else [])
    atts = TypeAdapter(list[CogAttExportRow]).validate_python(raw["attachments"] if "attachments" in raw else [])
    return await AICogNode.restore_exported(nodes, edges, atts)


def _merge_manifest_scopes(
    scope_map: dict[ResetItemKey, list[str]], manifest: archive.Manifest, items: Sequence[ResetItemKey]
) -> None:
    for item in items:
        if item in MEMORY_ITEMS and item in manifest["scope_keys"]:
            existing = scope_map[item] if item in scope_map else []
            scope_map[item] = sorted(set(existing) | set(manifest["scope_keys"][item]))


async def _run_restore(job: jobs.Job, path: Path, manifest: archive.Manifest, items: list[ResetItemKey]) -> None:
    persona = job.persona
    shared = any(i in GLOBAL_ITEMS for i in items)
    legacy_all: list[str] = []
    frozen = False
    src: Path | None = None
    logger.info(t("log.ai.persona_restore_start", persona=persona, file=path.name, items=",".join(items)))
    # 后台任务边界：异常由 task done-callback 收口为 failed/resumable（见 _spawn）。
    try:
        job.start("prepare")
        await to_thread(archive.sweep_stale_sync)()
        current_map, legacy = await _scope_map(persona, items)
        _merge_manifest_scopes(current_map, manifest, items)
        backup_self = manifest["scope_keys"]["self_memory"] if "self_memory" in manifest["scope_keys"] else []
        legacy_all = sorted(set(legacy) | {k for k in backup_self if len(k.split(":", 2)) == 2})
        guard.freeze([persona], shared=shared, scope_keys=legacy_all)
        frozen = True
        src = await to_thread(archive.extract_sync)(path)
        await clear.quiesce_writers()
        # 列清单到冻结之间新建的 scope 也要纳入先清后导的范围。
        current_map, _ = await _scope_map(persona, items)
        _merge_manifest_scopes(current_map, manifest, items)
        job.end("prepare")

        if any(i in MEMORY_ITEMS for i in items):
            await to_thread(_drop_persona_cursors_sync)(persona)
        job.start("clear", len(items))
        for item in items:
            if item in MEMORY_ITEMS:
                await clear.clear_memory(current_map[item], lambda _n: None)
                if item == "global_group":
                    await clear.clear_group_profiles()
            elif item == "journal":
                await clear.clear_journal(persona)
            elif item == "relationship":
                await clear.clear_relationship(persona)
            elif item == "persona_state":
                await clear.clear_persona_state(persona)
            job.advance("clear", 1)
        job.end("clear")

        job.start("import", _sql_import_total(manifest, items))
        for item in items:
            n = 0
            if item in MEMORY_ITEMS:
                for model in bulk_ops.MEMORY_MODELS:
                    n += await _import_table(job, src, item, model.__name__.lower())
                await _import_table(job, src, item, bulk_ops.MENTIONS_TABLE)
                cog_n = await _restore_cognition_bundle(src, item)
                if cog_n:
                    n += cog_n
                    job.advance("import", cog_n)
            elif item == "relationship":
                n += await _import_table(job, src, item, "userfavorability")
            elif item == "journal":
                await _import_table(job, src, item, "aiautonomyactivity")
            if n:
                job.add_count(item, n)
        job.end("import")

        job.start("files")
        await _restore_files(job, src, manifest, items)
        job.end("files")
    finally:
        if frozen:
            guard.unfreeze([persona], shared=shared, scope_keys=legacy_all)
        if src is not None:
            await to_thread(_rmtree_quiet)(src)

    mem_keys: list[str] = []
    for item in items:
        if item in MEMORY_ITEMS:
            mem_keys.extend(manifest["scope_keys"][item] if item in manifest["scope_keys"] else [])
    if job.has_step("vectors"):
        cursor: VectorCursor = {
            "job_id": job.job_id,
            "persona": persona,
            "file": path.name,
            "scope_keys": sorted(set(mem_keys)),
            "kind_index": 0,
            "chunk_index": 0,
            "last_id": "",
            "done": 0,
            "total": _vector_texts(manifest, items),
        }
        await _run_vectors(job, cursor)
        return
    job.start("finish")
    jobs.finish_job(job, "done")
    logger.info(t("log.ai.persona_restore_done", persona=persona, counts=json.dumps(job.counts)))


def _rmtree_quiet(path: Path) -> None:
    import shutil

    shutil.rmtree(path, True)


async def _restore_files(job: jobs.Job, src: Path, manifest: archive.Manifest, items: Sequence[ResetItemKey]) -> None:
    from gsuid_core.ai_core.state_store import state_set_value
    from gsuid_core.ai_core.journal.storage import put_journal_state_sync, forget_persona_cache_sync
    from gsuid_core.ai_core.persona.homeostasis import persona_scope
    from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask
    from gsuid_core.ai_core.scheduled_task.executor import register_restored_task

    persona = job.persona
    base = src / "items"
    if "journal" in items:
        tree, notes, legacy = targets.journal_paths(persona)
        files = await to_thread(archive.copy_back_sync)(base / "journal" / "tree", tree)
        files += await to_thread(archive.copy_back_sync)(base / "journal" / "notes", notes)
        files += await to_thread(archive.copy_back_sync)(base / "journal" / "legacy.md", legacy)
        entry = await to_thread(archive.read_state_entry_sync)(base / "journal" / "state.json")
        if entry is not None:
            await to_thread(put_journal_state_sync)(persona, entry)
        await to_thread(forget_persona_cache_sync)(persona)
        job.add_count("journal", files)
    if "persona_state" in items:
        values = await to_thread(archive.read_json_object_sync)(base / "persona_state" / "state.json")
        scope = persona_scope(persona)
        for key, value in values.items():
            await state_set_value(scope, key, value)
        job.add_count("persona_state", len(values))
    if "global_group" in items:
        profiles = await to_thread(archive.read_json_object_sync)(base / "global_group" / "group_profiles.json")
        for key, value in profiles.items():
            await state_set_value(targets.GROUP_PROFILE_SCOPE, key, value)
    if "scheduled_tasks" in items:
        restored = 0
        overdue_idx = 0
        for snap in manifest["tasks"]:
            task = await AIScheduledTask.get_by_task_id(snap["task_id"])
            if task is None or task.status != "cancelled":
                continue
            await AIScheduledTask.set_status(task.task_id, snap["status"])
            task.status = snap["status"]
            if snap["status"] == "pending":
                register_restored_task(task, overdue_idx)
                overdue_idx += 1
            restored += 1
        job.add_count("scheduled_tasks", restored)


# ───────────────────────────── 向量重建（可续跑） ─────────────────────────────


def _cursor_path(job_id: str) -> Path:
    return archive.JOBS_DIR / f"{job_id}.json"


def _save_cursor_sync(cursor: VectorCursor) -> None:
    archive.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = _cursor_path(cursor["job_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursor, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _drop_cursor_sync(job_id: str) -> None:
    _cursor_path(job_id).unlink(missing_ok=True)


def _drop_persona_cursors_sync(persona: str) -> None:
    """开启新一轮重置或还原前清理该人格遗留的断点游标，避免旧横幅残留。"""
    if not archive.JOBS_DIR.is_dir():
        return
    for path in archive.JOBS_DIR.glob("*.json"):
        cursor = archive.parse_cursor(path)
        if cursor is not None and cursor["persona"] == persona:
            path.unlink(missing_ok=True)


def _load_cursors_sync() -> list[VectorCursor]:
    if not archive.JOBS_DIR.is_dir():
        return []
    out: list[VectorCursor] = []
    for path in archive.JOBS_DIR.glob("*.json"):
        cursor = archive.parse_cursor(path)
        if cursor is not None:
            out.append(
                {
                    "job_id": cursor["job_id"],
                    "persona": cursor["persona"],
                    "file": cursor["file"],
                    "scope_keys": cursor["scope_keys"],
                    "kind_index": cursor["kind_index"],
                    "chunk_index": cursor["chunk_index"],
                    "last_id": cursor["last_id"],
                    "done": cursor["done"],
                    "total": cursor["total"],
                }
            )
    return out


async def _upsert_kind(kind: bulk_ops.VectorKind, rows: list[bulk_ops.JsonRow]) -> None:
    from gsuid_core.ai_core.memory.vector.ops import (
        upsert_edge_vectors_batch,
        upsert_entity_vectors_batch,
        upsert_episode_vectors_batch,
    )

    if kind == "episode":
        await upsert_episode_vectors_batch(rows)
    elif kind == "entity":
        await upsert_entity_vectors_batch(rows)
    else:
        await upsert_edge_vectors_batch(rows)


async def _run_vectors(job: jobs.Job, cursor: VectorCursor) -> None:
    from gsuid_core.ai_core.rag.base import client

    persona = job.persona
    if client is None:
        job.end("vectors", "skipped")
        await to_thread(_drop_cursor_sync)(cursor["job_id"])
        job.start("finish")
        jobs.finish_job(job, "done")
        return
    await to_thread(_save_cursor_sync)(cursor)
    job.start("vectors", cursor["total"], initial_done=cursor["done"])
    chunks = bulk_ops.chunk_scope_keys(cursor["scope_keys"])
    # 游标已先落盘：此处抛错由 done-callback 置 resumable，用户可稍后「继续」。
    for ki in range(cursor["kind_index"], len(bulk_ops.VECTOR_KINDS)):
        kind = bulk_ops.VECTOR_KINDS[ki]
        weight = 2 if kind == "entity" else 1
        start_ci = cursor["chunk_index"] if ki == cursor["kind_index"] else 0
        for ci in range(start_ci, len(chunks)):
            last_id = cursor["last_id"] if (ki == cursor["kind_index"] and ci == start_ci) else ""
            while True:
                rows = await bulk_ops.vector_batch(kind, chunks[ci], last_id, _VECTOR_BATCH)
                if not rows:
                    break
                await _upsert_kind(kind, rows)
                tail = rows[-1]["_row_id"]
                last_id = tail if isinstance(tail, str) else last_id
                job.advance("vectors", len(rows) * weight)
                cursor["kind_index"] = ki
                cursor["chunk_index"] = ci
                cursor["last_id"] = last_id
                cursor["done"] += len(rows) * weight
                await to_thread(_save_cursor_sync)(cursor)
        cursor["chunk_index"] = 0
        cursor["last_id"] = ""
    await to_thread(_drop_cursor_sync)(cursor["job_id"])
    job.end("vectors")
    job.start("finish")
    jobs.finish_job(job, "done")
    logger.info(t("log.ai.persona_restore_done", persona=persona, counts=json.dumps(job.counts)))


async def resume_job(job_id: str) -> StartResult:
    cursors = await to_thread(_load_cursors_sync)()
    match = [c for c in cursors if c["job_id"] == job_id]
    if not match:
        return {"job_id": "", "error": "没有可继续的向量重建任务"}
    cursor = match[0]
    remaining = max(cursor["total"] - cursor["done"], 0)
    job = jobs.begin_job(
        "restore",
        cursor["persona"],
        [("vectors", remaining / _DEFAULT_TPS), ("finish", 0.1)],
        job_id=cursor["job_id"],
    )
    if job is None:
        return {"job_id": "", "error": "已有重置或还原任务在运行，请稍后再试"}
    _spawn(job, _run_vectors(job, cursor))
    return {"job_id": job.job_id, "error": ""}


async def active_job_view(persona: str) -> JobView | None:
    """正在跑的任务；没有则返回该人格可续跑的向量重建（含进程重启后从磁盘游标恢复的）。"""
    running = jobs.active_job()
    if running is not None and running.persona == persona:
        return running.view()
    for cursor in await to_thread(_load_cursors_sync)():
        if cursor["persona"] != persona:
            continue
        known = jobs.get_job(cursor["job_id"])
        if known is not None and known.status == "resumable":
            return known.view()
        return {
            "job_id": cursor["job_id"],
            "kind": "restore",
            "persona": persona,
            "status": "resumable",
            "steps": [{"key": "vectors", "status": "failed", "done": cursor["done"], "total": cursor["total"]}],
            "eta_seconds": 0,
            "error": "向量重建未完成",
            "backup_file": cursor["file"],
            "counts": {},
            "started_at": 0,
            "finished_at": 0,
        }
    return None


async def job_view(job_id: str) -> JobView | None:
    job = jobs.get_job(job_id)
    if job is not None:
        return job.view()
    for cursor in await to_thread(_load_cursors_sync)():
        if cursor["job_id"] == job_id:
            return {
                "job_id": cursor["job_id"],
                "kind": "restore",
                "persona": cursor["persona"],
                "status": "resumable",
                "steps": [{"key": "vectors", "status": "failed", "done": cursor["done"], "total": cursor["total"]}],
                "eta_seconds": 0,
                "error": "向量重建未完成",
                "backup_file": cursor["file"],
                "counts": {},
                "started_at": 0,
                "finished_at": 0,
            }
    return None
