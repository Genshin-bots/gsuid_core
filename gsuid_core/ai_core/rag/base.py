"""RAG模块基础功能 - 共享常量和工具函数"""

import os
import json
import uuid
import hashlib
import zipfile
import tempfile
import threading
from typing import Final, Union, Callable, Sequence, Awaitable
from pathlib import Path

import httpx
from fastembed import SparseTextEmbedding
from qdrant_client import AsyncQdrantClient
from huggingface_hub import constants as hf_constants, snapshot_download

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import AI_CORE_PATH
from gsuid_core.ai_core.rag.embedding import (
    EmbeddingProvider,
    get_embedding_provider,
)
from gsuid_core.ai_core.configs.ai_config import ai_config, rerank_model_config, local_embedding_config
from gsuid_core.ai_core.rag.qdrant_provider import (
    LOCAL_QDRANT_DB_PATH,
    build_qdrant_client,
)

# ============== 向量库配置 ==============
# 默认向量维度（本地 bge-small-zh-v1.5 模型为 512），仅在嵌入提供方维度未知时回退使用
DEFAULT_DIMENSION: Final[int] = 512

# Embedding模型相关
EMBEDDING_MODEL_NAME: Final[str] = local_embedding_config.get_config("embedding_model_name").data
MODELS_CACHE = AI_CORE_PATH / "models_cache"
# 本地嵌入式 Qdrant 数据目录。实际的本地/远程连接选择已抽象到 rag/qdrant_provider.py，
# 此处保留 DB_PATH 仅为兼容历史导出（rag/__init__.py 等）。
DB_PATH = LOCAL_QDRANT_DB_PATH

# Reranker模型相关
RERANK_MODELS_CACHE = AI_CORE_PATH / "rerank_models_cache"
RERANKER_MODEL_NAME: Final[str] = rerank_model_config.get_config("rerank_model_name").data

# ============== Collection名称 ==============
TOOLS_COLLECTION_NAME: Final[str] = "bot_tools"
KNOWLEDGE_COLLECTION_NAME: Final[str] = "knowledge"
IMAGE_COLLECTION_NAME: Final[str] = "image"

# ============== RAG批量参数 ==============
# 远程 embedding / Qdrant upsert 使用较大批量减少网络和写入开销；
# 本地 fastembed 也应使用适中批量：ONNX Runtime 对批量推理做了高度优化，逐条(=1)提交会让
# 每条都付出一次完整的 Python↔执行器往返与算子启动开销，在维度迁移这类需要重嵌入数万~十几万条
# 的场景下慢得无法接受(60558 实体 ×2 向量 + 118781 边)。批量 64 的短文本推理在 CPU 上仍是
# 亚秒级，对单条会话同步几乎无感(min(64, n) 会退化为实际条数)，却能让批量重嵌入提速数十倍。
RAG_BATCH_SIZE: Final[int] = 300
RAG_LOCAL_EMBED_BATCH_SIZE: Final[int] = 64
RAG_REMOTE_EMBED_BATCH_SIZE: Final[int] = RAG_BATCH_SIZE
RAG_UPSERT_BATCH_SIZE: Final[int] = RAG_BATCH_SIZE


# ============== 模型HF仓库映射 ==============
def _get_embedding_hf_repo(model_name: str) -> str:
    """根据embedding模型名称获取对应的HuggingFace仓库名

    特别处理：只要文件名中包含 bge-small-zh-v1.5，就使用 Qdrant/bge-small-zh-v1.5
    """
    if "bge-small-zh-v1.5" in model_name:
        return "Qdrant/bge-small-zh-v1.5"
    return model_name


EMBEDDING_HF_REPO: Final[str] = _get_embedding_hf_repo(EMBEDDING_MODEL_NAME)
SPARSE_HF_REPO: Final[str] = "Qdrant/bm25"
RERANKER_HF_REPO: Final[str] = RERANKER_MODEL_NAME  # BAAI/bge-reranker-base


# ============== 配置开关（动态读取，避免模块加载时配置文件不存在导致默认值错误） ==============
def is_enable_ai() -> bool:
    return ai_config.get_config("enable").data


def is_enable_rerank() -> bool:
    return ai_config.get_config("enable_rerank").data


def _get_hf_endpoint() -> str:
    """获取HuggingFace服务器地址"""
    return ai_config.get_config("hf_endpoint").data


def get_rag_embed_batch_size() -> int:
    """按当前 embedding provider 返回 RAG 同步嵌入批大小。

    本地模型默认单条提交，避免一次大批量推理长时间占用 CPU；远程模型使用大批量减少 HTTP 往返。
    插件注册的 provider 按其注册时声明的 kind（local/remote）判断。
    """
    from gsuid_core.ai_core.rag.embedding_registry import is_local_kind

    provider_name = ai_config.get_config("embedding_provider").data
    if is_local_kind(provider_name):
        return RAG_LOCAL_EMBED_BATCH_SIZE
    return RAG_REMOTE_EMBED_BATCH_SIZE


def get_rag_upsert_batch_size() -> int:
    """返回 Qdrant upsert 批大小。"""
    return RAG_UPSERT_BATCH_SIZE


# ============== 413 退避重试工具 ==============
# 全局缓存：413 退避发现的有效批大小，避免每次调用都从默认值重新试错
_cached_embed_bs: int = 0  # 0 表示尚未发现过 413，使用默认值
_cached_upsert_bs: int = 0

_413_TEXT_MARKERS: tuple[str, ...] = (
    "413",
    "payload too large",
    "request entity too large",
    "request body too large",
    "context length",
    "too many tokens",
)


def _is_413_error(exc: BaseException) -> bool:
    """判断异常是否对应 413 / Payload Too Large。

    覆盖 httpx.HTTPStatusError、openai 异常、以及纯文本兜底。
    """
    # 1) 结构化 status_code / code 属性
    for attr in ("status_code", "code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and val == 413:
            return True
        if isinstance(val, str) and val.strip() == "413":
            return True
    # 2) httpx.Response 类
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 413:
        return True
    # 3) 纯文本兜底
    msg = (str(exc) or "").lower()
    return any(marker in msg for marker in _413_TEXT_MARKERS)


async def embed_texts_with_backoff(
    texts: Sequence[str],
    embed_fn: Callable[[Sequence[str]], Awaitable[Sequence[Sequence[float]]]],
    *,
    initial_batch_size: int | None = None,
    log_tag: str = "Embedding",
) -> list[Sequence[float] | None]:
    """对一组文本做远程嵌入，遇到 413 时把批大小减半后重试，最小到 1。

    已成功写入的批会保留在结果中（顺序与输入一致）。
    当批大小已为 1 且仍触发 413 时，对应位置返回 None 并记 warning。
    非 413 异常直接抛出，保留调用方原有降级/抛出策略。
    """
    global _cached_embed_bs
    if not texts:
        return []
    if initial_batch_size is not None:
        bs = initial_batch_size
    elif _cached_embed_bs > 0:
        bs = _cached_embed_bs
    else:
        bs = get_rag_embed_batch_size()
    if bs <= 0:
        bs = 1
    results: list[Sequence[float] | None] = [None] * len(texts)
    index = 0
    while index < len(texts):
        current_bs = min(bs, len(texts) - index)
        batch = texts[index : index + current_bs]
        try:
            vectors = await embed_fn(batch)
        except Exception as e:
            if not _is_413_error(e):
                raise
            if current_bs <= 1:
                logger.warning(
                    t(
                        "🧠 [{log_tag}] 单条仍触发 413 限流，跳过: index={index}, err={e}",
                        log_tag=log_tag,
                        index=index,
                        e=e,
                    )
                )
                index += 1
                continue
            new_bs = max(current_bs // 2, 1)
            logger.warning(
                t(
                    "🧠 [{log_tag}] 远端拒绝大批量(413)，批大小 {current_bs} -> {new_bs}: {e}",
                    log_tag=log_tag,
                    current_bs=current_bs,
                    new_bs=new_bs,
                    e=e,
                )
            )
            _cached_embed_bs = new_bs
            bs = new_bs
            continue  # 当前批不前进, 用更小批重试同一窗口
        if len(vectors) != len(batch):
            raise RuntimeError(
                t(
                    "🧠 [{log_tag}] 批量嵌入返回数量异常: expected={p0}, actual={p1}",
                    log_tag=log_tag,
                    p0=len(batch),
                    p1=len(vectors),
                )
            )
        results[index : index + current_bs] = vectors
        index += current_bs
    return results


async def upsert_points_with_backoff(
    points: Sequence,
    upsert_fn: Callable[[Sequence], Awaitable[None]],
    *,
    initial_batch_size: int | None = None,
    log_tag: str = "QdrantUpsert",
) -> int:
    """对一组 Qdrant PointStruct 写入，遇到 413 时把批大小减半后重试，最小到 1。

    返回成功写入的 point 数量。bs == 1 仍 413 时记录 warning 并跳过该条。
    非 413 异常直接抛出。
    """
    global _cached_upsert_bs
    if not points:
        return 0
    if initial_batch_size is not None:
        bs = initial_batch_size
    elif _cached_upsert_bs > 0:
        bs = _cached_upsert_bs
    else:
        bs = get_rag_upsert_batch_size()
    if bs <= 0:
        bs = 1
    written = 0
    index = 0
    while index < len(points):
        current_bs = min(bs, len(points) - index)
        batch = points[index : index + current_bs]
        try:
            await upsert_fn(batch)
        except Exception as e:
            if not _is_413_error(e):
                raise
            if current_bs <= 1:
                logger.warning(
                    t(
                        "🧠 [{log_tag}] 单条 Point 仍触发 413，跳过: index={index}, err={e}",
                        log_tag=log_tag,
                        index=index,
                        e=e,
                    )
                )
                index += 1
                continue
            new_bs = max(current_bs // 2, 1)
            logger.warning(
                t(
                    "🧠 [{log_tag}] Qdrant 远端拒绝大批量(413)，批大小 {current_bs} -> {new_bs}: {e}",
                    log_tag=log_tag,
                    current_bs=current_bs,
                    new_bs=new_bs,
                    e=e,
                )
            )
            _cached_upsert_bs = new_bs
            bs = new_bs
            continue
        written += current_bs
        index += current_bs
    return written


def get_dimension() -> int:
    """动态获取当前嵌入向量的维度。

    仅用于非严格兼容场景；Collection 创建/迁移必须使用 get_strict_dimension()，
    避免未知维度时以 DEFAULT_DIMENSION 创建出错误维度的 Qdrant Collection。
    """
    if embedding_provider is not None:
        dim = embedding_provider.dimension
        if dim > 0:
            return dim
        logger.warning(
            t(
                "🧠 [Embedding] 当前嵌入提供方维度未知(0)，回退到默认维度 {DEFAULT_DIMENSION}，"
                "如使用非标准维度的模型请在嵌入模型配置中手动指定 dimension",
                DEFAULT_DIMENSION=DEFAULT_DIMENSION,
            )
        )
    return DEFAULT_DIMENSION


def get_strict_dimension() -> int:
    """严格获取当前嵌入维度；未知时直接报错，禁止创建错误维度 Collection。"""
    if embedding_provider is None:
        raise RuntimeError(t("EmbeddingProvider 未初始化，无法确定向量维度"))

    dim = embedding_provider.dimension
    if dim <= 0:
        raise RuntimeError(
            t(
                "当前嵌入模型维度未知，已阻止创建/迁移 Qdrant Collection。"
                "请检查嵌入模型 API 是否可用，或在 OpenAI 嵌入模型配置中显式设置 dimension。"
            )
        )
    return dim


async def ensure_embedding_dimension() -> int:
    """确保启动阶段已解析出真实嵌入维度。

    OpenAI 兼容的非标准模型在配置 dimension=0 时可能只有首次 API 响应后才知道维度。
    因此在任何 Collection 创建前主动发起一次最小 embedding 预热。
    """
    if embedding_provider is None:
        raise RuntimeError(t("EmbeddingProvider 未初始化，无法预热向量维度"))

    if embedding_provider.dimension > 0:
        return embedding_provider.dimension

    logger.info(t("🧠 [Embedding] 嵌入维度未知，启动阶段执行一次最小向量预热..."))
    await embedding_provider.embed_single("维度探测")
    dim = embedding_provider.dimension
    if dim <= 0:
        raise RuntimeError(t("嵌入模型 API 调用后仍无法推断向量维度，请在 OpenAI 嵌入模型配置中显式设置 dimension。"))

    logger.info(t("🧠 [Embedding] 启动阶段已解析嵌入维度: {dim}", dim=dim))
    return dim


def _format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读的大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


async def _download_and_extract_zip(base_url: str, tag: str, zip_name: str, target_dir: Path) -> bool:
    """从资源库下载zip文件并解压到目标目录（流式下载，带进度日志）

    Args:
        base_url: 资源库基础URL
        tag: 资源站标签
        zip_name: zip文件名（不含扩展名），如 "models_cache"
        target_dir: 解压目标目录

    Returns:
        True 表示成功下载并解压，False 表示失败
    """
    zip_url = f"{base_url}/ai_core/{zip_name}.zip"
    logger.info(
        t("🧠 [RAG] 尝试从资源库下载 {zip_name}.zip: {tag} {zip_url}", zip_name=zip_name, tag=tag, zip_url=zip_url)
    )

    tmp_path = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            async with client.stream("GET", zip_url) as response:
                if response.status_code != 200:
                    logger.warning(
                        t(
                            "🧠 [RAG] 资源库下载 {zip_name}.zip 失败，HTTP状态码: {p0}",
                            zip_name=zip_name,
                            p0=response.status_code,
                        )
                    )
                    return False

                total_size = int(response.headers.get("content-length", 0))
                if total_size > 0:
                    logger.info(
                        t("🧠 [RAG] {zip_name}.zip 文件大小: {p0}", zip_name=zip_name, p0=_format_size(total_size))
                    )

                # 流式写入临时文件
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
                downloaded = 0
                last_log_bytes = 0
                log_interval = 5 * 1024 * 1024  # 每5MB打印一次进度

                with os.fdopen(tmp_fd, "wb") as tmp_file:
                    async for chunk in response.aiter_bytes(chunk_size=65536):  # type: ignore
                        if chunk:
                            tmp_file.write(chunk)
                            downloaded += len(chunk)

                            # 定期打印下载进度
                            if downloaded - last_log_bytes >= log_interval:
                                if total_size > 0:
                                    progress = downloaded / total_size * 100
                                    logger.info(
                                        t(
                                            "🧠 [RAG] {zip_name}.zip 下载进度: {p0} / {p1} ({progress:.1f}%)",
                                            zip_name=zip_name,
                                            p0=_format_size(downloaded),
                                            p1=_format_size(total_size),
                                            progress=progress,
                                        )
                                    )
                                else:
                                    logger.info(
                                        t(
                                            "🧠 [RAG] {zip_name}.zip 已下载: {p0}",
                                            zip_name=zip_name,
                                            p0=_format_size(downloaded),
                                        )
                                    )
                                last_log_bytes = downloaded

                if downloaded == 0:
                    logger.warning(t("🧠 [RAG] 资源库下载 {zip_name}.zip 失败，内容为空", zip_name=zip_name))
                    return False

                logger.info(
                    t(
                        "🧠 [RAG] {zip_name}.zip 下载完成: {p0}，开始解压...",
                        zip_name=zip_name,
                        p0=_format_size(downloaded),
                    )
                )

        # 解压到父目录，因为zip内部已包含同名文件夹（如 models_cache/models_cache）
        parent_dir = target_dir.parent
        parent_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.extractall(parent_dir)

        logger.success(
            t(
                "🧠 [RAG] 资源库 {zip_name}.zip 解压完成: {tag} -> {target_dir}",
                zip_name=zip_name,
                tag=tag,
                target_dir=target_dir,
            )
        )
        return True

    except Exception as e:
        logger.warning(t("🧠 [RAG] 资源库下载 {zip_name}.zip 失败: {e}", zip_name=zip_name, e=e))
        return False
    finally:
        # 清理临时文件
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def _try_download_from_resource_lib() -> bool:
    """尝试从资源库下载模型缓存zip包

    Returns:
        True 表示成功，False 表示失败
    """
    from gsuid_core.utils.download_resource.download_core import check_speed

    try:
        tag, base_url = await check_speed()
        if not base_url:
            logger.warning(t("🧠 [RAG] 资源库测速失败，无法获取可用资源站"))
            return False
    except Exception as e:
        logger.warning(t("🧠 [RAG] 资源库测速异常: {e}", e=e))
        return False

    # 下载 models_cache.zip
    models_ok = await _download_and_extract_zip(base_url, tag, "models_cache", MODELS_CACHE)
    if not models_ok:
        return False

    return True


def _get_dir_size(path: Path) -> int:
    """递归计算目录总大小（字节）"""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        pass
    return total


def _is_models_cache_valid() -> bool:
    """检查模型缓存是否已存在且有效

    通过检查 models--Qdrant--bge-small-zh-v1.5 文件夹是否存在且大小超过 88MB 来判断。
    """
    embedding_model_dir = MODELS_CACHE / "models--Qdrant--bge-small-zh-v1.5"
    if not embedding_model_dir.is_dir():
        return False

    dir_size = _get_dir_size(embedding_model_dir)
    min_size = 88 * 1024 * 1024  # 88MB
    if dir_size < min_size:
        logger.info(
            t(
                "🧠 [RAG] Embedding模型缓存目录存在但不完整: {p0} < {p1}",
                p0=_format_size(dir_size),
                p1=_format_size(min_size),
            )
        )
        return False

    logger.info(t("🧠 [RAG] 模型缓存已存在，大小: {p0}，跳过下载", p0=_format_size(dir_size)))
    return True


async def pre_download_models():
    """提前下载所有模型到缓存目录

    优先从资源库下载zip包并解压，如果失败则回退到HuggingFace下载。

    下载三个模型：
    1. Embedding模型: Qdrant/bge-small-zh-v1.5 -> MODELS_CACHE
    2. Sparse模型: Qdrant/bm25 -> MODELS_CACHE
    3. Reranker模型: BAAI/bge-reranker-base -> RERANK_MODELS_CACHE
    """
    if not is_enable_ai():
        return

    # 检查模型缓存是否已存在
    if _is_models_cache_valid():
        return

    # 优先尝试从资源库下载zip包
    logger.info(t("🧠 [RAG] 优先尝试从资源库下载模型缓存..."))
    resource_ok = await _try_download_from_resource_lib()
    if resource_ok:
        logger.success(t("🧠 [RAG] 资源库模型缓存下载完成，跳过HuggingFace下载"))
        return

    logger.info(t("🧠 [RAG] 资源库下载失败，回退到HuggingFace下载..."))

    hf_endpoint = _get_hf_endpoint()
    # 设置HF_ENDPOINT环境变量，并同步更新huggingface_hub.constants.ENDPOINT
    # 因为huggingface_hub在模块导入时就缓存了ENDPOINT值，仅修改os.environ不会生效
    old_endpoint = os.environ.get("HF_ENDPOINT")
    old_hf_constant = getattr(hf_constants, "ENDPOINT", None)
    os.environ["HF_ENDPOINT"] = hf_endpoint
    hf_constants.ENDPOINT = hf_endpoint.rstrip("/")
    logger.info(t("🧠 [RAG] HuggingFace 端点已设置: HF_ENDPOINT={p0}", p0=hf_constants.ENDPOINT))

    try:
        # 下载Embedding模型
        logger.info(t("🧠 [RAG] 预下载Embedding模型: {EMBEDDING_HF_REPO}", EMBEDDING_HF_REPO=EMBEDDING_HF_REPO))
        snapshot_download(
            repo_id=EMBEDDING_HF_REPO,
            cache_dir=str(MODELS_CACHE),
        )
        logger.info(t("🧠 [RAG] Embedding模型预下载完成"))

        # 下载Sparse模型
        logger.info(t("🧠 [RAG] 预下载Sparse模型: {SPARSE_HF_REPO}", SPARSE_HF_REPO=SPARSE_HF_REPO))
        snapshot_download(
            repo_id=SPARSE_HF_REPO,
            cache_dir=str(MODELS_CACHE),
        )
        logger.info(t("🧠 [RAG] Sparse模型预下载完成"))

        # 下载Reranker模型（仅本地 rerank 模式需要预下载）
        rerank_provider = ai_config.get_config("rerank_provider").data
        if is_enable_rerank() and rerank_provider == "local":
            logger.info(t("🧠 [RAG] 预下载Reranker模型: {RERANKER_HF_REPO}", RERANKER_HF_REPO=RERANKER_HF_REPO))
            snapshot_download(
                repo_id=RERANKER_HF_REPO,
                cache_dir=str(RERANK_MODELS_CACHE),
            )
            logger.info(t("🧠 [RAG] Reranker模型预下载完成"))
    except Exception as e:
        logger.warning(t("🧠 [RAG] 模型预下载失败，将在使用时尝试加载: {e}", e=e))
    finally:
        # 恢复原来的HF_ENDPOINT和huggingface_hub常量
        if old_endpoint is not None:
            os.environ["HF_ENDPOINT"] = old_endpoint
        elif "HF_ENDPOINT" in os.environ:
            del os.environ["HF_ENDPOINT"]
        if old_hf_constant is not None:
            hf_constants.ENDPOINT = old_hf_constant


class _EmbeddingModelWrapper:
    """向后兼容的嵌入模型包装器

    将 EmbeddingProvider 包装为 fastembed TextEmbedding 的接口风格，
    使得现有调用方（embedding_model.embed([text])）无需修改即可工作。
    """

    def __init__(self, provider: EmbeddingProvider):
        self._provider = provider

    def embed(self, texts: list[str]):
        """兼容 fastembed TextEmbedding.embed() 接口（同步，可能阻塞事件循环）

        返回一个生成器，每个元素是 list[float]（而非 numpy array）。
        警告：在异步环境中应使用 aembed() 以避免阻塞事件循环。
        """
        results = self._provider.embed_sync(texts)
        return iter(results)

    async def aembed(self, texts: list[str]):
        """异步批量嵌入（不阻塞事件循环）

        返回一个生成器，每个元素是 list[float]。
        在异步代码中应优先使用此方法代替 embed()。
        """
        results = await self._provider.embed(texts)
        return iter(results)

    @property
    def provider(self) -> EmbeddingProvider:
        """获取底层 EmbeddingProvider 实例"""
        return self._provider


embedding_model: "Union[_EmbeddingModelWrapper, None]" = None
embedding_provider: "Union[EmbeddingProvider, None]" = None
client: "Union[AsyncQdrantClient, None]" = None
# 全局 Sparse Embedding 模型（懒加载，线程安全）
_sparse_model = None
_sparse_model_lock = threading.Lock()
# Embedding/Qdrant 初始化锁：init_embedding_model 会经 asyncio.to_thread 在多个线程并发触发
# （RAG init_all、sync_knowledge 懒加载、init_memory_system）。check-then-act 若无锁，
# 两个线程会同时构造 AsyncQdrantClient，触发本地 Qdrant 文件锁冲突：
# "Storage folder ... is already accessed by another instance of Qdrant client"。
_client_init_lock = threading.Lock()


def _get_sparse_model():
    """隐患三修复：添加线程锁防止并发初始化模型"""
    global _sparse_model

    if not is_enable_ai():
        return

    if _sparse_model is None:
        with _sparse_model_lock:
            # 双重检查锁定
            if _sparse_model is None:
                try:
                    _sparse_model = SparseTextEmbedding(
                        model_name="Qdrant/bm25",
                        cache_dir=str(MODELS_CACHE),
                        threads=2,
                        local_files_only=True,
                    )
                except Exception as e:
                    logger.warning(t("🧠 [Memory] SparseTextEmbedding 初始化失败: {e}", e=e))
    return _sparse_model


def init_embedding_model():
    """初始化Embedding模型和Qdrant客户端"""
    global embedding_model, embedding_provider, client

    if not is_enable_ai():
        return

    # 快速路径：已初始化直接返回，避免无谓加锁
    if client is not None:
        return

    # 加锁 + 双重检查：本函数可能经 asyncio.to_thread 在多个线程并发触发，
    # 没有锁时 check-then-act 会让两个线程同时构造 AsyncQdrantClient，导致本地
    # Qdrant 文件锁冲突。锁内只做一次真正的初始化，保证全局只有一个 client 实例。
    with _client_init_lock:
        if client is not None:
            return

        # 通过统一的嵌入提供方抽象层初始化
        provider = get_embedding_provider()
        embedding_provider = provider
        embedding_model = _EmbeddingModelWrapper(provider)
        # 经 qdrant_provider 抽象层按配置构造本地/远程客户端，切换由抽象层内部统一处理
        client = build_qdrant_client()


def get_point_id(id_str: str) -> str:
    """生成向量化存储的唯一ID

    使用UUID5和DNS命名空间生成确定性的UUID，
    相同id_str始终生成相同的UUID，确保幂等性。

    Args:
        id_str: 唯一标识符字符串

    Returns:
        唯一的UUID字符串
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, id_str))


def calculate_hash(content: dict) -> str:
    """计算内容字典的MD5哈希

    用于检测内容是否有变更，支持知识库增量更新判断。
    排序键以确保相同内容产生相同的哈希值。

    Args:
        content: 要计算哈希的内容字典

    Returns:
        MD5哈希值（32位十六进制字符串）
    """
    json_str = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(json_str.encode("utf-8")).hexdigest()
