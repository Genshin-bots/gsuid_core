# HTTP 请求追踪 API - `/api/http-traces`

> 后端：`gsuid_core/webconsole/http_trace_api.py`
> 存储：`data/logs/http_traces/YYYY-MM-DD/index.jsonl`（列表，无 preview）+ `{trace_id 前 2 位 hex}.jsonl`（详情）+ `count`（非今天的去重条数旁路）+ `data/logs/YYYY-MM-DD.log`（字段 `http_trace_id`）。有 index 后旧的单日 `YYYY-MM-DD.jsonl` **不再读**（可删）。无 index 时 leftover 仍可读，超过 64MB 只读文件尾。与命令追踪共用 **一条** 写线程（`day_jsonl_store`）和 **一条** HTTP 读线程，不是各开一条。
> 与 [07-logs.md](./07-logs.md) §7.8 **命令追踪**相互独立，禁止写入 `logs/traces/`。

按一次 `/api` HTTP 请求查看该 **ASGI 请求任务**内的官方 `gsuid_core.logger` 行。插件挂在共享 `app` 上且使用官方 logger 时无需额外代码。

**不覆盖：** `/app`、`/ws`、命令函数体（走 `#/traces`）、`print` / 非 GsCore logger、`to_thread` 线程内日志。

**排除（不建追踪）：** `/api/traces*`、`/api/http-traces*`、`/api/logs*`、`/api/dashboard*`、`/api/version*`、`/api/v1/agent/chat/stream`、MCP 挂载路径、探活 `GET /api/system/health` 与 `GET /api/v1/agent/health`。仅 GET 的控制台壳子 / 看板 / 读图：`/api/auth/me`、`/api/auth/pubkey`、`/api/auth/admin/exists`、`/api/auth/avatar/*`、`/api/brand`、`/api/brand/icon`、`/api/theme/config`、`/api/theme/presets`、`/api/plugins/icon/*`、`/api/plugins/list`、`/api/plugin-pages`、`/api/assets/preview`、`/api/system/info`、`/api/persona/list`、人格媒体、`/api/getImage*`、`/api/image*`（不含 `/api/ai/images`）、`/api/meme/image*`、`/api/ops*`、`/api/ai/statistics*`、`/api/ai/performance*`、看板刷新（kanban board / approvals list / live-chat state / scheduler jobs / git-update status / budget overview）。同路径 POST/PUT 仍记。`Content-Type: text/event-stream` 在首包即 finalize。jsonl **不写 running 行**（列表仍合并内存 running）；completed 异步 append。

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

`data` 为 `{rows, count, page, per_page}`。合并：当天 **index.jsonl**（有 index 即忽略 leftover 整日 jsonl / shard）去重 → 内存 running 覆盖同 id → 过滤 → 文件倒序约等于 `start_time` 倒序 → 切片。无筛选时只从文件尾读当前页，用换行计数，不把整天装进内存。有筛选才扫 index。内存 running 即使跨日也会出现在本次列表。

读路径走独立 1 线程池（不进默认 `asyncio.to_thread` 池）；相同 query 合并成一次扫描。前端 5s 轮询叠请求也不会并行扫盘。`json.loads` 循环会隔一段 `sleep`，避免占死 GIL 导致其它 API 无响应。

有 `YYYY-MM-DD/index.jsonl` 后，旧的 `YYYY-MM-DD.jsonl`（可能十几 GB）不再被列表/计数读取，可手工删掉回收磁盘。

`client_request_id` / `content_length` 只在详情。

---

## 2. 日历计数

```
GET /api/http-traces/daily_counts?days=60
```

`days` 夹取 `[1, 366]`。`data` 升序，长度恒等于 `days`。`count==0` 的日期日历不可点。此路径必须声明在 `/{trace_id}` **之前**。今天的计数含内存中尚未落盘的 running 请求（jsonl 只写 completed）。有 index 且文件较大时按换行计数（不 json.loads 整天）。非今天读 `{date}/count`（与 index/legacy 大小指纹一起）；源文件变了会重扫并回写。与列表共用同一读线程，不堵事件循环。

---

## 3. 详情

```
GET /api/http-traces/{trace_id}?date=YYYY-MM-DD
```

内存 running 优先；否则 JSONL 元数据（只开对应 shard，不扫 leftover 巨文件）+ 扫描当天 daily log（键为 `http_trace_id`）。`log_count==0` 不扫 daily log。首版不自动试 `date-1`。走同一独立读线程。

- 存在：`{status:0, msg:"ok", data: detail}`
- 不存在：HTTP 200 + `{status:404, msg:"追踪不存在", data:null}`
- 非法日期：`status=1`

`logs[].plugin` 恒有；缺省 `"SayuCore"`。root 上的第三方/uvicorn 行若带 `http_trace_id` 会出现在 completed 的 `logs[]`，但不会进内存 collector。

响应头 `x-http-trace-id`（小写）在被追踪的响应上；现网无 CORS expose，浏览器 JS 读不到。
