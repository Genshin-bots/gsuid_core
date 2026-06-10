# 八、特殊平台适配要点

普通平台（一个 ID 定位会话、主动发消息）照 [§4](./04-report-message.md)/[§5](./05-send-message.md)
即可。本章讲那些有"怪癖"的平台。

## 8.1 双 ID 平台（米游社大别野 / 黑盒 Heybox / DoDo 私聊）

有些平台**需要两个 ID 才能定位一个会话**：

- 大别野：`villa_id`（别野）+ `room_id`（房间）。
- 黑盒：`channel_id`（频道）+ `room_id`（房间）。
- DoDo 私聊：`island_source_id`（群岛）+ `user_id`。

### 上报：用 `-` 把两个 ID 拼进 `group_id`

```python
# 大别野
group_id = f"{ev.villa_id}-{ev.room_id}"
# 黑盒
group_id = f"{ev.channel_id}-{ev.room_id}"
```

`Protocol.md` 的约定：双 ID 平台一律 `主id + "-" + 次id` 塞进 `group_id`，`user_type='group'`。

### 下发：`split('-')` 拆回 + 借助 `group` 段

下发时从 `target_id` 拆回两个 ID：

```python
async def villa_send(bot, ..., target_id, target_type):
    if target_type == "group" and target_id:
        villa_id, room_id = target_id.split("-")
        ...
        await bot.send_message(villa_id=int(villa_id), room_id=int(room_id), ...)
```

> 此外 core 在 `target_send` 里，当带 `group_id` 时会**额外附一段 `Message("group", group_id)`**
> 到 content 末尾。某些平台（DoDo 私聊需要 island_id + user_id）用这个 `group` 段拿到第二个 ID：
> ```python
> elif _c.type == "group":
>     group_id = _c.data          # 在 §5.2 抽取
> # DoDo 私聊：bot.send_to_personal(group_id, target_id, message)
> ```

## 8.2 QQ 官方：被动消息与 `msg_id` / `msg_seq` 时序

QQ 官方 API（频道/群/单聊）**不能随便主动发消息**，只能"被动回复"用户的某条消息，且：

1. **回复必须带 `msg_id`**：core 下发的 `MessageSend.msg_id` 就是用户那条消息的 ID，发送时回填：

   ```python
   await bot.send_to_group(group_openid=target_id, msg_id=msg_id, event_id=msg_id, message=message, msg_seq=msg_seq)
   ```

2. **同一 `msg_id` 多次回复要递增 `msg_seq`**：QQ 群/单聊对同一条来源消息回复多次时，每条要带
   递增的 `msg_seq`，否则后续消息发不出。官方做法维护一个 `OrderedDict` 计数：

   ```python
   msg_id_seq = OrderedDict()
   if msg_id not in msg_id_seq:
       msg_id_seq[msg_id] = 1
   if len(msg_id_seq) >= 30:                 # 限长，防泄漏
       del msg_id_seq[next(iter(msg_id_seq))]
   # 发送时：
   msg_seq = msg_id_seq[msg_id]
   await bot.send_to_group(..., msg_seq=msg_seq)
   msg_id_seq[msg_id] += 1
   ```

3. **上报时缓存 `msg_id`**：群/单聊场景里，后续按钮回调拿不到原 `msg_id`，需在上报普通消息时
   用 `user_id → msg_id` 缓存：

   ```python
   msg_id_cache[user_id] = msg_id           # 上报普通消息时存
   # 回调上报时取：
   msg_id = msg_id_cache.get(user_id, str(ev.id))
   ```

4. **`bot_id` 细分**：QQ 官方频道用 `qqguild`，群/单聊用 `qqgroup`——同一个 NoneBot QQ 适配器，
   按事件类型上报**不同的平台 `bot_id`**，让 core 走不同渲染（频道支持自由 MD，群只支持模板）。

## 8.3 回调按钮上报（notice → 重新驱动命令）

`action=1` 的回调按钮被点击后，平台发的是**交互/通知事件**而非普通消息。适配器要在 notice 钩子里
把回调携带的 `data` 当作一段 `text` 上报，从而重新走命令链。各平台取值位置：

| 平台 | 事件类型 | 回调数据字段 |
|------|---------|------------|
| QQ 官方 | `InteractionCreateEvent` | `ev.data.resolved.button_data`，并 `bot.put_interaction(ev.id, code=0)` 应答 |
| DoDo | `CardMessageButtonClickEvent` | `ev.value` |
| 开黑啦 | `CartBtnClickNoticeEvent` | `ev.extra.body['value']` |
| 大别野 | `ClickMsgComponentEvent` | `ev.extra` |
| Telegram | `CallbackQueryEvent` | `ev.data` |
| Discord | `MessageComponentInteractionEvent` | `ev.data.custom_id`，并回 `InteractionResponse(PONG)` |

模板：

```python
# 以 DoDo 回调为例
if isinstance(ev, CardMessageButtonClickEvent):
    msg = MessageReceive(
        bot_id="dodo", bot_self_id=self_id,
        user_type="group", group_id=ev.channel_id, user_id=ev.user_id,
        content=[Message("text", ev.value)],     # 把按钮 data 当文本
        msg_id=ev.event_id,
        sender={"nickname": ev.personal.nick_name, "avatar": ev.personal.avatar_url},
    )
    await gsclient._input(msg)
```

⚠️ 有些平台（QQ/Discord）要求**先 ACK 交互**（`put_interaction` / `PONG`）否则客户端转圈/报错，
别漏。

## 8.4 文件上传通知上报

群文件上传是 notice 而非 message。OneBot v11 示例：

```python
if raw_data["notice_type"] in ("group_upload", "offline_file"):
    if "url" in raw_data["file"]:
        val = raw_data["file"]["url"]                       # 直接拿 url
    elif raw_data["file"]["size"] <= 4 * 1024 * 1024:       # 小文件才取 base64
        vd = await bot.call_api("get_file", file_id=raw_data["file"]["id"])
        val = vd["base64"] if vd.get("base64") else await file_to_base64(Path(vd["file"]))
    name = raw_data["file"]["name"]
    message = [Message("file", f"{name}|{val}")]            # 名|内容
```

注意**限制大小**（如 ≤4MB 才取 base64），否则大文件 base64 会撑爆帧。

## 8.5 OneBot v11 文本/图片/引用上报要点

最常见的接入，几个细节：

```python
messages = ev.original_message            # 用 original_message（含原始 CQ 段）
if ev.sender.role == "owner": pm = 2
elif ev.sender.role == "admin": pm = 3
sender = ev.sender.dict(exclude_none=True)
sender["avatar"] = f"http://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
# 引用消息里的图片（可选）：
if ev.reply and is_reply_img:
    for seg in ev.reply.message:
        if seg.type == "image" and seg.data:
            message.append(Message("image", seg.data["url"]))
# 逐段转换 CQ 段 → GsMessage：
for seg in messages:
    if seg.type == "text":  message.append(Message("text", seg.data["text"]))
    elif seg.type == "image": message.append(Message("image", seg.data["url"]))
    elif seg.type == "at":  message.append(Message("at", seg.data["qq"]))
    elif seg.type == "reply": message.append(Message("reply", seg.data["id"]))
```

## 8.6 平台 `bot_id` 命名速查（官方约定）

接入时给你的平台取个稳定 `bot_id`，下发路由也用它。官方已用的命名：

| 平台 | `bot_id` |
|------|---------|
| OneBot v11（go-cqhttp 等） | `onebot` |
| OneBot v12 | `onebot_v12` |
| Red 协议 | `onebot:red` |
| QQ 官方频道 | `qqguild` |
| QQ 官方群/单聊 | `qqgroup` |
| Telegram | `telegram` |
| Discord | `discord` |
| 开黑啦 | `kaiheila` |
| 米游社大别野 | `villa` |
| 黑盒语音 | `heybox` |
| 飞书 | `feishu` |
| DoDo | `dodo` |
| Milky | `milky` |
| ntchat（微信） | `ntchat` |

> 接全新平台就起一个没被占用的短名；接某协议的变体用 `主协议:变体`（见 [§2.2](./02-data-structures.md)）。
</content>
