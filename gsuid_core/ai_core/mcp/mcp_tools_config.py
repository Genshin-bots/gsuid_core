"""
MCP 工具配置模块

管理所有业务模块使用的 MCP 工具配置。
配置 ID 格式为 "{mcp_id} - {tool_name}"，例如 "minimax - web_search"。

details 参数映射格式:
    details 字典的 **键** 为 MCP 工具的参数名，**值** 为映射来源:
    - `"params - <内部参数名>"` → 从内部函数的参数中取值
    - 字面量 (字符串 / 数字 / 布尔) → 固定值，每次调用都传入

    示例:
        {"query": "params - query", "results": "params - max_results", "search_model": "custom", "max": 6}

    含义:
    - MCP 的 `query` 参数 ← 内部 `query` 参数的值
    - MCP 的 `results` 参数 ← 内部 `max_results` 参数的值
    - MCP 的 `search_model` 参数 ← 固定值 `"custom"`
    - MCP 的 `max` 参数 ← 固定值 `6`

存储在 data/ai_core/mcp_tools_config.json
"""

from typing import Dict

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import GSC, GsStrConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

MCP_TOOLS_CONFIG: Dict[str, GSC] = {
    "websearch_mcp_tool_id": GsStrConfig(
        "Web Search MCP 工具",
        "指定 Web Search 使用的 MCP 工具，格式为 '{mcp_id} - {tool_name}'，例如 'minimax - web_search'",
        "",
        details={"query": "params - query", "max_results": "params - max_results"},
    ),
    "image_understand_mcp_tool_id": GsStrConfig(
        "Image Understand MCP 工具",
        "指定图片理解使用的 MCP 工具，格式为 '{mcp_id} - {tool_name}'，例如 'minimax - understand_image'",
        "",
        details={"image_source": "params - image_source", "prompt": "params - prompt"},
    ),
    "asr_mcp_tool_id": GsStrConfig(
        "ASR MCP 工具",
        "指定语音转文字(ASR)使用的 MCP 工具，格式为 '{mcp_id} - {tool_name}'，例如 'minimax - asr'",
        "",
        details={"audio_source": "params - audio_source", "language": "params - language"},
    ),
    "document_extract_mcp_tool_id": GsStrConfig(
        "Document Extract MCP 工具",
        "指定文档内容提取使用的 MCP 工具，格式为 '{mcp_id} - {tool_name}'，例如 'minimax - extract_document'",
        "",
        details={"file_source": "params - file_source", "page_range": "params - page_range"},
    ),
    "video_extract_mcp_tool_id": GsStrConfig(
        "Video Extract MCP 工具",
        "指定视频帧提取使用的 MCP 工具，格式为 '{mcp_id} - {tool_name}'，例如 'minimax - extract_video_frames'",
        "",
        details={
            "video_source": "params - video_source",
            "max_frames": "params - max_frames",
            "interval_seconds": "params - interval_seconds",
        },
    ),
    "video_understand_mcp_tool_id": GsStrConfig(
        "Video Understand MCP 工具",
        "指定视频理解使用的 MCP 工具，格式为 '{mcp_id} - {tool_name}'，例如 'minimax - understand_video'",
        "",
        details={"video_source": "params - video_source", "prompt": "params - prompt"},
    ),
}

mcp_tools_config = StringConfig(
    "GsCore AI MCP 工具配置",
    get_res_path("ai_core") / "mcp_tools_config.json",
    MCP_TOOLS_CONFIG,
)
