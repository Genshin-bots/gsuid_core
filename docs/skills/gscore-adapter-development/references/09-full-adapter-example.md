# 九、端到端完整示例

本章给两个层次的完整示例：①「裸 WebSocket 最小适配器」帮你理解骨架；②「OneBot v11 全功能适配器」
作为真实接入的对照模板。

## 9.1 最小适配器（裸 WebSocket，可独立运行）

适用场景：你的平台不是 NoneBot 生态，是个自己写的 Bot 进程 / 任意 SDK。把平台收发函数替换成你的即可。

```python
import asyncio
import base64
from typing import Any, Dict, List, Literal, Optional

import websockets.client
from msgspec import Struct
from msgspec import json as msgjson
from websockets.exceptions import ConnectionClosedError

# ---------- 1. 数据结构（照抄 core） ----------
class Message(Struct):
    type: Optional[str] = None
    data: Optional[Any] = None

class MessageReceive(Struct):
    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
    group_id: Optional[str] = None
    user_id: Optional[str] = None
    sender: Dict[str, Any] = {}
    user_pm: int = 6
    content: List[Message] = []

class MessageSend(Struct):
    bot_id: str = "Bot"
    bot_self_id: str = ""
    msg_id: str = ""
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    content: Optional[List[Message]] = None

# ---------- 2. 配置 ----------
BOT_ID = "MyBot"          # 路由 ID（连接级）
PLATFORM_ID = "mybot"     # 平台 ID（每条消息的 bot_id）
SELF_ID = "100001"        # 机器人账号 ID
HOST, PORT, WS_TOKEN = "localhost", "8765", ""

class GsClient:
    def __init__(self):
        self.ws = None
        self.queue: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        url = f"ws://{HOST}:{PORT}/ws/{BOT_ID}" + (f"?token={WS_TOKEN}" if WS_TOKEN else "")
        self.ws = await websockets.client.connect(url, max_size=2**26, open_timeout=60, ping_timeout=60)
        print(f"[MyBot] 已连接 core: {url}")

    # ---------- 3. 上报：平台事件 → core ----------
    async def report_text(self, group_id: str, user_id: str, text: str, is_at_bot=False):
        content = [Message("text", text)]
        if is_at_bot:
            content.append(Message("at", SELF_ID))   # 触发 is_tome
        msg = MessageReceive(
            bot_id=PLATFORM_ID, bot_self_id=SELF_ID,
            user_type="group", group_id=group_id, user_id=user_id,
            sender={"nickname": f"用户{user_id}"}, user_pm=6, content=content,
        )
        await self.queue.put(msg)

    async def _send_loop(self):
        while True:
            msg: MessageReceive = await self.queue.get()
            await self.ws.send(msgjson.encode(msg))   # bytes → 二进制帧

    # ---------- 4. 下发：core → 平台 ----------
    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                try:
                    msg = msgjson.decode(raw, type=MessageSend)
                    # 4a. 日志包特判
                    if msg.bot_id == BOT_ID:
                        if msg.content and (msg.content[0].type or "").startswith("log"):
                            lvl = msg.content[0].type.split("_")[-1]
                            print(f"[core-{lvl}] {msg.content[0].data}")
                        continue
                    # 4b. 路由 + 落地
                    if msg.bot_id == PLATFORM_ID:
                        await self._dispatch(msg)
                except Exception as e:
                    print("处理下发出错:", e)
        except ConnectionClosedError:
            print("[MyBot] 断连，5 秒后重连…")
            await asyncio.sleep(5)
            await self.connect()
            await self.start()

    async def _dispatch(self, msg: MessageSend):
        content = ""; image = None; node = []; at_list = []
        for _c in (msg.content or []):
            if not _c.data: continue
            if   _c.type == "text":  content += _c.data
            elif _c.type == "image": image = _c.data
            elif _c.type == "node":  node = _c.data
            elif _c.type == "at":    at_list.append(_c.data)

        async def _send(text, img):
            # —— 把下面替换成你平台真正的发送 API ——
            if img:
                if img.startswith("link://"):
                    print(f"[发图-url] -> {msg.target_id}: {img[7:]}")
                else:
                    raw_bytes = base64.b64decode(img.replace("base64://", ""))
                    print(f"[发图-bytes] -> {msg.target_id}: {len(raw_bytes)} bytes")
            if text:
                ats = "".join(f"@{a} " for a in at_list) if msg.target_type == "group" else ""
                print(f"[发文本] -> {msg.target_id}: {ats}{text}")

        if node:
            for n in node:
                await _send(None, n["data"]) if n["type"] == "image" else await _send(n["data"], None)
        else:
            await _send(content, image)

    async def start(self):
        recv = asyncio.create_task(self._recv_loop())
        send = asyncio.create_task(self._send_loop())
        # 演示：3 秒后模拟一条平台消息上报
        async def demo():
            await asyncio.sleep(3)
            await self.report_text(group_id="888", user_id="999", text="原神帮助")
        asyncio.create_task(demo())
        _, pending = await asyncio.wait([recv, send], return_when=asyncio.FIRST_COMPLETED)
        for t in pending: t.cancel()

async def main():
    c = GsClient()
    await c.connect()
    await c.start()

if __name__ == "__main__":
    asyncio.run(main())
```

把 `_send` 里的 `print` 换成平台真实 API、`demo()` 换成平台真实事件回调里调 `report_text`，
一个可用适配器就成型了。

## 9.2 OneBot v11 全功能适配器（NoneBot2 生态，结构对照）

真实接入通常基于某个 Bot 框架（NoneBot2 最常见）。结构分两个文件：

- `__init__.py`：注册事件钩子，**上报**（平台事件 → `MessageReceive` → 入队）。
- `client.py`：WS 连接 + **下发**（`MessageSend` → 平台 API）。

### 上报侧（`__init__.py` 核心片段）

```python
from nonebot import on_message
from nonebot.adapters.onebot.v11.event import GroupMessageEvent, PrivateMessageEvent

get_message = on_message(priority=0, block=False)

@get_message.handle()
async def _(bot, ev):
    if gsclient is None:
        return await connect()                 # 懒连接

    message: List[Message] = []
    pm, msg_id, sender = 6, "", {}
    self_id = str(bot.self_id)

    if isinstance(ev, (GroupMessageEvent, PrivateMessageEvent)):
        messages = ev.original_message
        msg_id = str(ev.message_id)
        if ev.sender.role == "owner": pm = 2
        elif ev.sender.role == "admin": pm = 3
        sender = ev.sender.dict(exclude_none=True)
        sender["avatar"] = f"http://q1.qlogo.cn/g?b=qq&nk={ev.get_user_id()}&s=640"
        if isinstance(ev, GroupMessageEvent):
            user_type, group_id = "group", str(ev.group_id)
        else:
            user_type, group_id = "direct", None
    else:
        return

    if await SUPERUSER(bot, ev): pm = 1
    if ev.is_tome():                            # 触发 is_tome
        message.append(Message("at", self_id))

    for seg in messages:                        # CQ 段 → GsMessage
        if seg.type == "text":  message.append(Message("text", seg.data["text"]))
        elif seg.type == "image": message.append(Message("image", seg.data["url"]))
        elif seg.type == "at":  message.append(Message("at", seg.data["qq"]))
        elif seg.type == "reply": message.append(Message("reply", seg.data["id"]))

    if not message: return
    msg = MessageReceive(
        bot_id="onebot", bot_self_id=self_id, user_type=user_type,
        group_id=group_id, user_id=ev.get_user_id(),
        sender=sender, content=message, msg_id=msg_id, user_pm=pm,
    )
    await gsclient._input(msg)
```

### 下发侧（`client.py` 核心片段）

```python
async def onebot_send(bot, content: List[GsMessage], target_id, target_type):
    if target_id is None or content is None: return
    _target_id = int(target_id)
    from nonebot.adapters.onebot.v11 import MessageSegment

    async def to_msg(gsmsgs):
        message = []
        for _c in gsmsgs:
            if not _c.data: continue
            if _c.type == "text":   message.append(MessageSegment.text(_c.data))
            elif _c.type == "image": message.append(MessageSegment.image(_c.data.replace("link://", "")))
            elif _c.type == "at":   message.append(MessageSegment.at(_c.data))
            elif _c.type == "record": message.append(MessageSegment.record(b64_to_bytes(_c.data)))
            elif _c.type == "video": message.append(MessageSegment.video(b64_to_bytes(_c.data)))
            elif _c.type == "node":
                forwards = [to_json(await to_msg([GsMessage(**i)]), "小助手", "2854196310") for i in _c.data]
                await _send_node(forwards)
            elif _c.type == "file": await to_file(_c.data)
        return message

    result = await to_msg(content)
    if result:
        if target_type == "group":
            await bot.call_api("send_group_msg", group_id=_target_id, message=result)
        else:
            await bot.call_api("send_private_msg", user_id=_target_id, message=result)
```

> 注意 OneBot 这里把整个 `content` 列表交给 `onebot_send`（而不是预抽取的散变量），因为 OneBot
> 的 `image` 段两种前缀都认（`link://` 去前缀即 url，base64 实现也接受），处理简单。其它平台多用
> [§5.2](./05-send-message.md) 的"预抽取散变量 + `_send` 闭包"模式。

### 完整参考

- 全平台下发：`GenshinUID/GenshinUID/client.py`（含 onebot/v12/red/villa/heybox/qqguild/qqgroup/
  telegram/kaiheila/discord/dodo/feishu/milky/ntchat 共十余个 `xxx_send`）。
- 全平台上报：`GenshinUID/GenshinUID/__init__.py`（`get_all_message` + `get_notice_message`）。
- 最小裸 WS：`gsuid_core/client.py`。

把这三个文件和本 SKILL 对照读一遍，任何新平台都能照葫芦画瓢。
</content>
