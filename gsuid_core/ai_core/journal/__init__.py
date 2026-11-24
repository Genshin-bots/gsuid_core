"""独立人格分片式生活手账与离线做梦模块（Sharded Journal & Dreaming）。"""

from .dream import (
    compact_journal,
    init_journal_scheduler,
    stop_journal_scheduler,
    check_and_compact_all_journals,
)
from .manager import (
    get_journal_path,
    get_journal_content,
    record_journal_entry,
    save_journal_content,
    get_journal_structured,
    read_journal_for_context,
    read_archived_journal_handle,
    resolve_persona_for_group_or_session,
)
from .storage import (
    JournalEntry,
    ShardDocument,
    get_last_compact_ts,
    get_all_journal_personas,
)
from .sanitizer import (
    clean_journal_tag,
    truncate_at_boundary,
    is_rejected_journal_body,
    sanitize_journal_summary,
)

__all__ = [
    "JournalEntry",
    "ShardDocument",
    "clean_journal_tag",
    "truncate_at_boundary",
    "is_rejected_journal_body",
    "sanitize_journal_summary",
    "record_journal_entry",
    "read_journal_for_context",
    "read_archived_journal_handle",
    "resolve_persona_for_group_or_session",
    "get_journal_content",
    "get_journal_structured",
    "save_journal_content",
    "compact_journal",
    "get_journal_path",
    "get_last_compact_ts",
    "get_all_journal_personas",
    "check_and_compact_all_journals",
    "init_journal_scheduler",
    "stop_journal_scheduler",
]
