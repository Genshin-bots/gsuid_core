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

AI_CONFIG: Dict[str, GSC] = {
    "enable": GsBoolConfig(
        "是否启用AI服务",
        "指定是否启用AI服务",
        False,
    ),
    "ai_mode": GsListStrConfig(
        "AI行动模式",
        "指定AI的行动模式, AI只会在预定条件完成时才会执行操作",
        ["提及应答"],
        options=["提及应答", "定时巡检", "趣向捕捉(暂不可用)", "困境救场(暂不可用)"],
    ),
    "enable_rerank": GsBoolConfig(
        "是否启用Rerank",
        "指定是否启用Rerank功能, Rerank可以提升RAG的检索质量, 但会增加一定的响应时间, 该模型较大, 请根据实际情况启用",
        False,
    ),
    "openai_provider": GsStrConfig(
        title="AI模型服务提供方",
        desc="指定AI服务提供格式, OpenAI兼容",
        data="openai",
        options=["openai"],
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

OPENAI_CONFIG: Dict[str, GSC] = {
    "base_url": GsStrConfig(
        "OpenAI API基础URL",
        "指定OpenAI API的基础URL, 注意是以 /v1 结尾",
        "https://api.openai.com/v1",
        options=[
            "https://api.openai.com/v1",
            "https://api.bltcy.ai/v1",
            "https://api.minimaxi.com/v1",
        ],
    ),
    "api_key": GsListStrConfig(
        "OpenAI API密钥",
        "指定OpenAI API的密钥, 注意是以 sk- 开头, 不要泄露, 支持添加多个",
        ["sk-"],
        options=["sk-"],
    ),
    "model_name": GsStrConfig(
        "调用模型名称",
        "指定OpenAI API的模型, 该模型将会用于处理大部分任务",
        "",
        options=[
            "gemini-2.5-flash",
            "gemini-3.1-flash-lite-preview",
            "MiniMax-M2.7",
        ],
    ),
    "embedding_model": GsStrConfig(
        "嵌入模型(暂不支持远程嵌入)",
        "指定OpenAI API的嵌入模型, 该模型将会用于处理文本嵌入",
        "text-embedding-3-small",
        options=["text-embedding-3-small"],
    ),
    "model_support": GsListStrConfig(
        "模型支持能力",
        "显式指定模型支持能力，如是否能看图、能处理文件/音频/视频等",
        ["text"],
        options=["text", "image", "audio", "video"],
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


openai_config = StringConfig(
    "GsCore AI OpenAI配置",
    get_res_path("ai_core") / "openai_config.json",
    OPENAI_CONFIG,
)

persona_config = StringConfig(
    "GsCore AI 人设配置",
    get_res_path("ai_core") / "persona_config.json",
    PERSONA_CONFIG,
)
