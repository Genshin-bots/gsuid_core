# 五、发送消息（core → 平台）

下发 = 适配器在 `recv_msg` 收到 core 的 `MessageSend`，按 `bot_id` 路由到平台专属发送函数，
逐段把 `content` 翻译成平台 API 调用。

## 5.1 recv 循环 + 按 `bot_id` 路由

```python
async def recv_msg(self):
    async for raw in self.ws:
        try:
            msg = msgjson.decode(raw, type=MessageSend)

            # ① 先处理日志包（见 §5.6）
            if msg.bot_id == BOT_ID:          # BOT_ID = 你的路由 ID，如 "NoneBot2"
                if msg.content and msg.content[0].type and msg.content[0].type.startswith("log"):
                    level = msg.content[0].type.split("_")[-1].lower()  # info/warning/error/success
                    getattr(logger, level)(msg.content[0].data)
                continue

            # ② 按平台 ID 路由到对应发送函数
            if msg.bot_id == "onebot":
                await onebot_send(bot, msg.content, msg.target_id, msg.target_type)
            elif msg.bot_id == "qqgroup":
                await group_send(bot, ...)
            elif msg.bot_id == "telegram":
                await telegram_send(bot, ...)
            # … 其余平台
        except Exception as e:
            logger.exception(e)
```

> 单适配器只接一个平台时，路由分支只有一个（你自己的 `bot_id`）。官方 GenshinUID 适配器一个进程
> 接了十几个平台，所以才有一长串 `elif`。

## 5.2 逐段解析 `content`

下发的 `content` 可能含多种段，**先一次性把它们抽取到局部变量，再按平台能力组装**。官方写法：

```python
content = ""; image = None; record = None; node = []
file = ""; at_list = []; group_id = ""; markdown = ""
video = ""; buttons = []; template_buttons = ""; template_markdown = {}

for _c in msg.content:
    if not _c.data:
        continue
    if   _c.type == "text":              content += _c.data
    elif _c.type == "image":             image = _c.data
    elif _c.type == "node":              node = _c.data
    elif _c.type == "file":              file = _c.data
    elif _c.type == "at":                at_list.append(_c.data)
    elif _c.type == "record":            record = _c.data
    elif _c.type == "video":             video = _c.data
    elif _c.type == "group":             group_id = _c.data        # 第二个定位 ID，见 §8.1
    elif _c.type == "markdown":          markdown = _c.data
    elif _c.type == "buttons":           buttons = _c.data
    elif _c.type == "template_markdown": template_markdown = _c.data
    elif _c.type == "template_buttons":  template_buttons = _c.data
    # image_size 一般只在 markdown 渲染时配合 image 用，可在需要时单独取
```

下面逐类型说明 `data` 格式与落地方式。

## 5.3 基础类型

### `text`

```python
data == "纯文本"
# → MessageSegment.text(data)
```

### `image`（**双形态，必须都处理**）

`data` 有两种前缀，缺一不可：

```python
if image.startswith("link://"):
    url = image.replace("link://", "")        # 远程 URL，直接发 url 或下载
    # 多数平台：MessageSegment.image(url)
else:  # "base64://"
    img_bytes = base64.b64decode(image.replace("base64://", ""))
    # MessageSegment.image(img_bytes) / 平台上传接口
```

- **`base64://`**：默认形态。`data[9:]` 是 base64，`b64decode` 得 bytes。
- **`link://`**：core 开启「发送图片自动转链接」时给的远程 URL（`data[7:]` 是 url）。
- ⚠️ **两种都要写**，否则用户一旦开启转链接，你的适配器就发不出图。详见 [§7](./07-image-and-media.md)。

### `at`

```python
data == "用户ID"
# 群聊里在消息前/后插入 @：MessageSegment.at(data) / mention_user(data)
```
- 通常**仅群聊**有意义，私聊忽略。注意 `at_list` 可能有多个。

### `reply`

```python
data == "要引用的 msg_id"
# → 平台的引用回复段
```

### `record` / `video`（语音 / 视频）

```python
data == "base64://...."   # 恒为 base64
b = base64.b64decode(data.replace("base64://", ""))
# record → 平台语音段；video → 平台视频段
```
- 平台不支持就 `logger.warning(...)` 跳过，别让整条消息失败。

### `file`

```python
data == "文件名|内容"
file_name, file_content = data.split("|")
# 内容以 link:// 开头 → url 上传；否则是 base64 → 解码后上传
```
落地见 [§7.4](./07-image-and-media.md)（各平台文件上传差异大）。

## 5.4 `node`：合并转发（**重点易错**）

```python
data == List[Message]   # 解码后是 list of dict：每项 {"type": "...", "data": "..."}
```

- 语义：QQ 的"合并转发卡片"。**禁止嵌套**（node 里不能再有 node）。
- **绝大多数平台不支持原生合并转发** → 标准做法是**遍历逐条发送**：

```python
if node:
    for _msg in node:
        if _msg["type"] == "image":
            await _send(content=None, image=_msg["data"])
        else:  # text / at...
            await _send(content=_msg["data"], image=None)
else:
    await _send(content, image)
```

- 仅 OneBot v11（QQ）能真正发合并转发，用 `send_group_forward_msg` + `to_json` 包装：

```python
def to_json(msg, name, uin):
    return {"type": "node", "data": {"name": name, "uin": uin, "content": msg}}
```

- core 侧还有个「合并转发处理方式」配置（`允许`/`全部拆成单独消息`/`合并为一条`/`禁止`/数字上限），
  core 会**先按配置把 node 拆好**再下发，所以你收到的 node 可能已被 core 预处理。适配器照「遍历逐条」
  兜底即可。

## 5.5 单条消息的发送骨架（`_send` 闭包模式）

官方适配器普遍用一个内层 `_send(content, image)` 闭包，把"组装一条消息并发出"封装起来，
再在外层处理 node 的循环。模板：

```python
async def some_platform_send(bot, content, image, node, at_list, target_id, target_type):
    async def _send(content, image):
        message = PlatformMessage()
        if image:
            message.append(make_image_segment(image))   # 内含 base64/link 双处理
        if content:
            message.append(PlatformMessage.text(content))
        if at_list and target_type == "group":
            for at in at_list:
                message.append(PlatformMessage.at(at))
        if not message:
            return
        if target_type == "group":
            await bot.send_group(target_id, message)
        else:
            await bot.send_private(target_id, message)

    if node:
        for _msg in node:
            if _msg["type"] == "image":
                await _send(None, _msg["data"])
            else:
                await _send(_msg["data"], None)
    else:
        await _send(content, image)
```

按 `target_type`（`group` / `direct`）选群发还是私发，是每个发送函数的标配分支。

## 5.6 `log_{LEVEL}`：日志回显包（**必须特判**）

core 想在适配器控制台打日志时，下发：

```python
MessageSend(
    bot_id=路由BOT_ID,            # 注意：是你的连接名（如 "NoneBot2"），不是平台 ID
    target_type=None, target_id=None,
    content=[Message("log_INFO", "某条日志文本")],
)
```

适配器**在路由分发之前**就要拦下它（见 §5.1 ①），按等级打印，`continue` 跳过，**绝不能当普通消息
往平台发**——否则用户群里会刷屏 core 的内部日志。`type` 形如 `log_INFO`/`log_WARNING`/
`log_ERROR`/`log_SUCCESS`，`split('_')[-1].lower()` 得 `logger` 方法名。

## 5.7 下发类型总表

| `type` | `data` 格式 | 落地方式 | 平台支持度 |
|--------|------------|----------|-----------|
| `text` | str | 文本段 | 全部 |
| `image` | `base64://` 或 `link://` | 图片段（双处理）| 全部 |
| `at` | 用户 ID str | @ 段（仅群） | 多数 |
| `reply` | msg_id str | 引用段 | 部分 |
| `record` | `base64://` | 语音段 | 部分 |
| `video` | `base64://` | 视频段 | 部分 |
| `file` | `名\|内容` | 文件上传 | 部分 |
| `node` | `List[Message]` | 合并转发 / 遍历逐发 | 仅 QQ 原生 |
| `markdown` | str（MD 文本） | MD 段 | QQ官方/开黑啦/DoDo… |
| `template_markdown` | `{template_id, para}` | MD 模板 | 仅 QQ 官方 |
| `buttons` | dict / 嵌套 list | 按钮键盘 | QQ/TG/DC/Villa… |
| `template_buttons` | 模板 ID str | 按钮模板 | 仅 QQ 官方 |
| `image_size` | `[w, h]` | 配合 MD 图片尺寸 | 渲染辅助 |
| `group` | group_id str | 第二定位 ID | 双 ID 平台 |
| `log_{LEVEL}` | 日志文本 | 打印日志，**不发送** | —— |
| `excute_delete_message` | `{"message_id": str}` | **单段控制包**：调平台撤回 API，**不发送** | 见 §11.3 |
| `excute_ban_user` | `{"user_id","group_id","duration"}` | 调平台禁言 API，**不发送** | 见 §11.4 |

`markdown` / `buttons` / `template_*` 的详细适配见 [§6](./06-buttons-and-markdown.md)；
`image` / `record` / `video` / `file` 见 [§7](./07-image-and-media.md)；`group` 见 [§8.1](./08-special-platforms.md)。

## 5.8 撤回回执（`MessageSend.echo`）与控制包

除上表的"内容段"，下发还有两类**非聊天**载荷，必须在 §5.1 的 recv 循环里**专门处理**，
不要塞进 §5.2 的内容抽取：

- **`MessageSend.echo`**：非空时，适配器发完真实消息**必须回传一条** `recall_message_id` 上行包
  （带回 `echo` + 平台 msg_id），供插件 `bot.send(..., wait_recall=True)` 拿到出站 id。漏回会被 core
  误判"不支持回执"而降级。
- **`excute_delete_message` / `excute_ban_user` 控制包**：`content` 单段、对应插件 `bot.unsend` / `bot.ban`，
  按 `bot_id` 调平台撤回/禁言 API，**绝不当普通消息发**（否则非 OneBot 平台会误发空消息）。

三者的协议契约、各平台 API 分支、落地模式见 [§11](./11-meta-and-control.md)。
</content>
