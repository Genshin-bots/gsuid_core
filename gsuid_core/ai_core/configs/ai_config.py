from typing import Dict

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsDictConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .openai_config import list_available_openai_configs
from .anthropic_config import list_available_anthropic_configs


def _get_openai_config_options() -> list[str]:
    """动态获取可用的 OpenAI 配置文件列表"""

    configs = list_available_openai_configs()
    if not configs:
        return ["openai_config"]
    return configs


def _get_anthropic_config_options() -> list[str]:
    """动态获取可用的 Anthropic 配置文件列表"""

    configs = list_available_anthropic_configs()
    if not configs:
        return ["anthropic_config"]
    return configs


def _get_provider_config_options(provider: str) -> list[str]:
    """动态获取可用的 Provider 配置文件列表（不含扩展名）"""
    if provider == "openai":
        return _get_openai_config_options()
    elif provider == "anthropic":
        return _get_anthropic_config_options()
    return ["openai_config"]


AI_CONFIG: Dict[str, GSC] = {
    "enable": GsBoolConfig(
        "是否启用AI服务",
        "指定是否启用AI服务",
        False,
    ),
    "enable_rerank": GsBoolConfig(
        "是否启用Rerank",
        "指定是否启用Rerank功能, Rerank可以提升RAG的检索质量, 但会增加一定的响应时间, 该模型较大, 请根据实际情况启用",
        False,
    ),
    "enable_memory": GsBoolConfig(
        "是否启用记忆",
        "指定是否启用记忆功能",
        True,
    ),
    "high_level_provider_config_name": GsStrConfig(
        title="高级任务AI模型配置名称",
        desc="指定用于高级任务(复杂推理/工具调用)的AI配置文件名称",
        data="",
    ),
    "low_level_provider_config_name": GsStrConfig(
        title="低级任务AI模型配置名称",
        desc="指定用于低级任务(简单问答/快速响应)的AI配置文件名称",
        data="",
    ),
    "embedding_provider": GsStrConfig(
        "嵌入模型服务提供方",
        "指定嵌入模型提供方",
        "local",
        options=["local"],
    ),
    "websearch_provider": GsStrConfig(
        "网络搜索服务提供方",
        "指定网络搜索服务提供方",
        "Tavily",
        options=["Tavily"],
    ),
    "multi_agent_lenth": GsIntConfig(
        "最多允许AI思考轮数",
        "指定多轮思考调用工具的最大递归深度, 注意: 多轮对话会占用更多的token, 请根据实际情况调整",
        12,
        options=[9, 12, 20, 30],
    ),
    "white_list": GsListStrConfig(
        "白名单",
        "指定白名单, 只有白名单中的用户才能使用AI服务",
        [],
        options=[],
    ),
    "black_list": GsListStrConfig(
        "黑名单",
        "指定黑名单, 黑名单中的用户将不能使用AI服务",
        [],
        options=[],
    ),
}


PERSONA_CONFIG: Dict[str, GSC] = {
    "enable_persona": GsListStrConfig(
        "启用人设服务",
        "指定启用某些人设服务",
        [],
        options=["早柚"],
    ),
    "persona_for_session": GsDictConfig(
        "人设服务针对群聊",
        "指定对某些群聊/用户启用某些人设服务",
        {},
    ),
}


TAVILY_CONFIG: Dict[str, GSC] = {
    "api_key": GsListStrConfig(
        "Tavily API密钥",
        "指定 Tavily API 的密钥，用于启用 web 搜索功能，请前往 https://tavily.com 获取 API Key",
        [],
        options=[],
    ),
    "max_results": GsIntConfig(
        "最大搜索结果数",
        "指定每次搜索的最大返回结果数量",
        10,
        options=[5, 10, 15, 20],
    ),
    "search_depth": GsStrConfig(
        "搜索深度",
        "指定搜索深度，basic 速度更快但结果较少，advanced 更详细但速度较慢",
        "basic",
        options=["basic", "advanced"],
    ),
}

LOCAL_EMBEDDING_CONFIG: Dict[str, GSC] = {
    "embedding_model_name": GsStrConfig(
        "指定嵌入模型名称",
        "指定启用的嵌入模型名称",
        "BAAI/bge-small-zh-v1.5",
        options=["BAAI/bge-small-zh-v1.5"],
    ),
}

RERANK_MODEL_CONFIG: Dict[str, GSC] = {
    "rerank_model_name": GsStrConfig(
        "指定Rerank模型名称",
        "指定启用的Rerank模型名称",
        "BAAI/bge-reranker-base",
        options=["BAAI/bge-reranker-base"],
    ),
}

MEMORY_CONFIG: Dict[str, GSC] = {
    "memory_mode": GsListStrConfig(
        "记忆路径",
        "指定启用的记忆路径, 被动感知全部群友会话或只记住自己有参与的聊天记录",
        ["被动感知", "主动会话"],
        options=["被动感知", "主动会话"],
    ),
    "memory_session": GsStrConfig(
        "被动感知范围",
        "指定被动感知的范围",
        "按人格配置",
        options=["按人格配置", "全部群聊"],
    ),
    "retrieval_top_k": GsIntConfig(
        "最终检索数量",
        "指定最终检索数量, 可以提高检索精度但会增加性能开销",
        15,
        options=[5, 10, 15, 20],
    ),
    "enable_system2": GsBoolConfig(
        "是否启用 System-2",
        "指定是否启用 System-2, 可以提高检索精度但会增加性能开销",
        True,
    ),
    "eval_mode": GsBoolConfig(
        "记忆评测模式",
        "指定是否启用记忆评测模式, 启用后无法使用 System-2 和 Rerank",
        False,
    ),
}

memory_config = StringConfig(
    "GsCore AI 记忆配置",
    get_res_path("ai_core") / "memory_config.json",
    MEMORY_CONFIG,
)

ai_config = StringConfig(
    "GsCore AI AI配置",
    get_res_path("ai_core") / "ai_config.json",
    AI_CONFIG,
)

local_embedding_config = StringConfig(
    "GsCore AI 嵌入模型配置",
    get_res_path("ai_core") / "local_embedding_config.json",
    LOCAL_EMBEDDING_CONFIG,
)

rerank_model_config = StringConfig(
    "GsCore AI Rerank模型配置",
    get_res_path("ai_core") / "rerank_model_config.json",
    RERANK_MODEL_CONFIG,
)

tavily_config = StringConfig(
    "GsCore AI Tavily搜索配置",
    get_res_path("ai_core") / "tavily_config.json",
    TAVILY_CONFIG,
)

persona_config = StringConfig(
    "GsCore AI 人设配置",
    get_res_path("ai_core") / "persona_config.json",
    PERSONA_CONFIG,
)
