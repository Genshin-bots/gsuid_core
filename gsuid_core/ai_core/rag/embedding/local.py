"""本地嵌入模型提供方（基于 fastembed）"""

from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.embedding.base import EmbeddingProvider


class LocalEmbeddingProvider(EmbeddingProvider):
    """本地嵌入模型提供方（基于 fastembed）"""

    def __init__(self, model_name: str, cache_dir: str, threads: int = 2):
        from fastembed import TextEmbedding

        self._model_name = model_name
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=cache_dir,
            threads=threads,
            local_files_only=True,
        )
        # 通过一次空推断获取维度
        test_vec = list(self._model.embed(["test"]))[0]
        self._dim = len(test_vec)
        logger.info(f"🧠 [Embedding] 本地嵌入模型已加载: {model_name}, 维度: {self._dim}")

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(texts)]

    def embed_single_sync(self, text: str) -> list[float]:
        return list(self._model.embed([text]))[0].tolist()
