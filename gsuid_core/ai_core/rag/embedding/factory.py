"""嵌入模型提供方的全局单例管理

根据 ai_config 中的 embedding_provider 配置项构造并缓存对应的 provider 实现，
支持内置 local/openai 以及插件注册的第三方 provider，并在插件不可用时降级回 local。
"""

from typing import Union

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.embedding.base import EmbeddingProvider
from gsuid_core.ai_core.rag.embedding.local import LocalEmbeddingProvider
from gsuid_core.ai_core.rag.embedding.openai import OpenAIEmbeddingProvider
from gsuid_core.ai_core.rag.embedding.modality import (
    EmbeddingModality,
    parse_modalities,
)

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
            t(
                "log.rag.embedding_fastembed_stembedding_clip_skip",
                p0=[m.value for m in extra],
            )
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
        raise RuntimeError(t("log.rag.model_ai"))

    provider_name = ai_config.get_config("embedding_provider").data

    if provider_name == "local":
        _provider = _build_local_provider()
    elif provider_name == "openai":
        base_url = openai_embedding_config.get_config("base_url").data
        api_key_list = openai_embedding_config.get_config("api_key").data
        if not api_key_list:
            raise ValueError(t("log.rag.openai_api_key"))
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
                t(
                    "log.rag.embedding_provider_name_local",
                    provider_name=provider_name,
                    p0=list_embedding_providers(),
                )
            )
            _provider = _build_local_provider()
        else:
            try:
                _provider = entry.factory()
                logger.info(
                    t(
                        "log.rag.embedding_plugin_provider_name_2",
                        provider_name=provider_name,
                        p0=entry.plugin or "未知",
                    )
                )
            except Exception as e:
                logger.error(
                    t(
                        "log.rag.embedding_plugin_provider_name",
                        provider_name=provider_name,
                        p0=entry.plugin or "未知",
                        e=e,
                    )
                )
                _provider = _build_local_provider()

    return _provider


def reset_embedding_provider() -> None:
    """重置全局嵌入提供方单例（用于配置热重载）"""
    global _provider
    _provider = None
