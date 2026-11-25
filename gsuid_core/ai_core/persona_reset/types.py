"""人格重置 / 备份 / 还原的公共前后端数据结构。"""

from __future__ import annotations

from typing import Literal, TypedDict

ResetItemKey = Literal[
    "journal",
    "self_memory",
    "relationship",
    "persona_state",
    "session_history",
    "scheduled_tasks",
    "global_group",
    "global_user",
    "user_preferences",
]
ItemGroup = Literal["persona", "global"]
ItemUnit = Literal["rows", "files", "sessions", "tasks", "keys"]
JobKind = Literal["reset", "restore"]
JobStatus = Literal["running", "done", "failed", "resumable"]
StepStatus = Literal["pending", "running", "done", "skipped", "failed"]
StepKey = Literal[
    "prepare",
    "backup",
    "memory",
    "user_preferences",
    "journal",
    "relationship",
    "persona_state",
    "scheduled_tasks",
    "clear",
    "import",
    "files",
    "vectors",
    "finish",
]

ALL_ITEMS: tuple[ResetItemKey, ...] = (
    "journal",
    "self_memory",
    "relationship",
    "persona_state",
    "session_history",
    "scheduled_tasks",
    "global_group",
    "global_user",
    "user_preferences",
)
GLOBAL_ITEMS: frozenset[ResetItemKey] = frozenset({"global_group", "global_user", "user_preferences"})
DEFAULT_OFF_ITEMS: frozenset[ResetItemKey] = frozenset(
    {"scheduled_tasks", "global_group", "global_user", "user_preferences"}
)
# 会话历史只在内存里（12h 滚动），不进备份也不能还原。
UNBACKUPABLE_ITEMS: frozenset[ResetItemKey] = frozenset({"session_history"})
MEMORY_ITEMS: frozenset[ResetItemKey] = frozenset({"self_memory", "global_group", "global_user"})

ITEM_UNITS: dict[ResetItemKey, ItemUnit] = {
    "journal": "files",
    "self_memory": "rows",
    "relationship": "rows",
    "persona_state": "keys",
    "session_history": "sessions",
    "scheduled_tasks": "tasks",
    "global_group": "rows",
    "global_user": "rows",
    "user_preferences": "rows",
}


def parse_items(raw: list[str]) -> list[ResetItemKey]:
    """按 ``ALL_ITEMS`` 顺序去重，未知键直接丢弃。"""
    wanted = set(raw)
    return [item for item in ALL_ITEMS if item in wanted]


class ResetItemPreview(TypedDict):
    key: ResetItemKey
    group: ItemGroup
    default_checked: bool
    count: int
    unit: ItemUnit
    backup_supported: bool
    backup_seconds: float
    backup_bytes: int


class JobStep(TypedDict):
    key: StepKey
    status: StepStatus
    done: int
    total: int


class JobView(TypedDict):
    job_id: str
    kind: JobKind
    persona: str
    status: JobStatus
    steps: list[JobStep]
    eta_seconds: int
    error: str
    backup_file: str
    counts: dict[str, int]
    started_at: int
    finished_at: int


class ResetPreview(TypedDict):
    persona: str
    items: list[ResetItemPreview]
    other_personas: list[str]
    legacy_self_key: bool
    active_job: JobView | None


class BackupInfo(TypedDict):
    file: str
    created_at: int
    items: list[ResetItemKey]
    counts: dict[str, int]
    size_bytes: int
    embedding_model: str


class RestorePreview(TypedDict):
    file: str
    items: list[ResetItemKey]
    overwrite_counts: dict[str, int]
    import_counts: dict[str, int]
    vector_texts: int
    vector_eta_seconds: int
    throughput_per_sec: float
    embedding_model: str
    active_job: JobView | None


class VectorCursor(TypedDict):
    job_id: str
    persona: str
    file: str
    scope_keys: list[str]
    kind_index: int
    chunk_index: int
    last_id: str
    done: int
    total: int
