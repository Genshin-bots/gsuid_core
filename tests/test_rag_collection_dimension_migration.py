"""维度不符的集合必须在启动时重建，不能只 warning。

回归背景：``init_tools_collection`` / ``ensure_artifact_collection`` 曾在
「集合已存在但维度是旧模型」时只打一条 warning 就返回，集合永远留在旧维度，
运行时每次检索都撞 ``expected dim: 512, got 768`` 并被
``is_vector_structure_error`` 降级为空——工具/产物检索整条链路瘫掉。
"""

from __future__ import annotations

import asyncio
from typing import TypeVar
from unittest.mock import AsyncMock, patch
from collections.abc import Coroutine

from gsuid_core.ai_core.rag.base import TOOLS_COLLECTION_NAME
from gsuid_core.ai_core.rag.tools import init_tools_collection
from gsuid_core.ai_core.planning.artifact_index import (
    ARTIFACT_COLLECTION,
    ensure_artifact_collection,
)
from gsuid_core.ai_core.planning.tool_output_index import (
    TOOL_OUTPUT_COLLECTION,
    ensure_tool_output_collection,
)

_DIM = 768
_T = TypeVar("_T")


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCollections:
    def __init__(self, collections: list[_FakeCollection]) -> None:
        self.collections = collections


class _FakeClient:
    """只实现 init 路径用到的方法。"""

    def __init__(self, existing: set[str]) -> None:
        self._existing = existing
        self.created: list[str] = []

    async def get_collections(self) -> _FakeCollections:
        return _FakeCollections([_FakeCollection(n) for n in self._existing])

    async def collection_exists(self, name: str) -> bool:
        return name in self._existing

    async def create_collection(self, **kwargs: object) -> None:
        name = str(kwargs["collection_name"]) if "collection_name" in kwargs else ""
        self.created.append(name)
        self._existing.add(name)

    async def delete_collection(self, **kwargs: object) -> None:
        if "collection_name" in kwargs:
            self._existing.discard(str(kwargs["collection_name"]))


def _run(coro: Coroutine[object, object, _T]) -> _T:
    return asyncio.run(coro)


def test_tools_collection_rebuilds_on_dimension_mismatch() -> None:
    client = _FakeClient({TOOLS_COLLECTION_NAME})
    recreate = AsyncMock()

    with (
        patch("gsuid_core.ai_core.rag.base.client", new=client),
        patch("gsuid_core.ai_core.rag.base.get_strict_dimension", return_value=_DIM),
        patch("gsuid_core.ai_core.rag.tools.get_strict_dimension", return_value=_DIM),
        patch("gsuid_core.ai_core.rag.tools.collection_vector_mismatched", new=AsyncMock(return_value=True)),
        patch("gsuid_core.ai_core.rag.tools.force_recreate_collection", new=recreate),
        patch("gsuid_core.ai_core.rag.tools.ensure_vector_on_disk", new=AsyncMock()),
    ):
        _run(init_tools_collection())

    assert recreate.await_count == 1, "维度不符必须重建，不能只 warning"


def test_tools_collection_kept_when_dimension_matches() -> None:
    client = _FakeClient({TOOLS_COLLECTION_NAME})
    recreate = AsyncMock()
    on_disk = AsyncMock()

    with (
        patch("gsuid_core.ai_core.rag.base.client", new=client),
        patch("gsuid_core.ai_core.rag.base.get_strict_dimension", return_value=_DIM),
        patch("gsuid_core.ai_core.rag.tools.get_strict_dimension", return_value=_DIM),
        patch("gsuid_core.ai_core.rag.tools.collection_vector_mismatched", new=AsyncMock(return_value=False)),
        patch("gsuid_core.ai_core.rag.tools.force_recreate_collection", new=recreate),
        patch("gsuid_core.ai_core.rag.tools.ensure_vector_on_disk", new=on_disk),
    ):
        _run(init_tools_collection())

    assert recreate.await_count == 0
    assert on_disk.await_count == 1


def test_artifact_collection_rebuilds_on_dimension_mismatch() -> None:
    client = _FakeClient({ARTIFACT_COLLECTION})
    recreate = AsyncMock()

    with (
        patch("gsuid_core.ai_core.rag.base.client", new=client),
        patch("gsuid_core.ai_core.rag.base.get_strict_dimension", return_value=_DIM),
        patch("gsuid_core.ai_core.rag.base.get_dimension", return_value=_DIM),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.collection_vector_mismatched",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.force_recreate_collection",
            new=recreate,
        ),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.ensure_payload_indexes",
            new=AsyncMock(),
        ),
    ):
        _run(ensure_artifact_collection())

    assert recreate.await_count == 1, "产物集合维度不符也必须重建"


def test_artifact_collection_created_when_absent() -> None:
    client = _FakeClient(set())
    recreate = AsyncMock()

    with (
        patch("gsuid_core.ai_core.rag.base.client", new=client),
        patch("gsuid_core.ai_core.rag.base.get_strict_dimension", return_value=_DIM),
        patch("gsuid_core.ai_core.rag.base.get_dimension", return_value=_DIM),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.collection_vector_mismatched",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.force_recreate_collection",
            new=recreate,
        ),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.ensure_payload_indexes",
            new=AsyncMock(),
        ),
    ):
        _run(ensure_artifact_collection())

    assert ARTIFACT_COLLECTION in client.created
    assert recreate.await_count == 0


def test_tool_output_collection_rebuilds_on_dimension_mismatch() -> None:
    client = _FakeClient({TOOL_OUTPUT_COLLECTION})
    recreate = AsyncMock()

    with (
        patch("gsuid_core.ai_core.rag.base.client", new=client),
        patch("gsuid_core.ai_core.rag.base.get_strict_dimension", return_value=_DIM),
        patch("gsuid_core.ai_core.rag.base.get_dimension", return_value=_DIM),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.collection_vector_mismatched",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.force_recreate_collection",
            new=recreate,
        ),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.ensure_payload_indexes",
            new=AsyncMock(),
        ),
    ):
        _run(ensure_tool_output_collection())

    assert recreate.await_count == 1, "工具产物集合维度不符也必须重建"


def test_tool_output_collection_kept_when_dimension_matches() -> None:
    client = _FakeClient({TOOL_OUTPUT_COLLECTION})
    recreate = AsyncMock()

    with (
        patch("gsuid_core.ai_core.rag.base.client", new=client),
        patch("gsuid_core.ai_core.rag.base.get_strict_dimension", return_value=_DIM),
        patch("gsuid_core.ai_core.rag.base.get_dimension", return_value=_DIM),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.collection_vector_mismatched",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.force_recreate_collection",
            new=recreate,
        ),
        patch(
            "gsuid_core.ai_core.rag.collection_migration.ensure_payload_indexes",
            new=AsyncMock(),
        ),
    ):
        _run(ensure_tool_output_collection())

    assert recreate.await_count == 0
