"""人格备份 zip 的写入与读取（只含 SQL 行与文件，不含向量；还原时按当前模型重嵌）。

布局::

    manifest.json
    items/<item>/<table>.jsonl.gz        # SQL 行（JSONL + gzip，可多段 member）
    items/journal/tree/...               # 手账目录原样
    items/journal/notes/...              # 自主探索笔记原样
    items/journal/legacy.md              # 迁移前旧单文件（若有）
    items/journal/state.json             # .journal_state.json 中该人格的条目
    items/persona_state/state.json       # state_store persona:{name} 下的键值
    items/global_group/group_profiles.json
"""

from __future__ import annotations

import gzip
import json
import time
import shutil
import zipfile
import tempfile
from typing import TypedDict
from pathlib import Path, PurePosixPath

from pydantic import JsonValue, TypeAdapter, ValidationError

from gsuid_core.pool import to_thread
from gsuid_core.data_store import AI_CORE_PATH
from gsuid_core.utils.path_safety import is_safe_filename
from gsuid_core.ai_core.journal.storage import PersonaJournalStateEntry
from gsuid_core.ai_core.persona_reset.types import BackupInfo, ResetItemKey, VectorCursor, parse_items
from gsuid_core.ai_core.memory.database.bulk_ops import JsonRow

MANIFEST_VERSION = 1
KEEP_BACKUPS = 5
BACKUP_ROOT: Path = AI_CORE_PATH / "persona_backups"
JOBS_DIR: Path = BACKUP_ROOT / "_jobs"
_ROW_ADAPTER: TypeAdapter[JsonRow] = TypeAdapter(JsonRow)


class TaskSnapshot(TypedDict):
    task_id: str
    status: str


class Manifest(TypedDict):
    version: int
    persona: str
    created_at: int
    items: list[str]
    counts: dict[str, int]
    scope_keys: dict[str, list[str]]
    tasks: list[TaskSnapshot]
    embedding_model: str


def persona_backup_dir(persona_name: str) -> Path:
    from gsuid_core.ai_core.journal.storage import persona_journal_dir

    # 与手账目录同一套安全化规则；收进 personas/ 子层，人格名无法与 _tmp/_jobs 撞上。
    return BACKUP_ROOT / "personas" / persona_journal_dir(persona_name).name


def resolve_backup(persona_name: str, file_name: str) -> Path | None:
    if not is_safe_filename(file_name) or not file_name.lower().endswith(".zip"):
        return None
    path = persona_backup_dir(persona_name) / file_name
    return path if path.is_file() else None


def current_embedding_model() -> str:
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        return ""
    return f"{type(embedding_provider).__name__}/{embedding_provider.dimension}"


class BackupWriter:
    """先写数据卷临时目录（gz 逐批追加），最后一次性打 zip。全部磁盘 I/O 走线程。"""

    def __init__(self, persona_name: str) -> None:
        self.persona = persona_name
        tmp_root = BACKUP_ROOT / "_tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="persona_backup_", dir=tmp_root))
        self.counts: dict[str, int] = {}
        self.scope_keys: dict[str, list[str]] = {}
        self.tasks: list[TaskSnapshot] = []
        self.items: list[ResetItemKey] = []

    def _path(self, rel: str) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def append_rows(self, item: ResetItemKey, table: str, rows: list[JsonRow]) -> None:
        if not rows:
            return
        path = self._path(f"items/{item}/{table}.jsonl.gz")
        await to_thread(_append_gz_sync)(path, rows)
        key = f"{item}/{table}"
        self.counts[key] = (self.counts[key] if key in self.counts else 0) + len(rows)

    async def write_json(self, rel: str, value: JsonValue) -> None:
        await to_thread(_write_json_sync)(self._path(rel), value)

    async def copy_tree(self, src: Path, rel: str) -> int:
        return await to_thread(_copy_into_sync)(src, self.tmp / rel)

    async def finalize(self) -> Path:
        manifest: Manifest = {
            "version": MANIFEST_VERSION,
            "persona": self.persona,
            "created_at": int(time.time()),
            "items": list(self.items),
            "counts": dict(self.counts),
            "scope_keys": dict(self.scope_keys),
            "tasks": list(self.tasks),
            "embedding_model": current_embedding_model(),
        }
        out_dir = persona_backup_dir(self.persona)
        stem = time.strftime("%Y%m%d_%H%M%S")
        target = out_dir / f"{stem}.zip"
        seq = 1
        while target.exists():
            target = out_dir / f"{stem}_{seq}.zip"
            seq += 1
        await to_thread(_zip_dir_sync)(self.tmp, target, json.dumps(manifest, ensure_ascii=False, indent=2))
        await to_thread(_prune_sync)(out_dir, KEEP_BACKUPS)
        return target

    async def discard(self) -> None:
        await to_thread(shutil.rmtree)(self.tmp, True)


def _append_gz_sync(path: Path, rows: list[JsonRow]) -> None:
    with gzip.open(path, "at", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, ensure_ascii=False))
            fp.write("\n")


def _write_json_sync(path: Path, value: JsonValue) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _copy_into_sync(src: Path, dst: Path) -> int:
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return 1
    if not src.is_dir():
        return 0
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return sum(1 for p in dst.rglob("*") if p.is_file())


def _zip_dir_sync(src: Path, target: Path, manifest_text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".zip.part")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("manifest.json", manifest_text)
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            arc = path.relative_to(src).as_posix()
            # gz 已压缩过，再 deflate 只会白耗 CPU。
            kind = zipfile.ZIP_STORED if arc.endswith(".gz") else zipfile.ZIP_DEFLATED
            zf.write(path, arc, compress_type=kind)
    partial.replace(target)
    shutil.rmtree(src, True)


def _prune_sync(out_dir: Path, keep: int) -> None:
    zips = sorted(out_dir.glob("*.zip"), key=lambda p: p.name, reverse=True)
    for old in zips[keep:]:
        old.unlink()


def sweep_stale_sync() -> None:
    """清掉上次中断遗留的备份/还原暂存目录与半截 ``.zip.part``（全局同时只有一个任务在跑）。"""
    tmp_root = BACKUP_ROOT / "_tmp"
    if tmp_root.is_dir():
        for child in tmp_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, True)
            else:
                child.unlink(missing_ok=True)
    personas_dir = BACKUP_ROOT / "personas"
    if personas_dir.is_dir():
        for part in personas_dir.glob("*/*.zip.part"):
            part.unlink(missing_ok=True)


def list_backups_sync(persona_name: str) -> list[BackupInfo]:
    out_dir = persona_backup_dir(persona_name)
    if not out_dir.is_dir():
        return []
    infos: list[BackupInfo] = []
    for path in sorted(out_dir.glob("*.zip"), key=lambda p: p.name, reverse=True):
        manifest = read_manifest_sync(path)
        if manifest is None:
            continue
        infos.append(
            {
                "file": path.name,
                "created_at": manifest["created_at"],
                "items": parse_items(manifest["items"]),
                "counts": manifest["counts"],
                "size_bytes": path.stat().st_size,
                "embedding_model": manifest["embedding_model"],
            }
        )
    return infos


def _str_list(raw: object) -> list[str]:
    return [v for v in raw if isinstance(v, str)] if isinstance(raw, list) else []


def read_manifest_sync(path: Path) -> Manifest | None:
    """解析用户可上传 / 可篡改的 zip 清单：结构不对一律视为无效备份。"""
    try:
        with zipfile.ZipFile(path) as zf:
            if "manifest.json" not in zf.namelist():
                return None
            raw: object = json.loads(zf.read("manifest.json").decode("utf-8"))
    except (zipfile.BadZipFile, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or "version" not in raw or raw["version"] != MANIFEST_VERSION:
        return None
    persona = raw["persona"] if "persona" in raw else None
    created = raw["created_at"] if "created_at" in raw else None
    if not isinstance(persona, str) or not isinstance(created, int):
        return None
    counts_raw = raw["counts"] if "counts" in raw else {}
    counts = (
        {k: v for k, v in counts_raw.items() if isinstance(k, str) and isinstance(v, int)}
        if isinstance(counts_raw, dict)
        else {}
    )
    scopes_raw = raw["scope_keys"] if "scope_keys" in raw else {}
    scopes = (
        {k: _str_list(v) for k, v in scopes_raw.items() if isinstance(k, str)} if isinstance(scopes_raw, dict) else {}
    )
    tasks: list[TaskSnapshot] = []
    tasks_raw = raw["tasks"] if "tasks" in raw else []
    if isinstance(tasks_raw, list):
        for item in tasks_raw:
            if isinstance(item, dict) and "task_id" in item and "status" in item:
                tid = item["task_id"]
                status = item["status"]
                if isinstance(tid, str) and isinstance(status, str):
                    tasks.append({"task_id": tid, "status": status})
    model = raw["embedding_model"] if "embedding_model" in raw else ""
    return {
        "version": MANIFEST_VERSION,
        "persona": persona,
        "created_at": created,
        "items": _str_list(raw["items"] if "items" in raw else []),
        "counts": counts,
        "scope_keys": scopes,
        "tasks": tasks,
        "embedding_model": model if isinstance(model, str) else "",
    }


def extract_sync(path: Path) -> Path:
    """解压到数据卷临时目录；任何越界条目（绝对路径 / ``..``）直接拒绝整包。"""
    tmp_root = BACKUP_ROOT / "_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix="persona_restore_", dir=tmp_root))
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or "\\" in name or ":" in name:
                shutil.rmtree(dest, True)
                raise ValueError(f"unsafe zip entry: {name}")
        zf.extractall(dest)
    return dest


class GzRowReader:
    """按批读 JSONL.gz（支持多 member）。解析的是备份文件内容，坏行跳过并计数。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.bad_lines = 0
        self._fp = gzip.open(path, "rt", encoding="utf-8") if path.is_file() else None

    def next_batch_sync(self, size: int) -> list[JsonRow]:
        out: list[JsonRow] = []
        if self._fp is None:
            return out
        while len(out) < size:
            line = self._fp.readline()
            if not line:
                break
            try:
                out.append(_ROW_ADAPTER.validate_json(line))
            except ValidationError:
                self.bad_lines += 1
        return out

    def close_sync(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None


def parse_row(line: str) -> JsonRow | None:
    try:
        return _ROW_ADAPTER.validate_json(line)
    except ValidationError:
        return None


def read_json_object_sync(path: Path) -> dict[str, JsonValue]:
    if not path.is_file():
        return {}
    try:
        return _ROW_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, UnicodeDecodeError):
        return {}


def read_state_entry_sync(path: Path) -> PersonaJournalStateEntry | None:
    raw = read_json_object_sync(path)
    if not raw:
        return None
    last_ts = raw["last_compact_ts"] if "last_compact_ts" in raw else 0.0
    dream_date = raw["last_dream_date"] if "last_dream_date" in raw else ""
    dirty_raw = raw["dirty_shards"] if "dirty_shards" in raw else []
    carry_raw = raw["light_carry"] if "light_carry" in raw else {}
    gc_month = raw["last_gc_month"] if "last_gc_month" in raw else ""
    schema_v = raw["schema_version"] if "schema_version" in raw else 2
    dirty = [s for s in dirty_raw if isinstance(s, str)] if isinstance(dirty_raw, list) else []
    carry = (
        {k: v for k, v in carry_raw.items() if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)}
        if isinstance(carry_raw, dict)
        else {}
    )
    ts_val = float(last_ts) if isinstance(last_ts, (int, float)) and not isinstance(last_ts, bool) else 0.0
    return {
        "last_compact_ts": ts_val,
        "last_dream_date": dream_date if isinstance(dream_date, str) else "",
        "dirty_shards": dirty,
        "light_carry": carry,
        "last_gc_month": gc_month if isinstance(gc_month, str) else "",
        "schema_version": schema_v if isinstance(schema_v, int) and not isinstance(schema_v, bool) else 2,
    }


def parse_cursor(path: Path) -> VectorCursor | None:
    raw = read_json_object_sync(path)
    if not raw:
        return None
    job_id = raw["job_id"] if "job_id" in raw else None
    persona = raw["persona"] if "persona" in raw else None
    file_name = raw["file"] if "file" in raw else None
    scopes_raw = raw["scope_keys"] if "scope_keys" in raw else None
    kind_index = raw["kind_index"] if "kind_index" in raw else None
    chunk_index = raw["chunk_index"] if "chunk_index" in raw else None
    last_id = raw["last_id"] if "last_id" in raw else None
    done = raw["done"] if "done" in raw else None
    total = raw["total"] if "total" in raw else None
    if not (
        isinstance(job_id, str)
        and isinstance(persona, str)
        and isinstance(file_name, str)
        and isinstance(scopes_raw, list)
        and isinstance(kind_index, int)
        and not isinstance(kind_index, bool)
        and isinstance(chunk_index, int)
        and not isinstance(chunk_index, bool)
        and isinstance(last_id, str)
        and isinstance(done, int)
        and not isinstance(done, bool)
        and isinstance(total, int)
        and not isinstance(total, bool)
    ):
        return None
    return {
        "job_id": job_id,
        "persona": persona,
        "file": file_name,
        "scope_keys": [s for s in scopes_raw if isinstance(s, str)],
        "kind_index": kind_index,
        "chunk_index": chunk_index,
        "last_id": last_id,
        "done": done,
        "total": total,
    }


def copy_back_sync(src: Path, dst: Path) -> int:
    """把解压出的目录 / 文件放回原处（目标先清空，保证是覆盖还原）。"""
    if dst.is_dir():
        shutil.rmtree(dst)
    elif dst.is_file():
        dst.unlink()
    return _copy_into_sync(src, dst)
