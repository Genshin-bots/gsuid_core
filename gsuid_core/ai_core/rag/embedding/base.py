"""嵌入模型提供方抽象基类

定义所有 provider 的统一接口，并提供同步计算移入线程池的异步桥接。

文本嵌入是所有 provider 的必备能力（``embed_sync``）。图片/音频/视频等
非文本模态为可选能力：provider 通过 ``supported_modalities`` 声明，并实现
对应的 ``embed_image_sync`` / ``embed_audio_sync`` / ``embed_video_sync``。
未实现的模态默认抛 ``NotImplementedError``，调用方应先用 ``supports`` 判定。
"""

import os
import asyncio
from abc import ABC, abstractmethod
from typing import Final
from concurrent.futures import ThreadPoolExecutor

from gsuid_core.ai_core.rag.embedding.modality import EmbeddingModality

# 有界线程池：用于将同步 CPU 计算（fastembed）移入线程池，避免阻塞事件循环
_EMBED_EXECUTOR: Final = ThreadPoolExecutor(max_workers=4, thread_name_prefix="embed")


# 批量 Embedding worker 数。>1 会让并发 N 路嵌入同时喂进共享的 onnxruntime session，
# 使其 CPU 内存 arena 按 ~N 倍放大且只增不减（峰值即进程内存地板）——这是生产 core 进程
# 内存最大头（实测 bge-small 8 路并发 ~5.4GB 常驻）。故默认取最低值 1（串行、最省内存）、
# 不再随核数(旧默认 cpu//4)放大；大机换吞吐再显式调高。
# 优先级：环境变量 GSUID_EMBED_BATCH_WORKERS > WebConsole 配置 embed_batch_workers > 兜底 1。
_FALLBACK_EMBED_BATCH_WORKERS = 1


def _resolve_batch_workers() -> int:
    _env = os.getenv("GSUID_EMBED_BATCH_WORKERS")
    if _env and _env.isdigit() and int(_env) > 0:
        return int(_env)
    try:
        from gsuid_core.ai_core.configs.ai_config import local_embedding_config

        val = int(local_embedding_config.get_config("embed_batch_workers").data)
        if val > 0:
            return val
    except Exception:
        pass
    return _FALLBACK_EMBED_BATCH_WORKERS


_EMBED_BATCH_EXECUTOR: Final = ThreadPoolExecutor(
    max_workers=_resolve_batch_workers(), thread_name_prefix="embed_batch"
)


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
