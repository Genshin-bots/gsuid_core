"""表情包 Qdrant 启动：少数点缺失不得整库重建；集合不存在时要按 SQL 灌入。"""

import asyncio
from unittest.mock import patch

from gsuid_core.ai_core.meme.library import _ensure_meme_collection, _needs_full_meme_reindex
from gsuid_core.ai_core.meme.database_model import AiMemeRecord


def test_partial_index_gap_does_not_force_rebuild() -> None:
    assert not _needs_full_meme_reindex(point_count=566, eligible=576)
    assert not _needs_full_meme_reindex(point_count=576, eligible=576)


def test_empty_collection_with_records_does_rebuild() -> None:
    assert _needs_full_meme_reindex(point_count=0, eligible=576)
    assert not _needs_full_meme_reindex(point_count=0, eligible=0)


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name


class _CollectionList:
    def __init__(self) -> None:
        self.collections: list[_Named] = []


class _Client:
    def __init__(self) -> None:
        self.indexes: list[str] = []

    async def get_collections(self) -> _CollectionList:
        return _CollectionList()

    async def create_payload_index(self, collection_name: str, field_name: str, field_schema: object) -> None:
        if collection_name != "ai_meme":
            raise AssertionError(collection_name)
        self.indexes.append(field_name)


def test_missing_collection_embeds_sql_records() -> None:
    record = AiMemeRecord(
        meme_id="m1",
        file_path="a.png",
        description="一只猫",
        emotion_tags=["猫"],
        status="tagged",
    )
    client = _Client()
    synced: list[str] = []
    created = 0

    async def _eligible() -> list[AiMemeRecord]:
        return [record]

    async def _sync(row: AiMemeRecord) -> None:
        synced.append(row.meme_id)

    async def _recreate(**_kwargs: object) -> None:
        nonlocal created
        created += 1

    async def _run() -> None:
        with (
            patch("gsuid_core.ai_core.rag.base.client", client),
            patch("gsuid_core.ai_core.rag.base.get_strict_dimension", lambda: 512),
            patch("gsuid_core.ai_core.rag.collection_migration.force_recreate_collection", _recreate),
            patch("gsuid_core.ai_core.meme.library._eligible_meme_records", _eligible),
            patch("gsuid_core.ai_core.meme.library.MemeLibrary.sync_to_qdrant", _sync),
        ):
            await _ensure_meme_collection()

    asyncio.run(_run())
    assert created == 1
    assert synced == ["m1"]
    assert client.indexes == ["folder", "status", "meme_id"]
