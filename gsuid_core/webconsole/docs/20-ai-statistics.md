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

## 20.2.1 近 N 天每日 Token（日历选择器）

```
GET /api/ai/statistics/daily-token-counts?days=60
```

供 `/ai-statistics` 日期选择器在每个日期格下展示 **input token**（前端可压缩为 `3M`）；`input_tokens == 0` 的日期可禁用。

**Query**：
- `days`：回溯天数，默认 60，夹取 `[1, 366]`

**行为**：
- 历史日读 `AIDailyStatistics`；**今日优先内存实时**
- 区间内无数据的日期补 0，序列连续

**响应**：
```json
{
  "status": 0,
  "msg": "ok",
  "data": [
    {
      "date": "2026-07-01",
      "input_tokens": 3200000,
      "output_tokens": 410000,
      "total_tokens": 3610000
    }
  ]
}
```

---

## 20.3 获取时间段 Token 消耗统计

```
GET /api/ai/statistics/token-by-range
```

对 `[start_date, end_date]` **闭区间**逐日聚合 Token 消耗，一次性返回时间段总量、按天趋势、按模型分布，适合直接渲染「近 N 天 Token 折线图 / 柱状图 + 模型占比饼图」。

**Query 参数**:
- `start_date`: 开始日期，格式 `YYYY-MM-DD`，默认 6 天前（即默认返回近 7 天）
- `end_date`: 结束日期，格式 `YYYY-MM-DD`，默认今天

**行为说明**:
- 今日数据取内存实时值，历史数据从数据库读取，二者自动拼接
- 区间内**无数据的日期以 0 补齐**，`daily` 序列连续，前端无需自行填充缺失日期
- `start_date > end_date` 时自动交换；跨度超过 366 天时保留靠近 `end_date` 的 366 天
- 日期格式非法时返回 `status: 1`，`msg` 为「日期格式错误，应为 YYYY-MM-DD」

**字段说明**:
- `total`: 整个时间段四类 Token 的总量，`total_tokens` = `input + output + cache_read + cache_write`
- `daily`: 按天的 Token 明细数组（升序），每项含四类 Token 及当日 `total_tokens`，用于趋势图
- `by_model`: 跨天聚合的按模型 Token 分布，按 `total_tokens` **降序**排列，用于占比图
- `days`: `daily` 数组长度（实际聚合天数）
- 四类 Token 互不重叠：`input_tokens` 为非缓存输入，`cache_read_tokens` / `cache_write_tokens` 为提示词缓存读写，因此可直接相加

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "start_date": "2024-01-09",
        "end_date": "2024-01-15",
        "days": 7,
        "total": {
            "input_tokens": 600000,
            "output_tokens": 280000,
            "cache_read_tokens": 30000,
            "cache_write_tokens": 15000,
            "total_tokens": 925000
        },
        "daily": [
            {
                "date": "2024-01-09",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0
            },
            {
                "date": "2024-01-15",
                "input_tokens": 300000,
                "output_tokens": 150000,
                "cache_read_tokens": 20000,
                "cache_write_tokens": 10000,
                "total_tokens": 480000
            }
        ],
        "by_model": [
            {
                "model": "claude",
                "input_tokens": 380000,
                "output_tokens": 190000,
                "cache_read_tokens": 20000,
                "cache_write_tokens": 10000,
                "total_tokens": 600000
            },
            {
                "model": "gpt-4",
                "input_tokens": 220000,
                "output_tokens": 90000,
                "cache_read_tokens": 10000,
                "cache_write_tokens": 5000,
                "total_tokens": 325000
            }
        ]
    }
}
```

**TypeScript 类型定义**:
```typescript
interface TokenBucket {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
}

interface TokenRangeDaily extends TokenBucket {
  date: string; // YYYY-MM-DD
}

interface TokenRangeByModel extends TokenBucket {
  model: string;
}

interface TokenRangeData {
  start_date: string;
  end_date: string;
  days: number;
  total: TokenBucket;
  daily: TokenRangeDaily[];
  by_model: TokenRangeByModel[];
}

interface ApiResp<T> {
  status: 0 | 1;
  msg: string;
  data: T | null;
}
```

**前端调用示例**:
```typescript
// 拉取近 30 天的 Token 消耗趋势与模型分布
async function fetchTokenRange(startDate?: string, endDate?: string) {
  const params = new URLSearchParams();
  if (startDate) params.set('start_date', startDate);
  if (endDate) params.set('end_date', endDate);

  const resp = await fetch(`/api/ai/statistics/token-by-range?${params}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const json: ApiResp<TokenRangeData> = await resp.json();
  if (json.status !== 0 || !json.data) throw new Error(json.msg);
  return json.data;
}

// 折线图数据：按天总量趋势
const data = await fetchTokenRange('2024-01-01', '2024-01-30');
const trend = data.daily.map((d) => ({ x: d.date, y: d.total_tokens }));

// 饼图数据：模型 Token 占比（by_model 已按 total_tokens 降序）
const pie = data.by_model.map((m) => ({ name: m.model, value: m.total_tokens }));
```

> 与 `token-by-model`（单日、按模型）的区别：本接口是**跨天时间段**聚合，同时给出按天趋势与跨天模型分布，一次请求即可支撑时间段维度的看板；单日明细仍用 `summary` / `token-by-model`。

---

## 20.4 获取活跃用户/群组排行

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

## 20.5 获取触发方式占比

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

## 20.6 获取意图分布统计

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

## 20.7 获取错误统计

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

## 20.8 获取 Heartbeat 巡检统计

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

## 20.9 获取 RAG 知识库效果统计

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

## 20.10 获取 RAG 文档命中统计

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

## 20.11 获取历史统计数据

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
