# Agent Mesh Kanban API - `/api/ai/kanban`

> 后端实现：`gsuid_core/webconsole/kanban_api.py`
>
> 数据来源：`gsuid_core/ai_core/planning/models.py`（`aiagenttask` 表，
> `node_kind="root|subtask"` 区分根 / 子任务）+ `aiagentartifact` 表 +
> `aiagenttasklog` 事件流。
>
> 关联实施记录：`docs/AGENT_MESH_KANBAN_IMPLEMENTATION_20260522.md`

「Kanban 任务树」是框架统一的多步任务承载：一个**根任务**
（`node_kind="root"`）组织 N 个**子任务节点**（`node_kind="subtask"`，每个独立
分配 `agent_profile`），按依赖关系并发推进，状态映射到 5 列看板（`target` /
`progress` / `Done` / `Blocked` / `failed`）。

调度模型以**事件驱动**为核心——`register_kanban_task` / `respawn_subtask` /
`resume` 等写操作触发 `kick_root` 一次，把所有依赖已满足的子任务并发派出；
子任务完成 / 失败后再自动 kick 下游。Kanban **不支持子任务级等待字段**（没有
`not_before` / 节点级延时），但支持根任务 `recurring_trigger` 周期模板：模板由
`planning/recurring.py` 桥接 APScheduler 到点克隆一次性实例树，再交给 `kick_root`
事件驱动执行。单步提醒仍走 `add_once_task` / `add_interval_task`。

---

## 1. 看板（5 列聚合视图）

```
GET /api/ai/kanban/board
```

**Query 参数**（全部可选，AND 组合）：

| 参数 | 说明 |
|------|------|
| `scope_key` | 作用域 |
| `bot_id` | 关联的 bot |
| `group_id` | 群号 |
| `owner_user_id` | 任务发起人 |
| `include_children` | 默认 `true`，是否同时把子任务卡片混入对应列 |
| `status` | 按原始 `AIAgentTask.status` 二次筛选 |

**列映射**（设计稿 §3.3）：

| 卡片状态 | 看板列 |
|----------|--------|
| `pending` 且依赖未满足 | `target` |
| `pending` 且依赖满足 即将调度 / `running` | `progress` |
| `completed` / `skipped` | `Done` |
| `paused` / `waiting_approval` | `Blocked` |
| `failed` / `cancelled` | `failed` |

**响应**：

```json
{
  "status": 0,
  "msg": "ok",
  "data": {
    "columns": {
      "target": [],
      "progress": [
        {
          "kind": "root",
          "id": "task_xxx",
          "root_task_id": "task_xxx",
          "parent_task_id": null,
          "ordinal": 3,
          "display": "根据热视频生成新内容",
          "goal": "...",
          "status": "running",
          "kanban_column": "progress",
          "agent_profile": "",
          "persona_name": "早柚",
          "dependency_task_ids": [],
          "respawn_count": 0,
          "failure_reason": null,
          "input_artifact_ids": [],
          "output_artifact_id": null,
          "workspace_path": "data/ai_core/artifacts/task_xxx/task_xxx/workspace",
          "subtask_count": 5,
          "subtask_done_count": 2,
          "created_at": "...",
          "updated_at": "..."
        }
      ],
      "Done": [],
      "Blocked": [],
      "failed": []
    },
    "summary": {
      "task_count": 12,
      "subtask_count": 47,
      "updated_at": "2026-05-22T10:11:12"
    }
  }
}
```

**卡片字段说明**：

| 字段 | 含义 |
|------|------|
| `kind` | `root` / `subtask` |
| `kanban_column` | 后端已计算好的列名，前端可直接据此分桶 |
| `agent_profile` | 子任务的 profile_id（根任务为空） |
| `dependency_task_ids` | 该子任务依赖的兄弟子任务 id 列表 |
| `respawn_count` | 已重派次数；≥3 时框架会自动转 `waiting_approval` |
| `failure_reason` | 最近一次失败原因；前端在 Blocked / failed 卡上展示 |
| `output_artifact_id` | 该节点的最终产出 `res_xxx`；点击跳 artifact 详情 |
| `workspace_path` | 该节点的 Artifact Workspace 路径，给"打开工作区"按钮 |
| `subtask_count` / `subtask_done_count` | 根任务子任务统计；前端进度条 |

---

## 2. 任务详情

```
GET /api/ai/kanban/tasks/{task_id}?log_limit=200
```

`task_id` 可以是根任务或子任务 id。返回：

```jsonc
{
  "data": {
    "task": { ...本任务卡片... },
    "root": { ...所属根任务卡片... },
    "subtasks": [ ...所有兄弟子任务卡片... ],
    "logs": [
      { "event_type": "plan_created", "content": "Kanban 任务树创建：1 根 + 5 子任务", "timestamp": "..." },
      { "event_type": "step_started", "content": "...", "step_id": null },
      { "event_type": "workspace_violation", "content": "工作区越界拒绝：write_file_content 越界拒绝: C:/...", "timestamp": "..." }
    ],
    "artifacts": [
      {
        "id": "res_abc...",
        "kind": "output",
        "summary": "周报草稿",
        "mime": "text/markdown",
        "size_bytes": 1234,
        "from_profile": "internal_reporter",
        "task_id": "...",
        "created_at": "...",
        "is_image": false,
        "has_inline": true,
        "has_payload_path": false,
        "payload_preview": "# 周报\n## 关键指标\n...",   // inline 直接吐；落盘文本 mime 且 ≤64KB 时也读出
        "raw_url": null                                    // 落盘文件统一带 /api/ai/artifacts/{id}/raw
      }
    ],
    "root_artifacts": [ /* 当 task_id 是根任务时额外返回整棵树所有 artifact，省去逐子任务请求 */ ]
  }
}
```

**前端建议**：
- 根任务详情用"父 + 子任务列表"布局；子任务详情用"父任务徽标 + 本节点细节"。
- 事件流里 `workspace_violation` / `approval` / `step_failed` 事件用醒目颜色高亮——它们是主人格 / 主人需要介入的信号。
- artifact 渲染：`is_image=true` 走 `<img src={raw_url}>` 直接挂图；落盘文本类（`has_payload_path && !is_image`）也用 `raw_url` 提供下载链接；`payload_preview` 已经包含 ≤ 8KB 的预览内容，无需二次请求 `/api/ai/artifacts/{id}` 获取详情。
- 根任务详情自带 `root_artifacts`（整棵树所有产物）——快速展示"这个任务总共生成了什么"无需为每个子任务发请求。

---

## 3. 管理端直接创建任务树（绕过 LLM 评估）

```
POST /api/ai/kanban/tasks
```

**请求体**（用于演示 / 调试；生产创建应走 `register_kanban_task` LLM 工具
以保证经过能力评估）：

```json
{
  "goal": "整理本周热点 + 生成周报海报",
  "persona_name": "早柚",
  "bot_id": "onebot",
  "owner_user_id": "admin",
  "interval_hours": 0,
  "subtasks": [
    {
      "description": "拉取本周热点话题",
      "agent_profile": "research_agent",
      "depends_on": []
    },
    {
      "description": "整理本周内部数据并写成周报草稿",
      "agent_profile": "internal_reporter",
      "depends_on": [0]
    },
    {
      "description": "把周报渲染成海报",
      "agent_profile": "code_agent",
      "depends_on": [1]
    }
  ]
}
```

请求体说明：

- `subtasks[i].depends_on` 用本数组的 0-based 下标；后端会自动转换成真实兄弟
  子任务 id。
- 管理端创建端点不挂任何定时器；生产周期多步任务应由主人格调用
  `register_kanban_task(recurring_trigger=...)` 创建周期模板。单步指定时刻提醒仍配合
  `/api/ai/scheduled_tasks` 唤醒主人格。

返回新建的 `task`（根） + `subtasks`（子节点）卡片列表，并立即触发一次调度
（`kick_root`），无依赖的子任务会立刻进入 `progress` 列。

---

## 4. 状态操作

| 方法 | 路径 | 行为 |
|------|------|------|
| POST | `/api/ai/kanban/tasks/{task_id}/pause` | 暂停（根 / 子均可） |
| POST | `/api/ai/kanban/tasks/{task_id}/resume` | 恢复并 `kick_root` 一次 |
| POST | `/api/ai/kanban/tasks/{task_id}/fail` | 软终结（根任务 `cascade=true` 级联标记 failed，保留表数据 / artifact） |
| DELETE | `/api/ai/kanban/tasks/{task_id}/hard` | **硬删除**单棵任务树（删表 + 可选删 workspace / payload 文件） |
| DELETE | `/api/ai/kanban/tasks` | **批量硬删除**任务树（按 scope / bot / owner / status 筛选） |
| POST | `/api/ai/kanban/subtasks/{task_id}/respawn` | 复活 failed / waiting_approval 子任务 |
| POST | `/api/ai/kanban/subtasks/{task_id}/approve` | 审批 waiting_approval 子任务 |
| PATCH | `/api/ai/kanban/subtasks/{task_id}` | 修正子任务字段（display_name / goal / agent_profile / 依赖 / params） |

### 4.1 fail 请求体

```json
{
  "reason": "需求变更，整树作废",
  "cascade": true
}
```

- 根任务 + `cascade=true` → 根任务 `failed` + 未完成子任务级联 `failed`；
- 子任务 → 仅本节点 `failed`，根任务由 `refresh_root_status` 重新汇总；
  汇总规则下"全 failed 子任务" **不会**自动把根任务染成 failed，避免单点失败
  误终结整树——只有主人格 / 主人显式 `fail` 根任务才级联。

### 4.2 hard delete 查询参数

```
DELETE /api/ai/kanban/tasks/{task_id}/hard?delete_files=true&include_instances=false
```

| 参数 | 默认 | 含义 |
|------|------|------|
| `delete_files` | `true` | 同时删除 `data/ai_core/artifacts/{root_task_id}/` 下的 workspace / artifact payload 文件 |
| `include_instances` | `false` | 当 `task_id` 是周期模板根任务时，是否连同该模板已克隆出的历史实例树一起删除 |

行为说明：

- 这是不可逆操作，会删除 `aiagenttask`、`aiagenttasklog`、`aiagentartifact` 相关行。
- 传入根任务 id → 删除整棵树。
- 传入子任务 id → 解析到所属根任务并删除整棵树，避免留下断裂依赖边。
- 若根任务是周期模板，会先尝试从 APScheduler 摘除对应 job；是否删除历史实例由
  `include_instances` 控制。
- 成功响应中的 `data` 会返回 `tasks_deleted`、`logs_deleted`、`artifacts_deleted`、
  `files_deleted`、`dirs_deleted`、`unscheduled_jobs` 等统计。

### 4.3 批量删除（按分类选择）

```
DELETE /api/ai/kanban/tasks?status=completed&delete_files=true
```

**Query 参数**（必须至少传一个筛选条件）：

| 参数 | 说明 |
|------|------|
| `scope_key` | 作用域筛选 |
| `bot_id` | 关联 bot 筛选 |
| `group_id` | 群号筛选 |
| `owner_user_id` | 任务发起人筛选 |
| `status` | **按原始状态筛选**，支持 `completed` / `failed` / `running` / `pending` / `paused` / `waiting_approval` / `cancelled` |
| `delete_files` | 默认 `true`，同时删除 workspace / payload 文件 |
| `include_instances` | 默认 `false`，删除周期模板时是否连带历史实例树 |

**安全机制**：

- 未传任何筛选条件时接口直接返回 `status=1`，拒绝执行，防止误删全部任务。
- 前端应在调用前弹二次确认，并展示"本次将删除 N 棵任务树"的预估提示（可先调
  `GET /api/ai/kanban/board?status=xxx` 做预览）。

**响应示例**：

```json
{
  "status": 0,
  "msg": "批量删除完成：成功 12 棵，失败 0 棵",
  "data": {
    "deleted_count": 12,
    "failed_count": 0,
    "matched_count": 12,
    "tasks_deleted": 87,
    "logs_deleted": 340,
    "artifacts_deleted": 25,
    "files_deleted": 18,
    "dirs_deleted": 12,
    "unscheduled_jobs": 0,
    "root_ids": ["task_aaa", "task_bbb", "..."],
    "failed_root_ids": []
  }
}
```

常见使用场景：

| 场景 | 请求示例 |
|------|----------|
| 删除全部已完成任务 | `DELETE /api/ai/kanban/tasks?status=completed` |
| 删除全部推进中任务 | `DELETE /api/ai/kanban/tasks?status=running` |
| 删除全部失败任务 | `DELETE /api/ai/kanban/tasks?status=failed` |
| 删除某用户的全部任务 | `DELETE /api/ai/kanban/tasks?owner_user_id=admin` |
| 删除某 Bot 的全部 pending 任务 | `DELETE /api/ai/kanban/tasks?bot_id=onebot&status=pending` |

### 4.4 respawn 请求体

```json
{
  "new_description": "改成只统计中文渠道的热点",
  "new_params": { "lang": "zh" },
  "new_agent_profile": "research_agent"
}
```

- `respawn_count` 自增；达 3 次后框架强制转 `waiting_approval`，端点返回
  `status=1` + `已转待审批`，前端应该提示"请用 approve 端点 / 主人格审批"。
- `new_agent_profile` 必须是已注册的 profile_id；不存在时 `status=1`。

### 4.5 approve 请求体

```json
{ "approved": true, "note": "可以继续" }
```

- `approved=true` → 子任务 `pending`，根任务 `kick_root` 一次；
- `approved=false` → 子任务 `failed`，根任务**不会**自动级联失败——主人格
  可后续选择 `respawn` 或 `fail_task_tree`。

### 4.6 PATCH 子任务请求体

```jsonc
{
  "display_name": "新的展示名",
  "goal": "新的描述",
  "agent_profile": "code_agent",
  "dependency_task_ids": ["task_aaa", "task_bbb"],
  "params_override": { "key": "value" }
}
```

所有字段可选；高风险字段（`dependency_task_ids` / `agent_profile`）改动会写入
`AIAgentTaskLog(event_type="decision")`。

---

## 5. 能力评估触发 / 代理候选

### 5.1 触发评估

```
POST /api/ai/capability-agents/evaluate-mesh
```

```json
{
  "user_goal": "帮我每天拉本周热点写周报",
  "owner_user_id": "user_web_01",
  "persona_name": "早柚"
}
```

后端一次性跑 `capability_evaluator` 代理，返回结构化结果：

```jsonc
{
  "data": {
    "covered": true,
    "missing_capabilities": [],
    "available_profiles": ["research_agent", "code_agent", "internal_reporter", ...],
    "suggested_subtasks": [
      {
        "description": "...",
        "required_capability": "内部数据汇总",
        "agent_profile": "internal_reporter",
        "depends_on": [],
        "params_hint": {}
      }
    ],
    "risk_notes": [],
    "summary": "internal_reporter + code_agent 可以覆盖",
    "owner_user_id": "user_web_01",
    "user_goal": "...",
    "created_at": 1716386400.12
  }
}
```

评估结果会被缓存 15 分钟；同期内创建任务树前不再重复评估。

### 5.2 可用代理候选

```
GET /api/ai/capability-agents/kanban-candidates
```

返回**可用于 Kanban 任务树**的画像列表（排除 `capability_evaluator` 这种框架
内部代理）：

```jsonc
{
  "data": {
    "count": 5,
    "items": [
      {
        "profile_id": "research_agent",
        "display_name": "调研助手",
        "when_to_use": "...",
        "match_keywords": [...],
        "tool_names": [...],
        "source": "builtin"
      },
      { "profile_id": "code_agent", "source": "builtin", ... },
      { "profile_id": "internal_reporter", "source": "builtin", ... },
      { "profile_id": "memory_curator", "source": "builtin", ... },
      { "profile_id": "scheduler_assistant", "source": "builtin", ... }
    ]
  }
}
```

---

## 6. 前端界面建议

### 6.1 看板主视图

- 顶部筛选：`scope_key` + `bot_id` + `owner_user_id` 三个下拉；
  另加 「只看根任务」/「展开子任务」 开关。
- 5 列：`target` `progress` `Done` `Blocked` `failed`。
- 卡片元素：
  - `kind` 徽标（🟦 root / 🟢 subtask）；
  - `display` 标题 + 序号；
  - `agent_profile` Tag（不同画像不同颜色）；
  - 子任务卡片左上角折角连到父根；
  - `respawn_count > 0` 加 🔄N 角标，提醒"已重派 N 次"；
  - Blocked / failed 卡片下方展示 `failure_reason` 前 60 字。
- 右上角刷新按钮 → 再调一次 `GET /board`；可选 setInterval 30s 自动刷新。
- 卡片右键 / 三点菜单：「详情」「暂停」「恢复」「重派」「审批通过 / 拒绝」「终结」。

### 6.2 任务详情抽屉

- Tab 1「子任务列表」：列出兄弟子任务，按依赖关系画 DAG / 列表两种模式切换。
- Tab 2「事件日志」：时间线；`workspace_violation` / `step_failed` / `approval`
  用 🔴 高亮，`plan_created` / `step_done` 用 🟢。
- Tab 3「Artifact」：来自 `/api/ai/artifacts?root_task_id=...`，展示 res_id +
  kind + summary，点击跳 Artifact 详情。
- Tab 4「Workspace」：链接到工作区文件 API（参见 `37-workspace.md`）。

### 6.3 评估辅助面板

主人格在对话中的"能力评估失败 → 缺什么"提示，前端可单独提供"测试评估覆盖"
按钮 → `POST /evaluate-mesh`，把结果以 JSON 形式直观展示，帮主人评估是否
需要装插件。

### 6.4 安全提示

- 「终结整树」端点不可逆——前端应弹两次确认，并显示"该操作会级联终止 N 个未完成
  子任务"。
- 「批量删除」端点同样不可逆，且**必须至少传一个筛选条件**（`status` /
  `owner_user_id` / `bot_id` / `scope_key` / `group_id`），未传条件时后端直接
  拒绝。前端建议：
  - 在批量删除按钮旁展示当前筛选条件下的预估数量（先调 `GET /board?status=xxx`
    计数）；
  - 弹二次确认框，列出"将删除 N 棵任务树（含 M 个子任务）"；
  - 提供常见快捷选项：「删除全部已完成」「删除全部失败」「删除全部推进中」。
- `respawn` 第 3 次后会被强制转待审批；前端应该在按钮下方提示"已达重派上限"。
- `PATCH` 子任务里改 `dependency_task_ids` / `agent_profile` 的影响很大，
  应在 UI 上加二次确认。
