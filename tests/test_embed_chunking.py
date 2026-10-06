"""嵌入前切分的契约：结构优先、预算夹紧、切法哈希、插件句柄用父文档。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from gsuid_core.ai_core.rag.base import calculate_hash
from gsuid_core.ai_core.rag.chunking import (
    CHUNKER_ID,
    EMBED_CHAR_BUDGET,
    chunk_body,
    split_text,
    chunker_stamp,
    document_bodies,
    pieces_for_embed,
    chunk_method_hash,
    embed_char_budget,
    logical_chunk_ids,
    should_skip_rebuild,
)
from gsuid_core.ai_core.rag.image_rag import _reindex_image_payloads
from gsuid_core.ai_core.rag.knowledge import (
    _expand_knowledge_row,
    _embed_and_upsert_chunks,
    _prepare_knowledge_reindex,
    _reindex_knowledge_payloads,
    update_manual_knowledge_in_db,
)
from gsuid_core.ai_core.cognition.types import CogScope
from gsuid_core.ai_core.database.models import AIKnowledgeChunk
from gsuid_core.ai_core.cognition.facade import _search_knowledge_backend


def test_structure_cut_keeps_decimals_headings_and_fences() -> None:
    decimal = chunk_body("版本是 3.14。下一句很短。", room=12)
    assert len(decimal) == 2
    assert "3.14" in decimal[0]

    headed = chunk_body("## 小节\n" + ("甲" * 300), room=80, overlap=10)
    assert len(headed) > 1
    assert all(piece.startswith("## 小节") and len(piece) <= 80 for piece in headed)

    sentence = "一二三四五六七八九十。"
    assert chunk_body(sentence * 2, room=16, overlap=10) == [sentence, sentence]

    raw = "".join(chr(0x4E00 + i) for i in range(100))
    windows = chunk_body(raw, room=40, overlap=10)
    assert all(len(piece) <= 40 for piece in windows)
    assert windows[1][:10] == windows[0][-10:]

    fence = "```\nKEEP\n```"
    fenced = chunk_body(("甲" * 40) + "\n\n" + fence + "\n\n" + ("乙" * 40), room=50)
    assert fence in fenced

    assert chunk_body("   ", room=40) == []
    assert split_text("") == []
    assert pieces_for_embed("") == []


def test_embed_budget_clamps_caller_and_recuts_long_sections() -> None:
    pieces = pieces_for_embed("甲" * 2000, title="标" * 40, tags=["标签"], max_tokens=512)
    assert len(pieces) > 1
    assert all(len(piece.embed_text) <= EMBED_CHAR_BUDGET for piece in pieces)

    huge_title = pieces_for_embed("正文", title="题" * 2000, max_tokens=512)
    assert huge_title
    assert all(len(piece.embed_text) <= EMBED_CHAR_BUDGET for piece in huge_title)

    clamped = split_text("乙" * 2000, max_chars=4000, max_tokens=512)
    assert len(clamped) > 1
    assert all(len(piece) <= EMBED_CHAR_BUDGET for piece in clamped)

    bodies = document_bodies(
        full_text="",
        sections=["甲" * 2000],
        title="题",
        tags=["a"],
        budget=4000,
        overlap=60,
        max_tokens=512,
    )
    assert len(bodies) > 1
    assert all(0 < len(body) <= EMBED_CHAR_BUDGET for body in bodies)


def test_budget_follows_declared_token_limit() -> None:
    assert embed_char_budget(512) == 480
    assert embed_char_budget(8192) == 8160
    assert len(pieces_for_embed("甲" * 2000, max_tokens=8192)) == 1
    narrow = pieces_for_embed("甲" * 2000, max_tokens=512)
    assert len(narrow) > 1
    assert chunk_method_hash("h", 512) != chunk_method_hash("h", 8192)
    kept = split_text("乙" * 900, max_chars=400, max_tokens=8192)
    assert len(kept) > 1
    assert all(len(piece) <= 400 for piece in kept)
    with patch("gsuid_core.ai_core.rag.base.embedding_provider", None):
        assert embed_char_budget() == 480
        assert chunker_stamp() == "embed-v1@512"


def test_model_card_sets_window_and_unknown_remote_stays_short() -> None:
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from gsuid_core.ai_core.rag.embedding.local import _configured_max_tokens
    from gsuid_core.ai_core.rag.embedding.openai import _remote_max_input_tokens

    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "models--demo--embed-zh"
        snap = root / "snapshots" / "snap1"
        snap.mkdir(parents=True)
        (root / "refs").mkdir()
        (root / "refs" / "main").write_text("snap1", encoding="utf-8")
        (snap / "tokenizer_config.json").write_text(
            json.dumps({"model_max_length": 512}),
            encoding="utf-8",
        )
        (snap / "config.json").write_text(
            json.dumps({"model_max_length": 8192, "max_position_embeddings": 512, "hidden_size": 768}),
            encoding="utf-8",
        )
        assert _configured_max_tokens(tmp, "demo/embed-zh") == 8192
        (snap / "config.json").write_text(
            json.dumps({"max_position_embeddings": 512, "hidden_size": 768}),
            encoding="utf-8",
        )
        assert _configured_max_tokens(tmp, "demo/embed-zh") == 512
        (snap / "config.json").write_text("{", encoding="utf-8")
        assert _configured_max_tokens(tmp, "demo/embed-zh") == 512
    assert _configured_max_tokens(tmp, "missing/model") == 512
    assert _remote_max_input_tokens("text-embedding-3-small") == 8191
    assert _remote_max_input_tokens("jina-embeddings-v2-base-zh") == 8192
    assert _remote_max_input_tokens("BAAI/bge-small-zh-v1.5") == 512
    assert _remote_max_input_tokens("my-private-embed") == 512


def test_skill_doc_hash_changes_when_token_limit_changes() -> None:
    from gsuid_core.ai_core.rag.skills_kb import _content_hash

    with patch("gsuid_core.ai_core.rag.base.embedding_provider", SimpleNamespace(max_input_tokens=512)):
        narrow = _content_hash("same")
    with patch("gsuid_core.ai_core.rag.base.embedding_provider", SimpleNamespace(max_input_tokens=8192)):
        wide = _content_hash("same")
    assert narrow != wide


def test_skip_rebuild_uses_chunker_stamp_and_keeps_legacy_one_piece() -> None:
    content_hash = "content-hash"
    stamp = chunk_method_hash(content_hash)
    content_only = calculate_hash({"_content": content_hash})
    assert stamp != content_only
    assert CHUNKER_ID

    cases: list[tuple[str, int, str, int, bool]] = [
        (stamp, 3, content_hash, 3, True),
        (stamp, 2, content_hash, 3, False),
        (content_hash, 1, content_hash, 1, True),
        (content_hash, 1, content_hash, 4, False),
        ("other-stamp", 3, content_hash, 3, False),
        ("", 1, content_hash, 1, False),
    ]
    for stored_hash, stored_count, content, piece_count, expected in cases:
        assert should_skip_rebuild(stored_hash, stored_count, content, piece_count) is expected


def test_single_piece_keeps_parent_id() -> None:
    assert logical_chunk_ids("doc", 1) == ["doc"]
    assert logical_chunk_ids("doc", 2) == ["doc#0", "doc#1"]


def test_plugin_search_handle_uses_parent_doc_id() -> None:
    plugin = SimpleNamespace(
        id="pt-plugin",
        score=0.9,
        payload={
            "id": "guide#2",
            "doc_id": "guide",
            "source": "plugin",
            "chunk_index": 2,
            "content": "正文",
            "title": "标题",
            "plugin": "demo",
        },
    )
    manual = SimpleNamespace(
        id="pt-manual",
        score=0.4,
        payload={
            "id": "doc#0",
            "doc_id": "doc",
            "source": "manual",
            "chunk_index": 0,
            "content": "卡片",
            "title": "手册",
            "plugin": "manual",
        },
    )

    async def _query(*_args: object, **_kwargs: object) -> list[SimpleNamespace]:
        return [plugin, manual]

    async def _run() -> None:
        with patch("gsuid_core.ai_core.rag.query_knowledge", _query):
            _ids, hits = await _search_knowledge_backend("问句", scope=CogScope(user_id=""), limit=5)
        plugin_hit = hits["kb_guide"]
        assert plugin_hit.handle == "kb_plugin:guide"
        assert plugin_hit.chunk_index == -1
        manual_hit = hits["kb_doc#0"]
        assert manual_hit.handle == "kb_kbdoc:doc"
        assert manual_hit.chunk_index == 0

    asyncio.run(_run())


def test_plugin_search_keeps_one_hit_per_parent_doc() -> None:
    def _plugin_point(chunk: int, score: float) -> SimpleNamespace:
        return SimpleNamespace(
            id=f"pt-{chunk}",
            score=score,
            payload={
                "id": f"guide#{chunk}",
                "doc_id": "guide",
                "source": "plugin",
                "chunk_index": chunk,
                "content": f"片{chunk}",
                "title": "标题",
                "plugin": "demo",
            },
        )

    other = SimpleNamespace(
        id="pt-other",
        score=0.5,
        payload={
            "id": "other#0",
            "doc_id": "other",
            "source": "plugin",
            "chunk_index": 0,
            "content": "另一篇",
            "title": "其它",
            "plugin": "demo",
        },
    )
    seen_limit = {"n": 0}

    async def _query(*_args: object, **kwargs: object) -> list[SimpleNamespace]:
        raw_limit = kwargs["limit"] if "limit" in kwargs else 0
        seen_limit["n"] = raw_limit if isinstance(raw_limit, int) else 0
        return [_plugin_point(0, 0.9), _plugin_point(1, 0.8), _plugin_point(2, 0.7), other]

    async def _run() -> None:
        with patch("gsuid_core.ai_core.rag.query_knowledge", _query):
            ids, hits = await _search_knowledge_backend("问句", scope=CogScope(user_id=""), limit=2)
        assert seen_limit["n"] >= 2
        assert ids == ["kb_guide", "kb_other"]
        assert hits["kb_guide"].handle == "kb_plugin:guide"
        assert hits["kb_other"].handle == "kb_plugin:other"

    asyncio.run(_run())


def _manual_row(
    *,
    row_id: str,
    doc_id: str,
    chunk_index: int,
    title: str,
    content: str,
) -> AIKnowledgeChunk:
    return AIKnowledgeChunk(
        id=row_id,
        doc_id=doc_id,
        chunk_index=chunk_index,
        title=title,
        content=content,
        tags="[]",
        source="manual",
        plugin="manual",
        qdrant_id=f"qid-{row_id}",
        content_hash="h",
    )


def test_segment_title_does_not_nest_packed_row() -> None:
    with patch("gsuid_core.ai_core.rag.base.embedding_provider", SimpleNamespace(max_input_tokens=512)):
        title = "手册"
        budget = embed_char_budget(512)
        room = budget - len(f"标题：{title}") - 1
        body = "甲" * room
        packed = pieces_for_embed(body, title=title, max_tokens=512)
        assert len(packed) == 1
        recut = pieces_for_embed(packed[0].body, title=f"{title} - 第1段", max_tokens=512)
        assert len(recut) > 1
        expanded = _expand_knowledge_row(
            _manual_row(
                row_id="doc#0",
                doc_id="doc",
                chunk_index=0,
                title=f"{title} - 第1段",
                content=packed[0].body,
            )
        )
        assert [part.id for part in expanded] == ["doc#0"]
        assert expanded[0].chunk_index == 0


def test_reindex_groups_siblings_instead_of_reissuing_doc0() -> None:
    with patch("gsuid_core.ai_core.rag.base.embedding_provider", SimpleNamespace(max_input_tokens=512)):
        long_a = "甲" * 800
        long_b = "乙" * 800
        rows = _prepare_knowledge_reindex(
            [
                (
                    "pid0",
                    {
                        "id": "doc#0",
                        "doc_id": "doc",
                        "chunk_index": 0,
                        "title": "文 - 第1段",
                        "content": long_a,
                        "tags": [],
                    },
                ),
                (
                    "pid1",
                    {
                        "id": "doc#1",
                        "doc_id": "doc",
                        "chunk_index": 1,
                        "title": "文 - 第2段",
                        "content": long_b,
                        "tags": [],
                    },
                ),
            ]
        )
        ids = [str(row[1]["id"]) for row in rows]
        assert ids == logical_chunk_ids("doc", len(rows))
        assert len(set(ids)) == len(ids)
        joined = "".join(str(row[1]["content"]) for row in rows)
        assert "甲" in joined
        assert "乙" in joined


def test_over_budget_chunk_update_rebuilds_whole_document() -> None:
    row0 = _manual_row(
        row_id="doc#0",
        doc_id="doc",
        chunk_index=0,
        title="手册 - 第1段",
        content="旧零",
    )
    row1 = _manual_row(
        row_id="doc#1",
        doc_id="doc",
        chunk_index=1,
        title="手册 - 第2段",
        content="旧一",
    )
    captured: dict[str, object] = {}

    async def fake_get(entity_id: str) -> AIKnowledgeChunk:
        assert entity_id == "doc#0"
        return row0

    async def fake_list(doc_id: str) -> list[AIKnowledgeChunk]:
        assert doc_id == "doc"
        return [row0, row1]

    async def fake_add(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"doc_id": "doc", "total_chunks": 2, "written": 2, "skipped": 0}

    async def _run() -> None:
        with (
            patch("gsuid_core.ai_core.rag.base.embedding_provider", SimpleNamespace(max_input_tokens=512)),
            patch.object(AIKnowledgeChunk, "get_by_id", fake_get),
            patch.object(AIKnowledgeChunk, "list_by_doc", fake_list),
            patch("gsuid_core.ai_core.rag.knowledge.add_knowledge_document", fake_add),
        ):
            ok = await update_manual_knowledge_in_db("doc#0", {"content": "甲" * 2000})
        assert ok is True
        assert captured["replace"] is True
        assert captured["title"] == "手册"
        assert captured["full_text"] == ("甲" * 2000) + "\n旧一"
        assert captured["doc_id"] == "doc"

    asyncio.run(_run())


def test_under_budget_chunk_update_rebuilds_whole_document() -> None:
    row0 = _manual_row(
        row_id="doc#0",
        doc_id="doc",
        chunk_index=0,
        title="手册 - 第1段",
        content="旧零",
    )
    row1 = _manual_row(
        row_id="doc#1",
        doc_id="doc",
        chunk_index=1,
        title="手册 - 第2段",
        content="旧一",
    )
    captured: dict[str, object] = {}

    async def fake_get(entity_id: str) -> AIKnowledgeChunk:
        assert entity_id == "doc#1"
        return row1

    async def fake_list(doc_id: str) -> list[AIKnowledgeChunk]:
        assert doc_id == "doc"
        return [row0, row1]

    async def fake_add(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"doc_id": "doc", "total_chunks": 2, "written": 2, "skipped": 0}

    async def _run() -> None:
        with (
            patch("gsuid_core.ai_core.rag.base.embedding_provider", SimpleNamespace(max_input_tokens=512)),
            patch.object(AIKnowledgeChunk, "get_by_id", fake_get),
            patch.object(AIKnowledgeChunk, "list_by_doc", fake_list),
            patch("gsuid_core.ai_core.rag.knowledge.add_knowledge_document", fake_add),
        ):
            ok = await update_manual_knowledge_in_db("doc#1", {"content": "新一"})
        assert ok is True
        assert captured["replace"] is True
        assert captured["title"] == "手册"
        assert captured["full_text"] == "旧零\n新一"
        assert captured["doc_id"] == "doc"

    asyncio.run(_run())


def test_reindex_prepare_failure_propagates() -> None:
    async def _run() -> None:
        with (
            patch("gsuid_core.ai_core.rag.base.client", object()),
            patch("gsuid_core.ai_core.rag.base.embedding_model", object()),
            patch(
                "gsuid_core.ai_core.rag.knowledge._prepare_knowledge_reindex",
                side_effect=RuntimeError("cut"),
            ),
        ):
            try:
                await _reindex_knowledge_payloads([("pid", {"id": "doc", "content": "x", "title": "t"})])
            except RuntimeError as exc:
                assert str(exc) == "cut"
            else:
                raise AssertionError("prepare failure must propagate")

    asyncio.run(_run())


def test_embed_upsert_deletes_old_only_after_vectors_written() -> None:
    order: list[str] = []
    old = _manual_row(row_id="doc", doc_id="doc", chunk_index=0, title="t", content="old")
    new0 = _manual_row(row_id="doc#0", doc_id="doc", chunk_index=0, title="t", content="a")
    new1 = _manual_row(row_id="doc#1", doc_id="doc", chunk_index=1, title="t", content="b")

    async def fake_upsert_many(rows: list[AIKnowledgeChunk]) -> int:
        order.append("sql_upsert")
        return len(rows)

    async def fake_delete_ids(ids: list[str]) -> int:
        order.append("sql_delete")
        return len(ids)

    class _Client:
        async def delete(self, collection_name: str, points_selector: object) -> None:
            order.append("qdrant_delete")

    class _Point:
        def __init__(self, point_id: str) -> None:
            self.id = point_id

    async def fake_compute(items: list[object]) -> list[_Point]:
        order.append("embed")
        return [_Point("qid-doc#0"), _Point("qid-doc#1")]

    async def fake_upsert_points(points: list[object], batch_size: int | None = None) -> None:
        order.append("qdrant_upsert")

    async def _run() -> None:
        with (
            patch.object(AIKnowledgeChunk, "upsert_many", fake_upsert_many),
            patch.object(AIKnowledgeChunk, "delete_ids", fake_delete_ids),
            patch("gsuid_core.ai_core.rag.knowledge._expand_knowledge_row", lambda row: [new0, new1]),
            patch("gsuid_core.ai_core.rag.knowledge._compute_knowledge_points", fake_compute),
            patch("gsuid_core.ai_core.rag.knowledge._upsert_knowledge_points", fake_upsert_points),
            patch("gsuid_core.ai_core.rag.base.client", _Client()),
            patch("gsuid_core.ai_core.rag.base.embedding_model", object()),
        ):
            await _embed_and_upsert_chunks([old])
        assert order == ["sql_upsert", "embed", "qdrant_upsert", "sql_delete", "qdrant_delete"]

    asyncio.run(_run())


def test_embed_upsert_keeps_old_when_embed_skips() -> None:
    order: list[str] = []
    old = _manual_row(row_id="doc", doc_id="doc", chunk_index=0, title="t", content="old")
    new0 = _manual_row(row_id="doc#0", doc_id="doc", chunk_index=0, title="t", content="a")
    new1 = _manual_row(row_id="doc#1", doc_id="doc", chunk_index=1, title="t", content="b")

    async def fake_upsert_many(rows: list[AIKnowledgeChunk]) -> int:
        order.append("sql_upsert")
        return len(rows)

    async def fake_delete_ids(ids: list[str]) -> int:
        order.append("sql_delete")
        return len(ids)

    class _Client:
        async def delete(self, collection_name: str, points_selector: object) -> None:
            order.append("qdrant_delete")

    async def fake_compute(items: list[object]) -> list[object]:
        order.append("embed")
        return []

    async def fake_upsert_points(points: list[object], batch_size: int | None = None) -> None:
        order.append("qdrant_upsert")

    async def _run() -> None:
        with (
            patch.object(AIKnowledgeChunk, "upsert_many", fake_upsert_many),
            patch.object(AIKnowledgeChunk, "delete_ids", fake_delete_ids),
            patch("gsuid_core.ai_core.rag.knowledge._expand_knowledge_row", lambda row: [new0, new1]),
            patch("gsuid_core.ai_core.rag.knowledge._compute_knowledge_points", fake_compute),
            patch("gsuid_core.ai_core.rag.knowledge._upsert_knowledge_points", fake_upsert_points),
            patch("gsuid_core.ai_core.rag.base.client", _Client()),
            patch("gsuid_core.ai_core.rag.base.embedding_model", object()),
        ):
            written, skipped = await _embed_and_upsert_chunks([old])
        assert written == 0
        assert skipped == 2
        assert order == ["sql_upsert", "embed"]

    asyncio.run(_run())


def test_image_reindex_embed_failure_propagates() -> None:
    class _Model:
        async def aembed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embed down")

    async def _run() -> None:
        with (
            patch("gsuid_core.ai_core.rag.base.client", object()),
            patch("gsuid_core.ai_core.rag.base.embedding_model", _Model()),
        ):
            try:
                await _reindex_image_payloads([("pid", {"id": "img", "content": "图", "tags": ["t"], "path": "p.png"})])
            except RuntimeError as exc:
                assert "embed down" in str(exc)
            else:
                raise AssertionError("batch embed failure must propagate")

    asyncio.run(_run())


def test_image_reindex_all_413_raises_to_keep_backup() -> None:
    import gsuid_core.ai_core.rag.base as rag_base

    class _Model:
        async def aembed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("413 payload too large")

    async def _run() -> None:
        rag_base._cached_embed_bs = 0
        with (
            patch("gsuid_core.ai_core.rag.base.client", object()),
            patch("gsuid_core.ai_core.rag.base.embedding_model", _Model()),
        ):
            try:
                await _reindex_image_payloads([("pid", {"id": "img", "content": "图", "tags": ["t"], "path": "p.png"})])
            except RuntimeError as exc:
                assert "backup" in str(exc).lower() or "备份" in str(exc)
            else:
                raise AssertionError("empty reindex must raise")
        rag_base._cached_embed_bs = 0

    asyncio.run(_run())


def test_knowledge_reindex_empty_embed_raises() -> None:
    def fake_prepare(payload_backup: object) -> list[tuple[str, dict[str, object], str]]:
        return [("qid", {"id": "doc"}, "text")]

    async def fake_compute(items: list[object]) -> list[object]:
        return []

    async def _run() -> None:
        with (
            patch("gsuid_core.ai_core.rag.base.client", object()),
            patch("gsuid_core.ai_core.rag.base.embedding_model", object()),
            patch("gsuid_core.ai_core.rag.knowledge._prepare_knowledge_reindex", fake_prepare),
            patch("gsuid_core.ai_core.rag.knowledge._compute_knowledge_points", fake_compute),
        ):
            try:
                await _reindex_knowledge_payloads([("pid", {"id": "doc", "content": "x", "title": "t"})])
            except RuntimeError as exc:
                assert "backup" in str(exc).lower() or "备份" in str(exc)
            else:
                raise AssertionError("empty knowledge reindex must raise")

    asyncio.run(_run())
