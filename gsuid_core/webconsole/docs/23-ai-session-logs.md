# AI Session Logs API - /api/ai/session_logs

提供 AI Agent 会话执行日志的查询接口。**统一合并内存活跃会话 + 磁盘持久化日志，按 session_uuid 去重**，前端只需调用一个列表接口即可获取完整历史，无需区分活跃/持久化。

---

## 目录

- [AI Session Logs API - /api/ai/session\_logs](#ai-session-logs-api---apiaisession_logs)
  - [目录](#目录)
  - [1. 日志列表（统一合并）](#1-日志列表统一合并)
  - [2. 日志详情（按 session\_id + session\_uuid）](#2-日志详情按-session_id--session_uuid)
  - [3. 日志文件详情（按文件名，调试用）](#3-日志文件详情按文件名调试用)
  - [4. 日志统计概览](#4-日志统计概览)
  - [5. 日志数据结构](#5-日志数据结构)
    - [Entry 类型说明](#entry-类型说明)
    - [Entry 类型列表](#entry-类型列表)
    - [列表条目额外字段](#列表条目额外字段)
  - [7. 查询会话关联 Agent](#7-查询会话关联-agent)
  - [6. 去重与合并规则](#6-去重与合并规则)
    - [前端渲染建议](#前端渲染建议)

---

## 1. 日志列表（统一合并）

**端点**: `GET /api/ai/session_logs`

**描述**: 获取 AI Session 日志列表，**自动合并内存活跃会话与磁盘持久化文件，按 session_uuid 去重**。每个条目包含 `source` 字段标识来源（`"memory"` 或 `"disk"`），`is_active` 字段标识是否仍在运行。结果按创建时间倒序排列。

**请求参数**（Query）:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 否 | 按 session_id 精确筛选 |
| create_by | string | 否 | 按创建来源筛选（Chat/SubAgent/BuildPersona/LLM） |
| persona_name | string | 否 | 按 Persona 名称筛选 |
| is_active | bool | 否 | 按是否活跃筛选（true=仅活跃，false=仅已结束） |
| date_from | string | 否 | 起始日期，格式 YYYY-MM-DD |
| date_to | string | 否 | 结束日期，格式 YYYY-MM-DD |
| limit | int | 否 | 返回数量限制，默认 50，最大 200 |
| offset | int | 否 | 偏移量，默认 0 |

**列表条目字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| session_id | string | Session ID（如 `bot:onebot:group:123456`） |
| session_uuid | string | Session 实例 UUID |
| persona_name | string | Persona 名称 |
| create_by | string | 创建来源（Chat/SubAgent/BuildPersona/LLM） |
| created_at | float | 创建时间（Unix 时间戳） |
| created_at_str | string | 创建时间格式化字符串 |
| updated_at | float | 最后更新时间（Unix 时间戳） |
| updated_at_str | string | 最后更新时间格式化字符串 |
| ended_at | float \| null | 结束时间（Unix 时间戳），未结束为 null |
| ended_at_str | string \| null | 结束时间格式化字符串 |
| duration_seconds | float \| null | 运行时长（秒） |
| entry_count | int | 日志条目总数 |
| type_counts | object | 各类型条目数量统计 |
| is_active | bool | 是否仍在运行 |
| source | string | 数据来源：`"memory"`（内存）或 `"disk"`（磁盘） |
| file_name | string \| null | 持久化文件名 |
| linked_agents | array | 关联的 Agent 列表（见下表） |
| linked_agent_count | int | 关联 Agent 数量 |

**linked_agents 条目字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| agent_type | string | 关联类型：`"sub_agent"`、`"peer_agent"`、`"parent_agent"` |
| session_id | string | 关联 Agent 的 Session ID |
| session_uuid | string | 关联 Agent 的 Session UUID |
| persona_name | string \| null | 关联 Agent 的 Persona 名称 |
| create_by | string \| null | 关联 Agent 的创建来源 |
| linked_at | float | 关联时间（Unix 时间戳） |
| entry_count | int | 关联 Agent 的日志条目数 |
| type_counts | object | 关联 Agent 的各类型条目统计 |
| is_active | bool \| null | 关联 Agent 是否仍在运行 |
| source | string | 关联 Agent 数据来源：`"memory"`、`"disk"` 或 `"unavailable"` |

**响应示例**:

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [
            {
                "session_id": "bot:onebot:group:123456",
                "session_uuid": "abc12345",
                "persona_name": "默认助手",
                "create_by": "Chat",
                "created_at": 1715305800.0,
                "created_at_str": "2026-05-10 10:30:00",
                "updated_at": 1715305900.0,
                "updated_at_str": "2026-05-10 10:31:40",
                "ended_at": null,
                "ended_at_str": null,
                "duration_seconds": 100.0,
                "entry_count": 12,
                "type_counts": {
                    "session_created": 1,
                    "user_input": 1,
                    "tool_call": 1,
                    "tool_return": 1,
                    "text_output": 1,
                    "result": 1
                },
                "is_active": true,
                "source": "memory",
                "file_name": "bot_onebot_group_123456_abc12345_20260510_103000.json",
                "linked_agents": [
                    {
                        "agent_type": "sub_agent",
                        "session_id": "bot:onebot:group:123456:sub:planner",
                        "session_uuid": "sub12345",
                        "persona_name": "规划助手",
                        "create_by": "SubAgent",
                        "linked_at": 1715305805.0,
                        "entry_count": 5,
                        "type_counts": {
                            "session_created": 1,
                            "tool_call": 2,
                            "tool_return": 2
                        },
                        "is_active": true,
                        "source": "memory"
                    }
                ],
                "linked_agent_count": 1
            },
            {
                "session_id": "bot:onebot:group:789012",
                "session_uuid": "def67890",
                "persona_name": "傲娇助手",
                "create_by": "Chat",
                "created_at": 1715200000.0,
                "created_at_str": "2026-05-09 10:00:00",
                "updated_at": 1715200100.0,
                "updated_at_str": "2026-05-09 10:01:40",
                "ended_at": 1715200100.0,
                "ended_at_str": "2026-05-09 10:01:40",
                "duration_seconds": 100.0,
                "entry_count": 8,
                "type_counts": {
                    "session_created": 1,
                    "user_input": 1,
                    "text_output": 1,
                    "result": 1,
                    "session_ended": 1
                },
                "is_active": false,
                "source": "disk",
                "file_name": "bot_onebot_group_789012_def67890_20260509_100000.json",
                "linked_agents": [],
                "linked_agent_count": 0
            }
        ],
        "total": 2,
        "limit": 50,
        "offset": 0
    }
}
```

---

## 2. 日志详情（按 session_id + session_uuid）

**端点**: `GET /api/ai/session_logs/{session_id}/{session_uuid}/detail`

**描述**: 获取指定 Session 实例的完整日志详情。通过 `session_id` + `session_uuid` 精确定位到某个具体实例。同一 `session_id` 可能有多个实例（不同 `session_uuid`），用于区分同一会话的不同运行记录。**优先从内存查找活跃会话的实时日志**，若不存在则从磁盘文件查找。返回的 `source` 字段标识数据来源。

**路径参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | Session ID（如 `bot:onebot:group:123456`） |
| session_uuid | string | 是 | Session 实例 UUID（如 `abc12345`，从列表接口的 `session_uuid` 字段获取） |

**请求示例**:

```
GET /api/ai/session_logs/bot:onebot:group:123456/abc12345/detail
```

**响应示例**:

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "session_id": "bot:onebot:group:123456",
        "session_uuid": "abc12345",
        "persona_name": "默认助手",
        "create_by": "Chat",
        "created_at": 1715305800.0,
        "updated_at": 1715305900.0,
        "ended_at": null,
        "entry_count": 12,
        "entries": [
            {
                "type": "session_created",
                "timestamp": 1715305800.0,
                "data": {
                    "session_id": "bot:onebot:group:123456",
                    "session_uuid": "abc12345",
                    "persona_name": "默认助手",
                    "create_by": "Chat",
                    "system_prompt": "你是一个智能助手..."
                }
            },
            {
                "type": "system_prompt",
                "timestamp": 1715305801.0,
                "data": { "content": "你是一个智能助手..." }
            },
            {
                "type": "run_start",
                "timestamp": 1715305802.0,
                "data": { "user_message": "【用户发言】\n你好" }
            },
            {
                "type": "user_input",
                "timestamp": 1715305802.0,
                "data": { "content": "【用户发言】\n你好" }
            },
            {
                "type": "node_transition",
                "timestamp": 1715305803.0,
                "data": { "node_type": "ModelRequestNode", "details": {} }
            },
            {
                "type": "node_transition",
                "timestamp": 1715305804.0,
                "data": { "node_type": "CallToolsNode", "details": {} }
            },
            {
                "type": "tool_call",
                "timestamp": 1715305804.0,
                "data": {
                    "tool_name": "get_current_date",
                    "args": "{}",
                    "tool_call_id": "call_xxx"
                }
            },
            {
                "type": "tool_return",
                "timestamp": 1715305805.0,
                "data": {
                    "tool_name": "get_current_date",
                    "content": "2026-05-10 10:30:05",
                    "tool_call_id": "call_xxx"
                }
            },
            {
                "type": "text_output",
                "timestamp": 1715305806.0,
                "data": { "content": "你好！今天是 2026-05-10。" }
            },
            {
                "type": "result",
                "timestamp": 1715305806.0,
                "data": {
                    "output": "你好！今天是 2026-05-10。",
                    "tool_calls": ["get_current_date"]
                }
            },
            {
                "type": "run_end",
                "timestamp": 1715305806.0,
                "data": { "output": "你好！今天是 2026-05-10。" }
            },
            {
                "type": "token_usage",
                "timestamp": 1715305806.0,
                "data": {
                    "input_tokens": 150,
                    "output_tokens": 20,
                    "model_name": "gpt-4"
                }
            }
        ],
        "source": "memory"
    }
}
```

---

## 3. 日志文件详情（按文件名，调试用）

**端点**: `GET /api/ai/session_logs/file/{file_name}`

**描述**: 按文件名直接读取磁盘上的持久化日志文件。适用于需要查看特定历史实例的场景（如同一 session_id 有多次运行记录）。

**路径参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file_name | string | 是 | 日志文件名（含 .json 后缀） |

**响应**: 与日志详情格式一致，但 `source` 固定为 `"disk"`。

---

## 4. 日志统计概览

**端点**: `GET /api/ai/session_logs/stats/overview`

**描述**: 获取 Session 日志的统计概览，基于统一合并后的数据。

**响应示例**:

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "total": 128,
        "today_count": 15,
        "active_count": 3,
        "memory_count": 3,
        "disk_count": 125,
        "create_by_distribution": {
            "Chat": 100,
            "SubAgent": 20,
            "BuildPersona": 5,
            "LLM": 3
        },
        "linked_agent_total": 25,
        "linked_agent_by_type": {
            "sub_agent": 20,
            "peer_agent": 0,
            "parent_agent": 0
        },
        "log_path": "F:/gsuid_core/data/ai_core/session_logs"
    }
}
```

**统计字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| total | int | 日志总数（去重后） |
| today_count | int | 今日新增日志数 |
| active_count | int | 当前活跃 Session 数 |
| memory_count | int | 来自内存的 Session 数 |
| disk_count | int | 来自磁盘的 Session 数 |
| create_by_distribution | object | 按创建来源分布的计数 |
| linked_agent_total | int | 所有 Session 关联的 Agent 总数 |
| linked_agent_by_type | object | 按类型分布的关联 Agent 计数 |
| log_path | string | 日志文件存储路径 |

---

## 5. 日志数据结构

### Entry 类型说明

每条日志记录（entry）包含以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| type | string | 条目类型，见下表 |
| timestamp | float | Unix 时间戳（秒） |
| data | object | 具体数据内容 |

### Entry 类型列表

| type | 说明 | data 内容 |
|------|------|-----------|
| `session_created` | 会话创建 | session_id, session_uuid, persona_name, create_by, system_prompt |
| `session_ended` | 会话结束 | ended_at |
| `system_prompt` | 系统提示词 | content |
| `run_start` | 单次 run 开始 | user_message |
| `run_end` | 单次 run 结束 | output |
| `user_input` | 用户输入 | content |
| `thinking` | 模型思考过程 | content |
| `tool_call` | 工具调用请求 | tool_name, args, tool_call_id |
| `tool_return` | 工具执行返回 | tool_name, content, tool_call_id |
| `text_output` | 模型直接输出文本 | content |
| `result` | 单次 run 最终结果 | output, tool_calls |
| `token_usage` | Token 使用量 | input_tokens, output_tokens, model_name |
| `error` | 错误信息 | error_type, message |
| `node_transition` | Agent 节点状态转换 | node_type (ModelRequestNode/CallToolsNode/End), details |

### 列表条目额外字段

| 字段 | 类型 | 说明 |
|------|------|------|
| source | string | 数据来源：`"memory"`（内存活跃会话）或 `"disk"`（磁盘持久化文件） |
| is_active | bool | 是否仍在运行（ended_at 为 null 时为 true） |
| type_counts | object | 各类型条目数量统计 |
| linked_agents | array | 关联的 Agent 列表（含 entry_count、type_counts、is_active、source 等 enriched 字段） |
| linked_agent_count | int | 关联 Agent 数量 |

---

## 7. 查询会话关联 Agent

**端点**: `GET /api/ai/session_logs/{session_id}/linked_agents`

**描述**: 获取指定 Session 关联的所有 Agent（SubAgent、PeerAgent、ParentAgent 等）。支持按 `agent_type` 过滤，为前端展示 Agent 关系图提供数据。

**路径参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | Session ID（如 `bot:onebot:group:123456`） |

**Query 参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| agent_type | string | 否 | 关联类型过滤：`"sub_agent"`、`"peer_agent"`、`"parent_agent"`。不传返回全部 |

**响应示例**:

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "session_id": "bot:onebot:group:123456",
        "session_uuid": "abc12345",
        "linked_agents": [
            {
                "agent_type": "sub_agent",
                "session_id": "bot:onebot:group:123456:sub:planner",
                "session_uuid": "sub12345",
                "persona_name": "规划助手",
                "create_by": "SubAgent",
                "linked_at": 1715305805.0,
                "entry_count": 5,
                "type_counts": {
                    "session_created": 1,
                    "tool_call": 2,
                    "tool_return": 2
                },
                "is_active": true,
                "source": "memory"
            }
        ],
        "total": 1,
        "by_type": {
            "sub_agent": 1,
            "peer_agent": 0,
            "parent_agent": 0
        }
    }
}
```

**响应字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| session_id | string | Session ID |
| session_uuid | string \| null | Session UUID |
| linked_agents | array | 关联 Agent 列表（已 enriched，含 entry_count、type_counts、is_active、source） |
| total | int | 关联 Agent 总数 |
| by_type | object | 按类型统计的关联 Agent 数量 |

---

## 6. 去重与合并规则

列表接口内部执行以下合并逻辑：

1. **收集内存活跃会话**：遍历 `AISessionRegistry._ai_sessions`，从每个 `GsCoreAIAgent._session_logger` 提取摘要
2. **收集磁盘持久化文件**：扫描 `data/ai_core/session_logs/*.json`，解析每个文件
3. **按 session_uuid 去重**：同一 session_uuid 在内存和磁盘中都存在时，**优先使用内存版本**（数据更新）
4. **按 created_at 倒序排列**

**前端使用建议**：无需关心数据来源，直接使用列表接口获取全部会话。点击某条记录查看详情时，调用 `GET /api/ai/session_logs/{session_id}/{session_uuid}/detail` 即可自动获取最新数据（活跃会话从内存读取，已结束会话从磁盘读取）。注意：同一 `session_id` 可能对应多个不同实例（不同 `session_uuid`），因此详情接口必须同时提供 `session_id` 和 `session_uuid` 以精确定位。

### 前端渲染建议

前端可以根据 `entry.type` 将日志渲染为不同的视觉组件：

- **session_created / session_ended**: 时间线起点/终点标记
- **system_prompt**: 可折叠的代码块或引用块
- **user_input**: 用户气泡（右侧）
- **thinking**: 灰色斜体思考过程（可折叠）
- **tool_call**: 工具调用卡片（显示工具名和参数）
- **tool_return**: 工具返回卡片（显示结果摘要）
- **text_output**: AI 文本气泡（左侧）
- **result**: 高亮总结卡片
- **error**: 红色警告卡片
- **node_transition**: 节点流转指示器（可选，用于调试视图）

通过按时间顺序排列 entries，可以清晰还原 AI Agent 的完整思考与执行链路。
