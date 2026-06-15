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

import base64
import asyncio
from abc import ABC, abstractmethod
from enum import Enum
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


class EmbeddingModality(str, Enum):
    """嵌入模型支持的模态类型

    用户在嵌入模型配置中声明所用模型支持哪些模态（持久化进配置），
    检索/入库管线据此决定能否对某类内容直接做向量嵌入。
    """

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"

    @classmethod
    def from_str(cls, value: str) -> "Union[EmbeddingModality, None]":
        normalized = value.strip().lower()
        for modality in cls:
            if modality.value == normalized:
                return modality
        return None


def parse_modalities(values: list[str]) -> set[EmbeddingModality]:
    """把配置里的字符串列表解析成模态集合。

    始终包含 TEXT（任何嵌入模型都至少支持文本）；无法识别的取值被忽略。
    """
    result: set[EmbeddingModality] = {EmbeddingModality.TEXT}
    for value in values:
        modality = EmbeddingModality.from_str(value)
        if modality is not None:
            result.add(modality)
    return result


class EmbeddingProvider(ABC):
    """嵌入模型提供方抽象基类

    文本嵌入是所有 provider 的必备能力（``embed_sync``）。图片/音频/视频等
    非文本模态为可选能力：provider 通过 ``supported_modalities`` 声明，并实现
    对应的 ``embed_image_sync`` / ``embed_audio_sync`` / ``embed_video_sync``。
    未实现的模态默认抛 ``NotImplementedError``，调用方应先用 ``supports`` 判定。
    """

    @property
    @abstractmethod
    def dimension(self) -> int:
        """返回嵌入向量的维度"""
        ...

    @property
    def supported_modalities(self) -> set["EmbeddingModality"]:
        """对外声明本 provider 支持的模态，默认仅文本。

        子类按"用户配置声明 ∩ 实现实际具备的能力"覆盖此属性。
        """
        return {EmbeddingModality.TEXT}

    def supports(self, modality: "EmbeddingModality") -> bool:
        """判断是否支持指定模态的直接嵌入"""
        return modality in self.supported_modalities

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

    # ────────────────── 多模态：图片 ──────────────────

    def embed_image_sync(self, images: list[bytes]) -> list[list[float]]:
        """同步批量图片嵌入；不支持图片的 provider 抛 NotImplementedError。"""
        raise NotImplementedError(f"{type(self).__name__} 不支持图片嵌入（embed_image_sync 未实现）")

    def embed_image_single_sync(self, image: bytes) -> list[float]:
        return self.embed_image_sync([image])[0]

    async def embed_image(self, images: list[bytes]) -> list[list[float]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EMBED_BATCH_EXECUTOR, self.embed_image_sync, images)

    async def embed_image_single(self, image: bytes) -> list[float]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EMBED_EXECUTOR, self.embed_image_single_sync, image)

    # ────────────────── 多模态：音频 ──────────────────

    def embed_audio_sync(self, clips: list[bytes]) -> list[list[float]]:
        """同步批量音频嵌入；不支持音频的 provider 抛 NotImplementedError。"""
        raise NotImplementedError(f"{type(self).__name__} 不支持音频嵌入（embed_audio_sync 未实现）")

    def embed_audio_single_sync(self, clip: bytes) -> list[float]:
        return self.embed_audio_sync([clip])[0]

    async def embed_audio(self, clips: list[bytes]) -> list[list[float]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EMBED_BATCH_EXECUTOR, self.embed_audio_sync, clips)

    async def embed_audio_single(self, clip: bytes) -> list[float]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EMBED_EXECUTOR, self.embed_audio_single_sync, clip)

    # ────────────────── 多模态：视频 ──────────────────

    def embed_video_sync(self, clips: list[bytes]) -> list[list[float]]:
        """同步批量视频嵌入；不支持视频的 provider 抛 NotImplementedError。"""
        raise NotImplementedError(f"{type(self).__name__} 不支持视频嵌入（embed_video_sync 未实现）")

    def embed_video_single_sync(self, clip: bytes) -> list[float]:
        return self.embed_video_sync([clip])[0]

    async def embed_video(self, clips: list[bytes]) -> list[list[float]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EMBED_BATCH_EXECUTOR, self.embed_video_sync, clips)

    async def embed_video_single(self, clip: bytes) -> list[float]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EMBED_EXECUTOR, self.embed_video_single_sync, clip)

    # ────────────────── 统一分发（多模态管线入口） ──────────────────

    async def embed_media(self, modality: "EmbeddingModality", items: list[bytes]) -> list[list[float]]:
        """按模态批量嵌入非文本二进制内容（文本请改用 embed）。

        调用方应先用 supports(modality) 判定能力；不支持的模态由底层方法抛
        NotImplementedError。
        """
        if modality is EmbeddingModality.IMAGE:
            return await self.embed_image(items)
        if modality is EmbeddingModality.AUDIO:
            return await self.embed_audio(items)
        if modality is EmbeddingModality.VIDEO:
            return await self.embed_video(items)
        raise ValueError(f"embed_media 不接受模态 {modality}（文本请调用 embed()）")

    async def embed_media_single(self, modality: "EmbeddingModality", item: bytes) -> list[float]:
        results = await self.embed_media(modality, [item])
        return results[0]


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
                "🧠 [Embedding] OpenAI 嵌入提供方声明了暂不支持的模态 "
                f"{[m.value for m in dropped]}，已忽略（当前仅支持 text/image）"
            )

        # 维度来源优先级：用户配置 > 已知映射 > 首次调用 API 时推断
        if dimension and dimension > 0:
            self._dim = dimension
        elif model_name in self.KNOWN_DIMENSIONS:
            self._dim = self.KNOWN_DIMENSIONS[model_name]

        logger.info(
            f"🧠 [Embedding] OpenAI 嵌入模型已配置: {model_name}, "
            f"URL: {base_url}, 维度: {self._dim or '(首次调用时推断)'}, "
            f"模态: {[m.value for m in self._modalities]}"
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


# ============== 全局单例 ==============
_provider: Union[EmbeddingProvider, None] = None


def _build_local_provider() -> LocalEmbeddingProvider:
    """构造内置本地（fastembed）provider"""
    from gsuid_core.data_store import AI_CORE_PATH
    from gsuid_core.ai_core.configs.ai_config import local_embedding_config

    model_name = local_embedding_config.get_config("embedding_model_name").data
    cache_dir = str(AI_CORE_PATH / "models_cache")

    # 内置本地 fastembed 仅支持文本；若用户声明了图片/音视频，提示其改用 STEmbedding/OpenAI 多模态
    declared = parse_modalities(local_embedding_config.get_config("embedding_modalities").data)
    extra = declared - {EmbeddingModality.TEXT}
    if extra:
        logger.warning(
            "🧠 [Embedding] 内置本地嵌入(fastembed)仅支持文本，已忽略声明的额外模态 "
            f"{[m.value for m in extra]}；图片请用 STEmbedding 插件(CLIP) 或 OpenAI 多模态接口"
        )

    return LocalEmbeddingProvider(
        model_name=model_name,
        cache_dir=cache_dir,
    )


def get_embedding_provider() -> EmbeddingProvider:
    """获取当前嵌入模型提供方（全局单例）

    根据 ai_config 中的 embedding_provider 配置项决定使用哪个实现：
    - "local": 使用本地 fastembed 模型
    - "openai": 使用 OpenAI 兼容格式的远程 API
    - 其他: 查询插件注册表（embedding_registry），由插件工厂构造

    插件 provider 不可用时（插件被卸载/构造失败）降级回 local 并记录错误，
    避免 RAG 初始化失败导致 AI 核心整体不可用。

    Returns:
        EmbeddingProvider 实例

    Raises:
        RuntimeError: AI 功能未启用或配置错误时抛出
    """
    global _provider

    if _provider is not None:
        return _provider

    from gsuid_core.ai_core.configs.ai_config import (
        ai_config,
        openai_embedding_config,
    )

    if not ai_config.get_config("enable").data:
        raise RuntimeError("AI 功能未启用，无法获取嵌入模型提供方")

    provider_name = ai_config.get_config("embedding_provider").data

    if provider_name == "local":
        _provider = _build_local_provider()
    elif provider_name == "openai":
        base_url = openai_embedding_config.get_config("base_url").data
        api_key_list = openai_embedding_config.get_config("api_key").data
        if not api_key_list:
            raise ValueError("OpenAI 嵌入模型 API 密钥不能为空，请在配置中至少设置一个 api_key")
        api_key = api_key_list[0]
        model_name = openai_embedding_config.get_config("embedding_model").data
        dimension = openai_embedding_config.get_config("dimension").data
        modalities = parse_modalities(openai_embedding_config.get_config("embedding_modalities").data)
        _provider = OpenAIEmbeddingProvider(
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            dimension=dimension,
            modalities=modalities,
        )
    else:
        from gsuid_core.ai_core.rag.embedding_registry import (
            get_external_provider,
            list_embedding_providers,
        )

        entry = get_external_provider(provider_name)
        if entry is None:
            # 配置指向的插件 provider 未注册（插件被卸载/加载失败）：
            # 降级回 local，向量空间变化由维度迁移机制兜底，比 AI 核心整体瘫痪好
            logger.error(
                f"🧠 [Embedding] 嵌入提供方 '{provider_name}' 未注册"
                f"（来源插件可能已卸载或加载失败），降级使用 local。"
                f"可用 provider: {list_embedding_providers()}"
            )
            _provider = _build_local_provider()
        else:
            try:
                _provider = entry.factory()
                logger.info(f"🧠 [Embedding] 插件嵌入提供方已加载: {provider_name} (plugin={entry.plugin or '未知'})")
            except Exception as e:
                logger.error(
                    f"🧠 [Embedding] 插件嵌入提供方 '{provider_name}' 构造失败"
                    f"（plugin={entry.plugin or '未知'}）: {e}，降级使用 local"
                )
                _provider = _build_local_provider()

    return _provider


def reset_embedding_provider() -> None:
    """重置全局嵌入提供方单例（用于配置热重载）"""
    global _provider
    _provider = None
