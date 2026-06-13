"""嵌入 Provider 注册表

允许插件以"第三种 provider"的形式扩展嵌入后端（与内置 local/openai 并列），
如 sentence_transformers、infinity/TEI 客户端、llama.cpp embedding 等。

插件侧用法（import 期注册，工厂内懒加载重依赖）::

    from gsuid_core.ai_core.rag.embedding_registry import (
        EmbeddingProviderEntry,
        register_embedding_provider,
    )


    def _factory():
        from .provider import MyProvider  # torch 等重依赖在这里才被 import

        return MyProvider.from_config()


    register_embedding_provider(
        EmbeddingProviderEntry(
            name="sentence_transformers",
            factory=_factory,
            kind="local",
            display_name="SentenceTransformers (本地)",
            plugin="st_embedding",
        )
    )

时序保证：插件在 load_gss（core.py）同步加载阶段执行 import 期注册，
而 get_embedding_provider() 首次被调用是在 WS 启动后的 AI 后台初始化里，
注册必然早于消费；工厂在后台线程执行，重模型加载不会冻住事件循环。
"""

from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Callable,
    Iterator,
    Optional,
    Protocol,
)
from dataclasses import dataclass

from gsuid_core.logger import logger

if TYPE_CHECKING:
    from gsuid_core.ai_core.rag.embedding import EmbeddingProvider

# 内置 provider 名称（注册表不允许覆盖）
BUILTIN_PROVIDERS: tuple[str, ...] = ("local", "openai")


class ConfigSource(Protocol):
    """插件自有配置源的最小协议：可迭代配置键并按键取值"""

    def __iter__(self) -> Iterator[str]: ...

    def get_config(self, key: str) -> Any: ...


@dataclass
class EmbeddingProviderEntry:
    """插件嵌入 Provider 注册项

    Attributes:
        name: 注册名（配置项 embedding_provider 的取值），如 "sentence_transformers"
        factory: 懒构造工厂，在 RAG 初始化的后台线程中被调用；
            重依赖（torch 等）应在工厂内部 import，不要在插件模块顶层 import
        kind: "local"（本地推理，RAG 同步走小批量）或 "remote"（远程 API，大批量）
        display_name: 网页控制台展示名
        check_config: AI 配置向导的状态检查钩子，返回
            {"configured": bool, "model_name": str, "note": str, "issues": list[str]}
            的部分或全部字段；为 None 时向导显示通用提示
        config_source: 插件自有 StringConfig 实例，供 webconsole summary API
            以与 local/openai 同构的格式返回该 provider 的配置项；可为 None
        plugin: 来源插件名（用于报错归因）
    """

    name: str
    factory: Callable[[], "EmbeddingProvider"]
    kind: str = "local"
    display_name: str = ""
    check_config: Optional[Callable[[], dict]] = None
    config_source: Optional[ConfigSource] = None
    plugin: str = ""


_EXTERNAL_PROVIDERS: Dict[str, EmbeddingProviderEntry] = {}


def _append_provider_option(name: str) -> None:
    """把注册名追加进 embedding_provider 配置项的 options

    必须双写"运行中的配置对象"与"默认模板"：gs_config.update_config()
    每次启动都会用默认模板的 options 回写已存 JSON，只改其一会在下次
    启动/重载时丢失选项。
    """
    from gsuid_core.ai_core.configs.ai_config import AI_CONFIG, ai_config
    from gsuid_core.utils.plugins_config.models import GsStrConfig

    running = ai_config.get_config("embedding_provider")
    for cfg in (running, AI_CONFIG["embedding_provider"]):
        if isinstance(cfg, GsStrConfig) and name not in cfg.options:
            cfg.options.append(name)


def register_embedding_provider(entry: EmbeddingProviderEntry) -> None:
    """注册一个插件嵌入 Provider（覆盖式写入，插件 reload 幂等）

    Raises:
        ValueError: 注册名与内置 provider 冲突
    """
    if entry.name in BUILTIN_PROVIDERS:
        raise ValueError(f"嵌入 Provider 注册名 '{entry.name}' 与内置 provider 冲突")
    _EXTERNAL_PROVIDERS[entry.name] = entry
    _append_provider_option(entry.name)
    logger.info(
        f"🧠 [Embedding] 已注册插件嵌入 Provider: {entry.name} (kind={entry.kind}, plugin={entry.plugin or '未知'})"
    )


def get_external_provider(name: str) -> Optional[EmbeddingProviderEntry]:
    """按注册名获取插件 Provider 注册项"""
    return _EXTERNAL_PROVIDERS.get(name)


def list_external_providers() -> Dict[str, EmbeddingProviderEntry]:
    """返回全部插件 Provider 注册项（浅拷贝）"""
    return dict(_EXTERNAL_PROVIDERS)


def list_embedding_providers() -> List[str]:
    """返回全部可用 provider 名称（内置 + 插件注册）"""
    return [*BUILTIN_PROVIDERS, *_EXTERNAL_PROVIDERS]


def is_local_kind(provider_name: str) -> bool:
    """判断 provider 是否为本地推理类型（决定 RAG 同步嵌入批大小）"""
    if provider_name == "local":
        return True
    if provider_name == "openai":
        return False
    entry = _EXTERNAL_PROVIDERS.get(provider_name)
    return entry is not None and entry.kind == "local"
