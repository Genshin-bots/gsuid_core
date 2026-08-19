"""Everything is Memory：统一写、web 恒落盘、联邦补路、主人格收工具面。"""

from __future__ import annotations

import time
import inspect
from typing import Any
from unittest.mock import patch

from gsuid_core.models import Event
from gsuid_core.ai_core.cognition import (
    ALL_KINDS,
    KIND_LABEL,
    MEDIA_KINDS,
    CogKind,
    CogScope,
    MemoryWrite,
    kinds_from_names,
)
from gsuid_core.message_history.manager import MessageRecord, HistoryManager
from gsuid_core.ai_core.planning.tool_output_helper import (
    is_searchish_tool,
    should_persist_tool_return,
)


def test_kind_labels_cover_media_and_record() -> None:
    assert CogKind.IMAGE in ALL_KINDS
    assert CogKind.MEME in ALL_KINDS
    assert CogKind.MEME_KNOWLEDGE in ALL_KINDS
    assert CogKind.RECORD in ALL_KINDS
    assert MEDIA_KINDS == {CogKind.IMAGE, CogKind.MEME}
    assert set(KIND_LABEL) == set(CogKind)
    assert kinds_from_names({"image", "meme", "record"}) == {
        CogKind.IMAGE,
        CogKind.MEME,
        CogKind.RECORD,
    }


def test_remember_is_index_only_contract() -> None:
    from gsuid_core.ai_core.cognition.remember import remember

    src = inspect.getsource(remember)
    assert "sync_node" in src
    write = MemoryWrite(kind=CogKind.RECORD, ref="r1", scope_key="group:g1", owner_user_id="u1")
    assert write.handle == ""
    assert "content" not in MemoryWrite.__dataclass_fields__


def test_searchish_always_persists_short_serp() -> None:
    assert is_searchish_tool("web_search_tool")
    assert is_searchish_tool("web_fetch_tool")
    assert not is_searchish_tool("plugin_stock_quote")
    body = "<search_results>\nquery: foo\n[1] bar\n</search_results>"
    assert should_persist_tool_return("web_search_tool", body)
    assert not should_persist_tool_return("web_search_tool", "错误：Web 搜索未配置 API Key")
    assert not should_persist_tool_return("web_search_tool", "短")


def test_history_a_keyword_projection() -> None:
    mgr = HistoryManager(max_messages=20)
    ev = Event(bot_id="b", bot_self_id="s", user_id="u1", group_id="g1", user_type="group")
    mgr.add_message(ev, role="user", content="下周去北海道旅行", user_name="甲")
    mgr.add_message(ev, role="user", content="今天吃了拉面", user_name="乙")
    hits = mgr.search_recent_for_cognition(group_id="g1", user_id="u1", bot_id="b", query="北海道", limit=5)
    assert len(hits) == 1
    rec, score = hits[0]
    assert "北海道" in rec.content
    assert score > 0
    private = mgr.search_recent_for_cognition(group_id=None, user_id="u1", bot_id="b", query="北海道", limit=5)
    assert private == []


def test_search_cognition_dispatches_extra_backends() -> None:
    from gsuid_core.ai_core.cognition import search_cognition

    calls: list[str] = []

    async def _empty(*args: Any, **kwargs: Any) -> Any:
        return [], {}

    def _track(name: str) -> Any:
        async def _inner(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return [], {}

        return _inner

    async def _run() -> None:
        with (
            patch("gsuid_core.ai_core.cognition.facade._search_memory", new=_empty),
            patch("gsuid_core.ai_core.cognition.facade._search_knowledge_backend", new=_empty),
            patch("gsuid_core.ai_core.cognition.facade._search_fileos", new=_empty),
            patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=_track("artifact")),
            patch("gsuid_core.ai_core.cognition.facade._search_history", new=_track("history")),
            patch("gsuid_core.ai_core.cognition.facade._search_records", new=_track("record")),
            patch("gsuid_core.ai_core.cognition.facade._search_images", new=_track("image")),
            patch("gsuid_core.ai_core.cognition.facade._search_memes", new=_track("meme")),
            patch("gsuid_core.ai_core.cognition.facade._search_meme_knowledge", new=_track("meme_knowledge")),
            patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=_empty),
            patch("gsuid_core.ai_core.cognition.facade._artifact_enabled", new=lambda: True),
        ):
            await search_cognition("北海道", kinds=ALL_KINDS, scope=CogScope(user_id="u1", group_id="g1"), limit=8)

    import asyncio

    asyncio.run(_run())
    assert set(calls) >= {"history", "record", "image", "meme", "artifact"}


def test_visible_to_capability_only_hides_main_persona() -> None:
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools.visibility import capability_only_from_deps

    assert capability_only_from_deps(ToolContext(allow_user_outbound=True)) is False
    assert capability_only_from_deps(ToolContext(allow_user_outbound=False)) is True
    assert capability_only_from_deps(None) is True


def test_sibling_search_tools_are_capability_only() -> None:
    import gsuid_core.ai_core.planning.kanban_tools as kanban_mod
    import gsuid_core.ai_core.buildin_tools.meme_tools as meme_mod
    import gsuid_core.ai_core.buildin_tools.rag_search as rag_mod
    import gsuid_core.ai_core.planning.tool_output_tools as fileos_mod

    assert "visible_to_capability_only" in inspect.getsource(rag_mod)
    assert "visible_to_capability_only" in inspect.getsource(meme_mod)
    assert "visible_to_capability_only" in inspect.getsource(kanban_mod)
    assert "visible_to_capability_only" in inspect.getsource(fileos_mod)


def test_message_record_timestamp_default() -> None:
    rec = MessageRecord(role="user", content="hi", user_id="u")
    assert rec.timestamp <= time.time()
