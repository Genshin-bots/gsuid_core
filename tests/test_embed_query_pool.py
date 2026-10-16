"""在线检索的查询嵌入不能排在后台批量索引后面。"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from gsuid_core.ai_core.rag import base as rag_base, tools as rag_tools
from gsuid_core.ai_core.rag.qmodels import QueryResponse
from gsuid_core.ai_core.rag.embedding import base as embedding_base


class _StuckBulkProvider(embedding_base.EmbeddingProvider):
    """「bulk」批次卡到放行为止，模拟研究任务的切块索引占满批量池。"""

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self.bulk_started = threading.Event()

    @property
    def dimension(self) -> int:
        return 2

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        if texts == ["bulk"]:
            self.bulk_started.set()
            self._release.wait(timeout=10)
        return [[1.0, 0.0] for _ in texts]


class _EmptyClient:
    def __init__(self) -> None:
        self.queries: list[object] = []

    async def query_points(self, **kwargs: object) -> QueryResponse:
        self.queries.append(kwargs["query"])
        return QueryResponse(points=[])


def test_tool_search_does_not_queue_behind_bulk_indexing(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    provider = _StuckBulkProvider(release)
    client = _EmptyClient()
    batch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test_embed_batch")
    monkeypatch.setattr(embedding_base, "_EMBED_BATCH_EXECUTOR", batch_pool)
    monkeypatch.setattr(rag_base, "client", client)
    monkeypatch.setattr(rag_base, "embedding_model", rag_base._EmbeddingModelWrapper(provider))

    async def _run() -> None:
        bulk = asyncio.ensure_future(provider.embed(["bulk"]))
        try:
            assert await asyncio.to_thread(provider.bulk_started.wait, 5)
            found = await asyncio.wait_for(rag_tools.search_tools("查天气", rerank=False), timeout=3)
            assert found == []
            assert client.queries == [[1.0, 0.0]]
        finally:
            release.set()
            await bulk

    try:
        asyncio.run(_run())
    finally:
        batch_pool.shutdown(wait=True)
