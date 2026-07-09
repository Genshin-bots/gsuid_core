# AI Session Logs API - /api/ai/session_logs

提供 AI Agent 会话执行日志的查询接口。**统一合并内存活跃会话 + 磁盘持久化日志，按 `chain_id` 归并成「逻辑会话链」卡片**，前端只需调用一个列表接口即可获取完整历史，无需区分活跃/持久化，也无需感知物理分段。

> **逻辑会话链（chain）与分段（segment）**：一条会话窗口内的日志，因单文件体积/条数上限（`MAX_ENTRIES_PER_FILE`，见 `session_logger.py`）会滚动到多个**物理分段文件**，它们共享同一 `chain_id`（`segment_index` 递增、`prev_segment` 指向上一分段）。列表接口按 `chain_id` 把多个分段**归并为一张卡片**（聚合条数/时长/关联 Agent），并在卡片上附 `segments[]` 有序分段元数据；详情接口仍以**单分段**为粒度返回，前端按 `segments` 顺序**懒加载并拼接**成完整时间线。分段对用户不可见——这消除了旧「按 500 条硬切、一段会话散成多张卡片」的中断感。**向后兼容**：升级前落盘的旧文件无 `chain_id`，读取时以其 `session_uuid` 兜底为独立一条链（每个旧文件各自成一张卡片），随 8 天日志清理自然淘汰。

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
| session_id | string | Session ID（如 `ws-onebot:onebot:bot_001:group:123456`），取链内最新分段 |
| session_uuid | string | 最新分段的实例 UUID（物理身份，每分段不同） |
| chain_id | string | **逻辑会话链标识**（同链多分段共享，列表按此归并）；旧文件缺失时回退为 `session_uuid` |
| segment_index | int | 最新分段在链中的序号（0 起） |
| segment_count | int | 链内分段总数（绝大多数会话为 1） |
| segments | array | 链内各分段的有序轻量元数据（见下表），供前端按序懒加载/拼接各分段详情 |
| persona_name | string | Persona 名称 |
| create_by | string | 创建来源（Chat/SubAgent/BuildPersona/LLM），取最新分段 |
| is_subagent | bool | 是否为子 Agent |
| created_at | float | 链创建时间 = 各分段最早（Unix 时间戳） |
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

> `entry_count` / `type_counts` 为**链内各分段求和**；`updated_at` 取最新分段；`ended_at` / `is_active` 由**最新分段**决定（链是否仍在写）；`duration_seconds` = 链结束 − 链创建（活跃则用当前时间近似）。

**segments 条目字段说明（SegmentMeta）**：

| 字段 | 类型 | 说明 |
|------|------|------|
| segment_index | int | 分段序号（0 起，升序即时间顺序） |
| session_uuid | string \| null | 该分段实例 UUID（用于精确定位分段详情） |
| session_id | string | 该分段的 Session ID |
| file_name | string \| null | 该分段的持久化文件名 |
| entry_count | int | 该分段条数 |
| created_at / updated_at | float | 该分段创建/更新时间 |
| ended_at | float \| null | 该分段结束时间 |
| is_active | bool | 该分段是否仍活跃（仅最新分段可能为 true） |
| source | string | `"memory"`（内存活跃分段）或 `"disk"`（磁盘分段） |

> **前端如何取某分段详情**：`source === "memory"` 的（最新活跃）分段用真实 `session_id` + `session_uuid` 调详情接口取实时数据；`"disk"` 分段用 `file_name` 去掉 `.json` 的 stem 作 `session_id` 调详情接口（O(1) 命中）。按 `segment_index` 升序拼接各分段的 `entries` 即完整时间线。

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
| `history_reset` | 历史重置事件（供前端时间线画独立色块） | reason（子类型）+ 各子类型附加字段（见下） |
| `mode_change` | 交互模式变化（主动 ↔ 被动，供前端画「模式变化」tag） | `mode`（新模式）/ `from`（上一模式） |

**`history_reset` 的 `reason` 子类型**（前端按其画不同色块，几类行为标记不同）：

| reason | 触发时机 | data 附加字段 | 建议色 |
|--------|----------|---------------|--------|
| `user_clear` | 用户 `/clear`、`清空会话`（清空历史 + 重置 AI Session，最强重置） | — | 红（醒目） |
| `persona_switch` | `persona`、`人格切换`（当前会话丢弃、按新人格重建） | `persona_name`（新人格） | 紫 |
| `auto_compact` | Agent 超长历史自动裁剪（`extract_history`） | `before` / `after`（裁剪前后条数） | 灰（低调） |

> `history_reset` 表示「会话仍在继续、但上下文被有意重置/压缩」的**时间线内**标记，**不**代表日志分段结束（分段仅按体积/条数滚动）。

**`mode_change` 的 `mode` 子类型**（仅在**模式翻转**时落一条，权威来源；前端在两模式边界画分隔 tag）：

| mode | 触发时机 | data 附加字段 | 建议色 |
|------|----------|---------------|--------|
| `reactive` | 用户发话触发的 run（`log_run_start`），此前为主动模式 | `from`（上一模式，如 `proactive`） | 蓝（被动/进入被动聊天） |
| `proactive` | Heartbeat/定时/看板/工具主动发言（`log_proactive_emission`），此前为被动模式 | `from`（如 `reactive`） | 粉（主动/转为主动发言） |

> 只在「已知模式 → 另一模式」翻转时打标（首次设定不打）；续写/滚动/重启时由 `_infer_mode_from_entries` 从既有 entries 重建当前模式，跨分段不误判。subagent 无模式概念、不打标。前端若日志无 `mode_change`（旧日志）则回退为按顶层项 kind 推断，二者同一套渲染。

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

1. **收集磁盘持久化分段**：扫描 `data/ai_core/session_logs/*.json` 和 `data/ai_core/session_logs/subagents/*.json`，解析每个文件为「单分段」摘要（带 mtime 缓存 + 头部快速读取）
2. **收集内存活跃会话的当前分段**：遍历 `AISessionRegistry._ai_sessions`，从每个 `GsCoreAIAgent._session_logger` 提取当前分段摘要
3. **按 session_uuid 合并单分段**：同一 session_uuid 在内存和磁盘中都存在时，**优先使用内存版本**（数据更新）；磁盘分段做 `is_active` 修正（`ended_at` 为 null 但不在内存 registry 中的视为已结束）
4. **按 chain_id 归并为链卡片**：把同一 `chain_id` 的多个分段聚合成一张卡片（条数/类型计数求和、时长取整段、`linked_agents` 跨分段去重合并、身份取最新分段），并附有序 `segments[]`
5. **按 created_at 倒序排列**

> 单分段会话（`segment_count === 1`，含全部旧格式文件）归并后行为与旧版一致——每张卡片仍对应一个 `session_uuid`。

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
- **agent_linked**: 关联 Agent 标记（可点击/展开子 Agent 子轨迹）
- **proactive_emission**: 主动消息标记（按 source 分类高亮；展开显示 source/触发原因/正文）
- **history_reset**: 历史重置分隔条（按 `data.reason` 画不同色块：`user_clear` 红 / `persona_switch` 紫 / `auto_compact` 灰）
- **mode_change**: 交互模式变化 tag（`reactive` 蓝「进入被动聊天」/ `proactive` 粉「转为主动发言」，居中虚线细条）

> 瀑布视图里模型响应按「对话(ModelRequestNode/CallToolsNode) → 思考过程/文本输出」嵌套一层；`run`/`chat` 默认展开、子 Agent 懒加载。

通过按时间顺序排列 entries，可以清晰还原 AI Agent 的完整思考与执行链路。

> **当前前端实现（gsuid_hub `AIHistoryPage` + `TraceWaterfall`）**：详情视图已从「聊天气泡时间线」重构为 **Logfire 式 Trace 瀑布**——把扁平 entries 重建为 span 树（`run_start`→`run_end` = 一个「Agent 运行」span，其内 `ModelRequestNode` = 「对话 <model>」span，`tool_call`+`tool_return` 按 `tool_call_id` 配对 = 工具 span，`sub_agent` 的 `agent_linked` = 可展开的子 Agent 子瀑布），每行密排展示「时间 · 缩进+展开 · 图标+标签 · token 徽章(Σ↗↙) · 甘特条 · 时长」，点击展开看内容/子 span。`history_reset` 提升为顶层醒目色块。

### 查找优先级

详情接口的查找优先级（`_find_log_by_session_id_and_uuid`）：

1. **文件名 stem 精确匹配**（O(1)，最高效）：当 session_id 实际上是文件名 stem（如 subagent 日志）时，直接按文件名查找
2. **内存活跃会话**：从 `AISessionRegistry` 查找活跃的 `GsCoreAIAgent` 实例
3. **JSON 内 session_id 字段全目录扫描**（兜底）：遍历所有日志文件，按 JSON 内的 `session_id` 字段匹配

当 `session_uuid` 为空字符串或 `None` 时，返回该 session_id 最新的实例（向后兼容）。
