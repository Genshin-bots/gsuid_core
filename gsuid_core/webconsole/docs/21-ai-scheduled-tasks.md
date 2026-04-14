# 21. AI Scheduled Task API - /api/ai/scheduled_tasks

AI 定时任务 API，用于管理 AI 创建的定时/循环任务，支持增删改查启停。

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
            "session_id": "onebot%%%private%%%user_001",
            "task_prompt": "帮我关注股市行情",
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
        "session_id": "onebot%%%private%%%user_001",
        "task_prompt": "帮我关注股市行情",
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

## 21.5 删除任务
```
DELETE /api/ai/scheduled_tasks/{task_id}
```

**响应**:
```json
{
    "status": 0,
    "msg": "任务已取消"
}
```

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
