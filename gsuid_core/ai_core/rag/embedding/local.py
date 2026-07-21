"""本地嵌入模型提供方（基于 fastembed）"""

import os

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.embedding.base import EmbeddingProvider

# 本地嵌入的 CPU/内存旋钮兜底默认值：刻意与 CPU 核数解耦、取「省内存」低值。
# 关键背景：本地嵌入走 fastembed→onnxruntime，其 CPU 内存 arena 大小由「并发峰值」决定、
# 且只增不减（峰值即进程内存地板）。实测同一个 bge-small(90MB) 8 路并发可撑到 ~5.4GB 常驻，
# 单路 threads=2/batch=32 仅 ~0.63GB。故旧默认 threads=cpu//2 会让核越多、内存地板越高——
# 大机反而更吃内存。现改为「低固定值」，大机想换吞吐再显式调高（配置或环境变量）。
# 面向 2C2G 小机的默认：threads=1 在 2 核上只吃一个核、把另一个核留给事件循环
# （避免嵌入抢占导致 Bot 卡顿）；batch=16 进一步压 onnxruntime 内存峰值。大机想换吞吐
# 在 WebConsole「嵌入模型配置」或用 GSUID_EMBED_* 环境变量上调即可。
_FALLBACK_EMBED_THREADS = 1
_FALLBACK_EMBED_BATCH = 16


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
    # 优先级：环境变量 GSUID_EMBED_THREADS > WebConsole 配置 embed_threads > 兜底低值。
    env = os.getenv("GSUID_EMBED_THREADS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return _config_int("embed_threads") or _FALLBACK_EMBED_THREADS


def _resolve_batch_size() -> int:
    # 优先级：环境变量 GSUID_EMBED_BATCH > WebConsole 配置 embed_batch_size > 兜底低值。
    env = os.getenv("GSUID_EMBED_BATCH")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
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
        logger.info(
            t(
                "🧠 [Embedding] 本地嵌入模型已加载: {model_name}, 维度: {p0}, threads={threads}, batch_size={p1}",
                model_name=model_name,
                p0=self._dim,
                threads=threads,
                p1=self._batch_size,
            )
        )

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        # 显式限制 batch_size 控制驻留内存峰值（2C2G 关键）；fastembed 内部按此分批。
        return [[float(x) for x in v] for v in self._model.embed(texts, batch_size=self._batch_size)]

    def embed_single_sync(self, text: str) -> list[float]:
        return [float(x) for x in next(iter(self._model.embed([text])))]
