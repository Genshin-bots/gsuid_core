"""嵌入模型提供方抽象层

提供统一的嵌入接口，支持本地模型（fastembed）和 OpenAI 兼容格式的远程 API。
通过 ai_config 中的 embedding_provider 配置项切换底层实现。

使用方式:
    from gsuid_core.ai_core.rag.embedding import get_embedding_provider

    provider = get_embedding_provider()
    vectors = provider.embed(["hello", "world"])       # 批量
    vector = provider.embed_single("hello")             # 单条
    dim = provider.dimension                            # 向量维度
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Final, Union
from concurrent.futures import ThreadPoolExecutor

import httpx

from gsuid_core.logger import logger

# 有界线程池：用于将同步 CPU 计算（fastembed）移入线程池，避免阻塞事件循环
_EMBED_EXECUTOR: Final = ThreadPoolExecutor(max_workers=4, thread_name_prefix="embed")

# 批量 Embedding 专用单线程执行器：
# FastEmbed 底层使用 ONNX Runtime，自带高度优化的多线程池（Rayon），
# 会自动打满所有 CPU 核心。如果用多线程 Python 线程池包装批量调用，
# 会导致线程过度订阅（Thread Oversubscription），反而比单线程更慢。
# 因此批量调用使用 max_workers=1，确保 ONNX 独占 CPU 资源。
_EMBED_BATCH_EXECUTOR: Final = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed_batch")


class EmbeddingProvider(ABC):
    """嵌入模型提供方抽象基类"""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """返回嵌入向量的维度"""
        ...

    @abstractmethod
    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        """同步批量嵌入（核心方法，子类必须实现）

        Args:
            texts: 待嵌入的文本列表

        Returns:
            嵌入向量列表，与 texts 一一对应
        """
        ...

    def embed_single_sync(self, text: str) -> list[float]:
        """同步单条嵌入（默认调用 embed_sync）"""
        return self.embed_sync([text])[0]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """异步批量嵌入（将同步计算移入线程池）

        Args:
            texts: 待嵌入的文本列表

        Returns:
            嵌入向量列表，与 texts 一一对应
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EMBED_BATCH_EXECUTOR, self.embed_sync, texts)

    async def embed_single(self, text: str) -> list[float]:
        """异步单条嵌入（将同步计算移入线程池）

        Args:
            text: 待嵌入的文本

        Returns:
            嵌入向量
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EMBED_EXECUTOR, self.embed_single_sync, text)


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


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI 兼容格式的远程嵌入模型提供方"""

    # 已知模型维度映射（用于无法从 API 响应推断时的回退）
    KNOWN_DIMENSIONS: Final[dict[str, int]] = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(self, base_url: str, api_key: str, model_name: str, dimension: int = 0):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self._dim: int = 0

        # 维度来源优先级：用户配置 > 已知映射 > 首次调用 API 时推断
        if dimension and dimension > 0:
            self._dim = dimension
        elif model_name in self.KNOWN_DIMENSIONS:
            self._dim = self.KNOWN_DIMENSIONS[model_name]

        logger.info(
            f"🧠 [Embedding] OpenAI 嵌入模型已配置: {model_name}, "
            f"URL: {base_url}, 维度: {self._dim or '(首次调用时推断)'}"
        )

    @property
    def dimension(self) -> int:
        return self._dim

    def _validate_and_update_dimension(self, vectors: list[list[float]]) -> None:
        """校验 API 实际返回维度与配置维度一致。"""
        if not vectors:
            return

        actual_dim = len(vectors[0])
        for index, vector in enumerate(vectors):
            if len(vector) != actual_dim:
                raise ValueError(
                    f"OpenAI 嵌入模型返回的第 {index} 个向量维度不一致: "
                    f"actual={len(vector)}, expected_batch_dim={actual_dim}"
                )

        if self._dim == 0:
            self._dim = actual_dim
            logger.info(f"🧠 [Embedding] 从 API 响应推断嵌入维度: {self._dim}")
            return

        if actual_dim != self._dim:
            raise ValueError(
                "OpenAI 嵌入模型实际返回维度与配置不一致: "
                f"actual={actual_dim}, configured={self._dim}。"
                "请修正 openai_embedding_config.json 中的 dimension，或设为 0 自动推断。"
            )

    def _call_api(self, texts: list[str]) -> list[list[float]]:
        """同步调用 OpenAI Embeddings API"""
        url = f"{self._base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model_name,
            "input": texts,
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        # 按 index 排序，确保与输入顺序一致
        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        vectors = [item["embedding"] for item in sorted_data]

        self._validate_and_update_dimension(vectors)
        return vectors

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        return self._call_api(texts)

    def embed_single_sync(self, text: str) -> list[float]:
        return self._call_api([text])[0]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """异步批量嵌入（直接使用 httpx 异步客户端，无需线程池）"""
        url = f"{self._base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model_name,
            "input": texts,
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        vectors = [item["embedding"] for item in sorted_data]

        self._validate_and_update_dimension(vectors)
        return vectors

    async def embed_single(self, text: str) -> list[float]:
        """异步单条嵌入"""
        results = await self.embed([text])
        return results[0]


# ============== 全局单例 ==============
_provider: Union[EmbeddingProvider, None] = None


def get_embedding_provider() -> EmbeddingProvider:
    """获取当前嵌入模型提供方（全局单例）

    根据 ai_config 中的 embedding_provider 配置项决定使用哪个实现：
    - "local": 使用本地 fastembed 模型
    - "openai": 使用 OpenAI 兼容格式的远程 API

    Returns:
        EmbeddingProvider 实例

    Raises:
        RuntimeError: AI 功能未启用或配置错误时抛出
    """
    global _provider

    if _provider is not None:
        return _provider

    from gsuid_core.data_store import AI_CORE_PATH
    from gsuid_core.ai_core.configs.ai_config import (
        ai_config,
        local_embedding_config,
        openai_embedding_config,
    )

    if not ai_config.get_config("enable").data:
        raise RuntimeError("AI 功能未启用，无法获取嵌入模型提供方")

    provider_name = ai_config.get_config("embedding_provider").data

    if provider_name == "local":
        model_name = local_embedding_config.get_config("embedding_model_name").data
        cache_dir = str(AI_CORE_PATH / "models_cache")
        _provider = LocalEmbeddingProvider(
            model_name=model_name,
            cache_dir=cache_dir,
        )
    elif provider_name == "openai":
        base_url = openai_embedding_config.get_config("base_url").data
        api_key_list = openai_embedding_config.get_config("api_key").data
        if not api_key_list:
            raise ValueError("OpenAI 嵌入模型 API 密钥不能为空，请在配置中至少设置一个 api_key")
        api_key = api_key_list[0]
        model_name = openai_embedding_config.get_config("embedding_model").data
        dimension = openai_embedding_config.get_config("dimension").data
        _provider = OpenAIEmbeddingProvider(
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            dimension=dimension,
        )
    else:
        raise ValueError(f"🧠 [Embedding] 不支持的嵌入模型提供方: '{provider_name}'，仅支持 'local' 或 'openai'")

    return _provider


def reset_embedding_provider() -> None:
    """重置全局嵌入提供方单例（用于配置热重载）"""
    global _provider
    _provider = None
