# 五、Bot 三类：`_Bot` / `Bot` / `MockBot`

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[四、事件处理与触发器流转](./04-event-trigger-flow.md) · **下一章**：[六、AI Session 路由与 Persona](./06-ai-session-and-persona.md)

> ⚠️ **这是全框架最高频的混淆点**。`_Bot`、`Bot`、`MockBot` 是三个不同的类，混用直接运行时
> 报错。历史致命缺陷 D-5 就是 `_Bot` 与 `Bot` 混淆导致 `bot_self_id` 缺失。

## 5.1 类层次

```
_Bot (底层连接实现)  ──包装──►  Bot (高层业务包装器)  ──代理──►  MockBot (AI 调用代理)
```

| 类 | 文件 | 构造 | 依赖 Event | 适用场景 |
|----|------|------|-----------|----------|
| `_Bot` | `bot.py` | `_Bot(_id, ws=None)` | ❌ | 框架内部、连接管理、HTTP 模式 |
| `Bot` | `bot.py` | `Bot(bot: _Bot, ev: Event)` | ✅ 强依赖 | 插件触发器函数参数、AI 调用 |
| `MockBot` | `ai_core/trigger_bridge.py` | `MockBot(real_bot, ctx)` | 代理 Bot | AI/MCP 调触发器时拦截 send |

## 5.2 `_Bot` —— 底层连接实现

负责 WebSocket 连接生命周期、消息队列、发送调度。**不依赖 Event**。

```python
class _Bot:
    def __init__(self, _id: str, ws: Optional[WebSocket] = None):
        self.bot_id = _id          # = WS_BOT_ID（WS 连接 ID，不是平台 ID！）
        self.bot = ws              # WebSocket 连接（HTTP 模式为 None）
        self.queue = asyncio.queues.PriorityQueue()    # 任务队列
        self.sem = asyncio.Semaphore(10)               # 任务并发控制
        self._send_queue = asyncio.queues.Queue()      # 独立发送队列
        self._send_task = None                          # 发送 worker 任务
```

| 关键方法 | 说明 |
|----------|------|
| `target_send()` | 底层发送：消息转换、Markdown、按钮、历史记录、记忆集成 |
| `_send_worker()` | 独立发送 worker，从 `_send_queue` 串行取消息发送 |
| `start_send_worker()` | WS 连接时启动发送 worker |
| `_process()` | 任务消费循环，支持 `shutdown_event` 优雅关闭 |
| `wait_task()` | HTTP 模式下等任务完成返回结果 |

### 发送队列串行化（重要设计）

所有 WebSocket 写入经 `_send_queue` 串行执行：

```python
async def target_send(self, ...):
    if self.bot:
        body = msgjson.encode(send)
        async def _do_send(ws=self.bot, body=body):
            await ws.send_bytes(body)
        await self._enqueue_send(_do_send())
```

> **目的**：AI 回复、Heartbeat 主动发言、定时任务结果等多个任务可能同时想写同一个
> WebSocket。不串行化会帧乱序 / 连接不稳。改发送逻辑时**不要**绕过 `_send_queue` 直接
> `ws.send_bytes`。

## 5.3 `Bot` —— 高层业务包装器

供插件和触发器使用，封装 `_Bot` + `Event`，提供面向业务的 `send()` 等 API。

```python
class Bot:
    instances: Dict[str, "Bot"] = {}           # 单轮交互实例
    mutiply_instances: Dict[str, "Bot"] = {}   # 多轮交互实例
    mutiply_map: Dict[str, str] = {}
    def __init__(self, bot: _Bot, ev: Event):
        self.bot = bot                # 底层 _Bot
        self.ev = ev                  # 当前事件
        self.bot_id = ev.bot_id
        self.bot_self_id = ev.bot_self_id
```

| 关键方法 | 说明 |
|----------|------|
| `send()` | 发送消息，自动从 `ev` 提取目标 |
| `receive_resp()` | 发送并等待用户回复（交互式） |
| `send_option()` | 发送带选项按钮的消息 |
| `wait_for_key()` | 等待用户回复 |
| `target_send()` | 指定目标发送 |

## 5.4 `MockBot` —— AI 调用代理

AI 调触发器时，真实 `Bot` 被 `MockBot` 代理，**拦截 `send` 把内容收集而非真正发送**。

```python
class MockBot:
    def __init__(self, real_bot: Bot, ctx: Dict[str, Any]):
        self._real_bot = real_bot
        self._ctx = ctx
    async def send(self, message, at_sender=False):
        # 文本 → 存入 ctx["bot_messages"]（作为工具返回值给 AI）
        # 图片/bytes/base64 → RM.register() → 存入 ctx["image_ids"]，返回资源 ID
    def __getattr__(self, name):
        return getattr(self._real_bot, name)   # 其他属性代理到 real_bot
```

> **资源 ID 机制**：图片不直接发，而是 `RM.register()` 注册返回 `img_xxx`，AI 据此决定是否调
> `send_message_by_ai(image_id=...)` 真正发给用户。详见 [§07](./07-tool-registry-and-agent.md)
> 与 `gscore-ai-core-api` 的触发器桥接章节。

## 5.5 连接管理（`GsServer`）

```python
class GsServer:
    active_ws: Dict[str, WebSocket]   # WebSocket 连接
    active_bot: Dict[str, _Bot]       # _Bot 实例（断开后保留 5 分钟以便重连复用）
```

- **首次连接**：`_Bot(bot_id, websocket)` + `start_send_worker()`。
- **5 分钟内重连**：复用旧实例，仅换 `bot.bot = websocket` 与 logger，`_send_queue` 中未发
  消息继续投递。
- **超过 5 分钟**：cancel 旧 `_send_task` + `clear_send_queue()` 后丢弃，避免内存泄漏。
- **断开**：close + 删 `active_ws`、cancel `_send_task` 与 bg_tasks、标记 `_disconnected_at`、
  清 `Bot.instances` 等本 bot 会话；**`active_bot[bot_id]` 不删**（留给重连）。

## 5.6 `gss.active_bot` 的 key 是 `WS_BOT_ID`（致命易错点）

```
Event.bot_id     = 平台 ID（如 QQ 号）—— Session 标识
Event.WS_BOT_ID  = WS 连接 ID（= _Bot.bot_id）—— gss.active_bot 的 key
```

历史缺陷 D-5：Heartbeat 用 `bot_id`（平台 ID）去 `gss.active_bot` 查，**永远查不到**。
正确写法（三级查找）：

```python
async def _get_bot_for_session(self, event: Event) -> Optional["_Bot"]:
    if event.WS_BOT_ID and event.WS_BOT_ID in gss.active_bot:   # 1. WS_BOT_ID 直查
        return gss.active_bot[event.WS_BOT_ID]
    # 2. 遍历历史 metadata 找 bot_id 兜底
    # 3. 返回任意可用 _Bot 兜底
```

> **写主动发言/后台发送代码时**：要拿 `_Bot` 一律走 `gss.active_bot[event.WS_BOT_ID]`，
> 不要用平台 `bot_id`，也不要用 `Bot.instances`。

## 5.7 各场景该用哪个类

| 场景 | 类 |
|------|----|
| 框架启动、WebSocket 连接管理 | `_Bot` |
| 插件触发器函数参数 `bot: Bot` | `Bot` |
| AI Agent 调触发器 | `MockBot` 包 `Bot` |
| MCP Server 调触发器 | `MockBot` 包 `Bot`（无 AI 上下文） |
| HTTP API 模式 | `_Bot("HTTP")` |
| 后台主动发送（Heartbeat/定时任务） | `gss.active_bot[WS_BOT_ID]` 取 `_Bot` 后 `target_send()` |

> 需要 `Bot` 的地方（`MockBot.__init__`、触发器函数参数）**必须**传 `Bot` 实例而非 `_Bot`，
> 缺 `Event` 会让 `send()` 崩溃。
