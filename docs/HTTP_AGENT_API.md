# HTTP 流式 Agent API 对接指南（v1）

> 面向要接 `POST /api/v1/agent/chat/stream` 的前端 / 客户端。
> 协议以源码为准：`gsuid_core/ai_core/http_agent/`。
> 日期：2026-08-26。

一句话：用 **Bearer API key** 发 **POST JSON**，读 **SSE**。浏览器 **不能** 用 `EventSource`（它只支持 GET）。

---

## 0. 最小必要清单

前端真正要准备的只有这些：

| 必要 | 说明 |
|------|------|
| Core 已开 AI | `data/ai_core/ai_config.json` 的 `enable=true`。关着时 **不挂** Agent 路由。从关到开需 **重启 Core**。 |
| 已开本面 | `data/ai_core/http_agent_api.json` 的 `enable_http_agent_api=true`（可热开，关则 404）。 |
| 一把 `gsk_…` 钥 | 管理员建钥；明文只在创建时返回一次。 |
| `Authorization: Bearer gsk_…` | **只认这个头**。不要用 `WS_TOKEN`、WebConsole 登录会话、`?token=`。 |
| `Content-Type: application/json` | |
| 字段 `text` 或 `images` 至少一个非空 | 纯空格不算。 |
| 字段 `client_msg_id` | 每条用户消息唯一；重试必须换新 id。 |
| 用 `fetch` / `httpx.stream` 读 SSE | 不要 `new EventSource()`。 |

可选但常用：`session_id`（多会话）、`persona`（首轮指定人格）、同源反代（浏览器跨域见 §10）。

探测面是否可用（无需钥）：

```http
GET /api/v1/agent/health
```

- `200` `{"ok":true}` → 面已开
- `404` `{"detail":"Not Found"}` → AI 关 / 本面关 / 路由未挂

---

## 1. 这是什么

把 GsCore 的 **同一套** 被动聊天（人格、工具、记忆）暴露成 HTTP SSE。

HTTP 入口默认 `outbound_stream=True`：`text` 帧在模型 delta 到达时增量推（预览）。**history / 主通道配额**仍在完整 TextPart 上走与 IM 相同的闸门（`pre_send_gate`、中间 OS 抑制、speech_policy 等）；闸门拒绝的预览不落 history、不计配额。IM 入口默认 `outbound_stream=False`，等完整 TextPart + 闸门。两边都走 pydantic-ai `node.stream()`（TTFT/TPS）。HTTP 用量记为 `Http_Chat`，与 IM 的 `Chat` 分开。

- **不是** OpenAI `chat/completions` 兼容接口。
- **不是** WebConsole `/api/chat_with_history`（那是评测门，非流）。
- **不进** 命令匹配 / 适配器黑白名单。
- v1 **没有** `thinking` / `tool.call` SSE；长时间只有心跳、没有台词是合法的。

默认端口 `8765`，路径前缀 `/api/v1/agent`。

---

## 2. 鉴权

```http
Authorization: Bearer gsk_<8位key_id>_<secret>
```

例：`Authorization: Bearer gsk_aB3dE-x1_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

规则：

- 缺头、非 `Bearer ` 前缀、错钥、已吊销 → **同一** `401`：
  `{"code":"unauthorized","message":"invalid api key"}`
- 连续失败会封禁（默认 10 次 / 900 秒）；成功一次清零。直连公网 IP 按 IP 封。
  反代后的 loopback/私网不按该 IP 封（否则会掐掉整条代理）；此时按提交的 Bearer 哈希计数。
  nginx 应设 `X-Real-IP` / `X-Forwarded-For`，并保证 Core `TRUSTED_IPS` 含反代地址。
- **禁止**把钥放 query：`?token=` 会被忽略，结果仍是 401。
- 身份绑定在钥上（`user_id` / `bot_id` / 可选人格约束）。请求体里再写 `user_id` **无效**（`extra=ignore`）。

**浏览器安全：** `gsk_` 等价于用户凭证。不要把它写进前端仓库或公开页面。推荐：

```
浏览器 ──► 你的后端/BFF（拿钥）──► GsCore
```

同源反代到 Core 也可以，但钥仍应只存在服务端。

管理员建钥（WebConsole 管理员 Bearer，**不是** `gsk_`）：

```http
POST /api/http-agent/admin/keys
Content-Type: application/json
Authorization: Bearer <WebConsole 管理员会话>

{"user_id":"u_123","bot_id":"web","persona":"","label":"web-frontend"}
```

成功时 `data.token` 是完整 `gsk_…`，**只此一次**。`user_id` / `bot_id` 不能含 `:`，最长 64。`persona` 非空则这把钥只能用该人格。建钥不依赖 AI / 本面开关。

---

## 3. 发一条消息

```http
POST /api/v1/agent/chat/stream
Content-Type: application/json
Authorization: Bearer gsk_...
Accept: text/event-stream
```

```json
{
  "text": "你好",
  "client_msg_id": "550e8400-e29b-41d4-a716-446655440000",
  "session_id": "default",
  "persona": "你的人格目录名",
  "images": []
}
```

### 3.1 字段

| 字段 | 必要 | 默认 | 约束 | 作用 |
|------|------|------|------|------|
| `client_msg_id` | **是** | 无 | `^[A-Za-z0-9._:-]{1,128}$` | 幂等键。同一把钥下重复 → 409，**不重放**旧流。 |
| `text` | 与 images 二选一 | `""` | 去空白后为空且无图 → 400 | 用户文本。 |
| `images` | 与 text 二选一 | `[]` | 条数 ≤ 配置（默认 8） | 图片字符串列表，见 §8。 |
| `session_id` | 否 | `"default"` | `^[A-Za-z0-9_-]{1,64}$` | **客户端**会话名。多开聊天页用不同值。 |
| `persona` | 视部署 | 省略 | 必须是已有人格目录名 | 首轮绑定；之后可省略。 |
| `group_id` | 否 | `null` | `^[A-Za-z0-9_-]{1,64}$`；省略 / `null` / `""` = 私聊 | 群聊房间。同 `bot_id` + 同 `session_id` + 同 `group_id` 共享 Agent。 |

注意：`session_id` **不能**含 `.` / `:`；`client_msg_id` 可以（UUID 可用）。

建议：

```ts
const client_msg_id = crypto.randomUUID(); // 合法
const session_id = "chat_home";            // 合法
```

请求体默认上限 **2 MiB**（`Content-Length` 或实读字节超限 → 413；缺 header 也会边读边计数）。
缺必填字段 / 非法 JSON → `400` `bad_request`（关闸时仍是不广告的 `404`，不会先解析 body）。
未知字段会被忽略。

### 3.2 `persona` 怎么填

解析顺序：已绑定会话人格 → 本次 `persona` → 钥上的人格 → 配置 `http_agent_default_persona` → 会话匹配。

| 情况 | HTTP |
|------|------|
| 什么都没有 | `422` `persona_unbound` |
| 人格目录不存在 | `422` `persona_unbound` |
| 钥锁了人格 A，你要 B | `403` `persona_forbidden` |
| 本 `session_id` 已绑 A，你要 B | `409` `persona_pinned`（不会改绑） |
| 已绑 A，省略 `persona` | 继续用 A |

实务：前端首次把当前角色名放进 `persona`；同一会话后续消息可以不带。换人格先 `POST /sessions/reset`，或换一个 `session_id`。

### 3.3 成功时你读到的是 SSE，不是 JSON

开流前校验全过才会 `200` + `Content-Type: text/event-stream`。失败是普通 JSON，**不会**出现 `run.start`。

---

## 4. SSE 事件（v1 只有这 5 种）

每帧：

```
id: 1
event: run.start
data: {"run_id":"…","session_id":"default","seq":1}

```

- `id` 与 `data.seq` 相同，从 1 递增。
- 心跳是注释行，**不是** event：

```
: ping

```

前端解析时跳过以 `:` 开头的行。不要把它当错误，也不要当台词。默认约 15 秒一次，用来保活反代。

| `event` | `data` | 前端该做什么 |
|---------|--------|----------------|
| `run.start` | `run_id`，`session_id`（**客户端**会话名），`seq` | 记下 `run_id`，用于取消。开始「生成中」。 |
| `text` | `text`，`seq` | **追加**到当前气泡。可能有多帧（模型 token / 短合批）。完整段不再整包重发。 |
| `attachment` | 见 §8 | 渲染图/文件，或提示「过大已省略」。 |
| `run.done` | `status`: `ok` \| `silence` \| `cancelled`，`seq` | **终态**。停 spinner。 |
| `run.error` | `code`，`message`，`seq` | **终态**。展示 `message`（已脱敏，无供应商原文）。 |

不变量：

1. 成功开流后，**恰好一个**终态（`run.done` 或 `run.error`）。
2. 本 API v1 没有 `thinking` / `tool.*`。需要思考增量的上层可自己接 `on_trace` 另发；工具在跑时可能长时间只有 `: ping`。
3. `outbound_stream=True` 时 `text` 在 pydantic-ai delta 到达时就开始推（合批、剥 `<think>`、hold 未闭合协议标签）。完整 TextPart 仍过与 IM 同一套闸门：通过则只补未推后缀、不整包重发；拒绝则不写 history。`outbound_stream=False` 时闸门后整段入队。
4. `silence` 不是失败：模型决定不说。UI 应显示「无回复」，不要当报错。
5. 客户端主动断开（`AbortController.abort()`）时，服务端停本轮；**不一定**还能把终态写完。按「用户取消」处理即可。
6. 同 `session_id` 再发一条：v1 是 **抢答**（后到取消先到）。先到的流可能 `run.done cancelled`。要排队自己在前端做。

`run.error.code` 常见：`timeout`、`internal`、`ai_unavailable`、`output_truncated`（出站队列溢出，transcript 可能不完整）。

---

## 5. 开流前的 HTTP 错误

这些都是 **JSON 响应**，没有 SSE。除 404 外，业务错误体为：

```json
{ "code": "unauthorized", "message": "invalid api key" }
```

404 为了不广告接口，固定：

```json
{ "detail": "Not Found" }
```

非法 JSON / 缺必填字段是 `400` `bad_request`，与业务 `422` `persona_unbound` 不同。前端应先看 `code`。

| HTTP | `code` | 含义 | 前端 |
|------|--------|------|------|
| 400 | `empty_message` | 没文本也没图 | 拦截空发送 |
| 400 | `bad_session` | `session_id` 非法 | 检查字符集 |
| 400 | `bad_client_msg_id` | id 非法或空 | 用 UUID |
| 400 | `bad_request` | 非法 JSON / 缺字段 / 坏 `Content-Length` | |
| 400 | `bad_image` | `images[]` 不是 `data:image` / `base64://` | 不要传 URL |
| 400 | `bad_group` | `group_id` 非法（含 `:` / `.` 等） | 与 `session_id` 同一字符集 |
| 401 | `unauthorized` | 钥无效 / IP 已封 | 引导重新配钥；不要区分「没带」和「错了」 |
| 403 | `persona_forbidden` | 与钥锁定人格不符 | 换钥或改人格 |
| 404 | — | 面未开 / 取消了别人的 run | 提示管理员开 AI + HTTP Agent |
| 409 | `idempotency_conflict` | `client_msg_id` 用过 | **换新 id** 再发，不要重放 |
| 409 | `persona_pinned` | 本会话已绑别人设 | reset 或换 `session_id` |
| 413 | `payload_too_large` | 体过大或图片过多 | 压缩/少图 |
| 422 | `persona_unbound` | 没有可用人格 | 传存在的 `persona` |
| 429 | `budget` | 该用户预算用尽 | 展示 `message` |
| 429 | `rate_limit` | 每钥每分钟次数 | 退避重试（换新 `client_msg_id`） |
| 429 | `concurrency` | 每钥或全局槽满 | 等上一轮结束；或 Abort 旧流 |
| 503 | `ai_unavailable` | Core 还在初始化 | 稍后重试 |

---

## 6. 其它接口

| 方法 | 路径 | 鉴权 | 成功 |
|------|------|------|------|
| `GET` | `/api/v1/agent/health` | 无 | `{"ok":true}` |
| `POST` | `/api/v1/agent/chat/stream` | Bearer `gsk_` | SSE |
| `POST` | `/api/v1/agent/runs/{run_id}/cancel` | Bearer `gsk_` | `{"ok":true}` |
| `POST` | `/api/v1/agent/sessions/reset` | Bearer `gsk_` | `{"ok":true}` |

### 6.1 取消

`run_id` 来自 `run.start`。只能取消 **自己这把钥** 的 run；否则 404。

```http
POST /api/v1/agent/runs/ab12cd34…/cancel
Authorization: Bearer gsk_...
```

前端点「停止」也可以直接 `abort()` 流；效果等价还槽。两手都做也可以。

### 6.2 重置会话

清掉该客户端 `session_id` 对应的 Agent / 历史，并取消进行中的 run。下一句是新对话，人格可重新指定。

```http
POST /api/v1/agent/sessions/reset
Content-Type: application/json
Authorization: Bearer gsk_...

{"session_id":"default"}
```

群聊重置把当时的 `group_id` 一并带上，否则清的是私聊会话。

---

## 7. 会话怎么理解

客户端传短 `session_id`，可选 `group_id`。服务端内部：

```
# 私聊（不传 / null / ""）
HTTP_AGENT:{钥.bot_id}:{钥.key_id}_{client_session}:private:{钥.user_id}

# 群聊
HTTP_AGENT:{钥.bot_id}:g_{client_session}:group:{group_id}
```

含义：

- 私聊：同一把钥 + 同一个 `session_id` = 同一段上下文。两把钥即使 `user_id` 相同也 **不共享** Agent 历史。
- 群聊：同一 `钥.bot_id` + 同一 `session_id` + 同一 `group_id` = **同一段**群上下文（和 QQ 群一样，session 不含发言人）。不同 `bot_id` 的钥进不了这个群。
- `group_id` 等同于房间口令：任何持有同 `bot_id` 钥、知道这个 id 的客户端都能加入。前端应用不可猜测的 id（UUID 去掉连字符即可）。
- 不同 `session_id` 仍是互不干扰的多聊天（群聊里也是一层分区）。
- 刷新页面只要钥 / `session_id` / `group_id` 不变，服务端还记得（进程没重启、会话没被 idle 清掉的前提下）。
- 群聊记忆走 `group:` scope；私聊仍是 `user_global:`（`group_id` 必须是真 `None`，不能填 user_id）。

---

## 8. 图片与附件

### 8.1 上行 `images`

字符串数组，v1 **只收内联**：

- `data:image/png;base64,……`
- `base64://……`（GsCore 习惯，无 data URL 头时按 png）

`http(s)://`、本地路径、`link://` → `400` `bad_image`（不在服务端拉取，避免 SSRF）。
默认最多 8 张。纯图、无 `text` 合法。客户端若只有 URL，先自己下成 base64 再发。

### 8.2 下行 `attachment`

```json
{
  "kind": "image",
  "encoding": "base64",
  "mime": "image/png",
  "data": "<payload>",
  "nbytes": 12345,
  "seq": 3
}
```

| `encoding` | `data` | UI |
|------------|--------|-----|
| `base64` | 裸 base64（**没有** `data:` 前缀） | `<img src="data:{mime};base64,{data}">` |
| `url` | URL 字符串 | `<img src="{data}">` |
| `omitted` | 空串 | 显示「附件过大未下发」，仍可用 `nbytes` / `mime` |

单帧 base64 超过 **256 KiB**，或本轮累计超过 **2 MiB**，会变成 `omitted`。`kind` 为 `image` 或 `file`。

---

## 9. 浏览器必须用 fetch，不要 EventSource

`EventSource` 只能 GET、不能自定义 `Authorization`。v1 是 **POST + Bearer**。

```ts
type AgentEvent =
  | { event: "run.start"; data: { run_id: string; session_id: string; seq: number } }
  | { event: "text"; data: { text: string; seq: number } }
  | {
      event: "attachment";
      data: {
        kind: "image" | "file";
        encoding: "base64" | "url" | "omitted";
        mime: string;
        data: string;
        nbytes: number;
        seq: number;
      };
    }
  | { event: "run.done"; data: { status: "ok" | "silence" | "cancelled"; seq: number } }
  | { event: "run.error"; data: { code: string; message: string; seq: number } };

type JsonError = { code?: string; message?: string; detail?: unknown };

async function* readSse(stream: ReadableStream<Uint8Array>): AsyncGenerator<AgentEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop() ?? "";
    for (const block of parts) {
      const parsed = parseSseBlock(block);
      if (parsed) yield parsed;
    }
  }
}

function parseSseBlock(block: string): AgentEvent | null {
  let event = "";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // 心跳
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!event) return null;
  const data = JSON.parse(dataLines.join("\n") || "{}");
  return { event, data } as AgentEvent;
}

export async function streamChat(opts: {
  baseUrl: string; // 例如 "" 表示同源，或 "http://127.0.0.1:8765"
  token: string;
  text: string;
  sessionId?: string;
  persona?: string;
  images?: string[];
  signal?: AbortSignal;
  onEvent: (ev: AgentEvent) => void;
}): Promise<void> {
  const res = await fetch(`${opts.baseUrl}/api/v1/agent/chat/stream`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${opts.token}`,
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({
      text: opts.text,
      client_msg_id: crypto.randomUUID(),
      session_id: opts.sessionId ?? "default",
      persona: opts.persona,
      images: opts.images ?? [],
    }),
    signal: opts.signal,
  });

  if (!res.ok) {
    const err = (await res.json()) as JsonError;
    const msg =
      typeof err.code === "string"
        ? `${err.code}: ${err.message ?? ""}`
        : `HTTP ${res.status}`;
    throw new Error(msg.trim());
  }
  if (!res.body) throw new Error("empty body");

  for await (const ev of readSse(res.body)) {
    opts.onEvent(ev);
    if (ev.event === "run.done" || ev.event === "run.error") return;
  }
}
```

UI 状态可以按这个走：

```ts
const ac = new AbortController();
let runId = "";
let bubble = "";

await streamChat({
  baseUrl: "",
  token,
  text: input,
  sessionId: "chat_home",
  persona: "MyPersona",
  signal: ac.signal,
  onEvent(ev) {
    if (ev.event === "run.start") runId = ev.data.run_id;
    if (ev.event === "text") {
      bubble += ev.data.text; // 追加，不要整段替换成最后一帧
    }
    if (ev.event === "attachment") { /* 插图 */ }
    if (ev.event === "run.done" && ev.data.status === "silence") {
      /* 无回复 */
    }
    if (ev.event === "run.error") { /* 展示 ev.data.message */ }
  },
});

// 用户点停止：
ac.abort();
// 或：await fetch(`/api/v1/agent/runs/${runId}/cancel`, { method: "POST", headers })
```

每条用户消息都要 **新的** `client_msg_id`。网络闪断后重试也要换 id，否则 409。

---

## 10. 同源、CORS、反代

v1 **没有**完整的浏览器 CORS 中间件：

- 成功的 SSE `200` 才会按白名单带 `Access-Control-Allow-Origin`（配置 `http_agent_cors_origins`，须与请求 `Origin` **全等**）。
- 4xx JSON、`health` / `reset` / `cancel`、以及浏览器预检 `OPTIONS` **不会**自动配齐 CORS 头。
- 带 `Authorization` + `application/json` 的 POST 一定是 preflight。跨域直连 Core，浏览器通常会在预检失败。

所以前端三种接法，推荐从上到下：

1. **同源反代（推荐给浏览器）**
   页面和 `/api/v1/agent` 同一 Origin。nginx 把 `/api/` 转到 Core。此时不需要 CORS。
2. **BFF**
   浏览器打你们自己的接口；服务器持有 `gsk_` 再流式转发给 Core。
3. **浏览器直连 Core**
   仅适合本机调试；生产不要把钥暴露给页面。跨域不要指望 v1 白名单单独救活预检。

nginx 流式要点（墙钟默认 600s）：

```nginx
location /api/v1/agent/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 660s;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Content-Type $http_content_type;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_http_version 1.1;
}
```

`TRUSTED_IPS` 默认含 `127.0.0.1` / `localhost` / `::1`，才会认上面两个转发头。不要把公网网段写进信任列表。

Core 是 **单进程内存** 状态（限流槽、幂等、进行中的 run）。前面不要无粘滞的多 worker 打同一面。

---

## 11. curl 自测

先 `GET /api/v1/agent/health` 应为 200。把钥和人格换成你的：

```bash
curl -N -X POST "http://127.0.0.1:8765/api/v1/agent/chat/stream" \
  -H "Authorization: Bearer gsk_xxxxxxxx_your_secret" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d "{\"text\":\"你好，用一句话打招呼。\",\"client_msg_id\":\"msg-1\",\"session_id\":\"cli\",\"persona\":\"YourPersona\"}"
```

`-N` 关闭缓冲。应先看到 `event: run.start`，再 `text`，最后 `run.done`。

仓库实机脚本：`eval/manual/http_agent_stream.py`（需已启动的完整 Core，不要 `--dev`）。

---

## 12. 默认限额（部署者可改）

配置文件：`data/ai_core/http_agent_api.json`。

| 项 | 默认 |
|----|------|
| 本面全局并发 | 8 |
| 每钥并发 | 2 |
| 每钥 RPM（60s 滑动） | 30 |
| 请求体 | 2 MiB |
| 每请求图片数 | 8 |
| 墙钟 / 硬超时 | 600s / 660s |
| SSE 心跳 | 15s |
| 幂等 TTL | 600s |
| CORS 白名单 | 空（不发 ACAO） |

---

## 13. 前端对接核对表

- [ ] 不用 `EventSource`，用 `fetch` + `AbortController`
- [ ] 每次发送新的 `client_msg_id`
- [ ] `text` 增量拼接，不覆盖
- [ ] 处理 `silence` / `cancelled` / `run.error` 三种终态
- [ ] 心跳 `: ping` 忽略
- [ ] 私聊不传 `group_id`（或 `null`）；群聊传稳定、不可猜测的 id
- [ ] 钥不进前端仓库；生产走 BFF 或同源反代
- [ ] 空输入在本地拦住（否则 400）
- [ ] 换人格先 reset 或换 `session_id`
- [ ] 409 幂等 → 换 id，不要死循环原请求
- [ ] 429 并发 → 先停旧流再发，或排队

---

## 14. 和别的 Core HTTP 的区别

| 面 | 用途 | 鉴权 |
|----|------|------|
| `/api/v1/agent/*` | 生产流式聊天 | `gsk_` Bearer |
| `/api/chat_with_history` | 评测 | 本地测试门 |
| `/api/send_msg` | 走完整 `handle_event` | `WS_TOKEN` |
| `/api/mcp` | 工具 MCP | 不要仿「无钥开放」 |
| WebConsole `/api/*` | 控制台 | 登录会话 Bearer |

不要混用令牌。

源码入口：`gsuid_core/ai_core/http_agent/routes.py`。部署开关见 [gscore-deploy §13.8b](../.agents/skills/gscore-deploy/references/13-ai.md)。
