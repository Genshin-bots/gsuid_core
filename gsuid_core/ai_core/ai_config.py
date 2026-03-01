from typing import Dict
from pathlib import Path

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import GSC, GsStrConfig, GsBoolConfig, GsListStrConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

# from gsuid_core.utils.database.base_models import DB_PATH
DB_PATH = Path(__file__).parents[3] / "data" / "GsData.db"


AI_CONFIG: Dict[str, GSC] = {
    "enable": GsBoolConfig(
        "是否启用AI服务",
        "指定是否启用AI服务",
        False,
    ),
    "enable_chat": GsBoolConfig(
        "是否启用闲聊服务",
        "指定是否启用闲聊服务",
        False,
    ),
    "enable_qa": GsBoolConfig(
        "是否启用问答服务",
        "指定是否启用问答服务",
        False,
    ),
    "enable_task": GsBoolConfig(
        "是否启用工具服务",
        "指定是否启用工具服务",
        True,
    ),
    "provider": GsStrConfig(
        title="AI服务提供格式",
        desc="指定AI服务提供格式, 目前共有两种",
        data="openai",
        options=["openai", "gemini(暂不可用)"],
    ),
    "multi_round_lenth": GsStrConfig(
        "最多允许多轮对话长度",
        "指定多轮对话的长度, 注意: 多轮对话会占用更多的token, 请根据实际情况调整",
        "10",
        options=["10", "20", "30"],
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
    "need_at": GsBoolConfig(
        "需要@",
        "指定是否需要@才能触发AI服务",
        True,
    ),
}

OPENAI_CONFIG: Dict[str, GSC] = {
    "base_url": GsStrConfig(
        "OpenAI API基础URL",
        "指定OpenAI API的基础URL, 注意是以 /v1 结尾",
        "https://api.openai.com/v1",
        options=["https://api.openai.com/v1", "https://api.bltcy.ai/v1"],
    ),
    "api_key": GsListStrConfig(
        "OpenAI API密钥",
        "指定OpenAI API的密钥, 注意是以 sk- 开头, 不要泄露, 支持添加多个",
        ["sk-"],
        options=["sk-"],
    ),
    "level_s_model": GsStrConfig(
        "高级模型",
        "指定OpenAI API的高级模型, 该模型将会用于处理复杂任务, 通常是一个完整任务的最终调用模型",
        "gemini-3-flash-preview-nothinking",
        options=["gemini-3-flash-preview-nothinking"],
    ),
    "level_a_model": GsStrConfig(
        "中级模型",
        "指定OpenAI API的中级模型, 该模型将会用于处理中等任务, 该模型也会用于处理闲聊任务",
        "gemini-2.5-flash",
        options=["gemini-2.5-flash"],
    ),
    "level_b_model": GsStrConfig(
        "低级模型",
        "指定OpenAI API的低级模型, 该模型将会用于处理简单任务, 大多数任务都会优先调用该模型处理路由",
        "gpt-5-nano",
        options=["gpt-5-nano"],
    ),
    "embedding_model": GsStrConfig(
        "嵌入模型",
        "指定OpenAI API的嵌入模型, 该模型将会用于处理文本嵌入",
        "text-embedding-3-small",
        options=["text-embedding-3-small"],
    ),
}


GEMINI_CONFIG: Dict[str, GSC] = {
    "base_url": GsStrConfig(
        "基础URL",
        "指定Gemini API的基础URL, 注意是以 /v1 结尾",
        "https://api.gemini.com/v1",
        options=["https://api.gemini.com/v1"],
    ),
    "api_key": GsListStrConfig(
        "API密钥",
        "指定Gemini API的密钥, 注意是以 sk- 开头, 不要泄露, 支持添加多个",
        ["sk-"],
        options=["sk-"],
    ),
    "model": GsStrConfig(
        "模型",
        "指定Gemini API的模型",
        "gemini-1.5",
        options=["gemini-1.5"],
    ),
}

ai_config = StringConfig(
    "GsCore AI配置",
    get_res_path("AI_Core") / "ai_config.json",
    AI_CONFIG,
)


openai_config = StringConfig(
    "GsCore OpenAI配置",
    get_res_path("AI_Core") / "openai_config.json",
    OPENAI_CONFIG,
)

gemini_config = StringConfig(
    "GsCore Gemini配置",
    get_res_path("AI_Core") / "gemini_config.json",
    GEMINI_CONFIG,
)
