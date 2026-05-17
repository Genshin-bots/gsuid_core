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
                "log_id": 42,
                "date": "2024-01-01",
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

**响应字段说明**：
- `id`: 分页后的序号（页面内递增）
- `log_id`: 日志在当天日志文件中的原始行号（用于上下文查询的定位标识）
- `date`: 日志所属日期，格式 YYYY-MM-DD（用于上下文查询的定位标识）
- 其余字段同前

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

## 7.5 获取日志上下文
```
GET /api/logs/context
```

当用户通过搜索定位到某条关键日志后，可使用此接口获取该日志前后的上下文日志，帮助快速理解关键日志的发生背景。

**Query 参数**：
- `log_id`: **必填**，目标日志的原始行号（来自 `/api/logs` 返回的 `log_id` 字段）
- `date`: **必填**，目标日志所在日期，格式 YYYY-MM-DD（来自 `/api/logs` 返回的 `date` 字段）
- `before`: 获取目标日志之前的日志条数，默认10，最大100
- `after`: 获取目标日志之后的日志条数，默认10，最大100

**使用示例**：
```bash
# 获取 log_id=42 前后各10条日志
GET /api/logs/context?log_id=42&date=2024-01-01

# 获取 log_id=42 前各5条、后各20条日志
GET /api/logs/context?log_id=42&date=2024-01-01&before=5&after=20

# 获取 log_id=100 前各50条日志（不获取后续日志）
GET /api/logs/context?log_id=100&date=2024-01-01&before=50&after=0
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "target": {
            "log_id": 42,
            "date": "2024-01-01",
            "timestamp": "2024-01-01 12:00:00",
            "level": "error",
            "source": "core",
            "message": "数据库连接失败"
        },
        "before_logs": [
            {
                "log_id": 41,
                "date": "2024-01-01",
                "timestamp": "2024-01-01 11:59:58",
                "level": "info",
                "source": "core",
                "message": "正在尝试连接数据库..."
            }
        ],
        "after_logs": [
            {
                "log_id": 43,
                "date": "2024-01-01",
                "timestamp": "2024-01-01 12:00:02",
                "level": "warn",
                "source": "core",
                "message": "将在5秒后重试连接"
            }
        ],
        "before_count": 10,
        "after_count": 10,
        "total_in_date": 500,
        "has_more_before": true,
        "has_more_after": true
    }
}
```

**响应字段说明**：
- `target`: 目标日志的完整信息
- `before_logs`: 目标日志之前的日志列表（按时间正序排列，最近的在前）
- `after_logs`: 目标日志之后的日志列表（按时间正序排列）
- `before_count`: 实际返回的前置日志条数
- `after_count`: 实际返回的后置日志条数
- `total_in_date`: 该日期当天的日志总条数
- `has_more_before`: 前方是否还有更多日志（用于前端判断是否显示"加载更多"按钮）
- `has_more_after`: 后方是否还有更多日志

**错误响应**：
```json
// 日期不存在
{"status": 404, "msg": "该日期的日志不存在", "data": null}

// log_id 不存在
{"status": 404, "msg": "未找到指定的日志条目", "data": null}
```

**前端集成建议**：
1. 在日志搜索结果列表中，每条日志记录都包含 `log_id` 和 `date` 字段
2. 用户点击某条日志的"查看上下文"按钮时，调用此接口传入对应的 `log_id` 和 `date`
3. 利用 `has_more_before` / `has_more_after` 字段实现"加载更多"的无限滚动体验
4. 可通过增大 `before` / `after` 参数值实现分页式上下文加载

---

## 7.6 实时日志流
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

## 7.7 搜索功能说明

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
