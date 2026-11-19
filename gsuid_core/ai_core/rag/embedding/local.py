"""本地嵌入模型提供方（基于 fastembed）"""

import os
import json
from pathlib import Path
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.embedding.base import EmbeddingProvider
from gsuid_core.ai_core.rag.embedding.static_mmap import (
    STATIC_MAX_LENGTH,
    StaticMmapEmbedding,
    resolve_snapshot_dir,
    find_known_static_model,
)

# onnxruntime arena 只增不减，兜底取低值：threads=1，batch=16。
# 线程数和 batch 只读嵌入配置，避免 env 把内存地板抬高。
_FALLBACK_EMBED_THREADS = 1
_FALLBACK_EMBED_BATCH = 16
_FALLBACK_MAX_TOKENS = 512
_warned_legacy_env: set[str] = set()


@dataclass(frozen=True)
class FastEmbedModelSpec:
    hf_repo: str
    model_file: str
    is_static: bool
    static_max_tokens: int


def resolve_fastembed_model_spec(model_name: str) -> FastEmbedModelSpec:
    """优先匹配内置静态模型表（不触发 fastembed/onnxruntime 导入），否则回退 fastembed 注册表。"""
    known_static = find_known_static_model(model_name)
    if known_static is not None:
        return FastEmbedModelSpec(
            hf_repo=known_static.hf_repo,
            model_file=known_static.model_file,
            is_static=True,
            static_max_tokens=STATIC_MAX_LENGTH,
        )

    from gsuid_core.ai_core.rag.base import _get_embedding_hf_repo

    try:
        from fastembed import TextEmbedding
    except ImportError:
        return FastEmbedModelSpec(
            hf_repo=_get_embedding_hf_repo(model_name),
            model_file="model.onnx",
            is_static=False,
            static_max_tokens=_FALLBACK_MAX_TOKENS,
        )

    try:
        from fastembed.text.static_embedding import MAX_LENGTH, StaticTextEmbedding

        static_cls: type | None = StaticTextEmbedding
        static_cap = MAX_LENGTH
    except ImportError:
        static_cls = None
        static_cap = _FALLBACK_MAX_TOKENS

    target = model_name.strip().lower()
    for model_cls in TextEmbedding.EMBEDDINGS_REGISTRY:
        for desc in model_cls._list_supported_models():
            if desc.model.lower() == target:
                hf_repo = desc.sources.hf or model_name
                is_static = (static_cls is not None and model_cls is static_cls) or desc.model_file.lower().endswith(
                    ".safetensors"
                )
                return FastEmbedModelSpec(
                    hf_repo=hf_repo,
                    model_file=desc.model_file,
                    is_static=is_static,
                    static_max_tokens=static_cap,
                )

    return FastEmbedModelSpec(
        hf_repo=_get_embedding_hf_repo(model_name),
        model_file="model.onnx",
        is_static=False,
        static_max_tokens=_FALLBACK_MAX_TOKENS,
    )


def resolve_model_cache_dir(cache_root: Path, hf_repo: str) -> Path:
    """定位 HF 模型缓存目录；大小写敏感文件系统兼容历史小写目录名。"""
    from gsuid_core.ai_core.rag.base import _hf_cache_dirname

    expected = cache_root / _hf_cache_dirname(hf_repo)
    if expected.is_dir() or not cache_root.is_dir():
        return expected
    target_lower = expected.name.lower()
    for entry in cache_root.iterdir():
        if entry.is_dir() and entry.name.lower() == target_lower:
            return entry
    return expected


def _warn_legacy_embed_env(env_name: str, config_key: str) -> None:
    if env_name in _warned_legacy_env:
        return
    raw = os.getenv(env_name)
    if raw is None or raw.strip() == "":
        return
    _warned_legacy_env.add(env_name)
    logger.warning(t("log.rag.embedding_env_ignored", env_name=env_name, config_key=config_key))


def _configured_max_tokens(cache_dir: str, model_name: str, spec: FastEmbedModelSpec | None = None) -> int:
    """读模型 config.json 里的最大长度；静态模型受限于 fastembed 固定截断上限。"""
    resolved = spec if spec is not None else resolve_fastembed_model_spec(model_name)
    root = resolve_model_cache_dir(Path(cache_dir), resolved.hf_repo)
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
    configured = _FALLBACK_MAX_TOKENS
    if cfg_path is not None:
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, dict):
            for key in ("model_max_length", "max_position_embeddings"):
                if key not in raw:
                    continue
                value = raw[key]
                if isinstance(value, int) and 8 <= value <= 32768:
                    configured = value
                    break
    if resolved.is_static:
        return min(configured, resolved.static_max_tokens)
    return configured


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
    """本地嵌入模型提供方（静态模型直读 safetensors mmap，ONNX 模型走 fastembed）。"""

    def __init__(self, model_name: str, cache_dir: str, threads: int | None = None, *, mmap: bool | None = None):
        spec = resolve_fastembed_model_spec(model_name)
        self._model_name = model_name
        self._batch_size = _resolve_batch_size()
        self._is_static = spec.is_static
        self._static_model: StaticMmapEmbedding | None = None

        if spec.is_static:
            # 静态模型直读 safetensors mmap，不导入 fastembed/onnxruntime。
            use_mmap = True if mmap is None else mmap
            effective_threads = 0
            cache_root = resolve_model_cache_dir(Path(cache_dir), spec.hf_repo)
            snapshot_dir = resolve_snapshot_dir(cache_root, spec.model_file)
            static_model = StaticMmapEmbedding(snapshot_dir, spec.model_file, use_mmap=use_mmap)
            self._static_model = static_model
            self._model = static_model
        else:
            from fastembed import TextEmbedding

            effective_threads = _resolve_threads() if threads is None else threads
            self._model = TextEmbedding(
                model_name=model_name,
                cache_dir=cache_dir,
                threads=effective_threads,
                local_files_only=True,
            )

        # 通过一次空推断获取维度
        test_vec = list(self._model.embed(["test"]))[0]
        self._dim = len(test_vec)
        self._max_input_tokens = _configured_max_tokens(cache_dir, model_name, spec)
        logger.info(
            t(
                "log.rag.embedding_local_name_dimension",
                model_name=model_name,
                p0=self._dim,
                threads=effective_threads,
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

    def close(self) -> None:
        """释放静态模型 mmap 文件句柄（Windows 重置或清理缓存目录前需显式关闭）。"""
        if self._static_model is not None:
            self._static_model.close()
            self._static_model = None
