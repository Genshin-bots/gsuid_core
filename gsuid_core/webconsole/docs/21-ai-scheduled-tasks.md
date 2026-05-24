# 21. AI Scheduled Task API - /api/ai/scheduled_tasks

AI 定时任务 API，用于管理 AI 创建的定时/循环任务，支持增删改查启停。

> **任务数据模型补充字段**：
> - `structured_context`：创建任务时填写的结构化上下文（JSON 字符串）。执行任务的 SubAgent 优先从此字段读取上下文（如关联的持久状态键名、广播目标），而非反复解析 `task_prompt`。
> - `last_result_summary`：上次执行的结果摘要。循环任务每次执行后自动回写，下次执行时注入消息，让 SubAgent 了解历史、避免重复操作。

## 21.1 获取任务列表
```
GET /api/ai/scheduled_tasks
```

**Query 参数**:
- `user_id`: 按用户ID筛选（可选）
- `status`: 按状态筛选 pending/paused/executed/failed/cancelled（可选）
- `task_type`: 按任务类型筛选 once/interval（可选）

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "task_id": "scheduled_task_abc123",
            "task_type": "interval",
            "user_id": "user_001",
            "group_id": null,
            "bot_id": "onebot",
            "bot_self_id": "123456",
            "user_type": "direct",
            "persona_name": "default",
            "session_id": "ws-onebot:onebot:bot_001:private:user_001",
            "task_prompt": "帮我关注股市行情",
            "structured_context": "{\"state_key\": \"stock:portfolio\"}",
            "last_result_summary": "上次巡检：账户余额 98500，持仓 2 支",
            "status": "pending",
            "created_at": "2024-05-14T22:00:00",
            "executed_at": null,
            "result": null,
            "error_message": null,
            "interval_seconds": 1800,
            "max_executions": 10,
            "current_executions": 3,
            "start_time": "2024-05-14T22:00:00",
            "next_run_time": "2024-05-14T22:30:00"
        }
    ]
}
```

---

## 21.2 获取任务详情
```
GET /api/ai/scheduled_tasks/{task_id}
```

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "task_id": "scheduled_task_abc123",
        "task_type": "interval",
        "user_id": "user_001",
        "group_id": null,
        "bot_id": "onebot",
        "bot_self_id": "123456",
        "user_type": "direct",
        "persona_name": "default",
        "session_id": "ws-onebot:onebot:bot_001:private:user_001",
        "task_prompt": "帮我关注股市行情",
        "structured_context": "{\"state_key\": \"stock:portfolio\"}",
        "last_result_summary": "上次巡检：账户余额 98500，持仓 2 支",
        "status": "pending",
        "created_at": "2024-05-14T22:00:00",
        "executed_at": null,
        "result": null,
        "error_message": null,
        "interval_seconds": 1800,
        "max_executions": 10,
        "current_executions": 3,
        "start_time": "2024-05-14T22:00:00",
        "next_run_time": "2024-05-14T22:30:00"
    }
}
```

---

## 21.3 创建任务
```
POST /api/ai/scheduled_tasks
```

**请求体**:
```json
{
    "task_type": "interval",
    "interval_type": "minutes",
    "interval_value": 30,
    "task_prompt": "帮我关注股市行情",
    "max_executions": 10
}
```

或一次性任务:
```json
{
    "task_type": "once",
    "run_time": "2024-05-15 06:30:00",
    "task_prompt": "查询英伟达股价"
}
```

**响应**:
```json
{
    "status": 0,
    "msg": "任务创建成功",
    "data": {
        "task_id": "manual_task_20240514220000"
    }
}
```

---

## 21.4 修改任务
```
PUT /api/ai/scheduled_tasks/{task_id}
```

**请求体**:
```json
{
    "task_prompt": "新的任务描述",
    "max_executions": 5
}
```

**响应**:
```json
{
    "status": 0,
    "msg": "任务修改成功"
}
```

---

## 21.5 软删除任务（保留历史）
```
DELETE /api/ai/scheduled_tasks/{task_id}
```

将任务状态改为 `cancelled`，并从 APScheduler 中移除该作业。DB 行保留，
执行历史/结果可继续审计与回溯。

**响应**:
```json
{
    "status": 0,
    "msg": "任务已取消"
}
```

---

## 21.5.1 硬删除任务（彻底移除）
```
DELETE /api/ai/scheduled_tasks/{task_id}/hard
```

彻底删除 DB 行 + 移除 APScheduler 作业，**无法找回**。
适用于前端"清理废弃任务"按钮场景。

**响应**:
```json
{
    "status": 0,
    "msg": "任务已彻底删除",
    "data": {
        "task_id": "scheduled_task_abc123"
    }
}
```

任务不存在时返回 `status: 1, msg: "任务不存在"`。

---

## 21.5.2 批量清空任务（按筛选条件硬删除）
```
DELETE /api/ai/scheduled_tasks?confirm=true[&user_id=...&status=...&task_type=...]
```

按筛选条件批量彻底删除任务。不传任何筛选条件时等同于"全部清空"，
因此**必须**显式传 `confirm=true`，否则直接拒绝。

**Query 参数**:
- `confirm`: 必填，必须为 `true` 才会执行（防误删）
- `user_id`: 仅清空指定用户的任务（可选）
- `status`: 仅清空指定状态的任务（可选，如 `cancelled` / `failed` / `executed`）
- `task_type`: 仅清空指定类型的任务（可选，`once` / `interval`）

**响应**:
```json
{
    "status": 0,
    "msg": "已彻底删除 12 个任务",
    "data": {
        "deleted": 12,
        "matched": 12
    }
}
```

未传 `confirm=true` 时：
```json
{
    "status": 1,
    "msg": "请显式传 confirm=true 以确认批量清空"
}
```

**典型用法**：
- 清空所有已取消的任务：`DELETE /api/ai/scheduled_tasks?status=cancelled&confirm=true`
- 清空某用户的全部任务：`DELETE /api/ai/scheduled_tasks?user_id=user_001&confirm=true`
- 全部清空（**慎用**）：`DELETE /api/ai/scheduled_tasks?confirm=true`

---

## 21.6 暂停任务
```
POST /api/ai/scheduled_tasks/{task_id}/pause
```

**响应**:
```json
{
    "status": 0,
    "msg": "任务已暂停"
}
```

---

## 21.7 恢复任务
```
POST /api/ai/scheduled_tasks/{task_id}/resume
```

**响应**:
```json
{
    "status": 0,
    "msg": "任务已恢复"
}
```

---

## 21.8 获取任务统计
```
GET /api/ai/scheduled_tasks/stats/overview
```

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "total": 15,
        "pending": 5,
        "paused": 2,
        "executed": 6,
        "failed": 1,
        "cancelled": 1,
        "interval_count": 8,
        "once_count": 7
    }
}
```
