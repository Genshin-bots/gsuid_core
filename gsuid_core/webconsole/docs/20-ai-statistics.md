# 20. AI Statistics API - /api/ai/statistics

提供 AI 模块的完整统计数据，包括 Token 消耗、费用估算、延迟统计、意图分布、Heartbeat 决策、RAG 效果等。

## 20.1 获取统计数据摘要

```
GET /api/ai/statistics/summary
```

**Query 参数**:
- `date`: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天（获取今日实时数据）。指定日期时从数据库查询历史数据

**`token_usage` 字段说明**:
- `total_input_tokens` / `total_output_tokens`: 当日输入 / 输出 Token 总量
- `total_cache_read_tokens`: 命中提示词缓存、按缓存价计费的读取 Token 总量（Anthropic / OpenAI 等的 prompt caching）
- `total_cache_write_tokens`: 写入提示词缓存所产生的 Token 总量
- `by_model` / `by_type`: 按模型名 / 会话类型（group / private）拆分，每项均含上述四类 Token（`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens`）

> 缓存 Token 字段对今日实时（内存）与历史（数据库）数据均返回；旧版本写入的历史行无缓存数据时按 `0` 返回。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "date": "2024-01-15",
        "token_usage": {
            "total_input_tokens": 150000,
            "total_output_tokens": 80000,
            "total_cache_read_tokens": 60000,
            "total_cache_write_tokens": 12000,
            "by_model": [
                {
                    "model": "gpt-4",
                    "input_tokens": 100000,
                    "output_tokens": 50000,
                    "cache_read_tokens": 40000,
                    "cache_write_tokens": 8000
                }
            ],
            "by_type": [
                {
                    "type": "group",
                    "input_tokens": 80000,
                    "output_tokens": 40000,
                    "cache_read_tokens": 32000,
                    "cache_write_tokens": 6000
                },
                {
                    "type": "private",
                    "input_tokens": 70000,
                    "output_tokens": 40000,
                    "cache_read_tokens": 28000,
                    "cache_write_tokens": 6000
                }
            ]
        },
        "latency": {
            "avg": 1.5,
            "p95": 3.2
        },
        "intent_distribution": {
            "chat": {"count": 120, "percentage": 40.0},
            "tool": {"count": 80, "percentage": 26.7},
            "qa": {"count": 100, "percentage": 33.3}
        },
        "errors": {
            "timeout": 2,
            "rate_limit": 1,
            "network_error": 0,
            "usage_limit": 0,
            "agent_error": 1,
            "api_529_error": 0,
            "total": 4
        },
        "heartbeat": {
            "should_speak_true": 45,
            "should_speak_false": 30,
            "conversion_rate": 60.0
        },
        "trigger_distribution": {
            "mention": {"count": 150, "percentage": 50.0},
            "keyword": {"count": 100, "percentage": 33.3},
            "heartbeat": {"count": 50, "percentage": 16.7},
            "scheduled": {"count": 20, "percentage": 6.7}
        },
        "rag": {
            "hit_count": 80,
            "miss_count": 20,
            "hit_rate": 80.0
        },
        "memory": {
            "observations": 25,
            "ingestions": 20,
            "ingestion_errors": 2,
            "retrievals": 50,
            "entities_created": 10,
            "edges_created": 15,
            "episodes_created": 5
        },
        "active_users": [
            {
                "group_id": "123456",
                "user_id": "user001",
                "ai_interaction": 30,
                "message_count": 100
            }
        ]
    }
}
```

---

## 20.2 获取按模型分组的 Token 消耗

```
GET /api/ai/statistics/token-by-model
```

**Query 参数**:
- `date`: 日期字符串 (YYYY-MM-DD)，默认为今天

**说明**: 即 `summary` 接口 `token_usage.by_model` 的内容，每项含输入 / 输出及缓存读写四类 Token。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "model": "gpt-4",
            "input_tokens": 100000,
            "output_tokens": 50000,
            "cache_read_tokens": 40000,
            "cache_write_tokens": 8000
        },
        {
            "model": "gpt-3.5-turbo",
            "input_tokens": 50000,
            "output_tokens": 30000,
            "cache_read_tokens": 20000,
            "cache_write_tokens": 4000
        }
    ]
}
```

---

## 20.3 获取活跃用户/群组排行

```
GET /api/ai/statistics/active-users
```

**Query 参数**:
- `date`: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天
- `limit`: 返回数量限制，默认为 20

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "group_id": "123456",
            "user_id": "user001",
            "ai_interaction": 30,
            "message_count": 100
        }
    ]
}
```

---

## 20.4 获取触发方式占比

```
GET /api/ai/statistics/trigger-distribution
```

**Query 参数**:
- `date`: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "mention": {"count": 150, "percentage": 50.0},
        "keyword": {"count": 100, "percentage": 33.3},
        "heartbeat": {"count": 50, "percentage": 16.7}
    }
}
```

---

## 20.5 获取意图分布统计

```
GET /api/ai/statistics/intent-distribution
```

**Query 参数**:
- `date`: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "chat": {"count": 120, "percentage": 40.0},
        "tool": {"count": 80, "percentage": 26.7},
        "qa": {"count": 100, "percentage": 33.3}
    }
}
```

---

## 20.6 获取错误统计

```
GET /api/ai/statistics/errors
```

**Query 参数**:
- `date`: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "timeout": 2,
        "rate_limit": 1,
        "network_error": 0,
        "usage_limit": 0,
        "agent_error": 1,
        "api_529_error": 0,
        "total": 4
    }
}
```

---

## 20.7 获取 Heartbeat 巡检统计

```
GET /api/ai/statistics/heartbeat
```

**Query 参数**:
- `date`: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "should_speak_true": 45,
        "should_speak_false": 30,
        "conversion_rate": 60.0
    }
}
```

---

## 20.8 获取 RAG 知识库效果统计

```
GET /api/ai/statistics/rag
```

**Query 参数**:
- `date`: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

**说明**: RAG 统计是全局数据，不区分 bot_id。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "hit_count": 80,
        "miss_count": 20,
        "hit_rate": 80.0
    }
}
```

---

## 20.9 获取 RAG 文档命中统计

```
GET /api/ai/statistics/rag/documents
```

**说明**: RAG 文档命中统计是全局累计数据，不区分日期和 bot_id。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "document_name": "游戏攻略",
            "hit_count": 15
        },
        {
            "document_name": "角色介绍",
            "hit_count": 8
        }
    ]
}
```

---

## 20.10 获取历史统计数据

```
GET /api/ai/statistics/history
```

**Query 参数**:
- `days`: 查询天数，默认为 7

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "date": "2024-01-14"
        },
        {
            "date": "2024-01-15"
        }
    ]
}
```
