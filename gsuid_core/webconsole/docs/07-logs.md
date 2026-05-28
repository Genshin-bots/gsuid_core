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

**Query 参数**：
- `level`: 允许推送的日志级别列表，支持重复参数。默认为 `DEBUG`、`INFO`、`ERROR`。传 `all` 时推送全部级别日志。

> **推送策略**：服务端仅推送 `level` 列表中指定的级别日志。默认推送 DEBUG/INFO/ERROR，前端如需切换级别可断开并使用新的 `level` 参数重连 SSE。

**使用示例**：
```bash
# 默认：仅推送 DEBUG、INFO、ERROR
GET /api/logs/stream

# 推送全部级别日志
GET /api/logs/stream?level=all

# 自定义级别组合：推送 DEBUG、INFO、WARNING、ERROR
GET /api/logs/stream?level=DEBUG&level=INFO&level=WARNING&level=ERROR

# 仅推送 ERROR 和 CRITICAL
GET /api/logs/stream?level=ERROR&level=CRITICAL
```

**SSE 数据格式**：
```json
data: {"level": "DEBUG", "message": "...", "message_type": "html", "timestamp": "05-28 10:00:00"}

data: {"level": "INFO", "message": "...", "message_type": "html", "timestamp": "05-28 10:00:01"}
```

**前端接入示例**：
```javascript
// 默认连接（DEBUG/INFO/ERROR）
const eventSource = new EventSource('/api/logs/stream');

// 自定义级别连接
const levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];
const eventSource = new EventSource(`/api/logs/stream?${levels.map(l => `level=${l}`).join('&')}`);

eventSource.onmessage = (event) => {
    const log = JSON.parse(event.data);
    appendLogToConsole(log); // 服务端已过滤，直接显示
};
```

---

## 7.7 获取可用日志级别
```
GET /api/logs/levels
```

返回可用的日志级别列表，供前端实时日志级别下拉选择器使用。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {"label": "全部", "value": "all"},
        {"label": "TRACE", "value": "trace"},
        {"label": "DEBUG", "value": "debug"},
        {"label": "INFO", "value": "info"},
        {"label": "SUCCESS", "value": "success"},
        {"label": "WARNING", "value": "warning"},
        {"label": "ERROR", "value": "error"},
        {"label": "CRITICAL", "value": "critical"}
    ]
}
```

---

## 7.8 追踪日志 API

> **适用范围**：仅追踪**命令执行路径**（用户发送 `/command` 或消息触发插件函数）。AI 核心路径（LLM 调用、tool use 等）有独立的 `ai_session_logs_api`，不走此追踪系统。

### 存储架构

| 层级 | 内容 | 用途 |
|------|------|------|
| 内存（30min TTL） | `TraceLogEntry` 列表 | 活跃追踪实时查询 |
| JSONL 目录 | 元数据（trace_id, command, user_id, status, duration_ms, log_count） | 已完成追踪目录索引 |
| daily log | 每条 JSON 带 `trace_id` 字段 | 完整日志持久化，供扫描提取 |

### 7.8.1 获取追踪列表（统一入口）
```
GET /api/traces?date=2026-05-28&limit=100
```

**Query 参数**：
- `date`: 日期 YYYY-MM-DD，默认今天。用于扫描 JSONL 已完成追踪。
- `limit`: 返回条数上限，默认 500

合并内存中的 **running** 追踪和 JSONL 中的 **completed** 追踪，返回统一目录。前端点击 `trace_id` 后可调用 `GET /api/traces/{trace_id}` 查看详情。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "trace_id": "a1b2c3d4-xxx",
            "command": "签到",
            "user_id": "12345",
            "group_id": "67890",
            "start_time": 1748356800.123,
            "duration_ms": 3000,
            "log_count": 15,
            "status": "completed"
        },
        {
            "trace_id": "e5f6g7h8-xxx",
            "command": "我的自选",
            "user_id": "12345",
            "group_id": "67890",
            "start_time": 1748356900.456,
            "duration_ms": null,
            "log_count": 42,
            "status": "running"
        }
    ]
}
```

**字段说明**：
- `status`: `running` 表示仍在执行中，`completed` 表示已结束
- `duration_ms`: 已完成时有值，running 时为 `null`

### 7.8.2 获取追踪详情
```
GET /api/traces/{trace_id}?date=2026-05-28
```

优先查内存；未命中时扫描 daily log 按 `trace_id` 提取完整日志。

**Query 参数**：
- `date`: 日期 YYYY-MM-DD，默认今天。用于定位 JSONL 目录和 daily log 文件。

**响应**（running 状态）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "trace_id": "e5f6g7h8-xxx",
        "command": "我的自选",
        "user_id": "12345",
        "group_id": "67890",
        "bot_id": "Bot",
        "session_id": "Bot%%%67890%%%12345",
        "start_time": 1748356900.456,
        "status": "running",
        "logs": [
            {"timestamp": "05-28 10:01:30", "level": "trace", "event": "[核心执行] 函数 xxx 开始执行"},
            {"timestamp": "05-28 10:01:31", "level": "info", "event": "[命令触发] ..."}
        ]
    }
}
```

**响应**（completed 状态）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "trace_id": "a1b2c3d4-xxx",
        "command": "签到",
        "user_id": "12345",
        "group_id": "67890",
        "bot_id": "Bot",
        "session_id": "Bot%%%67890%%%12345",
        "start_time": 1748356800.123,
        "duration_ms": 3000,
        "log_count": 15,
        "status": "completed",
        "logs": [
            {"timestamp": "05-28 10:00:00", "level": "trace", "event": "[核心执行] 函数 xxx 开始执行"},
            {"timestamp": "05-28 10:00:01", "level": "info", "event": "[命令完成] 签到成功"}
        ]
    }
}
```

**查询路径**：
1. 内存未命中 → 查 JSONL 目录确认元数据 → 从 daily log 扫描提取完整日志
2. 即使追踪仍在执行中（status=running），也可通过 `trace_id` 持续查询获取最新日志

**错误响应**：
```json
{"status": 404, "msg": "追踪不存在", "data": null}
```

---

## 7.9 搜索功能说明

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
