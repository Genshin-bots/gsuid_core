# 四、上报消息（平台 → core）

上报 = 平台收到消息时，构造 `MessageReceive` 发给 core。core 用 `handler.py` 的 `msg_process()`
把 `content` 解析成 `Event`，再交给触发器/插件。**理解 `msg_process()` 怎么解析，就知道每种段该怎么填。**

## 4.1 上报包整体构造模板

```python
msg = MessageReceive(
    bot_id="onebot",            # ② 平台 ID，决定 core 走哪套逻辑、下发路由到哪
    bot_self_id=str(bot.self_id),  # ③ 机器人账号 ID
    user_type="group",          # group / direct / channel / sub_channel
    group_id="888888",          # 群/频道 ID；私聊填 None
    user_id="999999",           # 发送者 ID
    sender={"nickname": "季落", "avatar": "http://..."},
    user_pm=6,                  # 权限，越小越高（见 §4.3）
    content=[Message("text", "查询 12345")],  # 正文段列表
    msg_id="平台侧消息ID",       # 建议填，被动回复/引用要用
)
await gsclient._input(msg)      # 塞进上报队列（见 §3.3）
```

## 4.2 `user_type`：会话类型

| 值 | 含义 | `group_id` 含义 | 备注 |
|----|------|----------------|------|
| `group` | 群聊（**推荐**：群/频道/房间统统用它） | 群号 | 最常用 |
| `direct` | 私聊 | `None`（或留空） | core 自动置 `is_tome=True` |
| `channel` | 频道（**已废弃**） | 频道号 | 新接入一律用 `group` |
| `sub_channel` | 子频道（**已废弃**） | 子频道号 | 同上 |

> 结论：除非历史兼容，**一律用 `group` / `direct` 两种**。频道、房间、子频道都映射到 `group`，
> 用 `group_id` 承载其 ID；需要两个 ID 才能定位的（大别野=别野+房间）见 [§8.1](./08-special-platforms.md)。

## 4.3 `user_pm`：用户权限（越小权限越高）

core 的 `get_user_pml()` 逻辑：

```python
if user_id in masters:      return 0   # core 配置的主人 → 最高
elif user_id in superusers: return 1   # core 配置的超级用户
else:                       return user_pm if user_pm >= 1 else 2  # 用你上报的值
```

即 **0/1 由 core 配置决定，适配器管不着；适配器只负责把"平台身份"映射成 `>=1` 的 `user_pm`**。
推荐映射（与 `Protocol.md` 一致）：

- **群（`group`）**：群主 `2`、管理员 `3`、普通成员 `6`（更低身份用更大数）。
- **频道映射到 group 时**：频道主 `2`、子频道主 `3`、频道管理 `4`、子频道管理 `5`、普通 `6`。
- **私聊（`direct`）**：非超级用户恒 `6`。
- 平台没有权限体系（如 Koishi）：最高给 `1`、普通给 `6`，中间自定。

```python
pm = 6
if ev.sender.role == "owner":   pm = 2
elif ev.sender.role == "admin": pm = 3
# 超管由 core 配置识别，但若平台 SDK 能判断，也可直接给 1
if await SUPERUSER(bot, ev):    pm = 1
```

> ⚠️ 别上报 `user_pm=0`：`0` 语义是"core 主人"，越权。最高只给到 `1`。

## 4.4 `content` 各段类型（上报方向 core 能解析的）

`msg_process()` 只识别下列 6 种段，**其余类型会被忽略**（仍原样存进 `event.content`，但不参与
命令解析）。逐条说明 `data` 该填什么：

### `text` —— 文本（最核心）

```python
Message("text", "查询 12345")
```
- core 把所有 `text` 段 `.strip()` 后**拼接**进 `event.raw_text` 和 `event.text`。
- 一条消息有多段 text 会被连起来，所以你可以分段上报，core 自动合并。
- **不要**在这里塞图片占位符、CQ 码之类，纯文本即可。

### `at` —— 提及

```python
Message("at", "被提及者的用户ID")
```
- 若 `data == bot_self_id` → core 置 `is_tome=True`（**这是机器人判断"被 @"的唯一机制**），
  且该段不计入 `at_list`。
- 否则进 `event.at`（最后一个）和 `event.at_list`（全部）。
- **要让"@机器人 + 命令"生效，必须额外上报一段 `at` = `bot_self_id`**。官方适配器的做法：

  ```python
  if ev.is_tome():                       # 平台判定该消息 @ 了机器人
      message.append(Message("at", self_id))
  ```

### `image` —— 图片

```python
Message("image", "https://.../pic.jpg")   # url
Message("image", "base64://....")          # 或 base64
```
- `data` 可以是 **url** 或 **base64 字符串**。core 进 `event.image` / `event.image_list`，
  并注册一个 `image_id` 供插件下载。
- 平台给的是 `file_id` 之类间接引用时，**先在适配器侧换成 url 或 base64 再上报**（core 拿不到
  你平台的 file_id）。

### `reply` —— 引用回复

```python
Message("reply", "被引用消息的 msg_id")
```
- 进 `event.reply`。插件可据此取被引用消息。

### `record` —— 语音

```python
Message("record", "base64://...." )   # 或平台可访问的音频引用
```
- core 注册 `audio_id`，进 `event.audio_id_list`。

### `file` —— 文件

```python
Message("file", f"{file_name}|{file_base64_or_url}")
```
- 格式是 **`文件名|内容`**，用单个 `|` 分隔。`split('|')` 后：`[0]`=文件名，`[1]`=内容。
- 内容以 `http`/`https` 开头 → core 标记 `file_type='url'`，否则 `'base64'`。
- 文件较大时优先传 url（避免 base64 撑爆帧）；小文件可 base64。

> 速记：上报侧真正"有用"的段就是 **text / at / image / reply / record / file**。
> 你想让机器人响应文字命令，至少要有一段 `text`；想触发 @ 才响应的命令，再加一段
> `at == bot_self_id`。

## 4.5 命令前缀：上报侧不要乱删

- core 端会按配置的 `command_start`（如 `["", "/", "#"]`）自动削掉前缀再匹配命令，**适配器一般
  不需要处理前缀**。
- 但有些平台（QQ 官方）把斜杠 `/` 当成平台指令、消息里不带它，或带了 `@机器人 ` 噪声。官方适配器
  会在 `convert_message()` 里**只对第 0/1 段** text 削掉 `command_start` 里的词：

  ```python
  if index in (0, 1):
      for word in command_start:
          if data.strip().startswith(word):
              data = data.strip()[len(word):]
              break
  ```
  这是为「平台已经把前缀吃掉了 / 格式不一致」做的兜底。**没有这个问题就别加**，否则可能把用户
  正常文本的开头误删。

## 4.6 `sender`：发送者信息

`sender` 是自由字典，**尽量提供 `nickname` 和 `avatar`** 两个字段（core/插件/AI 都会用）：

```python
sender = {"nickname": "季落", "avatar": "http://q1.qlogo.cn/g?b=qq&nk=123&s=640"}
```
- 不同平台字段不固定，能给多少给多少；至少给 `nickname`。
- OneBot v11 示例：`sender = ev.sender.dict(); sender['avatar'] = f'http://q1.qlogo.cn/g?b=qq&nk={uid}&s=640'`。

## 4.7 `msg_id`：平台消息 ID

- 群聊主动消息可不填；但**被动回复型平台（QQ 官方）必须回填**，否则发不出消息（见 [§8.2](./08-special-platforms.md)）。
- 撤回、引用、按钮回调上报也依赖它。**能填就填**。

## 4.8 通知/事件上报（notice）：按钮回调也走上报

除了普通消息，平台的**按钮点击回调**、**文件上传通知**等也通过上报链路进 core——它们本质是构造一条
`MessageReceive`，把回调携带的 `data` 当作一段 `text` 上报，从而**驱动下一轮命令**。例如按钮回调：

```python
# 用户点了 data="确认绑定 12345" 的按钮
msg = MessageReceive(
    bot_id="dodo", bot_self_id=self_id,
    user_type="group", group_id=channel_id, user_id=user_id,
    content=[Message("text", ev.value)],   # ev.value = 按钮的 data
    msg_id=ev.event_id,
)
```
这正是 `bot.send_option` / `Button(text, data, ...)` 能工作的原因：点击 → 平台回调 → 适配器把
`data` 当文本上报 → core 重新走触发器。各平台回调事件细节见 [§8.3](./08-special-platforms.md)。

## 4.9 元事件上报（meta）：进群 / 退群 / 戳一戳

平台的**通知类事件**不是聊天消息，但也通过上报链路进 core——构造一条 `content` 为**单段**
`Message("meta-<事件名>", data)` 的 `MessageReceive`，由 core 的 `@sv.on_meta(...)` 触发器分发。
这与按钮回调（§4.8 把 `data` 当 `text` 重新驱动命令）是**两条不同的路**：meta 段**不参与**文本/命令
解析，专供插件监听平台事件。

标准事件**仅三种**：`user_join_group` / `user_exit_group` / `poke`，为平台统一考量其他事件不做适配。
三者统一的 `data` 字段、多适配器映射拆分写法见 [§11.1](./11-meta-and-control.md#111-元事件上报meta-event)。
</content>
