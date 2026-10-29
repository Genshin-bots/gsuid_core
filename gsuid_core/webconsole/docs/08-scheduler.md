# 8. 调度器 API - /api/scheduler

列表 / 暂停 / 恢复 / 触发 / 删除都在**线程池**里访问 APScheduler jobstore。
不要在事件循环线程里调用 `scheduler.get_jobs()` / `get_job()`：jobstore 锁会卡住 WS 与 AI。

## 8.1 获取任务列表
```
GET /api/scheduler/jobs
```

**Query 参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| search | string | 否 | 按任务名/描述模糊匹配 |
| limit | integer | 否 | 单页条数（1–200）；**不传则返回全量** |
| offset | integer | 否 | 偏移量，默认 0 |

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
    ],
    "meta": {
        "total": 45,
        "count": 20,
        "limit": 20,
        "offset": 0,
        "has_more": true,
        "counts": {"total": 45, "running": 40, "paused": 5, "error": 0}
    }
}
```

> `data` **仍是数组**（旧前端直接 `jobs.map(...)` 遍历），所以分页与统计信息放在**信封的
> `meta`** 上——把 `data` 改成对象会打断旧前端。
>
> - `meta.total`：命中 `search` 的总条数
> - `meta.count`：本页条数
> - `meta.has_more`：是否还有下一页
> - `meta.counts`：各状态任务数，对**全量**任务统计，不随 `search`/翻页变化

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
