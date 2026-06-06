# MCP (Model Context Protocol) 模块

> 目录: `gsuid_core/ai_core/mcp/`

本模块实现了框架对 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) 的完整支持，包括客户端连接、工具调用、服务端暴露、配置管理和启动注册等功能。

## 架构概览

```
┌──────────────────────────────────────────────────────────┐
│                    业务模块 (调用方)                        │
│  web_search / image_understand / asr / document / video │
└──────────────────────┬───────────────────────────────────┘
                       │  调用复用函数
                       ▼
┌──────────────────────────────────────────────────────────┐
│                    utils.py (复用层)                       │
│  get_mcp_tool_id / call_mcp_tool_checked / save_binary   │
│  cleanup_tempfile / parse_binary_result / is_mcp_provider │
└──────────────────────┬───────────────────────────────────┘
                       │  内部调用
                       ▼
┌──────────────────────────────────────────────────────────┐
│               mcp_tool_caller.py (工具调用层)              │
│               call_mcp_tool()                            │
└──────────────────────┬───────────────────────────────────┘
                       │  构建客户端
                       ▼
┌──────────────────────────────────────────────────────────┐
│               client.py (MCP 客户端核心)                   │
│               MCPClient / MCPToolResult / MCPToolInfo     │
└──────────────────────┬───────────────────────────────────┘
                       │  读取配置
                       ▼
┌──────────────────────────────────────────────────────────┐
│             config_manager.py (配置管理)                   │
│             MCPConfig / MCPConfigManager / parse_mcp_tool_id │
└──────────────────────────────────────────────────────────┘
```

## 文件说明

### `__init__.py` — 模块入口

统一导出所有公共 API，外部代码只需 `from gsuid_core.ai_core.mcp import ...` 即可使用。

**导出的核心类:**
- `MCPClient` — MCP 客户端
- `MCPToolInfo` — MCP 工具信息
- `MCPToolResult` — MCP 工具调用结果

**导出的配置类:**
- `MCPConfig` / `MCPConfigManager` — 配置数据类与管理器
- `MCPToolDefinition` — 工具定义
- `mcp_config_manager` — 全局配置管理器实例
- `parse_mcp_tool_id` / `format_mcp_tool_id` — 工具 ID 解析/格式化

**导出的复用函数 (来自 utils.py):**
- `get_mcp_tool_id` — 获取并校验 MCP 工具 ID
- `call_mcp_tool_checked` — 调用 MCP 工具并自动校验错误
- `save_binary_to_tempfile` — 二进制数据保存为临时文件
- `cleanup_tempfile` — 安全删除临时文件
- `parse_binary_result` — 解析 MCP 返回的二进制数据
- `save_data_uri_to_tempfile` — DataURI 保存为临时文件
- `prepare_source_for_mcp` — 准备 MCP 所需的文件来源
- `is_mcp_provider` — 判断提供方是否为 MCP
- `MCP_PROVIDER` — MCP 提供方常量 `"MCP"`

---

### `client.py` — MCP 客户端核心

基于 [fastmcp](https://github.com/jlowin/fastmcp) 实现的 MCP 客户端，支持 **stdio** 和 **sse** 两种传输方式连接 MCP 服务器。

**设计原则:**
- **无状态模式**: 每次操作独立建立连接，完成后自动断开
- **完全异步**: 兼容项目的 async 架构
- **双传输模式**: 自动根据 url / command 字段推断使用 stdio 还是 sse
- **支持三种内容类型**: TextContent / ImageContent / EmbeddedResource

**核心类:**

| 类 | 说明 |
|---|------|
| `MCPClient` | MCP 客户端，支持 stdio（`command`+`args`+`env`）和 sse（`url`+`headers`）两种模式 |
| `MCPToolInfo` | 工具信息（name / description / input_schema） |
| `MCPToolResult` | 调用结果（content 列表 + is_error 标志 + `.text` 属性） |

**使用示例 (stdio):**
```python
client = MCPClient(
    name="MiniMax",
    command="uvx",
    args=["minimax-coding-plan-mcp"],
    env={"MINIMAX_API_KEY": "your_key"},
)
tools = await client.list_tools()
result = await client.call_tool("web_search", {"query": "Python"})
```

**使用示例 (sse):**
```python
client = MCPClient(
    name="知乎搜索",
    url="https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse",
    headers={"Authorization": "Bearer your_access_secret"},
)
tools = await client.list_tools()
result = await client.call_tool("zhihu_search", {"query": "RAG"})
```

---

### `mcp_tool_caller.py` — 通用工具调用层

封装 `MCPClient` 的创建和调用流程，提供更简洁的调用接口。

**核心函数:**

| 函数 | 说明 |
|---|------|
| `call_mcp_tool(mcp_tool_id, arguments)` | 通过工具 ID 调用 MCP 工具，自动解析 ID、获取配置、构建客户端 |

**工具 ID 格式:** `"{mcp_id} - {tool_name}"`，例如 `"minimax - web_search"`

---

### `utils.py` — 复用函数模块 🆕

**解决的问题:** 各业务模块（web_search / image_understand / asr / document / video）中都存在大量重复的 MCP 模式代码，例如:
- 获取 & 校验 MCP 工具 ID
- 调用 MCP 工具 & 检查错误
- 保存二进制数据到临时文件
- 清理临时文件
- 解析 MCP 返回的二进制数据

此模块将这些重复模式统一抽象为可复用函数，减少样板代码。

**核心函数:**

| 函数 | 说明 | 替代的旧模式 |
|---|------|-------------|
| `is_mcp_provider(provider)` | 判断提供方是否为 MCP | `if provider == "MCP"` |
| `get_mcp_tool_id(config_key, feature_name)` | 获取并校验 MCP 工具 ID | `mcp_tools_config.get_config(...).data` + 判空 |
| `get_mcp_tool_details(config_key)` | 获取 details 参数映射 | — |
| `build_mcp_arguments(config_key, internal_params)` | 根据 details 映射构建 MCP 工具参数 | 手工拼接 `arguments` 字典 |
| `call_mcp_tool_checked(mcp_tool_id, arguments, feature_name)` | 调用 MCP 工具并自动校验错误 | `await call_mcp_tool(...)` + `if result.is_error` |
| `save_binary_to_tempfile(data, suffix, log_prefix)` | 二进制数据保存为临时文件 | 各模块的 `_save_xxx_to_tempfile` |
| `cleanup_tempfile(path, log_prefix)` | 安全删除临时文件 | `finally` 块中的 `os.unlink` + 异常处理 |
| `parse_binary_result(result_text, media_type)` | 解析 MCP 返回的二进制数据 | 各模块的 `_parse_xxx_result` |
| `save_data_uri_to_tempfile(data_uri, log_prefix)` | DataURI → 临时文件 | `_prepare_image_for_mcp` 中的 DataURI 分支 |
| `prepare_source_for_mcp(source, log_prefix)` | URL/路径/DataURI → 文件路径 | `_prepare_image_for_mcp` 完整函数 |

**使用示例:**
```python
from gsuid_core.ai_core.mcp.utils import (
    is_mcp_provider, get_mcp_tool_id, build_mcp_arguments,
    call_mcp_tool_checked, save_binary_to_tempfile, cleanup_tempfile,
)

# ASR 模块重构前:
if provider == "MCP":
    mcp_tool_id = mcp_tools_config.get_config("asr_mcp_tool_id").data
    if not mcp_tool_id:
        raise RuntimeError("ASR MCP 工具未配置...")
    audio_path = await _prepare_audio_for_mcp(audio_data, audio_format)
    try:
        result = await call_mcp_tool(mcp_tool_id=mcp_tool_id, arguments=arguments)
        if result.is_error:
            raise RuntimeError(f"ASR MCP 调用失败: {result.text}")
        return result.text
    finally:
        if os.path.exists(audio_path):
            try: os.unlink(audio_path)
            except Exception as e: logger.warning(...)

# ASR 模块重构后:
if is_mcp_provider(provider):
    mcp_tool_id = get_mcp_tool_id("asr_mcp_tool_id", "ASR")
    audio_path = await save_binary_to_tempfile(audio_data, f".{audio_format}", "🎤 [ASR]")
    arguments = build_mcp_arguments(
        "asr_mcp_tool_id",
        {"audio_source": audio_path, "language": language},
    )
    try:
        result = await call_mcp_tool_checked(mcp_tool_id, arguments, "ASR")
        return result.text
    finally:
        cleanup_tempfile(audio_path, "🎤 [ASR]")
```

---

### `mcp_tools_config.py` — 工具配置模块

管理各业务模块使用的 MCP 工具 ID 配置和 **details 参数映射**。每个配置项存储：

- `data`: MCP 工具 ID，格式为 `"{mcp_id} - {tool_name}"`
- `details`: 参数映射字典，将内部函数参数名映射为 MCP 工具期望的参数名

**配置键一览:**

| 配置键 | 业务模块 | 说明 | 默认 details 映射 |
|---|--------|------|------|
| `websearch_mcp_tool_id` | web_search | Web 搜索 MCP 工具 | `{"query": "params - query", "max_results": "params - max_results"}` |
| `image_understand_mcp_tool_id` | image_understand | 图片理解 MCP 工具 | `{"image_source": "params - image_source", "prompt": "params - prompt"}` |
| `asr_mcp_tool_id` | multimodal/asr | 语音转文字 MCP 工具 | `{"audio_source": "params - audio_source", "language": "params - language"}` |
| `document_extract_mcp_tool_id` | multimodal/document | 文档提取 MCP 工具 | `{"file_source": "params - file_source", "page_range": "params - page_range"}` |
| `video_extract_mcp_tool_id` | multimodal/video | 视频帧提取 MCP 工具 | `{"video_source": "params - video_source", "max_frames": "params - max_frames", ...}` |
| `video_understand_mcp_tool_id` | multimodal/video | 视频理解 MCP 工具 | `{"video_source": "params - video_source", "prompt": "params - prompt"}` |

**details 参数映射格式:**

| 值的格式 | 含义 | 示例 |
|----------|------|------|
| `"params - <内部参数名>"` | 从内部函数的参数中取值 | `"params - query"` → 取内部 `query` 参数的值 |
| 字面量 (str/int/float/bool) | 固定值，每次调用都传入 | `"custom"` / `6` / `true` |
| `null` | 跳过该参数，不传给 MCP 工具 | |

**配置文件示例** (`mcp_tools_config.json`):
```json
{
    "websearch_mcp_tool_id": {
        "type": "GsStrConfig",
        "title": "Web Search MCP 工具",
        "desc": "...",
        "data": "minimax - web_search",
        "details": {
            "query": "params - query",
            "max_results": "params - max_results",
            "search_model": "custom",
            "max": 6
        }
    }
}
```

**存储位置:** `data/ai_core/mcp_tools_config.json`

**前端 API:** 通过 `/api/ai/mcp-tools-config/` 系列接口管理

---

### `config_manager.py` — MCP 服务器配置管理器

管理用户自定义的 MCP 服务器配置（增删改查），每个配置以独立 JSON 文件存储在 `data/ai_core/mcp_configs/` 目录下。

**核心类与函数:**

| 名称 | 说明 |
|---|------|
| `MCPConfig` | MCP 服务器配置数据类（name / command / args / env / enabled / tools / tool_permissions） |
| `MCPConfigManager` | 配置管理器，提供 `add_config` / `remove_config` / `get_config` / `list_configs` 等 |
| `MCPToolDefinition` | 工具定义（name / description / parameters） |
| `parse_mcp_tool_id(tool_id)` | 解析 `"{mcp_id} - {tool_name}"` 为元组 |
| `format_mcp_tool_id(mcp_id, tool_name)` | 格式化为工具 ID 字符串 |
| `mcp_config_manager` | 全局配置管理器单例 |

**配置文件格式:**
```json
{
    "name": "MiniMax",
    "command": "uvx",
    "args": ["minimax-coding-plan-mcp"],
    "env": {"MINIMAX_API_KEY": "your_key"},
    "enabled": true,
    "register_as_ai_tools": false,
    "tools": [
        {"name": "web_search", "description": "Web search tool"}
    ],
    "tool_permissions": {"send_email": 0, "query_data": 6}
}
```

---

### `startup.py` — 启动注册模块

框架启动时，读取所有启用的 MCP 配置，连接 MCP 服务器获取工具列表，并将每个 MCP 工具动态注册为 AI 工具（ai_tools），使 AI 可以自由调用。

**注册流程:**
1. 从 `mcp_config_manager` 获取所有 `enabled` 的配置
2. 对每个配置，创建 `MCPClient` 并获取工具列表
3. 为每个 MCP 工具动态创建包装函数并注册到 `_TOOL_REGISTRY`

**核心函数:**

| 函数 | 说明 |
|---|------|
| `register_all_mcp_tools()` | 注册所有启用的 MCP 工具为 AI Tools |
| `register_single_mcp_server(mcp_id)` | 注册单个 MCP 服务器的工具 |
| `unregister_mcp_server(mcp_id)` | 取消注册指定 MCP 服务器的工具 |

---

### `server.py` — MCP Server 模块

将框架的 `to_ai` 触发器对外暴露为 MCP 服务。启用后，外部 MCP 客户端（如 Claude Desktop、Cursor 等）可通过 SSE 或 stdio 协议连接到本框架。

**配置项:**
- `enable_mcp_server` — 是否启用 MCP Server（默认 False）
- `mcp_server_transport` — 传输协议 `"sse"` | `"stdio"`（默认 `"sse"`）
- `mcp_server_port` — SSE 监听端口（默认 8766）
- `mcp_server_api_key` — Bearer Token 认证密钥（留空不认证）

**核心函数:**

| 函数 | 说明 |
|---|------|
| `get_mcp_server()` | 获取 MCP Server 实例 |
| `get_mcp_trigger_count()` | 获取已注册的 MCP 触发器数量 |

---

### `mcp_presets.py` — 预设配置

提供常见 MCP 服务提供方的预设配置，方便用户通过 WebConsole 快速添加。

**预设列表（部分）:**

| 名称 | 功能 | 启动命令 |
|---|------|---------|
| Tavily | AI 搜索 | `uvx tavily-mcp` |
| Brave Search | 隐私搜索 | `uvx brave-search-mcp` |
| Exa | 语义搜索 | `uvx exa-mcp` |
| MiniMax | 搜索 + 图片理解 | `uvx minimax-coding-plan-mcp` |
| Firecrawl | 网页抓取 | `uvx firecrawl-mcp` |
| GitHub | 代码仓库管理 | `npx @modelcontextprotocol/server-github` |
| ... | ... | ... |

---

## 调用链路

### 业务模块 → MCP 工具调用

```
业务模块 (asr/document/video/search/understand)
  │
  ├─ is_mcp_provider(provider)         # 判断是否走 MCP
  ├─ get_mcp_tool_id(config_key, name) # 获取工具 ID
  ├─ save_binary_to_tempfile(...)       # 保存临时文件 (如需)
  ├─ call_mcp_tool_checked(...)         # 调用 MCP 工具
  │     └─ call_mcp_tool(...)
  │           └─ MCPClient.call_tool(...)
  │                 └─ fastmcp.Client → MCP Server (stdio)
  ├─ parse_binary_result(...)          # 解析返回数据 (如需)
  └─ cleanup_tempfile(...)              # 清理临时文件 (finally)
```

### 外部客户端 → 框架 MCP Server

```
Claude Desktop / Cursor / 其他 MCP 客户端
  │
  └─ SSE / stdio 连接
        └─ FastMCP Server (server.py)
              └─ 调用 _MCP_TRIGGER_REGISTRY 中注册的触发器
                    └─ 返回结果给外部客户端
```

## 扩展指南

### 添加新的 MCP 业务模块

1. 在 `mcp_tools_config.py` 的 `MCP_TOOLS_CONFIG` 中添加新的配置键
2. 在业务模块中导入 `utils.py` 的复用函数：
   ```python
   from gsuid_core.ai_core.mcp.utils import (
       is_mcp_provider,
       get_mcp_tool_id,
       call_mcp_tool_checked,
       save_binary_to_tempfile,
       cleanup_tempfile,
   )
   ```
3. 使用复用函数编写 MCP 分支逻辑，**不要**重复实现临时文件保存/清理/错误检查等

### 添加新的 MCP 预设

在 `mcp_presets.py` 的 `MCP_PRESETS` 列表中添加新的预设字典即可，格式参见已有预设。
