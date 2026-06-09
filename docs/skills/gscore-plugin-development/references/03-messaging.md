# 三、消息收发

## 3.1 Event 对象常用属性

```python
ev.user_id         # str：发送者用户 ID
ev.group_id        # str | None：群 ID（私聊为 None）
ev.bot_id          # str：Bot 标识
ev.bot_self_id     # str：机器人自身 ID
ev.raw_text        # str：原始消息文本（含前缀+命令）
ev.text            # str：去掉前缀+命令后的参数部分
ev.command         # str：匹配到的命令关键词
ev.is_tome         # bool：是否 @ 了机器人
ev.at              # str | None：被 @ 的用户 ID
ev.message         # list：消息段列表
ev.user_nickname   # str：用户昵称
ev.file            # bool：是否有文件附件
ev.file_name       # str | None：文件名
ev.regex_dict      # dict：正则命名分组 (?P<name>...)
ev.regex_group     # tuple：正则位置分组
```

## 3.2 Bot 发送方法

```python
from gsuid_core.segment import MessageSegment
from gsuid_core.message_models import Button

# ----- 发送纯文本 -----
await bot.send("Hello!")                      # 直接传字符串
await bot.send(MessageSegment.text("Hello!")) # 或显式构造 Message
# 多段文本可放在列表中
await bot.send(["第一行", "第二行"])

# ----- 发送图片 -----
# 支持 bytes、base64 字符串、URL、Path、PIL.Image
await bot.send(MessageSegment.image(b"图片字节"))
await bot.send(MessageSegment.image("base64://...."))
await bot.send(MessageSegment.image("https://example.com/img.png"))
await bot.send(MessageSegment.image(Path("local.png")))

# ----- 发送 Markdown（仅部分平台支持）-----
await bot.send(MessageSegment.markdown("# 标题\n内容"))
# 也可携带按钮
await bot.send(MessageSegment.markdown(
    "请选择：",
    [[Button("选项1"), Button("选项2")]]
))

# ----- 发送按钮（仅部分平台支持）-----
await bot.send([
    MessageSegment.text("点击下方按钮："),
    MessageSegment.buttons([[Button("确认"), Button("取消")]])
])

# ----- @某人 -----
await bot.send([MessageSegment.text("提醒："), MessageSegment.at("123456")])

# ----- 合并转发节点 -----
await bot.send(MessageSegment.node(["消息1", "消息2"]))

# ----- 发送文件 / 语音 / 视频 -----
await bot.send(MessageSegment.file(Path("doc.pdf"), file_name="doc.pdf"))
await bot.send(MessageSegment.record("base64://..."))
await bot.send(MessageSegment.video("https://example.com/video.mp4"))
```

## 3.3 **强烈推荐：用 `bot.send_option` 发选项 / 按钮**

只要消息后面跟着的是"用户该如何接着发"的选项，**首选 `bot.send_option`** 而不是
`bot.send` 拼一行"请输入：xxx / yyy / zzz"。理由：

- 在支持按钮的平台（QQ Bot 官方频道、QQ 机器人 Markdown 模板、Telegram、Discord 等），
  框架会自动渲染为可点击按钮，体验远好于让用户手敲命令。
- 在**不支持按钮**的平台，框架会自动 fallback 为纯文本"请输入以下命令之一: ..."，
  你只要传 `unsuported_platform=True` 它就帮你兜底——一份代码全平台通吃。
- `bot.send_option` 内部调用 `receive_resp(is_recive=False)`，**不阻塞、不等待回复**，
  纯粹发选项；想"发完等用户点一个"用 `bot.receive_resp(... option_list=[...])`。

```python
from gsuid_core.message_models import Button

@sv.on_fullmatch("菜单")
async def show_menu(bot: Bot, ev: Event) -> None:
    # 单行选项：3 个按钮自动布局
    await bot.send_option(
        reply="请选择你要查询的内容：",
        option_list=["查角色", "查武器", "查抽卡记录"],
        unsuported_platform=True,   # 不支持按钮的平台自动转纯文本
    )

@sv.on_fullmatch("绑定向导")
async def bind_wizard(bot: Bot, ev: Event) -> None:
    # 多行布局：每行一组
    await bot.send_option(
        reply="选择绑定类型：",
        option_list=[
            ["绑定UID", "绑定米游社ID"],
            ["绑定Cookie", "扫码登陆"],
            ["取消"],
        ],
        unsuported_platform=True,
    )

@sv.on_fullmatch("确认")
async def confirm_with_callback(bot: Bot, ev: Event) -> None:
    # 自定义按钮：Button(text, data, click_text)
    # - text: 按钮显示文字
    # - data: 点击后**作为下一条消息发送**给机器人（驱动下一轮触发器）
    # - click_text: 点击后给用户的反馈短语（可选）
    await bot.send_option(
        reply="是否确认绑定 UID 12345678？",
        option_list=[
            Button("✅ 确认", "确认绑定 12345678", "已确认"),
            Button("❌ 取消", "取消绑定", "已取消"),
        ],
        unsuported_platform=True,
    )
```

**`Button(text, data, click_text)` 关键点**：
- `data` 是**真正会被发送给机器人**的命令文本——点了"确认"按钮就相当于用户发了 `data` 这条
  消息，可以让框架重新走 `@sv.on_xxx` 触发链。
- 同一个 `option_list` 里混用 `str` 和 `Button` 都可以，纯字符串等价于 `Button(s, s, s)`。

**什么时候用 `bot.send`，什么时候用 `bot.send_option`**：

| 场景 | 用哪个 |
|------|--------|
| 单纯通知 / 结果 / 错误信息（没有"下一步"） | `bot.send` |
| 让用户从 N 个选项里挑一个继续 | `bot.send_option(... unsuported_platform=True)` |
| 让用户挑一个并等他回复 | `bot.receive_resp(... option_list=[...], unsuported_platform=True)` |
| 让群里**任何人**挑一个并等回复 | `bot.receive_mutiply_resp(... option_list=[...], unsuported_platform=True)` |

## 3.4 多步会话（Response）

用于需要用户多次交互的场景，分为**单用户响应**和**多用户响应**两种模式。

### 单用户响应

仅接收触发命令的同一用户后续消息。

```python
@sv.on_fullmatch("开始测试")
async def get_resp_msg(bot: Bot, ev: Event):
    await bot.send("开始多步会话测试")

    # 发送提示并等待该用户回复（默认超时60秒）
    resp = await bot.receive_resp("接下来你说的话我都会提取出来噢？")
    if resp is not None:
        await bot.send(f"你说的是 {resp.text} 吧？")
```

### 多用户响应

接收群内任意用户的后续消息，常用于游戏、投票等场景。

```python
@sv.on_fullmatch("开始多用户测试")
async def get_resp_msg(bot: Bot, ev: Event):
    await bot.send("开始多步会话测试")
    await bot.send("接下来开始游戏！？所有人的会话我都会收集起来的哦！")
    while True:
        resp = await bot.receive_mutiply_resp()
        if resp is not None:
            await bot.send(f"你说的是 {resp.text} 吧？")
```

如需限制收集时长，可配合 `asyncio.timeout`（Python 3.11+）或 `async_timeout`：

```python
@sv.on_fullmatch("开始一场60秒的游戏")
async def get_time_limit_resp_msg(bot: Bot, ev: Event):
    await bot.send("接下来开始60秒的游戏！？")
    try:
        async with asyncio.timeout(60):  # 限制时长60秒
            while True:
                resp = await bot.receive_mutiply_resp()
                if resp is not None:
                    await bot.send(f"你说的是 {resp.text} 吧？")
    except asyncio.TimeoutError:
        await bot.send("时间到!!现在开始计算每个人的分数...")
```

### 主要方法对比

| 方法 | 说明 |
|------|------|
| `bot.send_option(...)` | 发送按钮或选项提示，**不等待**回复。 |
| `bot.receive_resp(...)` | 发送可选消息，并等待**触发命令用户**的下一条消息。 |
| `bot.receive_mutiply_resp(...)` | 发送可选消息，并等待**群内任意用户**的后续消息。 |

`receive_mutiply_resp` 和 `send_option` 内部均调用 `receive_resp`，因此参数基本一致。

### 常用参数

- **`reply`**：可填入 `bot.send()` 接受的任何值（字符串、`Message`、`MessageSegment` 等），会在等待回复前先发送一次消息。
- **`option_list`**：类型 `List[str]`、`List[Button]`、`List[List[str]]` 或 `List[List[Button]]`，用于生成按钮或多选提示（部分平台支持）。
- **`timeout`**：等待回复的超时时间（秒），默认 `60`。
- **`unsuported_platform`**：当平台不支持按钮时，是否转为发送多选文本提示（默认 `False`）。
- **`sep`**、**`command_tips`**、**`command_start_text`**：在文本模式下自定义选项分隔符和提示语。

完整参数可参考代码中 `Bot.receive_resp` 的签名。
