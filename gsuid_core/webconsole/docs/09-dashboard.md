# 9. 仪表盘 API - /api/dashboard

## 9.1 获取关键指标
```
GET /api/dashboard/metrics
```

**Query 参数**：
- `bot_id`: Bot ID 筛选，格式 `bot_self_id:bot_id` 或 `all`

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "dau": 100,
        "dag": 50,
        "mau": 1000,
        "mag": 500,
        "retention": "10%",
        "newUsers": 20,
        "churnedUsers": 5,
        "dauMauRatio": "10",
        "dagMagRatio": "10"
    }
}
```

---

## 9.2 获取命令统计
```
GET /api/dashboard/commands
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "date": "2024-01-01",
            "sentCommands": 500,
            "receivedCommands": 1000,
            "commandCalls": 800,
            "imageGenerated": 100
        }
    ]
}
```

---

## 9.3 获取用户群组数据
```
GET /api/dashboard/users-groups
```

**响应**：30天用户/群组变化趋势数据

---

## 9.4 获取命令排行榜
```
GET /api/dashboard/commands/ranking
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "command": "帮助",
            "count": 1000
        }
    ]
}
```

---

## 9.5 获取活跃时段数据
```
GET /api/dashboard/active-time
```

**响应**：24小时各时段活跃度数据

---

## 9.6 近 N 天每日命令总数（日历选择器）

```
GET /api/dashboard/daily/command-counts?days=60&bot_id=all
```

供 Dashboard 日期选择器在每个日期格下展示命令数；`count == 0` 的日期前端应禁用，避免点进空详情。

**Query**：
- `days`：回溯天数，默认 60，夹取 `[1, 366]`
- `bot_id`：`all` 或 `bot_self_id:bot_id`

**口径**：仅汇总 `DataType.USER` 的 `command_count`（与 `/daily/commands` 一致，避免 group 双计）。

**响应**：
```json
{
  "status": 0,
  "msg": "ok",
  "data": [
    { "date": "2026-07-01", "count": 128 },
    { "date": "2026-07-02", "count": 0 }
  ]
}
```

`data` 按日期升序、连续补 0。
