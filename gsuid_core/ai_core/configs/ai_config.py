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
from gsuid_core.utils.plugins_config.gs_config import GsDivider, StringConfig

AI_CONFIG: Dict[str, GSC] = {
    "enable": GsBoolConfig(
        "是否启用AI服务",
        "指定是否启用AI服务",
        False,
    ),
    "hf_endpoint": GsStrConfig(
        "HuggingFace 服务器地址",
        "指定 HuggingFace 服务器地址",
        "https://hf-mirror.com",
        options=[
            "https://huggingface.co",
            "https://hf-mirror.com",
        ],
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
        desc="指定用于高级任务(工具调用)的AI配置文件名称，格式为 'provider++config_name'（如 'openai++MiniMAX'）",
        data="",
    ),
    "low_level_provider_config_name": GsStrConfig(
        title="低级任务AI模型配置名称",
        desc="指定用于低级任务(简单问答)的AI配置文件名称，格式为 'provider++config_name'（如 'openai++MiniMAX'）",
        data="",
    ),
    "embedding_provider": GsStrConfig(
        "嵌入模型服务提供方",
        "指定嵌入模型提供方",
        "local",
        options=["local", "openai"],
    ),
    "rerank_provider": GsStrConfig(
        title="Rerank模型服务提供方",
        desc="指定 Rerank 模型提供方。local 使用本地 fastembed 模型；openai 使用 OpenAI兼容 rerank API 的远程服务",
        data="local",
        options=["local", "openai"],
    ),
    "qdrant_provider": GsStrConfig(
        title="Qdrant向量库部署方式",
        desc=(
            "指定向量库(Qdrant)的部署方式。local 使用本地嵌入式 Qdrant; "
            "remote 连接远程 Qdrant 服务(需在 Qdrant配置 中填写 url/api_key)。"
            "切换后启动时会自动把历史数据迁移到新后端(保留原后端数据)"
        ),
        data="local",
        options=["local", "remote"],
    ),
    "websearch_provider": GsStrConfig(
        "网络搜索服务提供方",
        "指定网络搜索服务提供方",
        "Tavily",
        options=["Tavily", "Exa", "MCP"],
    ),
    "image_understand_provider": GsStrConfig(
        "图片理解服务提供方",
        "指定图片理解服务提供方，当LLM模型不支持图片时，使用该服务将图片转述为文本",
        "MCP",
        options=["MCP"],
    ),
    "asr_provider": GsStrConfig(
        "语音识别服务提供方",
        "指定语音识别（ASR）服务提供方，用于将用户发送的语音消息转为文字",
        "MCP",
        options=["MCP"],
    ),
    "video_understand_provider": GsStrConfig(
        "视频理解服务提供方",
        "指定视频理解服务提供方，用于从视频中提取关键帧并理解内容",
        "MCP",
        options=["MCP"],
    ),
    "document_extract_provider": GsStrConfig(
        "文档提取服务提供方",
        "指定文档内容提取服务提供方，用于将PDF/Word/Excel等文档转为文本",
        "MCP",
        options=["MCP"],
    ),
    "multi_agent_lenth": GsIntConfig(
        "最多允许AI思考轮数",
        "指定多轮思考调用工具的最大递归深度, 注意: 多轮对话会占用更多的token, 请根据实际情况调整",
        20,
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
    "enable_deepseek_rp": GsBoolConfig(
        "启用DS专属角色扮演模式",
        "用于在思考模式下切换思维链风格, [文档](https://github.com/victorchen96/deepseek_v4_rolepaly_instruct/blob/main/README.md)",
        False,
    ),
}


MCP_SERVER_CONFIG: Dict[str, GSC] = {
    "enable_mcp_server": GsBoolConfig(
        "是否启用MCP Server",
        "是否将框架的to_ai触发器对外暴露为MCP Server, 启用后外部MCP客户端可通过SSE/stdio协议连接并调用所有触发器工具",
        False,
    ),
    "mcp_server_transport": GsStrConfig(
        "MCP Server传输协议",
        "指定MCP Server使用的传输协议, sse为HTTP SSE模式(适合远程访问), stdio为标准输入输出模式(适合本地进程间通信)",
        "sse",
        options=["sse", "stdio"],
    ),
    "mcp_server_port": GsIntConfig(
        "MCP Server监听端口",
        "指定MCP Server SSE模式下的监听端口（监听地址复用框架HOST配置）",
        8766,
        options=[8766, 8767, 8768, 9000],
    ),
    "mcp_server_api_key": GsStrConfig(
        "MCP Server API密钥",
        "指定Bearer Token认证密钥, 留空则不启用认证。外部客户端连接时需在请求头中携带 Authorization: Bearer <api_key>",
        "",
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

EXA_CONFIG: Dict[str, GSC] = {
    "api_key": GsListStrConfig(
        "Exa API密钥",
        "指定 Exa API 的密钥，用于启用 web 搜索功能，请前往 https://exa.ai 获取 API Key",
        [],
        options=[],
    ),
    "max_results": GsIntConfig(
        "最大搜索结果数",
        "指定每次搜索的最大返回结果数量",
        10,
        options=[5, 10, 15, 20],
    ),
    "search_type": GsStrConfig(
        "搜索类型",
        "指定搜索类型，neural 为语义搜索（更智能），keyword 为关键词搜索（更精确）",
        "neural",
        options=["neural", "keyword"],
    ),
}

MINIMAX_CONFIG: Dict[str, GSC] = {
    "api_key": GsListStrConfig(
        "MiniMax API密钥",
        "指定 MiniMax API 的密钥，用于启用 web 搜索功能，请前往 https://platform.minimaxi.com 获取 API Key",
        [],
        options=[],
    ),
    "api_host": GsStrConfig(
        "MiniMax API 主机地址",
        "指定 MiniMax API 的主机地址",
        "https://api.minimaxi.com",
        options=["https://api.minimaxi.com"],
    ),
    "resource_mode": GsStrConfig(
        "资源提供方式",
        "指定资源提供方式，url 为返回 URL 链接，local 为返回本地文件路径",
        "url",
        options=["url", "local"],
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

OPENAI_EMBEDDING_CONFIG: Dict[str, GSC] = {
    "base_url": GsStrConfig(
        title="嵌入模型API基础URL",
        desc="指定OpenAI兼容格式的嵌入模型API基础URL, 注意一般是以 /v1 结尾",
        data="https://api.openai.com/v1",
        options=[
            "https://api.openai.com/v1",
            "https://api.siliconflow.cn/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://api.deepseek.com",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
    ),
    "api_key": GsListStrConfig(
        title="嵌入模型API密钥",
        desc="指定OpenAI兼容格式的嵌入模型API密钥, 支持添加多个",
        data=["sk-"],
        options=["sk-"],
    ),
    "embedding_model": GsStrConfig(
        title="嵌入模型名称",
        desc="指定嵌入模型名称, 该模型将会用于处理文本嵌入",
        data="text-embedding-3-small",
        options=[
            "text-embedding-3-small",
            "text-embedding-3-large",
            "text-embedding-ada-002",
            "BAAI/bge-m3",
            "BAAI/bge-large-zh-v1.5",
            "Pro/BAAI/bge-m3",
        ],
    ),
    "dimension": GsIntConfig(
        title="嵌入向量维度",
        desc=(
            "指定嵌入模型输出的向量维度。0 表示自动推断"
            "（OpenAI 官方模型可自动识别, 其它模型将在首次调用 API 时从响应推断）。"
            "切换到不同维度的模型时建议手动指定, 以确保向量库维度正确"
        ),
        data=0,
        options=[0, 256, 512, 768, 1024, 1536, 2048, 3072, 4096],
    ),
}

RERANK_MODEL_CONFIG: Dict[str, GSC] = {
    "rerank_model_name": GsStrConfig(
        "指定Rerank模型名称",
        "指定启用的Rerank模型名称。本地模式填写 fastembed 支持的模型名；远程模式填写服务商提供的 rerank 模型名",
        "BAAI/bge-reranker-base",
        options=[
            "BAAI/bge-reranker-base",
            "BAAI/bge-reranker-v2-m3",
            "bge-reranker-v2-m3",
            "jina-reranker-v2-base-multilingual",
            "rerank-multilingual-v3.0",
        ],
    ),
    "base_url": GsStrConfig(
        title="Rerank模型API基础URL",
        desc="指定远程 Rerank API 基础URL。一般为服务商 /v1 地址；程序会自动拼接 /rerank",
        data="https://api.siliconflow.cn/v1",
        options=[
            "https://api.siliconflow.cn/v1",
            "https://api.jina.ai/v1",
            "https://api.cohere.com/v1",
            "http://localhost:3000/v1",
            "http://127.0.0.1:3000/v1",
        ],
    ),
    "api_key": GsListStrConfig(
        title="Rerank模型API密钥",
        desc="指定远程 Rerank API 密钥，local 模式无需配置",
        data=["sk-"],
        options=["sk-"],
    ),
}

QDRANT_CONFIG: Dict[str, GSC] = {
    "url": GsStrConfig(
        title="Qdrant 远程服务地址",
        desc=(
            "指定远程 Qdrant 服务的 URL，例如 http://localhost:6333 或 "
            "https://xxxx.cloud.qdrant.io:6333。仅在 qdrant_provider 为 remote 时生效"
        ),
        data="http://localhost:6333",
        options=[
            "http://localhost:6333",
            "http://127.0.0.1:6333",
        ],
    ),
    "api_key": GsStrConfig(
        title="Qdrant API 密钥",
        desc="指定远程 Qdrant 服务的 API Key（可选，本地或无鉴权的服务可留空）",
        data="",
        secret=True,
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
    "MemoryRecall": GsDivider(
        "记忆检索设置",
        "记忆检索设置",
        "记忆检索设置",
    ),
    "retrieval_top_k": GsIntConfig(
        "最终检索数量",
        "指定最终检索数量, 可以提高检索精度但会增加性能开销",
        15,
        options=[5, 10, 15, 20],
    ),
    "memory_inject_max_chars": GsIntConfig(
        "记忆注入字符预算",
        "单次注入对话上下文的记忆文本最大字符数, 调大可保留更多历史但更费 Token",
        2000,
        options=[1000, 2000, 4000, 8000, 16000],
    ),
    "enable_system2get": GsBoolConfig(
        "是否启用 System-2",
        "指定是否启用 System-2, 可以提高检索精度但会增加性能开销",
        False,
    ),
    "MemoryRetrieval": GsDivider(
        "记忆摄取设置",
        "记忆摄取设置",
        "记忆摄取设置",
    ),
    "background_episode_count": GsIntConfig(
        "抽取背景片段数量",
        "实体抽取时注入的近期对话片段(Episode)数量, 用于跨批次指代消解; "
        "调小可显著降低每次抽取的 Token 开销(原始信息仍由 Episode 完整留存), 0 表示不注入背景",
        1,
        options=[0, 1, 2, 3],
    ),
    "background_episode_max_chars": GsIntConfig(
        "抽取背景片段字符上限",
        "每条注入的近期对话片段(Episode)在抽取提示词中的最大字符数, 超出部分截断; 调小可降低 Token 开销",
        600,
        options=[300, 600, 1000, 2000],
    ),
    "extraction_value_gate": GsStrConfig(
        "抽取价值门控档位",
        "决定哪些消息会触发 LLM 实体抽取(无论档位, 原文都完整存为 Episode, 不丢信息)。"
        "宽松: 默认全部抽取(最全, 最费 Token); "
        "均衡: 无实体特征的纯寒暄降级为仅存档不抽取; "
        "严格: 仅含强信号(姓名/承诺/情绪等)的消息才抽取(最省 Token)",
        "均衡",
        options=["宽松", "均衡", "严格"],
    ),
    "hiergraph_build_mode": GsStrConfig(
        "分层图构建模式",
        "分层类目树仅被 System-2 检索消费, 也是记忆重建 Token 的大头(实体/边/原文不受影响)。"
        "自动: 仅当启用 System-2 时才构建整棵类目树, 否则只按需刷新群摘要(推荐, 最省); "
        "始终: 总是构建完整类目树(旧行为); "
        "仅摘要: 从不建树, 仅按需刷新群摘要; "
        "关闭: 既不建树也不生成摘要(最省 Token)",
        "自动",
        options=["自动", "始终", "仅摘要", "关闭"],
    ),
    "hiergraph_batch_size": GsIntConfig(
        "分层图单批节点数",
        "建树时每次 LLM 分类的节点数。调大→单轮 LLM 调用更少、每批重发的固定开销(system+现有类目)"
        "被摊薄更省 Token; 但过大会拉长单次耗时、逼近超时(超时兜底会让每节点单独成类, 污染类目)。"
        "模型较慢时建议保持较小值",
        20,
        options=[15, 20, 30, 40],
    ),
    "hiergraph_vector_assign_threshold": GsStrConfig(
        "分层图向量预分配阈值",
        "建树时新实体与已归类近邻的余弦相似度 ≥ 此阈值即直接归类、跳过 LLM。"
        "调低→更多实体走零 LLM 的预分配路径、更省 Token, 但误归类风险上升(宁可漏分不可错分)",
        "0.85",
        options=["0.80", "0.82", "0.85", "0.88", "0.90"],
    ),
    "hiergraph_min_entities": GsIntConfig(
        "分层图最小实体门槛",
        "scope 实体数低于此值则整体跳过分层图(含轻量群摘要)。调大→更多小群被整体跳过、更省 Token; "
        "其召回仍由 System-1 向量 + edges 覆盖, 不影响记忆完整性",
        30,
        options=[30, 50, 80, 120, 200],
    ),
    "hiergraph_max_existing_cats": GsIntConfig(
        "分层图已有类目上限",
        "建树分类时每批最多带入的已有类目数(仅名称)。调小→每批 prompt 更省 Token, "
        "但过小会让 LLM 看不到已有类目而重复造新类目, 反而膨胀后续成本",
        50,
        options=[20, 30, 50, 80],
    ),
    "hiergraph_node_summary_chars": GsIntConfig(
        "分层图节点摘要字符上限",
        "建树分类时每个待分类节点附带的实体摘要字符数(名称+标签始终保留)。"
        "调小(含 0=不带摘要)更省 Token, 但摘要有助于消歧相近实体, 过小可能降低归类精度",
        60,
        options=[0, 30, 60, 100],
    ),
    "hiergraph_summary_delta": GsIntConfig(
        "群摘要刷新增量阈值",
        "自上次重建以来新增实体达此值才重新生成群摘要(Heartbeat/人格群语境消费)。"
        "调大→摘要刷新更稀疏、更省 Token, 代价是摘要新鲜度下降",
        50,
        options=[50, 100, 200, 500],
    ),
    "MemoryExtra": GsDivider(
        "记忆其他设置",
        "记忆其他设置",
        "记忆其他设置",
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

exa_config = StringConfig(
    "GsCore AI Exa搜索配置",
    get_res_path("ai_core") / "exa_config.json",
    EXA_CONFIG,
)

persona_config = StringConfig(
    "GsCore AI 人设配置",
    get_res_path("ai_core") / "persona_config.json",
    PERSONA_CONFIG,
)

minimax_config = StringConfig(
    "GsCore AI MiniMax搜索配置",
    get_res_path("ai_core") / "minimax_config.json",
    MINIMAX_CONFIG,
)

openai_embedding_config = StringConfig(
    "GsCore AI OpenAI嵌入模型配置",
    get_res_path("ai_core") / "openai_embedding_config.json",
    OPENAI_EMBEDDING_CONFIG,
)

qdrant_config = StringConfig(
    "GsCore AI Qdrant向量库配置",
    get_res_path("ai_core") / "qdrant_configs.json",
    QDRANT_CONFIG,
)

mcp_server_config = StringConfig(
    "GsCore AI MCP Server配置",
    get_res_path("ai_core") / "mcp_server_config.json",
    MCP_SERVER_CONFIG,
)
