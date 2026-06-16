# AI Session Logs API - /api/ai/session_logs

提供 AI Agent 会话执行日志的查询接口。**统一合并内存活跃会话 + 磁盘持久化日志，按 session_uuid 去重**，前端只需调用一个列表接口即可获取完整历史，无需区分活跃/持久化。

---

## 目录

- [AI Session Logs API - /api/ai/session\_logs](#ai-session-logs-api---apiaisession_logs)
  - [目录](#目录)
  - [1. 日志列表（统一合并）](#1-日志列表统一合并)
  - [2. 日志详情](#2-日志详情)
    - [2a. 查询参数版（推荐）](#2a-查询参数版推荐)
    - [2b. 路径参数版](#2b-路径参数版)
    - [2c. Catch-all 路由（边缘情况兜底）](#2c-catch-all-路由边缘情况兜底)
    - [详情响应格式](#详情响应格式)
  - [3. 日志文件详情（按文件名，调试用）](#3-日志文件详情按文件名调试用)
  - [4. 日志统计概览](#4-日志统计概览)
  - [5. 查询会话关联 Agent](#5-查询会话关联-agent)
  - [6. 日志分类（按会话来源聚合）](#6-日志分类按会话来源聚合)
  - [7. 日志数据结构](#7-日志数据结构)
    - [Entry 类型说明](#entry-类型说明)
    - [Entry 类型列表](#entry-类型列表)
    - [列表条目额外字段](#列表条目额外字段)
  - [8. 去重与合并规则](#8-去重与合并规则)
    - [前端使用建议](#前端使用建议)
      - [获取日志详情（推荐方式）](#获取日志详情推荐方式)
      - [点击 linked\_agents 中的子 Agent](#点击-linked_agents-中的子-agent)
      - [前端渲染建议](#前端渲染建议)
    - [查找优先级](#查找优先级)

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
| session_id | string | Session ID（如 `ws-onebot:onebot:bot_001:group:123456`） |
| session_uuid | string | Session 实例 UUID |
| persona_name | string | Persona 名称 |
| create_by | string | 创建来源（Chat/SubAgent/BuildPersona/LLM） |
| is_subagent | bool | 是否为子 Agent |
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
| agent_type | string | 关联类型：`"sub_agent"`、`"peer_agent"`、`"parent_agent"`、`"proactive_generator"` |
| session_id | string | 关联 Agent 的 Session ID（或文件名 stem） |
| session_uuid | string | 关联 Agent 的 Session UUID |
| persona_name | string \| null | 关联 Agent 的 Persona 名称 |
| create_by | string \| null | 关联 Agent 的创建来源 |
| log_file | string \| null | 关联 Agent 的日志文件路径 |
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
                "session_id": "ws-onebot:onebot:bot_001:group:123456",
                "session_uuid": "abc12345",
                "persona_name": "默认助手",
                "create_by": "Chat",
                "is_subagent": false,
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
                        "session_id": "ws-onebot:onebot:bot_001:group:123456:sub:planner",
                        "session_uuid": "sub12345",
                        "persona_name": "规划助手",
                        "create_by": "SubAgent",
                        "log_file": "/path/to/subagent_log.json",
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
            }
        ],
        "total": 1,
        "limit": 50,
        "offset": 0
    }
}
```

---

## 2. 日志详情

获取指定 Session 实例的完整日志详情（含所有 entries）。提供三种访问方式，推荐使用查询参数版。

### 2a. 查询参数版（推荐）

**端点**: `GET /api/ai/session_logs/detail`

**描述**: 通过查询参数传递 `session_id` 和 `session_uuid`，**避免路径参数中特殊字符（如冒号、中文、连续斜杠）导致的路由匹配问题**。这是最可靠的详情接口。

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | Session ID（如 `ws-onebot:onebot:bot_001:group:123456`）或文件名 stem（如 `heartbeat_decision_早柚_xxx_c7b1408f_20260531_134144`） |
| session_uuid | string | 否 | Session 实例 UUID（如 `abc12345`）；省略时返回该 session_id 最新的实例 |

**请求示例**:

```
GET /api/ai/session_logs/detail?session_id=ws-onebot:onebot:bot_001:group:123456&session_uuid=abc12345
GET /api/ai/session_logs/detail?session_id=heartbeat_decision_早柚_929275476_1780206104_c7b1408f_20260531_134144
```

### 2b. 路径参数版

**端点**: `GET /api/ai/session_logs/{session_id}/detail` 或 `GET /api/ai/session_logs/{session_id}/{session_uuid}/detail`

**描述**: 通过路径参数传递 session_id 和可选的 session_uuid。适用于 session_id 不含特殊字符的场景。

**路径参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | Session ID 或文件名 stem |
| session_uuid | string | 否 | Session 实例 UUID；省略时返回最新实例 |

**请求示例**:

```
GET /api/ai/session_logs/ws-onebot:onebot:bot_001:group:123456/detail
GET /api/ai/session_logs/ws-onebot:onebot:bot_001:group:123456/abc12345/detail
```

### 2c. Catch-all 路由（边缘情况兜底）

**端点**: `GET /api/ai/session_logs/{rest:path}/detail`

**描述**: 当 URL 中含连续斜杠等边缘情况时（如前端构造 `{file_stem}//detail`），标准路径参数路由无法匹配。本路由使用 `:path` 转换器兜底捕获，手动解析 session_id / session_uuid。

> **注意**：此路由主要用于向后兼容。**新代码应使用查询参数版（2a）**。

### 详情响应格式

所有详情端点返回相同的响应格式：

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "session_id": "ws-onebot:onebot:bot_001:group:123456",
        "session_uuid": "abc12345",
        "persona_name": "默认助手",
        "create_by": "Chat",
        "is_subagent": false,
        "created_at": 1715305800.0,
        "updated_at": 1715305900.0,
        "ended_at": null,
        "entry_count": 12,
        "entries": [
            {
                "type": "session_created",
                "timestamp": 1715305800.0,
                "data": {
                    "session_id": "ws-onebot:onebot:bot_001:group:123456",
                    "session_uuid": "abc12345",
                    "persona_name": "默认助手",
                    "create_by": "Chat"
                }
            },
            {
                "type": "user_input",
                "timestamp": 1715305802.0,
                "data": { "content": "【用户发言】\n你好" }
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
                "type": "token_usage",
                "timestamp": 1715305806.0,
                "data": {
                    "input_tokens": 150,
                    "output_tokens": 20,
                    "model_name": "gpt-4"
                }
            }
        ],
        "linked_agents": [],
        "linked_agent_count": 0,
        "source": "memory",
        "is_active": true
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
| file_name | string | 是 | 日志文件名（含或不含 `.json` 后缀） |

**响应**: 与日志详情格式一致，`source` 固定为 `"disk"`。

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

## 5. 查询会话关联 Agent

**端点**: `GET /api/ai/session_logs/{session_id}/linked_agents`

**描述**: 获取指定 Session 关联的所有 Agent（SubAgent、PeerAgent、ParentAgent 等）。支持按 `agent_type` 过滤，为前端展示 Agent 关系图提供数据。

**路径参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | Session ID 或文件名 stem |

**Query 参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| agent_type | string | 否 | 关联类型过滤：`"sub_agent"`、`"peer_agent"`、`"parent_agent"`、`"proactive_generator"`。不传返回全部 |

**响应示例**:

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "session_id": "ws-onebot:onebot:bot_001:group:123456",
        "session_uuid": "abc12345",
        "linked_agents": [
            {
                "agent_type": "sub_agent",
                "session_id": "ws-onebot:onebot:bot_001:group:123456:sub:planner",
                "session_uuid": "sub12345",
                "persona_name": "规划助手",
                "create_by": "SubAgent",
                "log_file": "/path/to/subagent_log.json",
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

## 6. 日志分类（按会话来源聚合）

**端点**: `GET /api/ai/session_logs/categories`

**描述**: 获取**当前后台日志中实际出现的所有会话来源（`create_by`）分类**，按来源聚合并附带前端展示名、说明、所属分组与数量统计。基于统一合并后的数据（内存活跃 + 磁盘持久化），供前端渲染分类筛选 Tab / Chip。返回的 `create_by` 可直接作为 [日志列表接口](#1-日志列表统一合并) 的 `create_by` 查询参数使用。

> 每个 AI 会话在创建时都会带一个 `create_by` 来源标识（如 `Chat`、`MemCategorization`、`Heartbeat_Decision`、`Heartbeat_Output`、`SubAgent`、`Proactive_*` 等）。本接口把这些原始标识归一化为可读分类，未在内置目录中的来源会回退到 `other` 分组（不会报错）。

**请求参数**: 无（仅需鉴权）。

**响应示例**:

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "categories": [
            {
                "create_by": "Chat",
                "label": "聊天对话",
                "description": "用户与 AI 的常规聊天会话",
                "group": "chat",
                "count": 100,
                "active_count": 2,
                "subagent_count": 0
            },
            {
                "create_by": "MemCategorization",
                "label": "记忆分类",
                "description": "对记忆进行分层图谱分类",
                "group": "memory",
                "count": 18,
                "active_count": 0,
                "subagent_count": 0
            },
            {
                "create_by": "Heartbeat_Decision",
                "label": "心跳决策",
                "description": "心跳机制判断是否主动发言",
                "group": "heartbeat",
                "count": 12,
                "active_count": 1,
                "subagent_count": 0
            },
            {
                "create_by": "Proactive_heartbeat",
                "label": "主动消息(heartbeat)",
                "description": "主动消息发送器触发的会话",
                "group": "proactive",
                "count": 4,
                "active_count": 0,
                "subagent_count": 0
            }
        ],
        "groups": {
            "chat": 103,
            "memory": 18,
            "heartbeat": 12,
            "proactive": 4
        },
        "total": 4
    }
}
```

**data 字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| categories | array | 分类列表，按 `count` 倒序排列（见下表） |
| groups | object | 分组维度的会话数量聚合 `{group: count}` |
| total | int | 分类种类数（即 categories 长度） |

**categories 条目字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| create_by | string | 原始来源标识，可直接作为列表接口的 `create_by` 查询参数 |
| label | string | 前端展示名（中文） |
| description | string | 分类说明 |
| group | string | 所属分组：`chat` / `agent` / `capability` / `image` / `persona` / `heartbeat` / `meme` / `memory` / `kanban` / `scheduled` / `proactive` / `other` |
| count | int | 该来源的会话总数 |
| active_count | int | 其中仍活跃（运行中）的会话数 |
| subagent_count | int | 其中属于 SubAgent 的会话数 |

**内置来源目录（create_by → 分类）**：

| create_by | label | group |
|-----------|-------|-------|
| `Chat` | 聊天对话 | chat |
| `Agent` | Agent 对话 | chat |
| `LLM` | 通用 LLM | chat |
| `SubAgent` | 子 Agent | agent |
| `AutoPlanner` | 自动规划 | agent |
| `CapabilityEvaluator` | 能力评估 | capability |
| `CapabilityAgent` | 能力 Agent | capability |
| `ImageUnderstand` | 图片理解 | image |
| `ImageDescSummary` | 图片描述汇总 | image |
| `BuildPersona` | 人格构建 | persona |
| `Heartbeat_Decision` | 心跳决策 | heartbeat |
| `Heartbeat_Output` | 心跳输出 | heartbeat |
| `MemeTagger` | 表情包打标 | meme |
| `MemCategorization` | 记忆分类 | memory |
| `MemGroupSummary` | 群记忆摘要 | memory |
| `MemEntityExtraction` | 记忆实体抽取 | memory |
| `MemNodeSelection` | 记忆节点选择 | memory |
| `Kanban_Relay` | 看板中继 | kanban |
| `ScheduledTask_Exec` | 定时任务执行 | scheduled |
| `Proactive_*`（前缀匹配） | 主动消息(*) | proactive |

> 目录之外的来源会以原始 `create_by` 作为 `label`、归入 `other` 分组。**新增来源时仅需在后端 `_CREATE_BY_CATALOG` 补充一行即可被本接口识别**，无需改动前端。

---

## 7. 日志数据结构

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
| `session_created` | 会话创建 | session_id, session_uuid, persona_name, create_by |
| `session_resumed` | 会话窗口续写 | session_id, session_uuid, persona_name, create_by, resumed_from_entries |
| `session_ended` | 会话结束 | ended_at |
| `system_prompt` | 系统提示词（唯一记录处） | content |
| `run_start` | 单次 run 开始（纯标记） | （空，用户输入见 user_input） |
| `run_end` | 单次 run 结束（纯标记） | （空，最终输出见 result） |
| `user_input` | 用户输入（唯一记录处） | content（含图片时见下方"图片外置"说明） |
| `thinking` | 模型思考过程 | content |
| `tool_call` | 工具调用请求 | tool_name, args, tool_call_id |
| `tool_return` | 工具执行返回 | tool_name, content, tool_call_id |
| `tools_list` | 传给 AI 的工具列表 | tools |
| `text_output` | 模型直接输出文本 | content |
| `result` | 单次 run 最终结果 | output, tool_calls |
| `token_usage` | Token 使用量 | input_tokens, output_tokens, model_name |
| `error` | 错误信息 | error_type, message |
| `node_transition` | Agent 节点状态转换 | node_type (ModelRequestNode/CallToolsNode/End), details |
| `agent_linked` | 关联 Agent 事件 | agent_type, session_id, session_uuid, persona_name, create_by, log_file, linked_at |
| `proactive_emission` | 主动消息发射 | source, content, trigger_reason, generator_log_files |

### 图片外置（user_input）

用户消息里的 base64 图片**不内联进日志 JSON**——那会让单个日志文件膨胀到几 MB。
`user_input` 的 `content` 在落盘前会把其中的 `data:image/...;base64,...` 按**内容哈希**
去重外置到 `data/ai_core/session_logs/images/<hash>.<ext>`，`content` 里只保留
`[图片引用: images/<hash>.<ext>]` 形式的引用。

- 引用路径相对 `session_logs/` 根目录；前端如需展示原图，可据此拼接图片文件路径。
- `images/` 目录与日志文件一样随 `ScheduledCleanLogDay` 定时清理；图片被引用（再次外置同一张图）时会刷新 mtime，仍在活跃日志里引用的图片不会被提前清掉。
- 外置失败（极少数：解码/写盘异常）时退化为 `[图片: base64 <N> 字符, 外置失败]` 占位，**绝不**把超长 base64 原样写进日志。

### 列表条目额外字段

| 字段 | 类型 | 说明 |
|------|------|------|
| source | string | 数据来源：`"memory"`（内存活跃会话）或 `"disk"`（磁盘持久化文件） |
| is_active | bool | 是否仍在运行（ended_at 为 null 时为 true） |
| type_counts | object | 各类型条目数量统计 |
| linked_agents | array | 关联的 Agent 列表（含 entry_count、type_counts、is_active、source 等 enriched 字段） |
| linked_agent_count | int | 关联 Agent 数量 |

---

## 8. 去重与合并规则

列表接口内部执行以下合并逻辑：

1. **收集内存活跃会话**：遍历 `AISessionRegistry._ai_sessions`，从每个 `GsCoreAIAgent._session_logger` 提取摘要
2. **收集磁盘持久化文件**：扫描 `data/ai_core/session_logs/*.json` 和 `data/ai_core/session_logs/subagents/*.json`，解析每个文件
3. **按 session_uuid 去重**：同一 session_uuid 在内存和磁盘中都存在时，**优先使用内存版本**（数据更新）
4. **修正 is_active**：磁盘上 `ended_at` 为 null 的 session 不一定仍活跃，只有在内存 registry 中真正存在的 session 才是活跃的
5. **按 created_at 倒序排列**

### 前端使用建议

#### 获取日志详情（推荐方式）

**推荐使用查询参数版详情接口**，避免路径参数中特殊字符导致的路由匹配问题：

```javascript
// 推荐：查询参数版
const url = `/api/ai/session_logs/detail?session_id=${encodeURIComponent(sessionId)}&session_uuid=${encodeURIComponent(sessionUuid)}`;

// 也可省略 session_uuid（返回最新实例）
const url = `/api/ai/session_logs/detail?session_id=${encodeURIComponent(sessionId)}`;
```

#### 点击 linked_agents 中的子 Agent

当用户点击父 Session 的 `linked_agents` 中某个子 Agent 时：

```javascript
// 方式1（推荐）：使用查询参数版，log_file 的 stem 作为 session_id
const stem = agent.log_file ? agent.log_file.replace(/^.*[\\/]/, '').replace(/\.json$/, '') : agent.session_id;
const url = `/api/ai/session_logs/detail?session_id=${encodeURIComponent(stem)}`;

// 方式2：如果知道 session_uuid
const url = `/api/ai/session_logs/detail?session_id=${encodeURIComponent(agent.session_id)}&session_uuid=${encodeURIComponent(agent.session_uuid)}`;
```

#### 前端渲染建议

前端可以根据 `entry.type` 将日志渲染为不同的视觉组件：

- **session_created / session_ended**: 时间线起点/终点标记
- **system_prompt**: 可折叠的代码块或引用块
- **user_input**: 用户气泡（右侧）
- **thinking**: 灰色斜体思考过程（可折叠）
- **tool_call**: 工具调用卡片（显示工具名和参数）
- **tool_return**: 工具返回卡片（显示结果摘要）
- **tools_list**: 工具列表卡片（可折叠）
- **text_output**: AI 文本气泡（左侧）
- **result**: 高亮总结卡片
- **error**: 红色警告卡片
- **node_transition**: 节点流转指示器（可选，用于调试视图）
- **agent_linked**: 关联 Agent 标记（可点击跳转）
- **proactive_emission**: 主动消息标记（按 source 分类高亮）

通过按时间顺序排列 entries，可以清晰还原 AI Agent 的完整思考与执行链路。

### 查找优先级

详情接口的查找优先级（`_find_log_by_session_id_and_uuid`）：

1. **文件名 stem 精确匹配**（O(1)，最高效）：当 session_id 实际上是文件名 stem（如 subagent 日志）时，直接按文件名查找
2. **内存活跃会话**：从 `AISessionRegistry` 查找活跃的 `GsCoreAIAgent` 实例
3. **JSON 内 session_id 字段全目录扫描**（兜底）：遍历所有日志文件，按 JSON 内的 `session_id` 字段匹配

当 `session_uuid` 为空字符串或 `None` 时，返回该 session_id 最新的实例（向后兼容）。
