"""两路 sync_knowledge 同时进来时，同一批正文只嵌入一次，后到者写入后再核对。"""

from __future__ import annotations

import asyncio
from typing import Protocol, Sequence
from unittest.mock import patch

from qdrant_client.models import Filter, MatchValue, FieldCondition

from gsuid_core.ai_core.rag import knowledge as knowledge_mod
from gsuid_core.ai_core.models import KnowledgeBase
from gsuid_core.ai_core.rag.base import KNOWLEDGE_COLLECTION_NAME, get_point_id, calculate_hash
from gsuid_core.ai_core.register import _ENTITIES
from gsuid_core.ai_core.rag.chunking import DEFAULT_CHUNK_OVERLAP, EmbedPiece, pieces_for_embed, chunk_method_hash
from gsuid_core.ai_core.rag.knowledge import sync_knowledge


def _kept_by_filter(payload: dict[str, object], scroll_filter: object) -> bool:
    if not isinstance(scroll_filter, Filter):
        return True
    must = scroll_filter.must
    if not must:
        return True
    for cond in must:
        if not isinstance(cond, FieldCondition) or cond.key != "source":
            continue
        match = cond.match
        if isinstance(match, MatchValue):
            source = payload["source"] if "source" in payload else ""
            if source != match.value:
                return False
    return True


class _HasIdPayload(Protocol):
    id: object
    payload: object


class _PointSelector(Protocol):
    points: Sequence[object]


class _Record:
    def __init__(self, point_id: str, payload: dict[str, object]) -> None:
        self.id = point_id
        self.payload = payload


class _Store:
    def __init__(self) -> None:
        self.points: dict[str, dict[str, object]] = {}
        self.scrolls = 0
        self.upserts = 0
        self.payload_fields: list[list[str]] = []
        self.unfiltered = 0

    async def scroll(
        self,
        collection_name: str,
        limit: int,
        with_payload: bool | list[str],
        with_vectors: bool,
        offset: object,
        scroll_filter: object = None,
    ) -> tuple[list[_Record], None]:
        if collection_name != KNOWLEDGE_COLLECTION_NAME or limit < 1:
            raise AssertionError(collection_name)
        if with_vectors:
            raise AssertionError("scroll flags")
        if isinstance(with_payload, list):
            if "content" in with_payload:
                raise AssertionError("content requested")
            self.payload_fields.append(list(with_payload))
        elif with_payload is not True:
            raise AssertionError("scroll flags")
        if scroll_filter is None:
            self.unfiltered += 1
        self.scrolls += 1
        if offset is not None:
            return [], None
        records: list[_Record] = []
        for point_id, payload in self.points.items():
            if not _kept_by_filter(payload, scroll_filter):
                continue
            if isinstance(with_payload, list):
                shown = {key: payload[key] for key in with_payload if key in payload}
            else:
                shown = payload
            records.append(_Record(point_id, shown))
        return records, None

    async def upsert(self, collection_name: str, points: Sequence[_HasIdPayload]) -> None:
        if collection_name != KNOWLEDGE_COLLECTION_NAME:
            raise AssertionError(collection_name)
        self.upserts += 1
        for point in points:
            raw = point.payload
            if not isinstance(raw, dict):
                raise AssertionError("payload")
            copied: dict[str, object] = {}
            for key, value in raw.items():
                if isinstance(key, str):
                    copied[key] = value
            self.points[str(point.id)] = copied

    async def delete(self, collection_name: str, points_selector: _PointSelector) -> None:
        if collection_name != KNOWLEDGE_COLLECTION_NAME:
            raise AssertionError(collection_name)
        for point_id in points_selector.points:
            key = str(point_id)
            if key in self.points:
                del self.points[key]


class _Embedder:
    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate
        self.calls = 0
        self.inflight = 0
        self.max_inflight = 0

    async def aembed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.inflight += 1
        if self.inflight > self.max_inflight:
            self.max_inflight = self.inflight
        try:
            # 先到的嵌入停在这里，让后到的调用堵在锁上，再放行。
            await self.gate.wait()
        finally:
            self.inflight -= 1
        return [[1.0] for _ in texts]


class _Switch:
    def __init__(self, data: bool) -> None:
        self.data = data


class _AiConfig:
    def get_config(self, name: str) -> _Switch:
        if name != "enable":
            raise AssertionError(name)
        return _Switch(True)


async def _sparse_none(texts: list[str]) -> list[None]:
    return [None for _ in texts]


def test_overlapping_sync_embeds_once_then_rechecks() -> None:
    entity: KnowledgeBase = {
        "id": "lock-doc",
        "plugin": "LockProbe",
        "title": "短文",
        "content": "一句很短的说明。",
        "tags": ["probe"],
    }
    saved = list(_ENTITIES)
    _ENTITIES.clear()
    _ENTITIES.append(entity)
    store = _Store()
    gate = asyncio.Event()
    embedder = _Embedder(gate)

    async def _run() -> None:
        first = asyncio.create_task(sync_knowledge())
        second = asyncio.create_task(sync_knowledge())
        try:
            for _ in range(20):
                if embedder.calls:
                    break
                await asyncio.sleep(0)
            assert embedder.calls == 1
            assert embedder.max_inflight == 1
            assert store.scrolls == 1
            assert store.upserts == 0
            assert not first.done()
            assert not second.done()
        finally:
            gate.set()
        await asyncio.gather(first, second)
        assert embedder.calls == 1
        assert embedder.max_inflight == 1
        assert store.scrolls == 2
        assert store.unfiltered == 0
        assert store.upserts == 1
        assert len(store.points) == 1
        payload = next(iter(store.points.values()))
        assert payload["source"] == "plugin"
        assert payload["id"] == "lock-doc"
        assert payload["doc_id"] == "lock-doc"
        stored_hash = payload["_hash"]
        assert isinstance(stored_hash, str) and stored_hash != ""

    try:

        async def fake_init() -> None:
            return None

        async def fake_ensure() -> None:
            return None

        with (
            patch("gsuid_core.ai_core.configs.ai_config.ai_config", _AiConfig()),
            patch("gsuid_core.ai_core.rag.base.client", store),
            patch("gsuid_core.ai_core.rag.base.embedding_model", embedder),
            patch("gsuid_core.ai_core.rag.base.ensure_embedding_dimension", fake_ensure),
            patch("gsuid_core.ai_core.rag.knowledge.init_knowledge_collection", fake_init),
            patch("gsuid_core.ai_core.rag.knowledge._sparse_embed_batch_async", _sparse_none),
        ):
            asyncio.run(_run())
    finally:
        _ENTITIES.clear()
        _ENTITIES.extend(saved)


def test_sync_prepares_collection_before_taking_sync_lock() -> None:
    held: list[bool] = []

    async def fake_init() -> None:
        held.append(knowledge_mod._knowledge_sync_lock.locked())

    async def fake_ensure() -> None:
        return None

    async def _run() -> None:
        await sync_knowledge()

    with (
        patch("gsuid_core.ai_core.configs.ai_config.ai_config", _AiConfig()),
        patch("gsuid_core.ai_core.rag.base.client", None),
        patch("gsuid_core.ai_core.rag.base.embedding_model", None),
        patch("gsuid_core.ai_core.rag.base.init_embedding_model", lambda: None),
        patch("gsuid_core.ai_core.rag.base.ensure_embedding_dimension", fake_ensure),
        patch("gsuid_core.ai_core.rag.knowledge.init_knowledge_collection", fake_init),
    ):
        asyncio.run(_run())
    assert held == [False]


def test_sync_prepares_collection_when_client_already_ready() -> None:
    held: list[bool] = []

    async def fake_init() -> None:
        held.append(knowledge_mod._knowledge_sync_lock.locked())

    async def fake_sync() -> None:
        return None

    async def _run() -> None:
        await sync_knowledge()

    with (
        patch("gsuid_core.ai_core.configs.ai_config.ai_config", _AiConfig()),
        patch("gsuid_core.ai_core.rag.base.client", object()),
        patch("gsuid_core.ai_core.rag.base.embedding_model", object()),
        patch("gsuid_core.ai_core.rag.knowledge.init_knowledge_collection", fake_init),
        patch("gsuid_core.ai_core.rag.knowledge._sync_knowledge_impl", fake_sync),
    ):
        asyncio.run(_run())
    assert held == [False]


def test_repeat_sync_scrolls_stamps_and_skips_bodies() -> None:
    entity: KnowledgeBase = {
        "id": "stamp-doc",
        "plugin": "StampProbe",
        "title": "印章",
        "content": "印章对账探针。不要拉正文。",
        "tags": ["probe"],
    }
    content_hash = calculate_hash(dict(entity))
    point_id = get_point_id(entity["id"])
    saved = list(_ENTITIES)
    _ENTITIES.clear()
    _ENTITIES.append(entity)
    knowledge_mod._piece_count_by_stamp.clear()
    store = _Store()
    store.points[point_id] = {
        "id": entity["id"],
        "doc_id": entity["id"],
        "source": "plugin",
        "_hash": chunk_method_hash(content_hash),
        "content": "正文" * 2000,
    }
    store.points["manual-point"] = {
        "id": "manual-doc",
        "doc_id": "manual-doc",
        "source": "manual",
        "_hash": "manual-hash",
        "content": "手动正文" * 2000,
    }
    cuts = {"n": 0}

    def _count_pieces(
        body: str,
        *,
        title: str = "",
        tags: Sequence[str] = (),
        budget: int | None = None,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
        max_tokens: int | None = None,
    ) -> list[EmbedPiece]:
        cuts["n"] += 1
        return pieces_for_embed(
            body,
            title=title,
            tags=tags,
            budget=budget,
            overlap=overlap,
            max_tokens=max_tokens,
        )

    embedder = _Embedder(asyncio.Event())
    embedder.gate.set()

    async def _run() -> None:
        await sync_knowledge()
        first_cuts = cuts["n"]
        await sync_knowledge()
        assert embedder.calls == 0
        assert store.upserts == 0
        assert store.scrolls == 2
        assert store.unfiltered == 0
        assert store.payload_fields
        assert all("content" not in fields for fields in store.payload_fields)
        assert first_cuts == 1
        assert cuts["n"] == 1
        assert point_id in store.points
        assert "manual-point" in store.points
        changed: KnowledgeBase = {
            "id": entity["id"],
            "plugin": entity["plugin"],
            "title": entity["title"],
            "content": "印章对账探针。正文已经改过。",
            "tags": ["probe"],
        }
        _ENTITIES.clear()
        _ENTITIES.append(changed)
        await sync_knowledge()
        assert embedder.calls == 1
        assert cuts["n"] == 2

    try:

        async def _ready() -> None:
            return None

        with (
            patch("gsuid_core.ai_core.configs.ai_config.ai_config", _AiConfig()),
            patch("gsuid_core.ai_core.rag.base.client", store),
            patch("gsuid_core.ai_core.rag.base.embedding_model", embedder),
            patch("gsuid_core.ai_core.rag.base.ensure_embedding_dimension", _ready),
            patch("gsuid_core.ai_core.rag.knowledge.init_knowledge_collection", _ready),
            patch("gsuid_core.ai_core.rag.knowledge._sparse_embed_batch_async", _sparse_none),
            patch("gsuid_core.ai_core.rag.knowledge.pieces_for_embed", _count_pieces),
        ):
            asyncio.run(_run())
    finally:
        knowledge_mod._piece_count_by_stamp.clear()
        _ENTITIES.clear()
        _ENTITIES.extend(saved)


def test_plugin_sync_keeps_old_points_when_embed_skips() -> None:
    entity: KnowledgeBase = {
        "id": "fail-doc",
        "plugin": "FailProbe",
        "title": "切成两片",
        "content": "会切成两片的正文。",
        "tags": ["probe"],
    }
    old_id = get_point_id(entity["id"])
    saved = list(_ENTITIES)
    _ENTITIES.clear()
    _ENTITIES.append(entity)
    knowledge_mod._piece_count_by_stamp.clear()
    store = _Store()
    store.points[old_id] = {
        "id": entity["id"],
        "doc_id": entity["id"],
        "source": "plugin",
        "_hash": "old-hash",
    }

    class _SkipEmbedder:
        async def aembed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("413 payload too large")

    def _two_pieces(
        body: str,
        *,
        title: str = "",
        tags: Sequence[str] = (),
        budget: int | None = None,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
        max_tokens: int | None = None,
    ) -> list[EmbedPiece]:
        return [EmbedPiece(body="a", embed_text="a"), EmbedPiece(body="b", embed_text="b")]

    async def _ready() -> None:
        return None

    async def _run() -> None:
        import gsuid_core.ai_core.rag.base as rag_base

        rag_base._cached_embed_bs = 0
        await sync_knowledge()
        rag_base._cached_embed_bs = 0
        assert old_id in store.points
        assert get_point_id("fail-doc#0") not in store.points
        assert get_point_id("fail-doc#1") not in store.points
        assert store.upserts == 0

    try:
        with (
            patch("gsuid_core.ai_core.configs.ai_config.ai_config", _AiConfig()),
            patch("gsuid_core.ai_core.rag.base.client", store),
            patch("gsuid_core.ai_core.rag.base.embedding_model", _SkipEmbedder()),
            patch("gsuid_core.ai_core.rag.base.ensure_embedding_dimension", _ready),
            patch("gsuid_core.ai_core.rag.knowledge.init_knowledge_collection", _ready),
            patch("gsuid_core.ai_core.rag.knowledge._sparse_embed_batch_async", _sparse_none),
            patch("gsuid_core.ai_core.rag.knowledge.pieces_for_embed", _two_pieces),
        ):
            asyncio.run(_run())
    finally:
        knowledge_mod._piece_count_by_stamp.clear()
        _ENTITIES.clear()
        _ENTITIES.extend(saved)
