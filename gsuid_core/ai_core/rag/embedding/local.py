"""本地嵌入模型提供方（基于 fastembed）"""

import os

from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.embedding.base import EmbeddingProvider

# fastembed ONNX intra-op 线程。默认 min(cpu,8) 会吃满全部核（小核机 CPU 常驻 100% 抢事件
# 循环）；改 cpu//2 留一半余量，吞吐仅微降（bge-small 8→2 仅 1.37x），GSUID_EMBED_THREADS 可覆盖。
_DEFAULT_EMBED_THREADS = max(1, (os.cpu_count() or 2) // 2)

# fastembed 单次推断 batch_size。默认 256 使大批量摄入驻留内存冲到 ~500MB 且并发按 N 倍放大；
# 降到 64（实测峰值 ~300MB）是 2C2G 主要省内存点，GSUID_EMBED_BATCH 可覆盖换吞吐。
_DEFAULT_EMBED_BATCH = 64


def _resolve_threads() -> int:
    env = os.getenv("GSUID_EMBED_THREADS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return _DEFAULT_EMBED_THREADS


def _resolve_batch_size() -> int:
    env = os.getenv("GSUID_EMBED_BATCH")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return _DEFAULT_EMBED_BATCH


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
        logger.info(
            f"🧠 [Embedding] 本地嵌入模型已加载: {model_name}, 维度: {self._dim}, "
            f"threads={threads}, batch_size={self._batch_size}"
        )

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        # 显式限制 batch_size 控制驻留内存峰值（2C2G 关键）；fastembed 内部按此分批。
        return [v.tolist() for v in self._model.embed(texts, batch_size=self._batch_size)]

    def embed_single_sync(self, text: str) -> list[float]:
        return list(self._model.embed([text]))[0].tolist()
