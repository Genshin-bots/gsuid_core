# 7. 日志 API - /api/logs

## 7.1 获取日志列表
```
GET /api/logs
```

**Query 参数**：
- `date`: 日期 YYYY-MM-DD，默认今天
- `level`: 级别筛选 (info/warn/error/debug)
- `source`: 来源筛选 (api/auth/database/scheduler/core)
- `page`: 页码，默认1
- `per_page`: 每页数量，默认50

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "count": 100,
        "rows": [
            {
                "id": 1,
                "timestamp": "2024-01-01 12:00:00",
                "level": "info",
                "source": "core",
                "message": "日志内容",
                "details": null
            }
        ],
        "page": 1,
        "per_page": 50
    }
}
```

---

## 7.2 获取可用日期列表
```
GET /api/logs/available-dates
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": ["2024-01-01", "2023-12-31"]
}
```

---

## 7.3 获取日志来源
```
GET /api/logs/sources
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": ["api", "auth", "database", "scheduler", "core"]
}
```

---

## 7.4 获取日志统计
```
GET /api/logs/stats
```

**Query 参数**：同 7.1

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "total": 100,
        "pages": 2,
        "count_by_level": {
            "info": 80,
            "warn": 15,
            "error": 5
        }
    }
}
```
