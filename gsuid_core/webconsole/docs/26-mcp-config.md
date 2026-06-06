# 26. MCP Config API - /api/ai/mcp

> MCP 配置 API 提供对 MCP (Model Context Protocol) 服务器配置的管理能力。用户可以通过这些 API 自由添加、编辑、删除 MCP 服务器配置。**所有增删改和 toggle 操作会自动触发实时工具注册/注销，无需重启服务或手动调用 reload。**

## MCP 配置说明

每个 MCP 配置对应一个 MCP 服务器，配置以 JSON 文件形式存储在 `data/ai_core/mcp_configs/` 目录下。

支持两种传输方式：
- **stdio** — 通过命令行启动本地 MCP 服务器（`command` + `args` + `env`）
- **sse** — 通过 HTTP/SSE 连接远程 MCP 服务器（`url` + `headers`）

**配置字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | string | 是 | MCP 服务器显示名称 |
| transport | string | 否 | 传输方式：`"stdio"` 或 `"sse"`，默认 `"stdio"`。若未显式设置，会根据 `url` 字段自动推断 |
| command | string | stdio 模式必填 | 启动命令，如 `uvx`, `npx`, `python` |
| args | array | 否 | 命令参数列表（stdio 模式），默认 `[]` |
| env | object | 否 | 环境变量字典（stdio 模式），默认 `{}` |
| url | string | sse 模式必填 | SSE 服务器 URL，如 `https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse` |
| headers | object | 否 | HTTP 请求头字典（sse 模式），如 `{"Authorization": "Bearer xxx"}`，默认 `{}` |
| enabled | boolean | 否 | 是否启用，默认 `true` |
| register_as_ai_tools | boolean | 否 | 是否将 MCP 工具注册为 AI 工具，默认 `false` |
| tools | array | 否 | 工具定义列表，默认 `[]` |
| tool_permissions | object | 否 | 工具权限配置，键为工具名，值为权限等级，默认 `{}` |

**配置文件示例 (stdio)**：
```json
{
    "name": "MiniMax",
    "transport": "stdio",
    "command": "uvx",
    "args": ["minimax-coding-plan-mcp"],
    "env": {"MINIMAX_API_KEY": "your_key"},
    "enabled": true,
    "register_as_ai_tools": false,
    "tools": [
        {
            "name": "search_code",
            "description": "搜索代码",
            "parameters": {
                "query": {"type": "string", "description": "搜索关键词"}
            }
        }
    ],
    "tool_permissions": {
        "search_code": 6
    }
}
```

**配置文件示例 (sse)**：
```json
{
    "name": "知乎搜索",
    "transport": "sse",
    "url": "https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse",
    "headers": {"Authorization": "Bearer your_access_secret"},
    "enabled": true,
    "register_as_ai_tools": false,
    "tools": [
        {
            "name": "zhihu_search",
            "description": "搜索知乎站内内容",
            "parameters": {
                "query": {"type": "string", "description": "搜索关键词", "required": true},
                "count": {"type": "integer", "description": "返回条数 1-10", "required": false}
            }
        }
    ]
}
```

---

## 26.1 获取 MCP 配置列表

```
GET /api/ai/mcp/list
```

**请求头**：
```
Authorization: Bearer <token>
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "configs": [
            {
                "config_id": "minimax",
                "name": "MiniMax",
                "command": "uvx",
                "args": ["minimax-coding-plan-mcp"],
                "env": {"MINIMAX_API_KEY": "***"},
                "enabled": true
            }
        ],
        "count": 1
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功 |
| msg | string | 状态信息 |
| data.configs | array | MCP 配置列表 |
| data.configs[].config_id | string | 配置 ID（文件名不含扩展名） |
| data.configs[].name | string | MCP 服务器名称 |
| data.configs[].command | string | 启动命令 |
| data.configs[].args | array | 命令参数 |
| data.configs[].env | object | 环境变量 |
| data.configs[].enabled | boolean | 是否启用 |
| data.count | integer | 配置总数 |

---

## 26.2 获取 MCP 配置详情

```
GET /api/ai/mcp/{config_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| config_id | string | 是 | 配置 ID |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "config_id": "minimax",
        "name": "MiniMax",
        "command": "uvx",
        "args": ["minimax-coding-plan-mcp"],
        "env": {"MINIMAX_API_KEY": "***"},
        "enabled": true
    }
}
```

**错误响应**（配置不存在）：
```json
{
    "status": 1,
    "msg": "MCP 配置 'xxx' 不存在",
    "data": null
}
```

---

## 26.3 创建 MCP 配置

```
POST /api/ai/mcp
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: application/json
```

**请求体**：
```json
{
    "name": "MiniMax",
    "command": "uvx",
    "args": ["minimax-coding-plan-mcp"],
    "env": {"MINIMAX_API_KEY": "your_key"},
    "enabled": true,
    "register_as_ai_tools": false,
    "tools": [],
    "tool_permissions": {}
}
```

**请求体字段说明**：
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| name | string | 是 | - | MCP 服务器名称，同时用于生成 config_id |
| transport | string | 否 | `"stdio"` | 传输方式：`"stdio"` 或 `"sse"`，未设置时自动推断 |
| command | string | stdio 模式必填 | `""` | 启动命令 |
| args | array | 否 | `[]` | 命令参数（stdio 模式） |
| env | object | 否 | `{}` | 环境变量（stdio 模式） |
| url | string | sse 模式必填 | `""` | SSE 服务器 URL |
| headers | object | 否 | `{}` | HTTP 请求头（sse 模式，如 `{"Authorization": "Bearer xxx"}`） |
| enabled | boolean | 否 | `true` | 是否启用 |
| register_as_ai_tools | boolean | 否 | `false` | 是否将 MCP 工具注册为 AI 工具 |
| tools | array | 否 | `[]` | 工具定义列表，见下方工具定义格式 |
| tool_permissions | object | 否 | `{}` | 工具权限配置，键为工具名，值为权限等级 |

**SSE 模式创建示例**：
```json
{
    "name": "知乎搜索",
    "transport": "sse",
    "url": "https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse",
    "headers": {"Authorization": "Bearer your_access_secret"},
    "enabled": true,
    "register_as_ai_tools": false,
    "tools": [],
    "tool_permissions": {}
}
```

**工具定义格式**：
```json
{
    "name": "tool_name",
    "description": "工具描述",
    "parameters": {
        "param1": {"type": "string", "description": "参数1"}
    }
}
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "config_id": "minimax",
        "name": "MiniMax",
        "tool_count": 3,
        "register_msg": "注册完成，共 3 个工具"
    }
}
```

**错误响应**（配置已存在）：
```json
{
    "status": 1,
    "msg": "配置 'minimax' 已存在",
    "data": null
}
```

**说明**：
- `config_id` 由 `name` 自动生成：转小写，特殊字符替换为下划线
- 创建后会自动连接 MCP 服务器并实时注册工具，无需手动 reload
- `tool_count` 表示成功注册的工具数量

---

## 26.4 更新 MCP 配置

```
PUT /api/ai/mcp/{config_id}
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: application/json
```

**路径参数**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| config_id | string | 是 | 配置 ID |

**请求体**（只需传要更新的字段）：
```json
{
    "command": "npx",
    "args": ["-y", "minimax-mcp"],
    "env": {"MINIMAX_API_KEY": "new_key"}
}
```

**请求体字段说明**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | string | 否 | MCP 服务器名称 |
| transport | string | 否 | 传输方式：`"stdio"` 或 `"sse"` |
| command | string | 否 | 启动命令 |
| args | array | 否 | 命令参数 |
| env | object | 否 | 环境变量 |
| url | string | 否 | SSE 服务器 URL |
| headers | object | 否 | HTTP 请求头（sse 模式） |
| enabled | boolean | 否 | 是否启用 |
| register_as_ai_tools | boolean | 否 | 是否将 MCP 工具注册为 AI 工具 |
| tools | array | 否 | 工具定义列表 |
| tool_permissions | object | 否 | 工具权限配置 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "config_id": "minimax",
        "tool_count": 5,
        "register_msg": "注册完成，共 5 个工具"
    }
}
```

**说明**：
- 只更新请求体中提供的字段，未提供的字段保持不变
- 更新后会自动重新连接 MCP 服务器并实时重新注册工具，无需手动 reload

---

## 26.5 删除 MCP 配置

```
DELETE /api/ai/mcp/{config_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| config_id | string | 是 | 配置 ID |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "config_id": "minimax",
        "removed_tool_count": 3
    }
}
```

**错误响应**（配置不存在）：
```json
{
    "status": 1,
    "msg": "配置 'xxx' 不存在",
    "data": null
}
```

**说明**：
- 删除操作会同时删除配置文件、内存缓存，并实时注销已注册的 MCP 工具
- `removed_tool_count` 表示被移除的工具数量

---

## 26.6 切换启用/禁用状态

```
POST /api/ai/mcp/{config_id}/toggle
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| config_id | string | 是 | 配置 ID |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "config_id": "minimax",
        "enabled": false,
        "tool_count": 0,
        "register_msg": "已禁用，移除了 3 个工具"
    }
}
```

**说明**：
- 此接口会将当前状态取反（启用→禁用，禁用→启用）
- 切换后会自动实时注册或注销工具：启用时连接服务器注册工具，禁用时注销已注册工具

---

## 26.7 热重载所有配置

```
POST /api/ai/mcp/reload
```

**请求头**：
```
Authorization: Bearer <token>
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "old_tool_count": 5,
        "new_tool_count": 8,
        "config_count": 2
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.old_tool_count | integer | 重载前已注册的 MCP 工具数量 |
| data.new_tool_count | integer | 重载后注册的 MCP 工具数量 |
| data.config_count | integer | 当前配置总数 |

**说明**：
- 此接口会清除所有已注册的 MCP 工具，重新加载配置文件，并重新连接所有启用的 MCP 服务器注册工具
- 通常不需要手动调用此接口，因为增删改和 toggle 操作已自动触发实时注册/注销
- 仅在需要批量刷新所有 MCP 工具时使用（如手动编辑了配置文件后）
- 重载过程中如果某个 MCP 服务器连接失败，不会影响其他服务器的注册

---

## 26.8 获取 MCP 预设配置

```
GET /api/ai/mcp/presets
```

**描述**: 获取常用的 MCP 服务提供商预设配置，用户可以快速添加。预设包含默认的 command、args，但不包含实际的环境变量值。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "presets": {
            "minimax": {
                "name": "MiniMax",
                "command": "uvx",
                "args": ["minimax-coding-plan-mcp"],
                "env": {"MINIMAX_API_KEY": ""}
            }
        },
        "count": 1
    }
}
```

---

## 26.9 从已配置服务器发现工具

```
GET /api/ai/mcp/{config_id}/tools
```

**描述**: 连接已配置的 MCP 服务器并列出其提供的所有工具，包括工具名称、描述和参数定义。发现的工具可以用于更新配置中的 tools 列表。

**路径参数**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| config_id | string | 是 | 配置 ID |

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "config_id": "minimax",
        "tools": [
            {
                "name": "search_code",
                "description": "搜索代码",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"}
                    }
                }
            }
        ],
        "count": 1
    }
}
```

**响应**（配置不存在）：
```json
{
    "status": 1,
    "msg": "MCP 配置 'xxx' 不存在",
    "data": null
}
```

---

## 26.10 从临时配置发现工具

```
POST /api/ai/mcp/tools/discover
```

**描述**: 用户输入 MCP 服务器配置后，先连接服务器发现其提供的工具，确认后再决定是否保存配置。此接口不会保存配置。

**请求体**：
```json
{
    "name": "TestServer",
    "command": "uvx",
    "args": ["test-mcp"],
    "env": {"API_KEY": "test_key"}
}
```

**请求体字段说明**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | string | 是 | MCP 服务器名称 |
| command | string | 是 | 启动命令 |
| args | array | 否 | 命令参数，默认 `[]` |
| env | object | 否 | 环境变量，默认 `{}` |

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "tools": [
            {
                "name": "search_code",
                "description": "搜索代码",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "title": "Query"}
                    },
                    "required": ["query"]
                },
                "parameters": {
                    "query": {"type": "string", "description": "Query", "required": true}
                }
            }
        ],
        "count": 1
    }
}
```

**响应字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 工具名称 |
| description | string | 工具描述 |
| input_schema | object | MCP 工具的原始 JSON Schema 输入定义 |
| parameters | object | 从 `input_schema` 自动转换的扁平化参数字典，可直接传给创建接口 |

**前端使用建议**：discover 返回的 `parameters` 字段可直接作为创建配置 `POST /api/ai/mcp` 时 `tools[].parameters` 的值，无需前端手动转换。

**响应**（连接失败）：
```json
{
    "status": 1,
    "msg": "连接 MCP 服务器失败: ...",
    "data": null
}
```

---

## 26.11 从 JSON 配置导入 MCP 服务器

```
POST /api/ai/mcp/tools/import
```

**描述**: 支持粘贴 MCP 官方格式的 JSON 配置（如 MiniMax MCP 的配置），自动解析并创建配置。

**请求体**：
```json
{
    "json_config": "{\"mcpServers\":{\"MiniMax\":{\"command\":\"uvx\",\"args\":[\"minimax-coding-plan-mcp\"],\"env\":{\"MINIMAX_API_KEY\":\"your_key\"}}}}"
}
```

**请求体字段说明**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| json_config | string | 是 | MCP 官方格式的 JSON 配置字符串，需包含 `mcpServers` 字段 |

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "config_id": "minimax",
        "name": "MiniMax",
        "tools_count": 3,
        "tool_names": ["search_code", "run_code", "explain_code"]
    }
}
```

**响应**（JSON 格式无效）：
```json
{
    "status": 1,
    "msg": "无效的 JSON 格式",
    "data": null
}
```

**响应**（配置已存在）：
```json
{
    "status": 1,
    "msg": "配置 'minimax' 已存在，请先删除或重命名",
    "data": null
}
```

**说明**：
- 只处理 `mcpServers` 中的第一个服务器
- 导入时会自动连接服务器发现工具列表
- 如果连接失败，配置仍会创建，但 tools 列表为空

---

## 26.11 MCP 工具参数映射配置

MCP 工具配置 API (`/api/ai/mcp-tools-config`) 用于管理各业务模块对接 MCP 工具时的参数映射。

### 背景说明

不同的 MCP 工具对外提供的参数名不同，而框架内部函数的参数名是固定的。例如内部 `web_search` 函数的参数叫 `query` 和 `max_results`，但某个 MCP 工具可能叫 `query_context` 和 `limit`。

通过 `details` 字段建立映射关系，框架在调用 MCP 工具时会自动将内部参数转换为 MCP 工具期望的参数。

**details 映射规则**：

| 值的格式 | 含义 | 示例 |
|----------|------|------|
| `"params - <内部参数名>"` | 从内部函数的参数中取值 | `"params - query"` → 取内部 `query` 参数的值 |
| 字面量 (str/int/float/bool) | 固定值，每次调用都传入 | `"custom"` / `6` / `true` |
| `null` | 跳过该参数，不传给 MCP 工具 | |

**配置示例** (`mcp_tools_config.json`)：
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

含义：
- MCP 的 `query` 参数 ← 内部 `query` 参数的值
- MCP 的 `max_results` 参数 ← 内部 `max_results` 参数的值
- MCP 的 `search_model` 参数 ← 固定值 `"custom"`
- MCP 的 `max` 参数 ← 固定值 `6`

---

### 26.11.1 获取 MCP 工具配置列表

```
GET /api/ai/mcp-tools-config/list
```

**请求头**：
```
Authorization: Bearer <token>
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [
            {
                "key": "websearch_mcp_tool_id",
                "title": "Web Search MCP 工具",
                "desc": "指定 Web Search 使用的 MCP 工具...",
                "data": "minimax - web_search",
                "details": {
                    "query": "params - query",
                    "max_results": "params - max_results"
                }
            }
        ],
        "count": 6
    }
}
```

---

### 26.11.2 获取指定 MCP 工具配置详情

```
GET /api/ai/mcp-tools-config/{item_key}
```

**路径参数**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| item_key | string | 是 | 配置项键名，如 `websearch_mcp_tool_id` |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "key": "websearch_mcp_tool_id",
        "title": "Web Search MCP 工具",
        "desc": "指定 Web Search 使用的 MCP 工具...",
        "data": "minimax - web_search",
        "details": {
            "query": "params - query",
            "max_results": "params - max_results"
        }
    }
}
```

---

### 26.11.3 更新 MCP 工具配置（含 details 参数映射）

```
PUT /api/ai/mcp-tools-config/{item_key}
```

**路径参数**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| item_key | string | 是 | 配置项键名 |

**请求体**：
```json
{
    "data": "brave - brave_web_search",
    "details": {
        "query": "params - query",
        "count": "params - max_results",
        "search_model": "custom",
        "max": 6
    }
}
```

**请求体字段说明**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| data | string | 否 | MCP 工具 ID，格式为 `"{mcp_id} - {tool_name}"` |
| details | object | 否 | 参数映射字典，键为 MCP 参数名，值为映射规则 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "key": "websearch_mcp_tool_id",
        "updated_fields": ["data", "details"],
        "data": "brave - brave_web_search",
        "details": {
            "query": "params - query",
            "count": "params - max_results",
            "search_model": "custom",
            "max": 6
        }
    }
}
```

---

### 26.11.4 details 参数映射规则说明

`details` 字典用于将框架内部工具的参数名映射到 MCP 工具期望的参数名。其 **值** 的解析规则如下：

| 值格式 | 含义 | 示例 |
|--------|------|------|
| `"params - <内部参数名>"` | **动态映射**：运行时从内部参数中取对应键的值 | `"params - query"` → 取内部 `query` 参数的实际值 |
| 其他字符串 / 数字 / 布尔 | **固定值**：每次调用都直接传入该字面量 | `"custom"` → 固定传入字符串 `"custom"` |
| `null` | 跳过该参数，不传给 MCP 工具 | `null` → 忽略 |

> ⚠️ **前端关键区分**：
> - `"params - query"` — 以 `params - ` 为前缀，表示"取内部参数 `query` 的运行时值"，是**动态映射**。
> - `"query"` — 不含 `params - ` 前缀，表示**固定字符串** `"query"`，每次调用都会原样传入字面量 `"query"`。
>
> 两者含义完全不同！前端在展示时应隐去 `params - ` 前缀，改用下拉列表让用户选择内部参数名；对于非 `params - ` 开头的值，应展示为"固定值"输入框。

#### 各内部工具可用参数一览

前端可利用此表为每个配置项的 `details` 提供"内部参数"下拉预设，用户选择后自动拼接为 `"params - <参数名>"` 格式保存。用户也可自行输入字符串作为固定值。

| 配置项键名 (`item_key`) | 内部参数名 | 参数类型 | 说明 |
|-------------------------|-----------|----------|------|
| `websearch_mcp_tool_id` | `query` | string | 搜索查询关键词 |
| | `max_results` | int | 最大返回结果数量 |
| `image_understand_mcp_tool_id` | `image_source` | string | 图片来源（文件路径或 URL） |
| | `prompt` | string | 对图片的提问/分析指令 |
| `asr_mcp_tool_id` | `audio_source` | string | 音频文件路径 |
| | `language` | string \| null | 语言代码，如 `"zh"`、`"en"`，null 表示自动检测 |
| `document_extract_mcp_tool_id` | `file_source` | string | 文档文件路径 |
| | `page_range` | string \| null | 页码范围，如 `"1-5"`，null 表示全部 |
| `video_extract_mcp_tool_id` | `video_source` | string | 视频文件路径 |
| | `max_frames` | int | 最大提取帧数 |
| | `interval_seconds` | float \| null | 提取间隔（秒），null 表示自动 |
| `video_understand_mcp_tool_id` | `video_source` | string | 视频文件路径 |
| | `prompt` | string | 对视频的提问/分析指令 |

#### 前端交互建议

1. **展示方式**：对 `details` 中每个键值对，将 MCP 参数名（键）作为标签固定展示；值的编辑区域分为两种模式：
   - **选择内部参数**（推荐）：下拉列表展示上表中对应配置项的可用内部参数名，用户选择后保存为 `"params - <参数名>"` 格式。
   - **自定义固定值**：文本/数字输入框，用户直接输入字面量，原样保存。
2. **新增映射**：用户可点击"添加参数"新增 `details` 条目，MCP 参数名由用户输入（或从工具发现结果中选择），值通过上述两种模式之一设置。
3. **区分标识**：可通过图标或颜色区分"动态映射"和"固定值"两种条目，便于用户一目了然。

---

## 前端使用建议

1. **首次加载**：调用 `GET /api/ai/mcp/list` 获取所有配置列表
2. **添加配置**：调用 `POST /api/ai/mcp` 创建新配置，工具会自动实时注册，响应中包含 `tool_count`
3. **编辑配置**：调用 `PUT /api/ai/mcp/{config_id}` 更新配置，工具会自动重新注册
4. **删除配置**：调用 `DELETE /api/ai/mcp/{config_id}` 删除配置，已注册工具会自动注销
5. **启用/禁用**：调用 `POST /api/ai/mcp/{config_id}/toggle` 切换状态，工具会自动注册或注销
6. **批量刷新**：调用 `POST /api/ai/mcp/reload` 重新加载所有配置并重新注册所有工具
7. **查看已注册工具**：调用 `GET /api/ai/tools/list?category=mcp` 查看所有已注册的 MCP 工具
8. **使用预设**：调用 `GET /api/ai/mcp/presets` 获取预设配置，快速填充表单
9. **发现工具**：在保存配置前，调用 `POST /api/ai/mcp/tools/discover` 预览服务器提供的工具
10. **导入配置**：调用 `POST /api/ai/mcp/tools/import` 从 MCP 官方 JSON 配置导入
11. **配置参数映射**：调用 `GET /api/ai/mcp-tools-config/list` 查看各业务模块的 MCP 工具映射，`PUT /api/ai/mcp-tools-config/{item_key}` 更新工具 ID 和参数映射
