# 8. 调度器 API - /api/scheduler

## 8.1 获取任务列表
```
GET /api/scheduler/jobs
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "id": "job_id",
            "name": "任务名称",
            "description": "任务描述",
            "next_run_time": "2024-01-01 12:00:00",
            "trigger": "date",
            "trigger_description": "...",
            "paused": false
        }
    ]
}
```

---

## 8.2 手动触发任务
```
POST /api/scheduler/jobs/{job_id}/run
```

---

## 8.3 删除任务
```
DELETE /api/scheduler/jobs/{job_id}
```

---

## 8.4 暂停任务
```
POST /api/scheduler/jobs/{job_id}/pause
```

---

## 8.5 恢复任务
```
POST /api/scheduler/jobs/{job_id}/resume
```
