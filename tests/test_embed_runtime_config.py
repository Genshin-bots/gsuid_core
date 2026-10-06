"""embed_threads 和 embed_batch_size 只读配置。环境变量不再覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest
import fastembed

from gsuid_core.ai_core.rag.embedding import local as local_mod
from gsuid_core.ai_core.configs.ai_config import LOCAL_EMBEDDING_CONFIG
from gsuid_core.ai_core.rag.embedding.local import LocalEmbeddingProvider


class _Vectors:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed(self, texts: list[str], batch_size: int | None = None) -> list[list[float]]:
        if texts == ["test"] and batch_size is None:
            return [[0.0, 0.0]]
        if batch_size is None:
            raise AssertionError(texts)
        self.batch_sizes.append(batch_size)
        return [[0.0, 0.0]]


class _Value:
    def __init__(self, data: int) -> None:
        self.data = data


class _Config:
    def __init__(self, values: dict[str, int]) -> None:
        self._values = values

    def get_config(self, name: str) -> _Value:
        if name not in self._values:
            raise KeyError(name)
        return _Value(self._values[name])


class _BrokenConfig:
    def get_config(self, name: str) -> _Value:
        raise RuntimeError(name)


def test_config_defaults_are_low_fixed_values() -> None:
    assert LOCAL_EMBEDDING_CONFIG["embed_threads"].data == 1
    assert LOCAL_EMBEDDING_CONFIG["embed_batch_size"].data == 16


def test_threads_and_batch_follow_config_not_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[dict[str, object]] = []
    models: list[_Vectors] = []

    def _factory(**kwargs: object) -> _Vectors:
        captured.append(kwargs)
        model = _Vectors()
        models.append(model)
        return model

    monkeypatch.setattr(fastembed, "TextEmbedding", _factory)
    monkeypatch.setenv("GSUID_EMBED_THREADS", "8")
    monkeypatch.setenv("GSUID_EMBED_BATCH", "256")
    local_mod._warned_legacy_env.clear()
    warned: list[str] = []
    monkeypatch.setattr(local_mod.logger, "warning", lambda msg, *a, **k: warned.append(str(msg)))
    monkeypatch.setattr(
        "gsuid_core.ai_core.configs.ai_config.local_embedding_config",
        _Config({"embed_threads": 4, "embed_batch_size": 32}),
    )

    provider = LocalEmbeddingProvider("dummy-model", cache_dir=str(tmp_path))
    provider.embed_sync(["hello"])

    assert captured[0]["threads"] == 4
    assert models[0].batch_sizes == [32]
    joined = " ".join(warned)
    assert "GSUID_EMBED_THREADS" in joined
    assert "GSUID_EMBED_BATCH" in joined
    assert "embed_threads" in joined
    assert "embed_batch_size" in joined

    monkeypatch.setattr(
        "gsuid_core.ai_core.configs.ai_config.local_embedding_config",
        _BrokenConfig(),
    )
    fallback = LocalEmbeddingProvider("dummy-model", cache_dir=str(tmp_path))
    fallback.embed_sync(["hello"])
    assert captured[1]["threads"] == 1
    assert models[1].batch_sizes == [16]
