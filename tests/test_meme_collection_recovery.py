"""表情包 Qdrant 启动：少数点缺失不得整库重建。"""

from gsuid_core.ai_core.meme.library import _needs_full_meme_reindex


def test_partial_index_gap_does_not_force_rebuild() -> None:
    assert not _needs_full_meme_reindex(point_count=566, eligible=576)
    assert not _needs_full_meme_reindex(point_count=576, eligible=576)


def test_empty_collection_with_records_does_rebuild() -> None:
    assert _needs_full_meme_reindex(point_count=0, eligible=576)
    assert not _needs_full_meme_reindex(point_count=0, eligible=0)
