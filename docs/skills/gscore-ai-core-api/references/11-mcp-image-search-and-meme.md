# 十一、MCP 工具集成 + Image Understand + Web Search + Meme 表情包

## 11.1 MCP 工具集成

### 11.1.1 概述

MCP (Model Context Protocol) 模块允许通过 MCP 协议集成外部工具服务器。用户可通过 WebConsole API 添加 MCP 服务器配置，框架启动时自动连接服务器并将工具注册为 AI 工具。

### 11.1.2 模块导入

```python
from gsuid_core.ai_core.mcp import (
    MCPClient,               # MCP 客户端
    MCPConfig,               # MCP 配置数据类
    MCPToolDefinition,       # MCP 工具定义
    mcp_config_manager,      # 全局配置管理器单例
    parse_mcp_tool_id,       # 解析 "{mcp_id} - {tool_name}" 格式
    format_mcp_tool_id,      # 格式化 MCP 工具 ID
    register_all_mcp_tools,  # 注册所有 MCP 工具
    register_single_mcp_server,  # 注册单个 MCP 服务器
    unregister_mcp_server,   # 注销 MCP 服务器
)

from gsuid_core.ai_core.mcp.mcp_tool_caller import call_mcp_tool
from gsuid_core.ai_core.mcp.mcp_tools_config import mcp_tools_config
```

### 11.1.3 `MCPConfig` 数据类

```python
@dataclass
class MCPConfig:
    name: str                                    # 服务器名称
    command: str                                 # 启动命令（如 "uvx"）
    args: list[str] = field(default_factory=list)  # 命令参数
    env: dict[str, str] = field(default_factory=dict)  # 环境变量
    enabled: bool = True                         # 是否启用
    register_as_ai_tools: bool = False           # 是否注册为 AI Tools
    tools: list[MCPToolDefinition] = field(default_factory=list)  # 工具列表
```

### 11.1.4 MCP 工具 ID 格式

MCP 工具 ID 格式为 `{mcp_id} - {tool_name}`，例如 `minimax - web_search`。

```python
from gsuid_core.ai_core.mcp import parse_mcp_tool_id, format_mcp_tool_id

mcp_id, tool_name = parse_mcp_tool_id("minimax - web_search")
# mcp_id = "minimax", tool_name = "web_search"

tool_id = format_mcp_tool_id("minimax", "web_search")
# "minimax - web_search"
```

### 11.1.5 通用 MCP 工具调用

无需将工具注册为 AI Tools，直接通过 `call_mcp_tool()` 调用：

```python
from gsuid_core.ai_core.mcp.mcp_tool_caller import call_mcp_tool

result = await call_mcp_tool(
    mcp_tool_id="minimax - web_search",
    arguments={"query": "Python 教程"},
)
print(result.text)  # 工具返回的文本结果
```

### 11.1.6 配置管理

```python
from gsuid_core.ai_core.mcp import mcp_config_manager

# 列出所有配置
configs = mcp_config_manager.list_configs()

# 获取启用的配置
enabled = mcp_config_manager.get_enabled_configs()

# 获取指定配置
config = mcp_config_manager.get_config("minimax")

# 创建配置
from gsuid_core.ai_core.mcp import MCPConfig
success, msg = mcp_config_manager.create_config("my_server", MCPConfig(
    name="MyServer",
    command="uvx",
    args=["my-mcp-server"],
    env={"API_KEY": "xxx"},
))

# 列出所有工具
tools = mcp_config_manager.list_all_tools()
```

---

## 11.2 Image Understand 图片理解

### 11.2.1 概述

提供统一的图片理解接口，将图片内容转述为文本描述。当 LLM 模型不支持图片输入时，`GsCoreAIAgent._prepare_user_message()` 会自动调用此模块。

### 11.2.2 核心函数

```python
from gsuid_core.ai_core.image_understand import understand_image

async def understand_image(
    image_url: str,           # 图片来源（HTTP URL 或 base64 DataURI）
    prompt: str | None = None,  # 对图片的提问，默认为通用描述
) -> str:                     # 图片内容的文本描述
```

### 11.2.3 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `image_understand_provider` | str | `"MCP"` | 图片理解服务提供方（目前仅支持 MCP） |
| `mcp_tools_config.image_understand_mcp_tool_id` | str | `""` | MCP 工具 ID，格式 `"{mcp_id} - {tool_name}"` |

### 11.2.4 使用示例

```python
from gsuid_core.ai_core.image_understand import understand_image

# 通用描述
description = await understand_image("https://example.com/image.png")

# 自定义提问
answer = await understand_image(
    "https://example.com/chart.png",
    prompt="这张图表显示了什么数据趋势？",
)
```

---

## 11.3 Web Search 统一搜索

### 11.3.1 概述

提供统一的 Web 搜索接口，根据用户配置自动选择搜索引擎（Tavily / Exa / MCP）。

### 11.3.2 核心函数

```python
from gsuid_core.ai_core.web_search.search import web_search, web_search_with_context

async def web_search(
    query: str,
    max_results: int | None = None,
) -> list[dict]

async def web_search_with_context(
    query: str,
    max_results: int = 5,
) -> dict  # {"results": [...], "answer": "..."}
```

### 11.3.3 配置

| 配置项 | 类型 | 默认值 | 选项 | 说明 |
|--------|------|--------|------|------|
| `websearch_provider` | str | `"Tavily"` | `Tavily` / `Exa` / `MCP` | Web 搜索服务提供方 |
| `mcp_tools_config.websearch_mcp_tool_id` | str | `""` | — | MCP 搜索工具 ID（provider=MCP 时必填） |

### 11.3.4 使用示例

```python
from gsuid_core.ai_core.web_search.search import web_search

results = await web_search("Python 教程", max_results=5)
for r in results:
    print(r["title"], r["url"], r["content"])
```

---

## 11.4 Meme 表情包模块

### 11.4.1 概述

让 AI 具备「表情包意识」：自动采集群聊图片、智能打标、分类存储、智能发送。

> **详细设计文档**: 见 [MEME_MODULE.md](../../MEME_MODULE.md)

### 11.4.2 AI 工具

| 工具 | category | 说明 |
|------|----------|------|
| `send_meme` | `buildin` | 根据情绪/场景智能选取并发送表情包 |
| `collect_meme` | `buildin` | 手动收集表情包入库 |
| `search_meme` | `buildin` | 搜索表情包库 |

三个工具的完整签名见 [§7.3 Common 工具](./07-builtin-tools.md)。

### 11.4.3 集成点

- `handler.py` 中通过 `asyncio.create_task(observe_message_for_memes(event))` 异步采集群聊图片
- `handle_ai.py` 中导入 `meme.startup` 和 `meme_tools` 以注册 `@on_core_start` 钩子和 `@ai_tools`
