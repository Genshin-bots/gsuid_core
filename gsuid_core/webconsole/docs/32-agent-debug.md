# Agent Debug API - /api/agent_debug

## 概述

Agent Debug API 提供 AI Agent 可视化调试台后端接口，用于支持管理员排查长任务编排、记忆图谱与自我认知演化问题。

该模块面向调试与运维场景，包含三个核心面板：

1. **Memory Graph View**：查看指定 scope 的记忆图谱 Edge，支持软删除错误 Edge，并查询记忆矛盾记录。
2. **Orchestration Board**：查看长任务列表、任务详情、步骤与执行日志，支持人工改写步骤和终止任务。
3. **Persona Evolution Inspector**：查看与人工修正 self_model 演化层字段。

**认证**：所有接口均需要认证。

**通用响应格式**：

```json
{
    "status": 0,
    "msg": "success",
    "data": {}
}
```

**错误响应格式**：

```json
{
    "status": -1,
    "msg": "错误信息",
    "data": null
}
```

---

## 目录

- [Agent Debug API - /api/agent\_debug](#agent-debug-api---apiagent_debug)
  - [概述](#概述)
  - [目录](#目录)
  - [1. 记忆图谱 Edge 列表](#1-记忆图谱-edge-列表)
  - [2. 软删除记忆图谱 Edge](#2-软删除记忆图谱-edge)
  - [3. 记忆矛盾记录列表](#3-记忆矛盾记录列表)
  - [4. 长任务列表](#4-长任务列表)
  - [5. 长任务详情](#5-长任务详情)
  - [6. 人工改写任务步骤](#6-人工改写任务步骤)
  - [7. 手动终止长任务](#7-手动终止长任务)
  - [8. 查看 self\_model 演化层](#8-查看-self_model-演化层)
  - [9. 覆盖修正 self\_model 字段](#9-覆盖修正-self_model-字段)
  - [前端使用建议](#前端使用建议)
    - [调试台页面布局](#调试台页面布局)
    - [风险提示](#风险提示)

---

## 1. 记忆图谱 Edge 列表

**端点**：`GET /api/agent_debug/memory/edges`

**描述**：列出指定作用域的记忆图谱 Edge。默认仅返回未软删除的 Edge，可通过 `include_invalid=true` 同时返回已失效记录。

**请求参数**（Query）：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| scope_key | string | 是 | - | 作用域 key，如 `group:789012` |
| include_invalid | bool | 否 | false | 是否包含已软删除 Edge |
| limit | int | 否 | 200 | 返回数量限制，最大 1000 |

**响应示例**：

```json
{
    "status": 0,
    "msg": "success",
    "data": [
        {
            "id": "edge_001",
            "fact": "用户 A 喜欢讨论深度学习",
            "source_entity_id": "entity_user_a",
            "target_entity_id": "entity_topic_dl",
            "mention_count": 5,
            "decay_score": 0.92,
            "valid_at": "2026-05-19T10:00:00+00:00",
            "invalid_at": null,
            "last_accessed": "2026-05-19T12:30:00+00:00"
        }
    ]
}
```

**响应字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | Edge ID |
| fact | string | 记忆事实文本 |
| source_entity_id | string | 源实体 ID |
| target_entity_id | string | 目标实体 ID |
| mention_count | int | 被提及次数 |
| decay_score | float | 衰减后的记忆评分 |
| valid_at | string \| null | 生效时间，ISO 8601 格式 |
| invalid_at | string \| null | 失效时间；为 null 表示仍有效 |
| last_accessed | string \| null | 最近访问时间 |

---

## 2. 软删除记忆图谱 Edge

**端点**：`POST /api/agent_debug/memory/edge/{edge_id}/invalidate`

**描述**：将指定 Edge 标记为失效。该操作只设置 `invalid_at`，不会物理删除数据库记录。

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| edge_id | string | 是 | 要软删除的 Edge ID |

**响应示例**：

```json
{
    "status": 0,
    "msg": "success",
    "data": {
        "edge_id": "edge_001"
    }
}
```

**错误示例**：

```json
{
    "status": -1,
    "msg": "Edge 不存在",
    "data": null
}
```

---

## 3. 记忆矛盾记录列表

**端点**：`GET /api/agent_debug/memory/conflicts`

**描述**：列出指定作用域内检测到的记忆矛盾记录，用于辅助管理员定位冲突事实。

**请求参数**（Query）：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| scope_key | string | 是 | - | 作用域 key，如 `group:789012` |
| limit | int | 否 | 100 | 返回数量限制，最大 500 |

**响应示例**：

```json
{
    "status": 0,
    "msg": "success",
    "data": [
        {
            "id": "conflict_001",
            "fact_signature": "user_a.preference.food",
            "summary": "同一用户的饮食偏好存在相互矛盾的记录",
            "created_at": "2026-05-19T11:00:00+00:00"
        }
    ]
}
```

**响应字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 矛盾记录 ID |
| fact_signature | string | 冲突事实签名 |
| summary | string | 冲突摘要 |
| created_at | string \| null | 创建时间，ISO 8601 格式 |

---

## 4. 长任务列表

**端点**：`GET /api/agent_debug/tasks`

**描述**：看板式列出 AI Agent 长任务。可按任务状态筛选，结果按更新时间倒序排列。

**请求参数**（Query）：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| status | string | 否 | null | 按状态过滤，不传返回全部 |
| limit | int | 否 | 100 | 返回数量限制，最大 500 |

**响应示例**：

```json
{
    "status": 0,
    "msg": "success",
    "data": [
        {
            "id": "task_001",
            "ordinal": 12,
            "display_name": "整理群公告草案",
            "goal": "根据近期讨论生成群公告草案",
            "status": "running",
            "owner_user_id": "123456789",
            "updated_at": "2026-05-19T12:00:00+00:00"
        }
    ]
}
```

**响应字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 任务 ID |
| ordinal | int | 任务序号 |
| display_name | string | 展示名称 |
| goal | string | 任务目标 |
| status | string | 任务状态 |
| owner_user_id | string \| null | 任务所属用户 ID |
| updated_at | string \| null | 更新时间，ISO 8601 格式 |

---

## 5. 长任务详情

**端点**：`GET /api/agent_debug/tasks/{task_id}`

**描述**：获取单个长任务详情，包括任务主记录、步骤列表与执行日志。

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| task_id | string | 是 | 任务 ID |

**响应示例**：

```json
{
    "status": 0,
    "msg": "success",
    "data": {
        "task": {
            "id": "task_001",
            "ordinal": 12,
            "display_name": "整理群公告草案",
            "goal": "根据近期讨论生成群公告草案",
            "status": "running",
            "review_notes": "需要管理员审核最终内容",
            "broadcast_targets": ["group:789012"]
        },
        "steps": [
            {
                "id": "step_001",
                "seq": 1,
                "description": "收集最近 24 小时群聊要点",
                "status": "done",
                "schedule_kind": "immediate",
                "result_summary": "已汇总 5 条要点"
            }
        ],
        "logs": [
            {
                "event_type": "decision",
                "content": "创建任务计划",
                "timestamp": "2026-05-19T12:00:00+00:00"
            }
        ]
    }
}
```

**错误示例**：

```json
{
    "status": -1,
    "msg": "任务不存在",
    "data": null
}
```

**响应字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| task | object | 任务主记录 |
| task.id | string | 任务 ID |
| task.ordinal | int | 任务序号 |
| task.display_name | string | 展示名称 |
| task.goal | string | 任务目标 |
| task.status | string | 任务状态 |
| task.review_notes | string \| null | 审核备注 |
| task.broadcast_targets | array \| null | 广播目标 |
| steps | array | 任务步骤列表 |
| steps[].id | string | 步骤 ID |
| steps[].seq | int | 步骤序号 |
| steps[].description | string | 步骤描述 |
| steps[].status | string | 步骤状态 |
| steps[].schedule_kind | string \| null | 调度类型 |
| steps[].result_summary | string \| null | 步骤结果摘要 |
| logs | array | 执行日志列表，最多返回 200 条 |
| logs[].event_type | string | 事件类型 |
| logs[].content | string | 日志内容 |
| logs[].timestamp | string \| null | 日志时间，ISO 8601 格式 |

---

## 6. 人工改写任务步骤

**端点**：`POST /api/agent_debug/tasks/{task_id}/step/{step_id}`

**描述**：人工改写 AI 制定的步骤描述。提交内容会执行 `strip()` 并截断到 2000 字符，同时写入一条任务日志。

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| task_id | string | 是 | 任务 ID |
| step_id | string | 是 | 步骤 ID |

**请求体**：

```json
{
    "description": "重新整理任务步骤：先收集资料，再生成摘要，最后等待管理员审核。"
}
```

**请求体字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| description | string | 是 | 新步骤描述，服务端最多保留 2000 字符 |

**响应示例**：

```json
{
    "status": 0,
    "msg": "success",
    "data": {
        "step_id": "step_001"
    }
}
```

---

## 7. 手动终止长任务

**端点**：`POST /api/agent_debug/tasks/{task_id}/abort`

**描述**：管理员手动终止一个长任务，并注销该任务关联的执行 Job。

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| task_id | string | 是 | 任务 ID |

**响应示例**：

```json
{
    "status": 0,
    "msg": "success",
    "data": {
        "task_id": "task_001"
    }
}
```

---

## 8. 查看 self_model 演化层

**端点**：`GET /api/agent_debug/self_model`

**描述**：查看指定 Bot 的 self_model 演化层数据，包括承诺、偏好、反思等字段。具体字段由 self cognition 模块维护。

**请求参数**（Query）：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| bot_id | string | 否 | default | Bot ID |

**响应示例**：

```json
{
    "status": 0,
    "msg": "success",
    "data": {
        "commitments": ["保持回复简洁"],
        "preferences_learned": ["优先使用中文交流"],
        "recurring_topics": ["长任务编排"],
        "self_notes": ["需要在执行长任务前确认目标"]
    }
}
```

**说明**：当前 self_model 合法字段为 `commitments`、`preferences_learned`、`recurring_topics`、`self_notes`；前端仍应按对象动态渲染，以兼容后续字段扩展。

---

## 9. 覆盖修正 self_model 字段

**端点**：`POST /api/agent_debug/self_model`

**描述**：人工修正跑偏的 self_model 字段。该接口以整字段覆盖方式写入，适合管理员清理错误承诺、偏好或反思。

**请求体**：

```json
{
    "bot_id": "default",
    "field": "commitments",
    "items": [
        "保持回复简洁",
        "在执行危险操作前进行确认"
    ]
}
```

**请求体字段说明**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| bot_id | string | 否 | default | Bot ID |
| field | string | 是 | - | 要覆盖的 self_model 字段，必须属于后端允许字段集合 |
| items | array[string] | 是 | - | 新字段值，整字段覆盖 |

**响应示例**：

```json
{
    "status": 0,
    "msg": "success",
    "data": {
        "field": "commitments",
        "count": 2
    }
}
```

**错误示例**：

```json
{
    "status": -1,
    "msg": "非法字段，须为 [...] 之一",
    "data": null
}
```

---

## 前端使用建议

### 调试台页面布局

建议前端将 Agent Debug 页面拆分为三个 Tab：

1. **Memory Graph View**
   - 输入或选择 `scope_key` 后调用 `GET /api/agent_debug/memory/edges`。
   - 使用表格或图谱组件展示 Edge，突出 `decay_score`、`mention_count` 与 `invalid_at`。
   - 对误记忆提供“软删除”按钮，调用 `POST /api/agent_debug/memory/edge/{edge_id}/invalidate`。
   - 额外调用 `GET /api/agent_debug/memory/conflicts` 展示冲突事实。

2. **Orchestration Board**
   - 调用 `GET /api/agent_debug/tasks` 展示长任务看板。
   - 点击任务后调用 `GET /api/agent_debug/tasks/{task_id}` 展示步骤与日志。
   - 对待修正步骤提供编辑入口，调用 `POST /api/agent_debug/tasks/{task_id}/step/{step_id}`。
   - 对异常卡死任务提供终止入口，调用 `POST /api/agent_debug/tasks/{task_id}/abort`。

3. **Persona Evolution Inspector**
   - 调用 `GET /api/agent_debug/self_model` 展示 self_model 字段。
   - 对数组字段提供可编辑列表。
   - 保存时调用 `POST /api/agent_debug/self_model`，注意该接口是整字段覆盖，前端应在保存前提示用户。

### 风险提示

- Edge 失效接口是软删除，但会影响后续记忆检索结果，建议前端增加二次确认。
- Step 改写会直接影响长任务后续执行，应记录操作者与变更原因（如前端具备审计能力）。
- self_model 覆盖接口会整字段替换，前端应避免只提交增量项。
