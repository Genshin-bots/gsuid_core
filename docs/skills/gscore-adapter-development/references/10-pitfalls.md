# 十、易错点与红线清单

交付前逐条自查。这些坑几乎每个新适配器都会踩一遍。

## 红线 1：必须用二进制帧

- core 用 `receive_bytes()` / `send_bytes()`。适配器**必须发二进制帧**。
- `msgspec.json.encode(...)` 返回 `bytes`，`websockets` 库里 `await ws.send(bytes_obj)` 即发二进制帧——
  **不要**先 `.decode()` 成 `str` 再发（那会发文本帧，core 端 `receive_bytes` 行为不一致 / 解析异常）。
- 接收同理：`async for msg in ws` 拿到的是 `bytes`，直接喂 `msgjson.decode`。

## 红线 2：bot_id 三层别搞混

- 路由 `/ws/{bot_id}`（连接名，如 `NoneBot2`）≠ 每条消息的 `bot_id`（平台名，如 `onebot`）≠ `bot_self_id`（账号）。
- **上报和下发要用同一个平台 `bot_id`**，否则 core 下发回来你路由不到、或走错渲染。
- 含 `:` 的平台 ID（`onebot:red`）：core 会按 `:` 拆，`event.bot_id` 只取前半，但下发 `MessageSend.bot_id`
  回填的是**完整值**，你的路由判断要用完整值。详见 [§2.2](./02-data-structures.md)。

## 红线 3：图片必须双形态处理

- `image` 段 `data` 既可能 `base64://` 又可能 `link://`。**只写一种 = 用户开了"自动转链接"就静默发不出图**。
- 标准模板见 [§7.2](./07-image-and-media.md)。`record`/`video` 恒 base64，`file` 内容也可能是 link。

## 红线 4：`log_` 包不能当消息发

- `bot_id == 路由BOT_ID` 且 `content[0].type` 以 `log` 开头的包是**日志回显**，按等级 `print`/`logger`，
  然后 `continue`。**误当普通消息发会在用户群里刷 core 内部日志。** 见 [§5.6](./05-send-message.md)。

## 红线 5：`node` 遍历发送，不要嵌套

- `node.data` 是 `List[Message]`，多数平台没有原生合并转发 → **遍历逐条发**。
- node 内**不允许再嵌 node**。core 侧也可能已按配置把 node 拆好再下发。见 [§5.4](./05-send-message.md)。

## 红线 6：`max_size` 必须调大

- `websockets` 默认最大帧 1MB，base64 图片轻松超 → 连接被动断开且报错难懂。
- 连接时设 `max_size=2**26`（或更大）。见 [§3.1](./03-connection-lifecycle.md)。

## 红线 7：断线必须能自愈，但别狂连

- core 重启/网络抖动会断，适配器要重连。
- **加退避**（≥5s 或指数退避）、**限次数**，别 0 延迟无限重连打满 CPU/日志。
- 重连用**同一路由 `bot_id`**，能复用 core 保留 5 分钟的发送队列、少丢消息。见 [§3.5](./03-connection-lifecycle.md)。

## 红线 8：上报队列串行化，别并发裸 send

- 平台事件是并发回调，多个回调同时 `ws.send` 会交错损坏帧。用 `asyncio.Queue` + 单协程串行发。
  见 [§3.3](./03-connection-lifecycle.md)。

## 红线 9：下发循环单条出错不能整体崩

- `recv_msg` 的 `for` 体内**包 `try/except`**，单条消息处理异常（某平台 API 报错）只记日志，
  不能让整个接收循环退出导致后续全收不到。

## 红线 10：`user_pm` 不要给 0

- `0` = core 主人，`1` = 超级用户，都由 **core 配置**决定。适配器映射的平台身份**最高只到 `1`**，
  群主 `2` / 管理 `3` / 普通 `6`。见 [§4.3](./04-report-message.md)。

## 红线 11：`is_tome` 靠额外 `at` 段，不是 `is_tome` 字段

- 想让"@机器人 + 命令"触发，上报时要**额外加一段 `Message("at", bot_self_id)`**。
- 私聊（`direct`）core 自动置 `is_tome=True`，不用加。见 [§4.4](./04-report-message.md)。

## 红线 12：被动消息平台要回填 `msg_id` / 维护 `msg_seq`

- QQ 官方等被动平台：下发的 `MessageSend.msg_id` 要回填进发送 API；同一 `msg_id` 多次回复要递增
  `msg_seq`。漏了就发不出。见 [§8.2](./08-special-platforms.md)。

## 红线 13：双 ID 平台 `group_id` 用 `-` 拼接并一致拆分

- 上报 `group_id = f"{id1}-{id2}"`，下发 `id1, id2 = target_id.split("-")`。两边格式必须一致。
  必要时用 core 附带的 `group` 段拿第二个 ID。见 [§8.1](./08-special-platforms.md)。

## 红线 14：不支持的类型 warning + 跳过，不抛异常

- 平台不支持语音/视频/MD/按钮时，`logger.warning('[xxx] 暂不支持 yyy')` 然后跳过，
  **不要 raise**——否则同一条消息里其它能发的内容也跟着失败。

## 红线 15：平台 `file_id` 类引用要在适配器侧解开再上报

- core 看不懂你平台的 `file_id` / 内部图片引用。**上报前**把图片/文件换成 url 或 base64。
  见 [§7.6](./07-image-and-media.md)。

## 自查清单（复制到 PR 描述里逐条打勾）

- [ ] 发的是二进制帧，`max_size` 已调大
- [ ] 上报/下发用同一平台 `bot_id`；含 `:` 的路由判断用完整值
- [ ] `image` 同时处理 `base64://` 和 `link://`
- [ ] `log_` 包已特判、不外发
- [ ] `node` 遍历逐发、不嵌套
- [ ] 断线重连有退避有限次、复用同一路由 `bot_id`
- [ ] 上报走队列串行；下发循环单条 `try/except`
- [ ] `user_pm` 不给 0；`is_tome` 用额外 `at` 段
- [ ] 被动平台回填 `msg_id`、维护 `msg_seq`
- [ ] 双 ID 平台 `group_id` 用 `-` 拼/拆一致
- [ ] 不支持的类型 warning 跳过、不抛异常
- [ ] 平台内部引用（file_id 等）上报前已转 url/base64
</content>
