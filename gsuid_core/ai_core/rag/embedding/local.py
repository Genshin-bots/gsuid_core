"""本地嵌入模型提供方（基于 fastembed）"""

import os
import json
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.embedding.base import EmbeddingProvider

# onnxruntime arena 只增不减，兜底取低值：threads=1，batch=16。
# 线程数和 batch 只读嵌入配置，避免 env 把内存地板抬高。
_FALLBACK_EMBED_THREADS = 1
_FALLBACK_EMBED_BATCH = 16
_FALLBACK_MAX_TOKENS = 512
_warned_legacy_env: set[str] = set()


def _warn_legacy_embed_env(env_name: str, config_key: str) -> None:
    if env_name in _warned_legacy_env:
        return
    raw = os.getenv(env_name)
    if raw is None or raw.strip() == "":
        return
    _warned_legacy_env.add(env_name)
    logger.warning(t("log.rag.embedding_env_ignored", env_name=env_name, config_key=config_key))


def _configured_max_tokens(cache_dir: str, model_name: str) -> int:
    """读模型 config.json 里的最大长度。tokenizer 的 512 经常是没改过的默认值。"""
    from gsuid_core.ai_core.rag.base import _hf_cache_dirname, _get_embedding_hf_repo

    root = Path(cache_dir) / _hf_cache_dirname(_get_embedding_hf_repo(model_name))
    cfg_path: Path | None = None
    ref = root / "refs" / "main"
    if ref.is_file():
        named = root / "snapshots" / ref.read_text(encoding="utf-8").strip() / "config.json"
        if named.is_file():
            cfg_path = named
    if cfg_path is None:
        snapshots = root / "snapshots"
        if snapshots.is_dir():
            for snap in snapshots.iterdir():
                candidate = snap / "config.json"
                if candidate.is_file():
                    cfg_path = candidate
                    break
    if cfg_path is None:
        return _FALLBACK_MAX_TOKENS
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _FALLBACK_MAX_TOKENS
    if not isinstance(raw, dict):
        return _FALLBACK_MAX_TOKENS
    for key in ("model_max_length", "max_position_embeddings"):
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, int) and 8 <= value <= 32768:
            return value
    return _FALLBACK_MAX_TOKENS


def _config_int(key: str) -> "int | None":
    """读取 local_embedding_config 的整数配置项；异常或非正数一律返回 None（回退兜底常量）。

    延迟 import 避免与配置模块的循环依赖；try/except 保证配置文件缺键/损坏时不炸初始化。
    """
    try:
        from gsuid_core.ai_core.configs.ai_config import local_embedding_config

        val = int(local_embedding_config.get_config(key).data)
        return val if val > 0 else None
    except Exception:
        return None


def _resolve_threads() -> int:
    _warn_legacy_embed_env("GSUID_EMBED_THREADS", "embed_threads")
    return _config_int("embed_threads") or _FALLBACK_EMBED_THREADS


def _resolve_batch_size() -> int:
    _warn_legacy_embed_env("GSUID_EMBED_BATCH", "embed_batch_size")
    return _config_int("embed_batch_size") or _FALLBACK_EMBED_BATCH


class LocalEmbeddingProvider(EmbeddingProvider):
    """本地嵌入模型提供方（基于 fastembed）"""

    def __init__(self, model_name: str, cache_dir: str, threads: int | None = None):
        from fastembed import TextEmbedding

        if threads is None:
            threads = _resolve_threads()

        self._model_name = model_name
        self._batch_size = _resolve_batch_size()
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=cache_dir,
            threads=threads,
            local_files_only=True,
        )
        # 通过一次空推断获取维度
        test_vec = list(self._model.embed(["test"]))[0]
        self._dim = len(test_vec)
        self._max_input_tokens = _configured_max_tokens(cache_dir, model_name)
        logger.info(
            t(
                "log.rag.embedding_local_name_dimension",
                model_name=model_name,
                p0=self._dim,
                threads=threads,
                p1=self._batch_size,
            )
        )

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def max_input_tokens(self) -> int:
        return self._max_input_tokens

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        # 显式限制 batch_size 控制驻留内存峰值（2C2G 关键）；fastembed 内部按此分批。
        return [[float(x) for x in v] for v in self._model.embed(texts, batch_size=self._batch_size)]

    def embed_single_sync(self, text: str) -> list[float]:
        return [float(x) for x in next(iter(self._model.embed([text])))]
