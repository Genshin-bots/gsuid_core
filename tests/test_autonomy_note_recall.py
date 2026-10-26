"""自主行动笔记回想、句柄读取与语境合并测试。"""

import asyncio
from typing import Any
from pathlib import Path
from unittest.mock import patch

from gsuid_core.ai_core.autonomy.tools import artifact_dir, safe_filename, index_note_node, sync_persona_artifacts
from gsuid_core.ai_core.autonomy.models import FactPack
from gsuid_core.ai_core.cognition.types import DEFAULT_RECALL_KINDS, CogKind, CogScope
from gsuid_core.ai_core.cognition.facade import search_cognition
from gsuid_core.ai_core.autonomy.dispatch import stage_text
from gsuid_core.ai_core.cognition.remember import MemoryWrite
from gsuid_core.ai_core.planning.handle_resolver import handle_kind_of, resolve_handle, format_resolved


def test_default_recall_kinds_includes_self_note() -> None:
    assert CogKind.SELF_NOTE in DEFAULT_RECALL_KINDS


def test_note_handle_kind() -> None:
    assert handle_kind_of("note:persona_alpha:mock_note_a") == "artifact"
    assert handle_kind_of("note:persona_beta:mock_note_b") == "artifact"


def test_stage_text_prioritizes_summary() -> None:
    pack = FactPack(
        verified_tools=["web_search_tool"],
        factual_observations=["<search_results>外部资料仅供参考..."],
        generated_resources=[],
        has_substantive_outcome=True,
    )
    assert stage_text("检索到测试相关结论", pack) == "检索到测试相关结论"
    assert "<search_results>" in stage_text("", pack)


def test_resolve_handle_reads_note_from_disk(tmp_path: Path) -> None:
    persona_name = "test_persona"
    title = "test_note_title"
    body = "这是测试笔记的模拟正文内容，包含要点A与要点B。"
    handle = f"note:{persona_name}:{title}"

    folder = artifact_dir(persona_name)
    folder.mkdir(parents=True, exist_ok=True)
    note_file = folder / safe_filename(title)
    note_file.write_text(body, encoding="utf-8")

    try:
        resolved = asyncio.run(resolve_handle(handle))
        assert resolved is not None
        assert resolved.source == "artifact"
        assert body in (resolved.payload_inline or "")
        rendered = format_resolved(resolved)
        assert "这是测试笔记的模拟正文内容" in rendered
    finally:
        if note_file.exists():
            note_file.unlink()


def test_index_note_node_calls_remember() -> None:
    captured_writes: list[MemoryWrite] = []

    async def _mock_remember(write: MemoryWrite) -> int:
        captured_writes.append(write)
        return 1

    with patch("gsuid_core.ai_core.cognition.remember.remember", new=_mock_remember):
        asyncio.run(
            index_note_node(
                "test_persona",
                "mock_note",
                "测试笔记模拟摘要内容",
                "note:test_persona:mock_note",
                bot_self_id="mock_bot_1",
            )
        )

    assert len(captured_writes) == 1
    w = captured_writes[0]
    assert w.kind == CogKind.SELF_NOTE
    assert w.ref == "autonomy_note:test_persona:mock_note"
    assert w.title == "mock_note"
    assert "测试笔记模拟摘要内容" in w.summary
    assert w.handle == "note:test_persona:mock_note"


def test_sync_persona_artifacts_reads_disk(tmp_path: Path) -> None:
    captured: list[MemoryWrite] = []

    async def _mock_remember(write: MemoryWrite) -> int:
        captured.append(write)
        return 1

    folder = artifact_dir("test_persona_sync")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "item_1.txt").write_text("content_1", encoding="utf-8")
    (folder / "item_2.txt").write_text("content_2", encoding="utf-8")

    try:
        with patch("gsuid_core.ai_core.cognition.remember.remember", new=_mock_remember):
            count = asyncio.run(sync_persona_artifacts("test_persona_sync", bot_self_id="mock_bot_1"))
            assert count == 2
            assert len(captured) == 2
            titles = {w.title for w in captured}
            assert "item_1" in titles and "item_2" in titles
    finally:
        for p in folder.glob("*.txt"):
            p.unlink()
        folder.rmdir()


def test_search_cognition_default_recall_includes_self_note_in_kinds() -> None:
    captured_kinds: list[Any] = []

    async def _fake_search_nodes(query: str, *, kinds: Any, scope: Any, limit: int = 12) -> Any:
        captured_kinds.append(kinds)
        return ([], {})

    with patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=_fake_search_nodes):
        asyncio.run(
            search_cognition(
                "test_search_query",
                kinds=DEFAULT_RECALL_KINDS,
                scope=CogScope(user_id="test_user_1", bot_self_id="mock_bot_1"),
                limit=10,
            )
        )

    assert len(captured_kinds) == 1
    assert CogKind.SELF_NOTE in captured_kinds[0]
