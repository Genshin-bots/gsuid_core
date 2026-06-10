# 一、早柚协议总览

## 1.1 适配器在整个系统中的位置

```
┌───────────────┐   平台原生事件    ┌─────────────────────┐   MessageReceive(bytes)  ┌──────────────┐
│  聊天平台      │ ───────────────► │   适配器 (Adapter)   │ ───────────────────────► │              │
│ (QQ/TG/微信…) │                  │  运行在平台一侧       │                          │  GsCore      │
│               │ ◄─────────────── │  WebSocket 客户端     │ ◄─────────────────────── │  (早柚核心)  │
└───────────────┘   平台原生 API    └─────────────────────┘   MessageSend(bytes)      └──────────────┘
                                                                                       插件/SV/AI 在这里
```

- 适配器是 **WebSocket 客户端**，core 是 **WebSocket 服务端**。
- 适配器**不写业务逻辑**：它不判断"用户发的是不是某条命令"，只负责把平台消息**翻译**成
  `MessageReceive` 上报，把 core 下发的 `MessageSend` **翻译**成平台 API 调用。
- 所有命令匹配、画图、数据库、AI 都在 core 内部由插件完成（见 `gscore-plugin-development`）。

## 1.2 传输层：WebSocket + 二进制帧 + msgspec JSON

- **路由**：适配器连接 core 的 `ws://{host}:{port}/ws/{bot_id}`，默认端口 `8765`。
  - `{bot_id}` 是**连接级标识**（路由 ID），填你这个适配器/框架的名字，例如 `NoneBot2`、`Koishi`、`MyBot`。
  - 一个适配器进程通常只建**一条** WS 连接，不要为每个聊天账号各开一条。
- **鉴权**：若 core 配置了 `WS_TOKEN`，URL 需带查询参数 `?token={WS_TOKEN}`。详见 [§3.2](./03-connection-lifecycle.md)。
- **帧格式**：⚠️ **二进制帧**，不是文本帧。core 端实现：

  ```python
  # gsuid_core/core.py — core 读取上报
  data = await websocket.receive_bytes()
  msg = msgjson.decode(data, type=MessageReceive)
  # gsuid_core/bot.py / gs_logger.py — core 下发
  await self.bot.send_bytes(msgjson.encode(send))
  ```

  所以适配器侧：
  - **发送**（上报）：`payload = msgspec.json.encode(message_receive)` 得到 `bytes`，直接 `await ws.send(payload)`。
    用 `websockets` 库时，传 `bytes` 即发二进制帧。
  - **接收**（下发）：`async for message in ws:` 拿到的 `message` 是 `bytes`，用
    `msgspec.json.decode(message, type=MessageSend)` 解码。
  - 历史文档（`Protocol.md`）写的是"text 类型 UTF-8 JSON"，**以实际实现的二进制帧为准**。
    `msgspec` 编码出的本就是 UTF-8 字节，用二进制帧承载即可，不要 `.decode()` 成 str 再发文本帧。

- **编解码库**：core 用 [`msgspec`](https://jcristharif.com/msgspec/)。适配器**不强制**用 msgspec——
  你也可以用标准 `json` 手搓 dict 再 `json.dumps().encode()`，只要字段名和结构对得上即可。
  但用 msgspec + 与 core 一致的 `Struct` 定义最省心、最不易错（见 [§2](./02-data-structures.md)）。

## 1.3 两条独立链路（适配器的核心骨架）

适配器内部几乎总是**两个并行协程**：

```python
async def start(self):
    recv_task = asyncio.create_task(self.recv_msg())  # 监听 core 下发 → 调平台 API 发出去
    send_task = asyncio.create_task(self.send_msg())  # 监听平台事件 → 编码上报给 core
    await asyncio.wait({recv_task, send_task}, return_when=asyncio.FIRST_COMPLETED)
```

- **上报链路（send_msg / 平台事件回调）**：平台来消息 → 构造 `MessageReceive` → `ws.send(encode(...))`。
- **下发链路（recv_msg）**：`async for raw in ws:` → `decode(raw, MessageSend)` → 按 `msg.bot_id`
  分发到平台专属发送函数。

> 命名提示：在官方 NoneBot2 适配器里，平台事件是通过 NoneBot 的 `on_message` 钩子触发的，所以"上报"
> 不是一个 while 循环而是事件回调 `_input()` 往队列里塞，再由 `send_msg()` 协程消费队列统一发。
> 你的平台如果是 SDK 回调式，照这个模式做；如果是自己 poll 的，就写成 while 循环。

## 1.4 还有一条 HTTP 旁路（可选，了解即可）

core 若开启 `ENABLE_HTTP`，会暴露 `POST /api/send_msg`，body 是 `MessageReceive` 的 JSON。
它走完整的命令处理流程并**同步返回**结果（`MessageSend` 列表）。这条路用于"无状态地调一次 core"
（如脚本、Webhook），**不是适配器的主路**——适配器请用 WebSocket。最小示例见 `gsuid_core/client.py` 的 `http_test()`。

## 1.5 一条消息的完整生命周期（端到端）

以"群里有人发 `查询 12345`，机器人回一张图"为例：

1. 平台把群消息推给适配器。
2. 适配器构造 `MessageReceive`：`bot_id='onebot'`、`user_type='group'`、`group_id='888'`、
   `user_id='999'`、`content=[Message('text','查询 12345')]`，`ws.send(encode(msg))`。
3. core `receive_bytes` → `decode` → `msg_process()` 拆出 `Event(raw_text='查询 12345', ...)`
   → 削掉命令前缀 → 触发器匹配 → 插件画图 → `bot.send(图片)`。
4. core `target_send()` 把图片编码成 `MessageSend`：`bot_id='onebot'`、`target_type='group'`、
   `target_id='888'`、`content=[Message('image','base64://....')]`，`send_bytes(encode(send))`。
5. 适配器 `recv_msg` 收到，按 `msg.bot_id=='onebot'` 路由到 `onebot_send()`，把 `base64://`
   解成 bytes，调平台 API `send_group_msg` 发出去。

把这 5 步在脑子里跑通，整个适配器就不神秘了。下一章先把数据结构吃透。
</content>
