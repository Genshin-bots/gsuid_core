# 二、数据结构详解

所有结构定义见 core 的 `gsuid_core/models.py` 与 `gsuid_core/message_models.py`。适配器侧可以
**原样照抄**这几个 `Struct`（官方 NoneBot2 适配器就是把它们复制到自己的 `models.py`），也可以用
普通 dict 手搓。字段名/默认值务必对齐。

## 2.1 `Message`：消息段（最小单元）

```python
from typing import Any, Optional
from msgspec import Struct

class Message(Struct):
    type: Optional[str] = None   # 段类型：text / image / at / file / ...
    data: Optional[Any] = None   # 段内容，类型随 type 而变
```

一条消息的正文 `content` 是 `List[Message]`，即多个段拼起来（如：一段 text + 一段 image + 一段 at）。
每种 `type` 对应的 `data` 格式见 [§4](./04-report-message.md)（上报方向）和 [§5](./05-send-message.md)（下发方向）。

## 2.2 `bot_id` 的三层语义（**最重要、最易错**）

GsCore 里有三个 ID，名字相似但含义完全不同，务必分清：

| 层级 | 字段 / 位置 | 含义 | 例子 | 谁来填 |
|------|------------|------|------|--------|
| ① 路由 ID | `/ws/{bot_id}` 路径参数 | **这条 WS 连接**的标识，= 你这个适配器/框架的名字 | `NoneBot2`、`Koishi` | 适配器连接时写死 |
| ② 平台 ID | `MessageReceive.bot_id` / `MessageSend.bot_id` | **这条消息属于哪个聊天平台**，决定 core 内部走哪套渲染、决定下发时路由到哪个发送函数 | `onebot`、`qqgroup`、`qqguild`、`telegram`、`onebot:red` | 适配器**每条消息**填 |
| ③ 账号 ID | `bot_self_id` | **机器人自己的账号 ID**（平台侧的 self_id），用于区分同适配器下的多个机器人账号、判断 `is_tome` | `3399214199` | 适配器每条消息填 |

对应到 `Event`（core 内部，插件看到的）：

- 路由 ID → `Event.WS_BOT_ID`
- 平台 ID（`:` 前半部分）→ `Event.bot_id`；平台 ID 原值 → `Event.real_bot_id`
- 账号 ID → `Event.bot_self_id`

### `bot_id` 含 `:` 的拆分规则

core 的 `msg_process()` 里有这么一段：

```python
# gsuid_core/handler.py
if ":" in msg.bot_id:
    bot_id = msg.bot_id.split(":")[0]   # event.bot_id
else:
    bot_id = msg.bot_id
event = Event(bot_id, ..., real_bot_id=msg.bot_id)
```

含义：**同一套协议的多个实现共用触发器逻辑**。例如 `onebot`（go-cqhttp）和 `onebot:red`（Red 协议）
都是 OneBot 系，插件只认 `event.bot_id == 'onebot'` 即可同时命中两者；而下发时 core 会用**完整**的
`real_bot_id`（`onebot:red`）回填到 `MessageSend.bot_id`，适配器据此路由到 Red 专属发送函数。

> 实战结论：
> - 如果你的平台就是某主流协议的一个变体，用 `主协议:变体` 命名（如 `onebot:red`），白嫖现有插件适配。
> - 否则用一个独立短名（如 `mybot`），并保证上报和下发用**同一个** `bot_id`。

## 2.3 `MessageReceive`：上报包（平台 → core）

```python
class MessageReceive(Struct):
    bot_id: str = "Bot"            # ② 平台 ID
    bot_self_id: str = ""          # ③ 账号 ID
    msg_id: str = ""               # 平台侧消息 ID（被动回复/撤回/引用要用，强烈建议填）
    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
    group_id: Optional[str] = None # 群/频道/房间 ID（私聊为 None）
    user_id: str = ""              # 发送者用户 ID
    sender: Dict[str, Any] = {}    # 发送者信息，尽量含 nickname / avatar
    user_pm: int = 6               # 用户权限，越小越高（见 §4.3）
    content: List[Message] = []    # 消息正文
```

字段逐条说明见 [§4](./04-report-message.md)。

## 2.4 `MessageSend`：下发包（core → 平台）

```python
class MessageSend(Struct):
    bot_id: str = "Bot"               # ② 平台 ID（= 你上报时填的那个，据此路由）
    bot_self_id: str = ""             # ③ 账号 ID（多账号时据此选对 bot 实例）
    msg_id: str = ""                  # 原消息 ID（被动消息要回填，见 §8.2）
    target_type: Optional[str] = None # group / direct / channel / sub_channel
    target_id: Optional[str] = None   # 发送目标 ID（群号 / 用户号 / 频道号）
    content: Optional[List[Message]] = None  # 要发送的正文
```

> 注意：下发包**没有** `user_id`，发给谁完全由 `target_type + target_id` 决定。私聊时
> `target_id` 是用户 ID；群聊时是群 ID。双 ID 平台见 [§8.1](./08-special-platforms.md)。

### 特殊：`log_{LEVEL}` 日志包

core 想让适配器侧打印日志时，会下发一个 `bot_id == 路由BOT_ID`（即你的连接名，如 `NoneBot2`）、
`target_type/target_id` 为 `None`、`content=[Message('log_INFO','...')]` 的包。适配器收到后
**只按等级打印 `data`，不要当消息发出去**。详见 [§5.6](./05-send-message.md)。

## 2.5 `Button`：按钮（下发，富交互平台）

```python
# gsuid_core/message_models.py
class Button(Struct):
    text: str                      # 按钮显示文字
    data: str                      # 点击后作为"用户下一条消息"发给机器人的内容
    pressed_text: Optional[str] = None  # 点击后显示的文字
    style: Literal[0, 1] = 1       # 0 灰色线框 / 1 蓝色线框
    action: Literal[-1, 0, 1, 2] = -1  # -1 自适应 / 0 跳转 / 1 回调 / 2 命令
    permisson: Literal[0, 1, 2, 3] = 2 # 0 指定用户 / 1 管理者 / 2 所有人 / 3 指定身份组
    specify_role_ids: List[str] = []   # 仅频道：可按的身份组
    specify_user_ids: List[str] = []   # 指定可按的用户
    unsupport_tips: str = "您的客户端暂不支持该功能, 请升级后适配..."
    prefix: str = ""               # 命令前缀（core 侧已处理，适配器一般无需关心）
    _edited: bool = False          # core 内部标记
```

⚠️ 注意字段名拼写：是 **`permisson`**（少一个 `i`）不是 `permission`——core 源码就是这个拼写，
解析时别写错 key。

下发到适配器时，`buttons` 段的 `data` 已经被 `msgspec.to_builtins()` 转成
**dict / 嵌套 list of dict**（不是 Button 对象），按 key 取值即可。`buttons` 的两种布局
（`List[Button]` 自动排版 vs `List[List[Button]]` 自定义每行）见 [§6](./06-buttons-and-markdown.md)。

## 2.6 适配器侧最小结构定义（可直接复制）

如果用 msgspec，把下面这段放进适配器的 `models.py`：

```python
from typing import Any, Dict, List, Literal, Optional
from msgspec import Struct

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
```

> 不用 msgspec 也行：上报时直接构造等价的 dict 再 `json.dumps(d).encode()`；接收时
> `json.loads(raw)` 后按 key 取。只是失去类型校验，字段拼错不会报错、更难调。
</content>
