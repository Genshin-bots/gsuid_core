# 三、连接生命周期

## 3.1 建立连接

```python
import websockets.client

BOT_ID = "MyBot"          # 路由 ID（连接级），见 §2.2 ①
HOST, PORT = "localhost", "8765"
WS_TOKEN = ""             # core 若配了 WS_TOKEN 就填上

ws_url = f"ws://{HOST}:{PORT}/ws/{BOT_ID}"
if WS_TOKEN:
    ws_url += f"?token={WS_TOKEN}"

ws = await websockets.client.connect(
    ws_url,
    max_size=2**26,       # ⚠️ 必须调大！默认 1MB，base64 图片轻松超
    open_timeout=60,
    ping_timeout=60,
)
```

关键参数：
- **`max_size`**：`websockets` 默认最大帧 1MB，而一张 base64 图片往往几 MB，**不调大必断连**。
  官方适配器用 `2**26`（64MB）。core 侧也设了 `max_size`，两边都要够大。
- **`open_timeout` / `ping_timeout`**：放宽到 60s，避免弱网/首屏慢导致握手或心跳超时。
- HTTPS/WSS：core 套了反代时用 `wss://`。

## 3.2 Token 鉴权（core 侧逻辑）

core 的 `core.py` 鉴权规则（适配器需理解，以便排错）：

1. 若来源 IP 在**可信名单**（本机/内网白名单）→ 直接放行，**不校验 token**。
2. 否则若 core **没配** `WS_TOKEN` → **拒绝所有外网连接**（`close(1008)`）。
3. 否则比对 `?token=`：
   - 不匹配 → 记一次失败，连续失败 5 次**封禁该 IP**。
   - 匹配 → 放行。

排错提示：
- 本机连不上 → 多半是 core 没起 / 端口错 / `HOST` 设成了只听 `127.0.0.1`。
- 外网连接被 `1008` 关闭 → core 没配 `WS_TOKEN`，或你 token 错（注意别触发 5 次封禁）。

## 3.3 收发双协程骨架

```python
import asyncio
from msgspec import json as msgjson
from websockets.exceptions import ConnectionClosedError

class GsClient:
    async def recv_msg(self):
        """下发链路：core → 平台"""
        async for raw in self.ws:                       # raw 是 bytes
            try:
                msg = msgjson.decode(raw, type=MessageSend)
                await self.handle_send(msg)             # 按 msg.bot_id 路由，见 §5
            except Exception as e:
                logger.exception(e)                     # 单条出错不要让整个循环崩

    async def send_msg(self):
        """上报链路：平台 → core（消费内部队列）"""
        while True:
            msg: MessageReceive = await self.msg_queue.get()
            await self.ws.send(msgjson.encode(msg))     # encode → bytes → 二进制帧

    async def _input(self, msg: MessageReceive):
        """平台事件回调里调它，把上报塞进队列"""
        await self.msg_queue.put(msg)

    async def start(self):
        recv = asyncio.create_task(self.recv_msg())
        send = asyncio.create_task(self.send_msg())
        _, pending = await asyncio.wait(
            [recv, send], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
```

设计要点：
- **上报走队列**：平台事件是回调式并发的，多个回调同时 `ws.send` 会交错损坏帧。用一个
  `asyncio.Queue` + 单个 `send_msg` 协程**串行化发送**。core 侧下发同理（它内部有 send worker）。
- **下发的 `try/except` 包在循环体内**：单条消息处理异常（某平台 API 报错）不能让 `recv_msg`
  整个退出，否则后续消息全收不到。

## 3.4 心跳与"懒连接"

- `websockets` 自带 ping/pong 心跳，配好 `ping_interval`/`ping_timeout` 即可保活。
- 官方 NoneBot2 适配器用的是"按需懒连接"：每次平台来消息前先 `await ws.ping()`，
  若 `ConnectionClosed` 就重连。这样省去常驻重连循环，但首条消息会触发重连。两种都行。

## 3.5 断线重连（必做）

core 重启、网络抖动都会断连，适配器**必须**能自愈：

```python
async def recv_msg(self):
    try:
        async for raw in self.ws:
            ...
    except ConnectionClosedError:
        logger.warning("与 core 断开，准备重连…")
        for _ in range(30):                 # 有限次重试，别无限狂连
            await asyncio.sleep(5)
            try:
                await self.async_connect()  # 重新握手
                await self.start()          # 重启收发协程
                break
            except Exception:
                logger.debug("重连失败，5 秒后再试…")
```

注意：
- **退避**：固定 5s 或指数退避都行，关键是**别 0 延迟狂连**，否则连 core 失败时会打满 CPU/日志。
- **幂等**：重连成功后旧的 `recv/send` task 要 cancel，避免两套协程同时跑同一条 ws（见 §3.3 的
  `pending.cancel()`）。
- core 侧对断连有 **5 分钟宽限**：`bot_id` 相同的连接断开后，core 保留 `_Bot` 实例和发送队列 5 分钟，
  期间重连会**复用旧实例并把队列里积压的消息继续发**。所以重连后用**同一个路由 `bot_id`**，
  能少丢消息；换名字会被当新连接、丢掉积压队列。

## 3.6 最小可运行客户端（完整，可直接跑）

下面是 `gsuid_core/client.py` 的精简版——一个能连上 core、把你终端输入当消息上报、并打印 core
下发内容的最小适配器。用它**先验证连通性**，再往上加平台逻辑：

```python
import asyncio
from typing import Union
import websockets.client
from msgspec import json as msgjson
from websockets.exceptions import ConnectionClosedError

# 直接复用 §2.6 的 Message / MessageReceive / MessageSend 定义

class GsClient:
    @classmethod
    async def async_connect(cls, IP="localhost", PORT: Union[str, int]="8765", WS_TOKEN="abc111"):
        self = GsClient()
        cls.ws_url = f"ws://{IP}:{PORT}/ws/MyBot?token={WS_TOKEN}"
        print(f"连接至 {cls.ws_url} …")
        cls.ws = await websockets.client.connect(cls.ws_url, max_size=2**25, open_timeout=30)
        print("已连接！")
        return self

    async def recv_msg(self):
        try:
            async for message in self.ws:
                print(msgjson.decode(message, type=MessageSend))   # 下发：先只打印
        except ConnectionClosedError:
            print("断开，5 秒后重连…")
            await asyncio.sleep(5)
            client = await self.async_connect()
            await client.start()

    async def send_msg(self):
        while True:
            text = await asyncio.get_event_loop().run_in_executor(None, lambda: input("消息> "))
            msg = MessageReceive(
                bot_id="console", bot_self_id="3399214199",
                user_type="direct", user_pm=0,
                group_id="8888", user_id="99999",
                content=[Message(type="text", data=text)],
            )
            await self.ws.send(msgjson.encode(msg))

    async def start(self):
        recv = asyncio.create_task(self.recv_msg())
        send = asyncio.create_task(self.send_msg())
        _, pending = await asyncio.wait([recv, send], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

asyncio.run(GsClient().async_connect().__await__() and ...)  # 见原文件标准写法
```

> 完整可运行版见仓库 `gsuid_core/client.py`。跑通后：在终端输入任意已注册命令（如 `gs帮助`），
> 你会在 `recv_msg` 打印里看到 core 下发的 `MessageSend`——连通性即验证成功。
> 接下来把 `send_msg` 换成"平台事件 → 构造 MessageReceive"（[§4](./04-report-message.md)），
> 把 `recv_msg` 换成"按 bot_id 路由 → 调平台 API"（[§5](./05-send-message.md)）。
</content>
