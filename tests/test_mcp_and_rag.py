"""MCP 运行时与 RAG 文本切分契约测试。"""

from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import patch

import pytest
from fastmcp import FastMCP
from starlette.applications import Starlette

from gsuid_core.ai_core.mcp import server as mcp_server
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


def test_structure_cut_keeps_decimals_headings_and_fences() -> None:
    decimal = chunk_body("版本是 3.14。下一句很短。", room=12)
    assert len(decimal) == 2
    assert "3.14" in decimal[0]

    headed = chunk_body("## 小节\n" + ("甲" * 300), room=80, overlap=10)
    assert len(headed) > 1
    assert all(piece.startswith("## 小节") and len(piece) <= 80 for piece in headed)

    sentence = "一二三四五六七八九十。"
    assert chunk_body(sentence * 2, room=16, overlap=10) == [sentence, sentence]

    fence = "```\nKEEP\n```"
    fenced = chunk_body(("甲" * 40) + "\n\n" + fence + "\n\n" + ("乙" * 40), room=50)
    assert fence in fenced
    assert chunk_body("   ", room=40) == []
    assert split_text("") == []


def test_embed_budget_clamps_caller_and_recuts_long_sections() -> None:
    pieces = pieces_for_embed("甲" * 2000, title="标" * 40, tags=["标签"], max_tokens=512)
    assert len(pieces) > 1
    assert all(len(piece.embed_text) <= EMBED_CHAR_BUDGET for piece in pieces)

    huge_title = pieces_for_embed("正文", title="题" * 2000, max_tokens=512)
    assert huge_title
    assert all(len(piece.embed_text) <= EMBED_CHAR_BUDGET for piece in huge_title)

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
    with patch("gsuid_core.ai_core.rag.base.embedding_provider", None):
        assert embed_char_budget() == 480
        assert chunker_stamp() == "embed-v1@512"


def test_skip_rebuild_uses_chunker_stamp() -> None:
    content_hash = "content-hash"
    stamp = chunk_method_hash(content_hash)
    content_only = calculate_hash({"_content": content_hash})
    assert stamp != content_only
    assert CHUNKER_ID
    assert should_skip_rebuild(stamp, 3, content_hash, 3) is True
    assert should_skip_rebuild(stamp, 2, content_hash, 3) is False


def test_single_piece_keeps_parent_id() -> None:
    assert logical_chunk_ids("doc", 1) == ["doc"]
    assert logical_chunk_ids("doc", 2) == ["doc#0", "doc#1"]


def test_rebuild_skips_when_unmounted(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FastMCP("synthetic-unmounted")
    calls: List[int] = []

    def factory(*, path: str, transport: str, stateless_http: bool) -> Starlette:
        calls.append(1)
        return Starlette()

    monkeypatch.setattr(server, "http_app", factory)
    runtime = mcp_server._McpHttpRuntime(server)
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", runtime)
    monkeypatch.setattr(mcp_server, "_http_mounted", False)
    monkeypatch.setattr(mcp_server, "_mcp_last_rebuild", 0.0)
    monkeypatch.setattr(mcp_server, "_mcp_rebuild_lock", asyncio.Lock())

    asyncio.run(mcp_server._rebuild_http_mcp_session())
    assert calls == []


def test_rag_base_calculate_hash() -> None:
    h1 = calculate_hash({"a": 1, "b": "test"})
    h2 = calculate_hash({"b": "test", "a": 1})
    assert h1 == h2
    assert len(h1) == 32


def test_chunk_method_hash_changes_with_tokens() -> None:
    h512 = chunk_method_hash("base", 512)
    h1024 = chunk_method_hash("base", 1024)
    assert h512 != h1024
