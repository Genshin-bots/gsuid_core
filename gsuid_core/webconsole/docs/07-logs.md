# 7. 日志 API - /api/logs

## 7.1 获取日志列表
```
GET /api/logs
```

**Query 参数**：
- `date`: 日期 YYYY-MM-DD，默认今天（与 start_date/end_date 互斥）
- `start_date`: 开始日期 YYYY-MM-DD，与 end_date 配合使用进行多日期搜索
- `end_date`: 结束日期 YYYY-MM-DD，与 start_date 配合使用进行多日期搜索
- `level`: 级别筛选 (info/warn/error/debug)
- `source`: 来源筛选 (api/auth/database/scheduler/core)
- `search`: 文本搜索，匹配日志内容（支持模糊匹配）
- `page`: 页码，默认1
- `per_page`: 每页数量，默认50

**使用示例**：
```bash
# 单日期搜索
GET /api/logs?date=2024-01-01

# 日期范围搜索
GET /api/logs?start_date=2024-01-01&end_date=2024-01-07

# 日期+等级+文本组合搜索
GET /api/logs?date=2024-01-01&level=error&search=数据库连接失败

# 多日期范围+文本搜索
GET /api/logs?start_date=2024-01-01&end_date=2024-01-07&level=warn&search=超时
```

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

**使用示例**：
```bash
# 单日期统计
GET /api/logs/stats?date=2024-01-01

# 多日期范围统计
GET /api/logs/stats?start_date=2024-01-01&end_date=2024-01-07

# 带筛选条件的统计
GET /api/logs/stats?date=2024-01-01&level=error&search=失败
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "total": 100,
        "total_pages": 2,
        "per_page": 100,
        "info_count": 80,
        "warn_count": 15,
        "error_count": 5,
        "debug_count": 0
    }
}
```

---

## 7.5 实时日志流
```
GET /api/logs/stream
```

使用 Server-Sent Events (SSE) 实时推送日志内容。

**响应**：SSE 流格式
```
data: [2024-01-01 12:00:00] [INFO] 日志内容

data: [2024-01-01 12:00:01] [ERROR] 错误信息
```

---

## 7.6 搜索功能说明

日志搜索功能支持以下组合方式：

| 场景 | 参数组合 |
|------|----------|
| 单日期搜索 | `date` |
| 日期范围搜索 | `start_date` + `end_date` |
| 单日期 + 等级筛选 | `date` + `level` |
| 单日期 + 文本搜索 | `date` + `search` |
| 单日期 + 等级 + 文本 | `date` + `level` + `search` |
| 多日期 + 等级 + 文本 | `start_date` + `end_date` + `level` + `search` |

**注意**：
- `date` 与 `start_date/end_date` 互斥，不能同时使用
- `search` 参数支持模糊匹配，不区分大小写
- 多日期范围搜索时，页码基于所有匹配日志的总数计算
