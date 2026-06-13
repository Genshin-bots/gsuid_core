# MCP Server — 将 to_ai 触发器对外暴露为 MCP 服务

## 概述

本功能模块允许将 GsCore 框架中所有注册了 `to_ai` 参数的触发器，对外以 **MCP (Model Context Protocol) Server** 的形式暴露，使外部 AI 客户端（如 Claude Desktop、Cursor、其他 MCP 兼容客户端）可以直接调用框架的插件功能。

## 架构设计

```
┌─────────────────────────────────────────────────────────────────┐
│                        GsCore 框架                              │
│                                                                 │
│  插件触发器 (sv.on_command/to_ai)                                │
│       │                                                         │
│       ▼                                                         │
│  trigger_bridge._MCP_TRIGGER_REGISTRY                           │
│       │  (存储所有 to_ai 触发器的原始函数和元信息)                  │
│       │                                                         │
│       ▼                                                         │
│  mcp/server.py                                                  │
│       │  1. 读取 _MCP_TRIGGER_REGISTRY                          │
│       │  2. 为每个触发器创建 MCP Tool handler                    │
│       │  3. 使用 FastMCP 创建 MCP Server                        │
│       │  4. 以 SSE 或 stdio 协议启动服务                         │
│       ▼                                                         │
│  ┌─────────────────────────────────────────┐                    │
│  │         MCP Server (FastMCP)            │                    │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐   │                    │
│  │  │ Tool A  │ │ Tool B  │ │ Tool C  │   │                    │
│  │  │(触发器1)│ │(触发器2)│ │(触发器3)│   │                    │
│  │  └─────────┘ └─────────┘ └─────────┘   │                    │
│  └──────────────┬──────────────────────────┘                    │
│                 │                                               │
└─────────────────┼───────────────────────────────────────────────┘
                  │
          SSE / stdio
                  │
    ┌─────────────┼─────────────┐
    ▼             ▼             ▼
 Claude      Cursor        其他 MCP
 Desktop                   客户端
```

## 配置项

在 WebConsole → AI 配置 → **MCP Server 配置** 中提供以下独立配置项（不再放在主 AI 配置中）：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_mcp_server` | bool | `False` | 是否启用 MCP Server |
| `mcp_server_transport` | str | `"sse"` | 传输协议：`sse`（HTTP SSE）或 `stdio`（标准输入输出） |
| `mcp_server_port` | int | `8766` | SSE 模式下的监听端口（监听地址复用框架 HOST 配置） |
| `mcp_server_api_key` | str | `""` | Bearer Token 认证密钥，留空则不启用认证 |

## 使用方法

### 1. 启用 MCP Server

在 WebConsole 的 AI 配置页面中，将 `enable_mcp_server` 设置为 `True`，然后重启框架。

### 2. 配置 Claude Desktop 连接

在 Claude Desktop 的配置文件 `claude_desktop_config.json` 中添加：

```json
{
  "mcpServers": {
    "gscore": {
      "url": "http://localhost:8766/sse",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

> 如果未配置 `mcp_server_api_key`，则无需 `headers` 字段。

### 3. 配置 Cursor 连接

在 Cursor 的 MCP 配置中添加：

```json
{
  "mcpServers": {
    "gscore": {
      "url": "http://localhost:8766/sse",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

> 如果未配置 `mcp_server_api_key`，则无需 `headers` 字段。

### 4. 使用 stdio 模式（本地进程间通信）

如果选择 stdio 模式，在 AI 配置中将 `mcp_server_transport` 设置为 `"stdio"`，然后重启框架。框架启动时会自动以 stdio 模式运行 MCP Server。

> **注意**: stdio 模式下 MCP Server 与框架主进程共享标准输入输出，适合被本地 MCP 客户端（如 Claude Desktop）直接调用。SSE 模式则适合远程连接。

## 工具命名规则

每个 to_ai 触发器在 MCP Server 中的工具名称即为触发器函数的 `__name__`，描述即为 `to_ai` 参数中填写的文档字符串。

例如：

```python
@sv.on_command("个股", to_ai="""
查询指定股票或ETF的分时图/K线图。
Args:
    text: 股票代码或名称，多个以空格分隔
""")
async def send_stock_img(bot: Bot, ev: Event):
    ...
```

在 MCP Server 中会注册为：
- **工具名**: `send_stock_img`
- **描述**: `查询指定股票或ETF的分时图/K线图。Args: text: 股票代码或名称，多个以空格分隔`
- **参数**: `text: str` — 传递给触发器的文本；`image_id: str`、`audio_id: str` — 可选，传入已有资源 ID 作为参考输入

## 返回值（文本与图片）

工具调用的返回会按触发器的实际产出组装为 MCP content：

- **纯文本**：触发器通过 `ai_return()` 或 `bot.send(文字)` 产出的文本，作为 `TextContent` 返回。
- **图片**：触发器通过 `bot.send(图片)` 产出的图片，框架会取回其二进制并作为 **`ImageContent`** 一并返回，外部 MCP 客户端（Claude Desktop / Cursor 等）可**直接收到图片**，而非仅一段文字描述。
- **音频 / 视频**：受外部客户端支持度限制，暂仍以「文字描述 + 资源 ID」形式返回。

> 说明：图片二进制由框架内部的资源管理器（RM）临时持有（默认 TTL 30 分钟）。MCP 工具返回时即时取回并内联进 `ImageContent`，因此外部客户端无需、也无法再用资源 ID 二次回源。若图片在返回前已过期/解码失败，则该张图退回为文字提示，不影响整次调用的其余内容。

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `gsuid_core/ai_core/mcp/server.py` | **新增** | MCP Server 核心实现 |
| `gsuid_core/ai_core/trigger_bridge.py` | 修改 | 添加 `_MCP_TRIGGER_REGISTRY` 注册表 |
| `gsuid_core/ai_core/configs/ai_config.py` | 修改 | 将 MCP Server 配置项拆分为独立子配置 `MCP_SERVER_CONFIG` / `mcp_server_config` |
| `gsuid_core/ai_core/mcp/__init__.py` | 修改 | 导出新模块 |
| `docs/MCP_SERVER.md` | **新增** | 本文档 |

## 启动时序

```
框架启动
    │
    ▼
插件加载 → 触发器注册 → _register_trigger_as_ai_tool()
    │                        │
    │                        ├── 注册到 _TOOL_REGISTRY["by_trigger"]  (AI 工具)
    │                        └── 注册到 _MCP_TRIGGER_REGISTRY          (MCP Server 用)
    │
    ▼
on_core_start (priority=5)  → MCP 客户端工具注册
    │
    ▼
on_core_start (priority=10) → MCP Server 启动
    │                           ├── 读取 _MCP_TRIGGER_REGISTRY
    │                           ├── 创建 FastMCP 实例
    │                           ├── 注册所有触发器为 MCP Tools
    │                           └── 以 SSE/stdio 模式启动服务
    ▼
框架运行中...
```

## 安全说明

- **Bearer Token 认证**：通过 `mcp_server_api_key` 配置 API Key，外部客户端连接时需在请求头中携带 `Authorization: Bearer <api_key>`。留空则不启用认证
- **监听地址复用**：MCP Server 的监听地址复用框架的 `HOST` 配置，无需单独配置
- MCP Server 调用触发器时，默认使用 `user_pm=0`（master 权限），即拥有最高权限
- MCP Server 不经过 AI Agent 的权限检查流程，直接调用原始触发器函数
- 建议仅在可信网络环境中启用 MCP Server，或通过防火墙限制访问
- SSE 模式下，MCP Server 监听的端口应避免暴露到公网

## 与现有 MCP 客户端的关系

| 功能 | MCP Client（现有） | MCP Server（新增） |
|------|-------------------|-------------------|
| 方向 | 框架 → 外部 MCP 服务器 | 外部客户端 → 框架 |
| 用途 | AI 调用外部工具（搜索、图片理解等） | 外部 AI 调用框架插件功能 |
| 配置 | WebConsole MCP 配置页面 | AI 配置页面 |
| 工具来源 | 外部 MCP 服务器 | 框架 to_ai 触发器 |
