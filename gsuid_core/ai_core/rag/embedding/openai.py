"""OpenAI 兼容格式的远程嵌入模型提供方"""

import base64
from typing import Final, Union

import httpx

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.embedding.base import EmbeddingProvider
from gsuid_core.ai_core.rag.embedding.modality import EmbeddingModality


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI 兼容格式的远程嵌入模型提供方"""

    # 已知模型维度映射（用于无法从 API 响应推断时的回退）
    KNOWN_DIMENSIONS: Final[dict[str, int]] = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    # 本实现实际具备的模态能力：文本走标准 /embeddings；图片走多模态 input（Jina 风格）。
    # 音频/视频暂无通用的 OpenAI 兼容嵌入协议，故不在此列（声明了也会在调用时报未实现）。
    _IMPLEMENTED_MODALITIES: Final[set["EmbeddingModality"]] = {EmbeddingModality.TEXT, EmbeddingModality.IMAGE}

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_name: str,
        dimension: int = 0,
        modalities: "Union[set[EmbeddingModality], None]" = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self._dim: int = 0
        # 用户声明的模态 ∩ 实现实际具备的能力（声明但未实现的模态会被剔除并告警）
        declared = modalities if modalities is not None else {EmbeddingModality.TEXT}
        self._modalities = declared & self._IMPLEMENTED_MODALITIES
        dropped = declared - self._IMPLEMENTED_MODALITIES
        if dropped:
            logger.warning(
                t(
                    "🧠 [Embedding] OpenAI 嵌入提供方声明了暂不支持的模态 {p0}，已忽略（当前仅支持 text/image）",
                    p0=[m.value for m in dropped],
                )
            )

        # 维度来源优先级：用户配置 > 已知映射 > 首次调用 API 时推断
        if dimension and dimension > 0:
            self._dim = dimension
        elif model_name in self.KNOWN_DIMENSIONS:
            self._dim = self.KNOWN_DIMENSIONS[model_name]

        logger.info(
            t(
                "🧠 [Embedding] OpenAI 嵌入模型已配置: {model_name}, URL: {base_url}, 维度: {p0}, 模态: {p1}",
                model_name=model_name,
                base_url=base_url,
                p0=self._dim or "(首次调用时推断)",
                p1=[m.value for m in self._modalities],
            )
        )

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def supported_modalities(self) -> set["EmbeddingModality"]:
        return set(self._modalities)

    def _validate_and_update_dimension(self, vectors: list[list[float]]) -> None:
        """校验 API 实际返回维度与配置维度一致。"""
        if not vectors:
            return

        actual_dim = len(vectors[0])
        for index, vector in enumerate(vectors):
            if len(vector) != actual_dim:
                raise ValueError(
                    t(
                        "OpenAI 嵌入模型返回的第 {index} 个向量维度不一致:"
                        " actual={p0}, expected_batch_dim={actual_dim}",
                        index=index,
                        p0=len(vector),
                        actual_dim=actual_dim,
                    )
                )

        if self._dim == 0:
            self._dim = actual_dim
            logger.info(t("🧠 [Embedding] 从 API 响应推断嵌入维度: {p0}", p0=self._dim))
            return

        if actual_dim != self._dim:
            raise ValueError(
                t(
                    "OpenAI 嵌入模型实际返回维度与配置不一致:"
                    " actual={actual_dim}, configured={p0}。请修正 openai_embedding_config.json"
                    " 中的 dimension，或设为 0 自动推断。",
                    actual_dim=actual_dim,
                    p0=self._dim,
                )
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

    def _call_image_api(self, images: list[bytes]) -> list[list[float]]:
        """同步调用多模态 Embeddings API 嵌入图片（Jina 风格 input）。

        input 采用 ``[{"image": "<base64>"}]`` 形式——这是目前最通用的 OpenAI 兼容
        多模态嵌入入参格式（如 Jina CLIP v2）。图文共享同一向量空间，故可文搜图。
        """
        url = f"{self._base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model_name,
            "input": [{"image": base64.b64encode(img).decode("utf-8")} for img in images],
        }

        with httpx.Client(timeout=120.0) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        vectors = [item["embedding"] for item in sorted_data]
        self._validate_and_update_dimension(vectors)
        return vectors

    def embed_image_sync(self, images: list[bytes]) -> list[list[float]]:
        return self._call_image_api(images)

    async def embed_image(self, images: list[bytes]) -> list[list[float]]:
        """异步批量图片嵌入（直接使用 httpx 异步客户端，无需线程池）"""
        url = f"{self._base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model_name,
            "input": [{"image": base64.b64encode(img).decode("utf-8")} for img in images],
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        vectors = [item["embedding"] for item in sorted_data]
        self._validate_and_update_dimension(vectors)
        return vectors
