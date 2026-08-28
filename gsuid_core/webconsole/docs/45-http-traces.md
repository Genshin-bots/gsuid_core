# HTTP 请求追踪 API - `/api/http-traces`

> 后端：`gsuid_core/webconsole/http_trace_api.py`
> 存储：`data/logs/http_traces/YYYY-MM-DD.jsonl`（元数据）+ `data/logs/YYYY-MM-DD.log`（字段 `http_trace_id`）
> 与 [07-logs.md](./07-logs.md) §7.8 **命令追踪**相互独立，禁止写入 `logs/traces/`。

按一次 `/api` HTTP 请求查看该 **ASGI 请求任务**内的官方 `gsuid_core.logger` 行。插件挂在共享 `app` 上且使用官方 logger 时无需额外代码。

**不覆盖：** `/app`、`/ws`、命令函数体（走 `#/traces`）、`print` / 非 GsCore logger、`to_thread` 线程内日志。

**排除（不建追踪）：** `/api/traces*`、`/api/http-traces*`、`/api/logs*`、`/api/dashboard*`、`/api/version*`、`/api/v1/agent/chat/stream`、MCP 挂载路径、`GET /api/system/health`。仅 GET 的控制台壳子：`/api/auth/me`、`/api/auth/pubkey`、`/api/auth/admin/exists`、`/api/auth/avatar/*`、`/api/brand`、`/api/brand/icon`、`/api/theme/config`、`/api/plugins/icon/*`、`/api/assets/preview`（同路径 POST 仍记）。`Content-Type: text/event-stream` 在首包即 finalize。

不记录**请求 body**。响应会存脱敏截断预览（`response_preview`，JSON 按键名掩 `api_key` 等；图片/SSE 不采）。无内部 logger 时 `logs=[]`，详情仍有 query + 响应预览。

认证：`Authorization: Bearer`（`require_auth`）。封套 `{status, msg, data}`。hub `ApiClient` 在 `status !== 0`（含 404 封套）时抛错。

OpenAPI tag：`控制台/系统/HTTP 请求追踪`。

---

## 1. 列表

```
GET /api/http-traces
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `date` | 今天 | `YYYY-MM-DD`；非法 → `status=1` |
| `page` | 1 | 从 1 起；超出末页时夹到末页 |
| `per_page` | 100 | 夹取 `[1, 100]` |
| `method` | 不筛 | `GET/POST/PUT/PATCH/DELETE/OPTIONS/HEAD` |
| `path_prefix` | 不筛 | `path.startswith`；含 `..` 则 `status=1` |
| `status_class` | 不筛 | `2xx`/`3xx`/`4xx`/`5xx`；running 无码不匹配 |
| `user_id` | 不筛 | 精确匹配 |
| `errors_only` | 假 | `1`/`true`/`yes`：`error_count>0` 或 `status_code>=400`；在分页 **之前**过滤 |

`data` 为 `{rows, count, page, per_page}`。合并：当天 JSONL 去重 → 内存 running 覆盖同 id → 过滤 → `start_time` 倒序 → 切片。内存 running 即使跨日也会出现在本次列表。

`client_request_id` / `content_length` 只在详情。

---

## 2. 日历计数

```
GET /api/http-traces/daily_counts?days=60
```

`days` 夹取 `[1, 366]`。`data` 升序，长度恒等于 `days`。`count==0` 的日期日历不可点。此路径必须声明在 `/{trace_id}` **之前**。

---

## 3. 详情

```
GET /api/http-traces/{trace_id}?date=YYYY-MM-DD
```

内存 running 优先；否则 JSONL 元数据 + `to_thread` 扫描当天 daily log（键为 `http_trace_id`）。首版不自动试 `date-1`。

- 存在：`{status:0, msg:"ok", data: detail}`
- 不存在：HTTP 200 + `{status:404, msg:"追踪不存在", data:null}`
- 非法日期：`status=1`

`logs[].plugin` 恒有；缺省 `"SayuCore"`。root 上的第三方/uvicorn 行若带 `http_trace_id` 会出现在 completed 的 `logs[]`，但不会进内存 collector。

响应头 `x-http-trace-id`（小写）在被追踪的响应上；现网无 CORS expose，浏览器 JS 读不到。
