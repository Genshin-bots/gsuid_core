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
    transport: str = "stdio"                     # stdio / sse / streamable_http
    command: str                                 # 启动命令（stdio，如 "uvx"）
    args: list[str] = field(default_factory=list)  # 命令参数
    env: dict[str, str] = field(default_factory=dict)  # 环境变量
    url: str = ""                                # 远程 URL（sse / streamable_http）
    headers: dict[str, str] = field(default_factory=dict)  # HTTP 请求头
    enabled: bool = True                         # 是否启用
    register_as_ai_tools: bool = False           # 是否注册为 AI Tools
    tools: list[MCPToolDefinition] = field(default_factory=list)  # 工具列表
```

传输：`stdio` 走本地子进程；`sse` 为旧版 HTTP+SSE；`streamable_http` 是当前推荐的远程传输。
导入配置里的 `http` / `streamable-http` / `type: "http"` 会归一为 `streamable_http`。

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

提供统一的 Web 搜索接口：`web_search()` / `web_search_with_context()`。
实现位于 `gsuid_core/ai_core/web_search/`：

| 文件 | 用途 |
|------|------|
| `search.py` | 顶层调度：主用源 + 多源策略（错误切换 / 自动分流 / 无） |
| `tavily_search.py` | **Tavily**（需 API Key） |
| `jina_search.py` | **Jina** `https://s.jina.ai`（备选；**搜索需要 API Key**） |
| `exa_search.py` | Exa |
| `anysearch_search.py` | **AnySearch**（默认主用）`POST https://api.anysearch.com/v1/search`（Key 可选，匿名有每日免费额度） |
| MCP 分支 | `websearch_mcp_tool_id` 指向的 MCP 工具 |

调用方（插件 / 内置 `web_search_tool`）**只应** `from gsuid_core.ai_core.web_search import web_search`，不要直接绑死某一家 SDK。

配置热读（`ai_config.get_config(...)` 每次调用），控制台改完**无需重启 core**。

### 11.3.2 核心函数

```python
from gsuid_core.ai_core.web_search.search import web_search, web_search_with_context

async def web_search(
    query: str,
    max_results: int | None = None,
) -> list[dict]
# 每条: title / url / content / score；部分源可附 image_url / kind=image

async def web_search_with_context(
    query: str,
    max_results: int = 5,
) -> dict  # {"results": [...], "answer": str | None}  # answer 仅 Tavily 等支持
```

失败语义：

- 各 provider 在 **Key 未配置 / 全部 Key 失败（额度、鉴权）** 时 **抛异常**；
- 返回 **空列表 `[]`**（或 with_context 且无 results 且无 answer）也视为失败，触发换源；
- `search.py` 按多源策略捕获后切换下一源。全部失败则返回 `[]`（`with_context` 返回空 results）。

内置工具外层：`web_search_tool` 使用 `@ai_tools(..., timeout=100.0)`，覆盖多源串行 failover。

### 11.3.3 配置（`ai_config` / `data/ai_core/ai_config.json`）

| 配置项 | 类型 | 默认值 | 选项 / 说明 |
|--------|------|--------|-------------|
| `websearch_provider` | str | **`AnySearch`** | 主用：`AnySearch` / `Tavily` / `Jina` / `Exa` / `MCP`。未填或主用无 Key 时落到 AnySearch 匿名额度 |
| `websearch_lb_strategy` | str | **`error_switch`** | `none`：仅主用；`error_switch`：主用失败按备用顺序试下一源；`auto_balance`：已配置源间轮询 |
| `websearch_fallback_order` | list[str] | `[]` | 备用顺序（不含主用）。**空 = 自动收集所有已配置源**（顺序 AnySearch → Tavily → Exa → Jina → MCP） |
| `mcp_tools_config.websearch_mcp_tool_id` | str | `""` | provider=MCP 时必填，格式 `"{mcp_id} - {tool_name}"` |

各源密钥（独立 StringConfig，热读）：

| 配置名 | 路径 | 要点 |
|--------|------|------|
| `GsCore AI Tavily搜索配置` | `tavily_config.json` | `api_key` 池、`max_results`、`search_depth` |
| `GsCore AI Jina搜索抓取配置` | `jina_config.json` | `api_key` 池（**搜索必填**；抓取可选）、`max_results`、`timeout`、`search_base_url`（默认 `https://s.jina.ai`）、`reader_base_url`（抓取共用） |
| `GsCore AI Exa搜索配置` | `exa_config.json` | `api_key` 池、`max_results`、`search_type` |
| `GsCore AI AnySearch搜索配置` | `anysearch_config.json` | `api_key` 池（**可选**；空则匿名）、`max_results`（1–100）、`timeout`、`zone`（`cn`/`intl`，可空）、`language`（`zh-CN`/`en`，可空） |

### 11.3.4 多源策略行为

1. **构建链**：主用置首；`none` 时链仅含主用。
2. **error_switch**：按链顺序调用；某源**抛错**或**空结果**则试下一个；非空成功即返回。
3. **auto_balance**：在「已配置」源上轮询起点，再按 error_switch 语义失败切换。
4. **已配置判定**：Tavily/Exa/Jina 有非空 `api_key`；AnySearch **始终视为已配置**（可匿名）；MCP 有有效 `websearch_mcp_tool_id`。空备用链自动收集含 AnySearch。

日志关键字：`[WebSearch] 提供方 {provider} 失败`、`已切换到 {provider}`、`全部提供方失败`。

### 11.3.5 使用示例

```python
from gsuid_core.ai_core.web_search.search import web_search

results = await web_search("Python 教程", max_results=5)
for r in results:
    print(r["title"], r["url"], r["content"])
```

---

## 11.3b Web Fetch 网页抓取

### 11.3b.1 概述

`web_fetch_tool` → `gsuid_core.ai_core.web_fetch.fetch_webpage_as_markdown`。
支持 **Jina Reader**（默认）与 **local** 本机直连，并带与搜索同构的多源策略。

| 提供方 | 端点 / 方式 | API Key |
|--------|-------------|---------|
| **Jina**（默认主用） | `https://r.jina.ai/{url}` | **可选**；匿名有额度，填 Key 更高 |
| **local**（默认备用） | aiohttp 直连 + BS4/markdownify | 无；走 `web_fetch_config` 的 proxy/UA/超时 |

### 11.3b.2 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `webfetch_provider` | **`Jina`** | 主用：`Jina` / `local` |
| `webfetch_lb_strategy` | **`error_switch`** | 同搜索：`none` / `error_switch` / `auto_balance` |
| `webfetch_fallback_order` | **`["local"]`** | 备用顺序；空列表运行时也回落 `["local"]` |
| `web_fetch_config` | — | local 的 proxy / trust_env / timeout / UA / 体积上限 |
| `jina_config` | — | 与搜索共用；抓取用 `api_key`（可选）+ `reader_base_url` + `timeout` |

失败语义：提供方**抛错**或返回**空 Markdown** 时，在 `error_switch` / `auto_balance` 下切换下一源。

内置工具外层：`web_fetch_tool` 使用 `@ai_tools(..., timeout=100.0)`。

日志：开始即打
`[WebFetch] 开始抓取 provider=Jina|local url=... (...)`，
完成 `[WebFetch][Jina|local] 抓取完成`；失败切换见 `webfetch_provider_fail` / `webfetch_failover_ok`。

### 11.3b.3 使用示例

```python
from gsuid_core.ai_core.web_fetch import fetch_webpage_as_markdown

md = await fetch_webpage_as_markdown("https://example.com")
```

插件侧一般通过 Agent 调 `web_fetch_tool(url=...)`，无需直接 import。

---

## 11.4 Meme 表情包模块

### 11.4.1 概述

让 AI 具备「表情包意识」：自动采集群聊图片、智能打标、分类存储、智能发送。

> **详细设计文档**: 见 [MEME_MODULE.md](../../../../docs/MEME_MODULE.md)

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
