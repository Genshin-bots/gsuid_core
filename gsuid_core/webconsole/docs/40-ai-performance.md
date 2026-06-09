# AI 性能统计 API

## 概述

AI 性能统计提供按小时分组的模型请求性能数据，包括：

- **TTFT** (Time To First Token): 首字延迟，单位毫秒
- **TPS** (Tokens Per Second): 每秒生成 Token 数
- **Token 消耗**: 输入/输出/缓存读取/缓存写入 Token 数
- **工具调用次数**: 每小时各模型的工具调用总次数

数据按 **提供商 + 模型名称** 分组，支持按日期查询。统计粒度为
**单次模型请求**（一次 Agent run 含多轮工具调用时，每轮请求各计一次）。

---

## 接口列表

### 1. 获取单日小时级性能统计

```
GET /api/ai/performance/hourly?date=2026-06-09
```

**参数：**

| 参数名 | 类型   | 必填 | 说明                    |
|--------|--------|------|-------------------------|
| date   | string | 否   | 日期，格式 YYYY-MM-DD，默认为今天 |

**返回示例：**

```json
{
  "status": 0,
  "msg": "ok",
  "data": [
    {
      "hour": 14,
      "providers": [
        {
          "provider": "openai",
          "model": "gpt-4o",
          "request_count": 12,
          "ttft_min_ms": 245.0,
          "ttft_max_ms": 890.0,
          "ttft_avg_ms": 456.5,
          "tps_min": 12.3,
          "tps_max": 45.6,
          "tps_avg": 28.4,
          "input_tokens": 3450,
          "output_tokens": 2890,
          "cache_read_tokens": 1200,
          "cache_write_tokens": 800,
          "tool_call_count": 5
        }
      ]
    }
  ]
}
```

---

### 2. 获取日期范围小时级性能统计

```
GET /api/ai/performance/hourly/range?start_date=2026-06-01&end_date=2026-06-09
```

**参数：**

| 参数名     | 类型   | 必填 | 说明                              |
|------------|--------|------|-----------------------------------|
| start_date | string | 否   | 开始日期，默认7天前               |
| end_date   | string | 否   | 结束日期，默认今天                |

**返回示例：**

```json
{
  "status": 0,
  "msg": "ok",
  "data": [
    {
      "date": "2026-06-09",
      "hour": 14,
      "provider": "openai",
      "model": "gpt-4o",
      "request_count": 12,
      "ttft_min_ms": 245.0,
      "ttft_max_ms": 890.0,
      "ttft_avg_ms": 456.5,
      "tps_min": 12.3,
      "tps_max": 45.6,
      "tps_avg": 28.4,
      "input_tokens": 3450,
      "output_tokens": 2890,
      "cache_read_tokens": 1200,
      "cache_write_tokens": 800,
      "tool_call_count": 5
    }
  ]
}
```

---

## 字段说明

| 字段名            | 说明                                          |
|-------------------|-----------------------------------------------|
| request_count     | 该小时该模型的请求次数                          |
| ttft_min_ms       | TTFT 最小值（毫秒，仅统计有效样本）             |
| ttft_max_ms       | TTFT 最大值（毫秒，仅统计有效样本）             |
| ttft_avg_ms       | TTFT 平均值（毫秒，总和 / 有效样本数）          |
| tps_min           | TPS 最小值（tokens/s，仅统计有效样本）          |
| tps_max           | TPS 最大值（tokens/s，仅统计有效样本）          |
| tps_avg           | TPS 平均值（tokens/s，总和 / 有效样本数）       |
| input_tokens      | 输入 Token 总数                                 |
| output_tokens     | 输出 Token 总数                                 |
| cache_read_tokens | 缓存读取 Token 总数                             |
| cache_write_tokens| 缓存写入 Token 总数                             |
| tool_call_count   | 工具调用次数                                    |

> 无文本输出的请求（如纯工具调用轮次）不产生 TTFT/TPS 有效样本，
> 只计入 request_count 与 Token 消耗，不会污染 min/avg。

---

## 数据采集原理

1. **流式打点**: Agent 主循环在 `ModelRequestNode` 上主动消费 `node.stream()`，
   请求以流式发起，逐 event 记录首个/最后一个 event 的到达时间
2. **TTFT**: 请求发起 → 首个流式 event 到达的耗时
3. **TPS**: `本轮响应的 output_tokens / (最后一个 event - 首个 event)`，
   即纯生成阶段的吞吐，使用该轮请求自身的 usage 而非整个 run 的累计值
4. **数据聚合**: 内存中按 `(date, hour, provider, model)` 维度累加，
   每30分钟持久化（增量合并进 DB 后立即出队，避免重复累加）
5. **查询合并**: 查询时自动合并 DB 基线数据与内存中未持久化的增量，
   重启或持久化周期内的数据均不丢失
