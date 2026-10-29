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
- `total_cost`: 基于 `genai-prices` 估算的预估总费用（美元 USD）
- `by_model` / `by_type`: 按模型名 / 会话类型（group / private）拆分，每项均含上述四类 Token（`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens`）；`by_model` 额外返回该模型的预估费用 `cost`（美元 USD）

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
        "efficiency": {
            "user_turn_count": 128,
            "agent_run_count": 341,
            "root_agent_run_count": 130,
            "nested_agent_run_count": 211,
            "user_turn_agent_run_count": 320,
            "user_turn_input_tokens": 4200000,
            "user_turn_output_tokens": 980000,
            "user_turn_cache_read_tokens": 2100000,
            "user_turn_cache_write_tokens": 80000,
            "avg_tokens_per_user_turn": 40546.88,
            "avg_input_tokens_per_user_turn": 32812.5,
            "avg_output_tokens_per_user_turn": 7656.25,
            "avg_tokens_per_agent_run": 15200.0,
            "avg_input_tokens_per_agent_run": 12000.0,
            "avg_output_tokens_per_agent_run": 3200.0,
            "avg_agent_runs_per_user_turn": 2.5
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

### `efficiency` 字段说明（Token 效率）

体量指标（日总 Token）会随活跃人数变化；**效率**看单次成本是否更省。

| 字段 | 含义 |
|------|------|
| `user_turn_count` | **用户回合**数：主人格交互 root 一次完整 `agent.run` 记 1（过软门后真正跑 Agent） |
| `agent_run_count` | **代理运行**数：任意 settle 的 `agent.run`（主 / AutoPlanner / CapabilityAgent…） |
| `root_agent_run_count` / `nested_agent_run_count` | 非嵌套 vs 嵌套（`is_subagent` 或挂在父 user_turn 下的非 root） |
| `user_turn_agent_run_count` | 挂在用户回合树内的 run 数（含 root + 同步嵌套） |
| `user_turn_*_tokens` | 用户回合树内 token 合计（主+同步子代理；**不含**无 `user_turn_id` 的纯后台） |
| `avg_tokens_per_user_turn` | `(user_turn_input + user_turn_output) / user_turn_count` — **主 KPI：系统是否更省** |
| `avg_tokens_per_agent_run` | `(total_input + total_output) / agent_run_count` — 单节点成本 |
| `avg_agent_runs_per_user_turn` | `user_turn_agent_run_count / user_turn_count` — 委派/拆分强度 |

> 主 session 日志里的单次 `token_usage` **不含**子代理；效率块的 `user_turn_*` 在统计层按 `user_turn_id` 树汇总，与预算 scope 记账独立。

---

## 20.1.1 获取效率指标（独立接口）

```
GET /api/ai/statistics/efficiency
```

**Query 参数**:
- `date`: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

**说明**: 与 `summary.efficiency` 同源；`data` 额外带 `date` 字段。今日读内存，历史读 `aidailystatistics`。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "date": "2024-01-15",
        "user_turn_count": 128,
        "agent_run_count": 341,
        "root_agent_run_count": 130,
        "nested_agent_run_count": 211,
        "user_turn_agent_run_count": 320,
        "user_turn_input_tokens": 4200000,
        "user_turn_output_tokens": 980000,
        "user_turn_cache_read_tokens": 2100000,
        "user_turn_cache_write_tokens": 80000,
        "avg_tokens_per_user_turn": 40546.88,
        "avg_input_tokens_per_user_turn": 32812.5,
        "avg_output_tokens_per_user_turn": 7656.25,
        "avg_tokens_per_agent_run": 15200.0,
        "avg_input_tokens_per_agent_run": 12000.0,
        "avg_output_tokens_per_agent_run": 3200.0,
        "avg_agent_runs_per_user_turn": 2.5
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
      "total_tokens": 3610000,
      "user_turn_count": 40,
      "agent_run_count": 95,
      "user_turn_agent_run_count": 90,
      "avg_tokens_per_user_turn": 90250.0,
      "avg_tokens_per_agent_run": 38000.0,
      "avg_agent_runs_per_user_turn": 2.25
    }
  ]
}
```

> 效率字段从引入日起有值；历史日未补列前可能为 0。

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
- `total`: 整个时间段四类 Token 的总量与预估总费用，`total_tokens` = `input + output + cache_read + cache_write`，`total_cost` 为该周期的预估总费用（美元 USD）
- `daily`: 按天的 Token 明细数组（升序），每项含四类 Token、当日 `total_tokens`、当日预估费用 `total_cost`，以及效率字段
  `user_turn_count` / `agent_run_count` / `avg_tokens_per_user_turn` / `avg_tokens_per_agent_run` /
  `avg_agent_runs_per_user_turn`
- `efficiency`: 区间汇总的效率（回合数/运行数累加，均值按区间合计重算）
- `by_model`: 跨天聚合的按模型 Token 分布，按 `total_tokens` **降序**排列，用于占比图
- `by_type`: 跨天聚合的按 `chat_type` Token 分布，同样按 `total_tokens` 降序。与 `by_model`
  同区间同口径，前端的「各模型 / 各类型」两张构成卡直接吃这份，一次请求即可
- `latency`: 区间延迟（秒）。`avg` 由各日 `avg_latency` 按当日 `latency_sample_count`
  **加权**得出 —— 每日样本量实测能差两个数量级（439 vs 3339），直接取算术平均会被小样本日拉偏；
  `p95` 是区间内各日 P95 的**最大值**，逐日 P95 无法合并成一个真 P95，前端据此标注成「峰值」；
  `samples` 为区间样本数合计，**为 0 表示区间全是加 `latency_sample_count` 这一列之前落的历史行**，
  此时 `avg` 退化为「各日均值的算术平均」的估算值（比读成 0 可用，但精度较低）
- `errors`: 区间内六类错误计数与 `total` 合计，字段与 `summary.errors` 一致（`timeout` /
  `rate_limit` / `network_error` / `usage_limit` / `agent_error` / `api_529_error`）
- `days`: `daily` 数组长度（实际聚合天数）
- 四类 Token 互不重叠：`input_tokens` 为非缓存输入，`cache_read_tokens` / `cache_write_tokens` 为提示词缓存读写，因此可直接相加

**Why 延迟和错误也在这份里**: 两者原先只存在于 `summary` 的「选中日」口径，前端切到 7/30 天档时
会出现「Token 已经是这 30 天的、错误率却还停在某一天」，同一屏两段时间互相打架。放进来之后
Token / 延迟 / 错误共享同一个区间，错误率的分母可以直接取 `hourly-range` 那份请求数，
两个读数还能互相验算。

**性能**: 三张表（`aidailystatistics` / `aitokenusagebymodel` / `aitokenusagebytype`）各发**一次**
`BETWEEN` 区间查询，回程行在 Python 侧按日期分组，往返数与区间长度无关。实测 30 天档
从「逐日 2×30 = 60 次往返」降到 3 次，裸 sqlite 耗时 1.21ms → 0.39ms；走真实 ORM 时
每次往返都带一次 session 获取，差距比这更大。

**索引**: 三张表都在 `date` 上有索引。`aitokenusagebytype` / `aitokenusagebymodel` 由
`(date, X)` 唯一约束自带；`aidailystatistics` / `airagmissstatistics` 是统计模块里唯二没有
唯一约束、也就没有附带索引的按日表，原先 `BETWEEN + ORDER BY date` 走 `SCAN` 再排一次，
已在 `models.py` 用 `Index(...)` 声明并由 `startup.exec_list` 补建。769MB 真实库副本实测：
建索引 12.7ms、无重建无丢行，`EXPLAIN` 由 `SCAN + USE TEMP B-TREE` 变为
`SEARCH ... USING INDEX`，60 天区间 0.628ms → 0.305ms。`latency_sample_count` 列同样由
`exec_list` 补，老行默认为 0。

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
        ],
        "latency": {
            "avg": 1.42,
            "p95": 5.8,
            "samples": 2140
        },
        "errors": {
            "timeout": 12,
            "rate_limit": 7,
            "network_error": 3,
            "usage_limit": 1,
            "agent_error": 5,
            "api_529_error": 9,
            "total": 37
        }
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

interface TokenRangeByType extends TokenBucket {
  type: string;
}

interface TokenRangeLatency {
  avg: number;    // 秒，按样本数加权；samples 为 0 时是日均估算
  p95: number;    // 秒，区间内各日 P95 的最大值
  samples: number;
}

interface TokenRangeErrors {
  timeout: number;
  rate_limit: number;
  network_error: number;
  usage_limit: number;
  agent_error: number;
  api_529_error: number;
  total: number;
}

interface TokenRangeData {
  start_date: string;
  end_date: string;
  days: number;
  total: TokenBucket;
  daily: TokenRangeDaily[];
  by_model: TokenRangeByModel[];
  by_type: TokenRangeByType[];
  latency?: TokenRangeLatency;
  errors?: TokenRangeErrors;
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

// 延迟 / 错误率：与 Token 同一区间。分母取 hourly-range 那份请求数，
// 不要退回 summary（那是「选中日」口径，和这里的区间不是同一段时间）。
const errorRate = data.errors && requests > 0 ? (data.errors.total / requests) * 100 : null;

// 类型分布同口径：区间内跨天按 chat_type 聚合
const byType = data.by_type.map((t) => ({ name: t.type, value: t.total_tokens }));
```

> 与 `token-by-model`（单日、按模型）的区别：本接口是**跨天时间段**聚合，同时给出按天趋势、跨天模型分布与跨天类型分布，一次请求即可支撑时间段维度的看板；单日明细仍用 `summary` / `token-by-model`。

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

## 20.7.1 获取错误事件

```
GET /api/ai/statistics/error-events?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
```

返回这段闭区间里每一次错误的日期、时间、类型和消息。不包含堆栈。这个接口上线之前的历史错误只有计数，事件列表为空。

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
