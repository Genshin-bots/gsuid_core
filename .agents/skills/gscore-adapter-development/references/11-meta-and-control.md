# 十一、元事件上报与控制消息（meta / recall / ban）

普通消息收发之外，core 与适配器之间还有三类**非聊天**交互：

| 方向 | 特性 | 载荷形态 |
|------|------|---------|
| 上报 平台→core | **元事件**（进群 / 退群 / 戳一戳，**标准事件仅此三种**） | `MessageReceive`，单段 `Message("meta-<事件名>", data)` |
| 上报 平台→core | **撤回回执**（发完消息把平台 msg_id 回传，供插件 `wait_recall`） | `MessageReceive`，单段 `Message("recall_message_id", {...})` |
| 下发 core→平台 | **主动撤回**（插件 `bot.unsend`）/ **禁言**（插件 `bot.ban`） | `MessageSend`，单段控制 `Message`，**不当普通消息发** |

> 协议契约的权威来源：仓库根目录 `RECALL_AND_META_EVENTS.md`。本章是其**适配器侧**落地说明，
> 配合官方参考实现 `GenshinUID/GenshinUID/meta_event.py`（上报映射）、`client.py`（下发分发与回执）、
> `send_utils.py`（撤回/禁言 API）阅读。

---

## 11.1 元事件上报（meta event）

平台收到**通知/请求类**事件（非聊天消息）时，构造一条 `MessageReceive`，`content` 放**单个**
`Message("meta-<事件名>", data)`：

```python
msg = MessageReceive(
    bot_id="onebot",
    bot_self_id=str(bot.self_id),
    user_type="group",              # 群相关→group, 私聊/好友相关→direct
    group_id="888888",              # 尽量填(用于 area / 黑白名单)
    user_id="999999",               # 尽量填
    sender={},
    content=[Message("meta-user_exit_group", {   # 注意 meta- 前缀
        "user_id": "999999",
        "group_id": "888888",
        "operator_id": "10000",
    })],
    msg_id="",
    user_pm=pm,                      # 参考实现仅判 SUPERUSER→1, 其余→6 即可
)
await gsclient._input(msg)
```

core 端 `@sv.on_meta("user_exit_group")` 触发器据此分发；插件用 `ev.get_meta("user_id")` 读 `data`。

### 关键约定

1. **`type` 必须是 `meta-` + 事件名**（事件名本身不含前缀）。core 用前缀识别 meta 段并走**独立分发路径**
   （早于黑名单与一切常规处理）。
2. **`content` 只放这一个 meta 段**，不要和文本混发——整包会被劫持进 meta 路径而跳过文本触发器。
3. **`data` 是 dict，字段名按下方统一表**，务必带上 `user_id` / `group_id`（若适用）：插件靠它读取，core 也会在
   顶层字段为空时从 `data` 回填。**但推荐顶层 `user_id`/`group_id` 也照常填**，保证 area / 黑白名单准确。
4. **只上报三种标准事件**：`user_join_group` / `user_exit_group` / `poke`（见下表）。机制上 core 能分发
   任意 `meta-*` 段，但**为平台统一考量，除这三种外其他事件不做适配**——插件侧文档只承诺监听这三种，
   自创事件名（`group_ban` / `group_recall` / `friend_add` 等）不会有插件消费，不要上报。
5. 旧 core / 无 `on_meta` 的插件收到 meta 段会**静默忽略**，零副作用——放心上报。

### 标准事件（适配器需要适配，仅此三种）

| 事件名 | 触发时机 | 必填字段 | 可选字段 |
|--------|---------|---------|---------|
| `user_join_group` | 有人进群/进频道 | `user_id` `group_id` | `operator_id`（平台提供操作者时带） |
| `user_exit_group` | 有人退群/被踢 | `user_id` `group_id` | `operator_id`（同上） |
| `poke` | 戳一戳（群/私聊） | `user_id`(发起者) `target_id`(被戳者) | `group_id`（**仅群聊**带；私聊省略，被戳者即 bot 自身——平台不提供 target 时填 `bot.self_id`） |

- **字段名必须严格按上表统一**：插件侧基于「三种事件跨平台 `data` 一致」的承诺直接读这些键，
  改名/漏填会破坏所有跨平台插件。id 值一律 `str()` 归一。
- 平台**没有对应概念就不上报**（如 QQ 官方/Discord/Telegram/飞书无 `poke`），不要硬造。
- 平台特有的**补充字段可以附加**进 `data`（如 OneBot v11 的 `sub_type`、Milky 的 `invitor_id`），
  但只能是增量，不得取代或挪用标准字段名。

### 多适配器映射要拆分（参考实现）

一个进程接多平台时，**不要**把所有 `if adapter == ... isinstance(ev, ...)` 堆进上报入口。官方做法
（`meta_event.py`）：每个适配器一个 `_xxx_to_meta(bot, ev) -> Optional[MetaEvent]` 映射函数，用
`bot.adapter.get_name()` 选函数；返回 `None` 表示该事件无需上报。适配器 import 仍**惰性置于函数内部**。

```python
# meta_event.py 精简骨架
class MetaEvent(NamedTuple):
    event_name: str
    data: Dict[str, Any]
    bot_id: Optional[str] = None      # None→由事件模块名推断
    user_type: Optional[str] = None   # None→按 data 有无 group_id 推断

def _milky_to_meta(bot, ev) -> Optional[MetaEvent]:
    from nonebot.adapters.milky.event import GroupMemberDecreaseEvent  # 惰性 import
    if isinstance(ev, GroupMemberDecreaseEvent):
        d = ev.data
        return MetaEvent("user_exit_group", {
            "user_id": str(d.user_id), "group_id": str(d.group_id),
            "operator_id": str(d.operator_id),
        })
    return None

_META_MAPPERS = {"OneBot V11": _ob11_to_meta, "Milky": _milky_to_meta, ...}

def build_meta_receive(bot, ev, pm) -> Optional[MessageReceive]:
    mapper = _META_MAPPERS.get(bot.adapter.get_name())
    result = mapper(bot, ev) if mapper else None
    if result is None:
        return None
    data = result.data
    group_id = data.get("group_id") or None
    bot_id = result.bot_id or ev.__class__.__module__.split(".")[2]
    user_type = result.user_type or ("group" if group_id else "direct")
    return MessageReceive(bot_id=bot_id, ..., user_type=user_type,
                          content=[Message(f"meta-{result.event_name}", data)], user_pm=pm)
```

> ⚠️ 这三种元事件都是平台的 **notice / request** 类事件。在 NoneBot2 里用 `on_notice()` 与
> `on_request()` 注册监听后交给映射函数（参考实现两者都注册，因个别适配器把进群分到 request）；
> 其它框架挂在对应的事件钩子上即可。**QQ 官方**等被动平台：进退群事件在频道场景，
> 按事件显式覆盖 `bot_id="qqguild"`、`user_type="group"`。

---

## 11.2 撤回回执（`echo` ↔ `recall_message_id`）

让插件 `await bot.send(msg, wait_recall=True)` 能拿到平台真实出站 msg_id（用于后续撤回/编辑/表情回应）。

### 下行：`MessageSend.echo`

`MessageSend` 多一个字段 `echo: Optional[str]`：

- `echo is None` ⇒ **fire-and-forget**，适配器**不回执**（零回程流量，旧行为）。
- `echo` 非空 ⇒ 适配器**发完真实消息后必须回执一次**，`echo` 原样带回。

> 旧适配器用不含 `echo` 字段的 `MessageSend` 解码时，msgspec **忽略未知字段** ⇒ 对旧适配器零影响。

### 上行：回传 `recall_message_id`

每个带 `echo` 的 `MessageSend`，发送成功后回传一条 `MessageReceive`（**单独成包**）：

```python
MessageReceive(
    bot_id=msg.bot_id,
    bot_self_id=msg.bot_self_id,
    content=[Message("recall_message_id", {
        "echo": msg.echo,          # 原样回传, core 据此关联
        "id": "<平台出站msg_id>",   # 见下
    })],
)
```

`id` 三种取值：

| 情形 | `id` |
|------|------|
| 正常单条消息 | 平台 msg_id（`str` 或 `int` 均可，core 会 `str()` 归一） |
| **拿不到** 平台 id（纯文件上传帧 / 平台没建模回执 / 发送异常） | `None`——**仍必须回执**，让 core 立即结算该帧 |
| 一帧被平台展开为**多条**（如不支持合并转发把 node 逐条发） | `List[str]`，core 会 flatten |

> 规则极简，适配器**无需理解"多帧"**：**每个**带 `echo` 的 `MessageSend`，发完回一次。一次插件
> `send()` 拆出 N 帧自然回 N 次。**只要 `echo` 非空就回执，与是否拿到 id 无关**——漏回会让 core
> 空等满 10 秒（`RECALL_WAIT_TIMEOUT`），插件的命令处理在这期间是阻塞的。

### 落地模式（参考实现）

让每个平台的 `*_send(...)` 函数**返回** `Optional[Union[str, List[str]]]`（出站 id），在 recv 循环里统一回执：

```python
recall_id = None
try:
    if msg.bot_id == "onebot":
        recall_id = await onebot_send(bot, msg.content, msg.target_id, msg.target_type)
    elif msg.bot_id == "telegram":
        recall_id = await telegram_send(bot, ...)
    # ...
finally:
    if msg.echo:                       # 只要 core 要回执就回, 即便 recall_id 为 None/[]
        await self._send_recall_receipt(msg, recall_id)
```

`*_send` 内部从平台返回里取 id（OneBot `ret["message_id"]`、Telegram `ret.message_id`、
QQ `ret.id`、Milky `ret.message_seq`…）；`node` 逐条发时累积成 `List[str]` 返回。

---

## 11.3 主动撤回（`excute_delete_message` 控制包）

插件 `await bot.unsend(message_id, target_type, target_id)` 时，core 对**每个** id **单独下发一个控制包**：

```python
MessageSend(
    bot_id=..., bot_self_id=...,
    target_type="group" | "direct",       # 消息所在会话
    target_id="<会话 id>",
    content=[Message("excute_delete_message", {"message_id": "<待撤回 id>"})],
    # echo 为 None: 撤回请求本身不参与回执
)
```

适配器在 recv 循环里**优先短路**（早于普通发送分发），识别"`content` 单段且 `type == "excute_delete_message"`"，
取 `data["message_id"]` 调平台撤回 API，**不当普通消息发**（否则非 OneBot 平台会误发一条空消息）：

```python
if msg.content and len(msg.content) == 1 \
        and msg.content[0].type == "excute_delete_message":
    _data = msg.content[0].data                      # data 是 dict, 不是裸串
    _mid = _data.get("message_id") if isinstance(_data, dict) else None
    if _mid is not None:
        for bot in bot_list:
            await del_msg(bot, msg.bot_id, str(_mid), msg.target_id, msg.target_type)
    continue
```

各平台撤回入参差异大，按 `bot_id` 分支（`del_msg` 参考实现）：

| 平台 | 撤回调用 | 需要会话定位 |
|------|---------|------------|
| OneBot v11 | `bot.delete_msg(message_id=int(id))` | 否（仅 id） |
| OneBot v12 | `call_api("delete_message", message_id=id)` | 否 |
| 飞书 | `call_api(f"im/v1/messages/{id}", method="DELETE")` | 否 |
| Telegram | `delete_message(chat_id=target_id, message_id=int(id))` | **是**（chat） |
| QQ 频道 | `delete_message(channel_id=target_id, message_id=id)` | **是** |
| Discord | `delete_message(channel_id=int(target_id), message_id=int(id))` | **是** |
| Milky | `recall_group_message` / `recall_private_message`（按 target_type）+ `message_seq=int(id)` | **是** |

> 因此协议**恒携带 `target_type` / `target_id`**：OneBot 系撤回只要 id，但 TG/QQ/DC/Milky 需要会话定位。
> 平台无撤回 API 时 `logger.warning("[xxx] 暂不支持撤回")` 跳过，**别误发空消息、别抛异常**。

---

## 11.4 禁言（`excute_ban_user`）

插件 `await bot.ban(user_id, group_id, duration)` 时，core 下发单段 `excute_ban_user`：

```python
MessageSend(
    content=[Message("excute_ban_user", {
        "user_id": "999999",
        "group_id": "888888",
        "duration": 600,          # 秒; 0 表示解除禁言; 协议上为 int(core 原样下发)
    })],
    bot_id=..., bot_self_id=..., target_type=..., target_id=...,
)
```

适配器调平台禁言 API（OneBot v11 参考实现）：

```python
elif _c.type == "excute_ban_user":
    user_id = _c.data.get("user_id")
    group_id = _c.data.get("group_id")
    duration = _c.data.get("duration")
    if user_id is not None and group_id is not None:
        if isinstance(duration, int) or (isinstance(duration, str) and duration.isdigit()):
            await bot.set_group_ban(
                group_id=int(group_id), user_id=int(user_id), duration=int(duration),
            )
```

- **`duration` 单位是秒，`0` = 解除禁言**——把"解禁"也走同一条路径。
- **core 不校验 `duration`，校验责任在适配器**：按参考实现兼容 int 与纯数字串，非法值静默跳过。
- 平台无禁言能力（Telegram/Discord 概念不同、私聊无禁言）时 warning 跳过即可，不必硬实现。
- Milky 等有原生禁言 API 的平台，按上面的分支接 `bot.set_group_ban` 同义接口即可补齐。

---

## 11.5 三类交互的隔离与边界

- **互不冲突**：回执在 **core 收包处**按 `type=="recall_message_id"` 拦截；meta 在 **上报分发顶部**按
  `type` 前缀 `meta-` 拦截；撤回/禁言是**下行**单段控制包。各自作用于不同 `type`，不会串。
- **撤回/禁言与普通消息共用同一发送队列**：保持与在途消息的相对顺序；断连时同样暂存、重连后发出。
- **回执降级**：旧适配器从不回执 ⇒ core 等满 10 秒（`RECALL_WAIT_TIMEOUT`）后返回 `[]`；
  连续 3 次（`RECALL_DISABLE_AFTER_TIMEOUTS`）整次零回执会把该连接 latch 为"不支持回执"，
  此后 `wait_recall=True` 立即返回 `[]` 不再空等（任意一帧成功回执即清零计数并判定支持）。
  所以**新适配器一定要老实回执**，否则被误判降级。
- **畸形容错**：meta 的 `data` 不是 dict 时，core 仍记事件名、`meta_event_data` 留空 dict，
  插件 `ev.get_meta(k, default)` 返回 default 不报错——但你**该填的字段尽量填全**。

---

## 11.6 自查清单

- [ ] meta 段 `type` 带 `meta-` 前缀，且**单独成包**、不与文本混发
- [ ] meta `data` 带齐 `user_id`/`group_id`，顶层字段也照常填
- [ ] 只上报三种标准事件（`user_join_group`/`user_exit_group`/`poke`），字段名严格按 §11.1 统一表
- [ ] 多平台映射已拆分、import 惰性
- [ ] `echo` 非空就回执，**即便没拿到 id 也回**（`id=None`）；node 逐条发回 `List[str]`
- [ ] `excute_delete_message` 在分发前短路、按 `bot_id` 调撤回 API、带会话定位、不误发空消息
- [ ] `excute_ban_user` 的 `duration=0` 当解禁；无能力平台 warning 跳过、不抛异常
