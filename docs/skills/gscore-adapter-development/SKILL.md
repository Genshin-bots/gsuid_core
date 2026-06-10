---
name: gscore-adapter-development
description: >
  当用户要求"帮我写一个 GsCore 适配器"、"把 XXX 平台接入早柚核心 / gsuid-core"、
  "怎么连接 core 的 WebSocket"、"MessageReceive / MessageSend 怎么填"、
  "上报消息怎么写"、"core 发回来的消息怎么解析"、"base64:// 和 link:// 有什么区别"、
  "按钮 / Markdown 怎么适配到我的平台"、"node 合并转发怎么处理"、"双 ID 平台 group_id 怎么拼"、
  "QQ 官方 msg_id / msg_seq 时序问题"、"为什么我的命令前缀被吞了 / 没被识别"、
  "适配器 token 鉴权 / 断线重连怎么写"、"log_ 日志包是什么"时触发此 SKILL。
  凡是"把某个聊天平台接入 GsCore"或"调试 core 与适配器之间通信"的任务都应优先读取此 SKILL。

  为 GsCore（早柚核心 / gsuid-core）机器人框架编写**平台适配器**的完整指南。适配器是运行在
  Bot 平台一侧、通过 WebSocket 与 core 通信的连接器（区别于运行在 core 内部的"插件"）。
  涵盖：早柚协议总览与二进制帧、三类数据结构（Message / MessageReceive / MessageSend / Button）、
  bot_id 的三层语义（路由 ID / 平台 ID / bot_self_id）、连接生命周期（token 鉴权 / 心跳 / 断线重连 /
  收发双协程骨架）、上报消息（平台→core，每种 content 类型如何构造、user_pm 映射、is_tome 机制、
  命令前缀处理）、发送消息（core→平台，recv 循环按 bot_id 路由、每种 type 的落地处理）、
  base64:// 与 link:// 双形态图片处理、按钮与 Markdown 跨平台映射、node 合并转发、双 ID 平台
  （villa / heybox）、QQ 官方时序、回调按钮上报、log_ 日志包、易错点红线清单与端到端完整示例。
---

# GsCore 适配器开发完整指南（核心入口）

> **什么是"适配器"？** 在 GsCore 生态里有两类扩展：
> - **插件（Plugin）**：跑在 core *内部*，用 `SV` + 触发器响应命令、画图、读写数据库——见同目录
>   [`gscore-plugin-development`](../gscore-plugin-development/SKILL.md)。
> - **适配器（Adapter）**：跑在 *某个聊天平台一侧*（NoneBot2 / Koishi / 原生 SDK / 你自己的 Bot 进程），
>   通过 **WebSocket** 把平台收到的消息上报给 core，再把 core 下发的消息翻译成平台 API 调用发出去。
>   **本 SKILL 只讲适配器。**
>
> 一句话：适配器是 core 与聊天平台之间的「翻译官 + 邮差」，它不处理任何业务逻辑，只做
> **协议转换**和**消息搬运**。

> 本 SKILL 按章节拆分为「主入口 + `references/` 子文档」。需要某专题细节时，顺着下文相对路径
> 按需 `ReadFile` 对应文件，**不要**一次性把所有内容塞进上下文。

## 文档目录索引

| 章节 | 主题 | 链接 |
|------|------|------|
| 一 | 早柚协议总览（WebSocket、`/ws/{bot_id}` 路由、二进制帧、收发全景图、与插件的区别） | [references/01-protocol-overview.md](./references/01-protocol-overview.md) |
| 二 | 数据结构详解（`Message` / `MessageReceive` / `MessageSend` / `Button` 全字段表 + bot_id 三层语义） | [references/02-data-structures.md](./references/02-data-structures.md) |
| 三 | 连接生命周期（token 鉴权、心跳、断线重连、收发双协程骨架、最小可运行客户端） | [references/03-connection-lifecycle.md](./references/03-connection-lifecycle.md) |
| 四 | 上报消息（平台→core，`MessageReceive` 每种 content 类型构造 + `user_pm` 映射 + `is_tome` 机制） | [references/04-report-message.md](./references/04-report-message.md) |
| 五 | 发送消息（core→平台，recv 循环按 `bot_id` 路由 + 每种 `type` 落地处理） | [references/05-send-message.md](./references/05-send-message.md) |
| 六 | 按钮与 Markdown 适配（`Button` 全字段、单行/多行布局、各平台映射实例、template_* 模板） | [references/06-buttons-and-markdown.md](./references/06-buttons-and-markdown.md) |
| 七 | 图片与多媒体（`base64://` vs `link://` 双形态、`image_size`、语音/视频/文件、上传图床） | [references/07-image-and-media.md](./references/07-image-and-media.md) |
| 八 | 特殊平台适配要点（双 ID 平台 / `group` 段 / QQ 官方 msg_id-msg_seq 时序 / 回调按钮上报 / 文件上报） | [references/08-special-platforms.md](./references/08-special-platforms.md) |
| 九 | 端到端完整示例（最小适配器 → OneBot v11 全功能适配器） | [references/09-full-adapter-example.md](./references/09-full-adapter-example.md) |
| 十 | 易错点与红线清单（二进制帧、bot_id 路由、双形态图片、node、log 包、msg_id 时序…） | [references/10-pitfalls.md](./references/10-pitfalls.md) |

## 推荐开发流程（按需跳转）

1. **先建立心智模型**：读 [一、协议总览](./references/01-protocol-overview.md)，搞清「适配器干什么、数据怎么流」。
2. **背下数据结构**：读 [二、数据结构](./references/02-data-structures.md)，尤其是 **bot_id 的三层语义**——这是最容易搞错的地方。
3. **先把连接跑起来**：照 [三、连接生命周期](./references/03-connection-lifecycle.md) 的双协程骨架建立 WS 连接，能 ping 通即可。
4. **打通上报链路**：读 [四、上报消息](./references/04-report-message.md)，让平台的文本消息能传到 core 并触发命令。
5. **打通下发链路**：读 [五、发送消息](./references/05-send-message.md)，让 core 回复的文本/图片能发回平台。
6. **补齐富媒体**：图片走 [七、图片与多媒体](./references/07-image-and-media.md)，按钮/MD 走 [六、按钮与 Markdown](./references/06-buttons-and-markdown.md)。
7. **处理平台怪癖**：双 ID、QQ 时序、回调按钮等看 [八、特殊平台](./references/08-special-platforms.md)。
8. **对照完整示例**：随时参考 [九、端到端示例](./references/09-full-adapter-example.md)。
9. **交付前自查**：逐条过 [十、易错点红线](./references/10-pitfalls.md)。

## 关键概念速记（先看这一段再决定读哪一章）

- **两条独立链路**：上报（平台→core，发 `MessageReceive`）和下发（core→平台，收 `MessageSend`），
  在适配器里通常是**两个并行协程**：一个监听平台事件往 core 推，一个监听 core 下发往平台发。详见 [§1.3](./references/01-protocol-overview.md)。
- **帧是二进制不是文本**：core 用 `websocket.receive_bytes()` 读、`send_bytes()` 写，适配器必须发**二进制帧**
  （`msgspec.json.encode(...)` 得到 `bytes` 直接 `ws.send(bytes)`）。发文本帧会解析失败。详见 [§1.2](./references/01-protocol-overview.md) 与 [§10 红线 1](./references/10-pitfalls.md)。
- **bot_id 有三层，别搞混**：① 路由 `/ws/{bot_id}`（连接级，如 `NoneBot2`，对应 `Event.WS_BOT_ID`）；
  ② 每条消息的 `bot_id`（平台级，如 `onebot` / `qqgroup` / `onebot:red`，**下发时按它路由到对应平台**）；
  ③ `bot_self_id`（机器人账号 ID）。详见 [§2.2](./references/02-data-structures.md)。
- **`bot_id` 含 `:` 会被 core 拆分**：core 用 `:` 前的部分做 `event.bot_id`，完整值留在 `real_bot_id`
  （如 `onebot:red` → `event.bot_id='onebot'`）。这是同一协议多实现共用触发器的机制。详见 [§2.2](./references/02-data-structures.md)。
- **图片永远是双形态**：core 下发的 `image` 既可能是 `base64://...` 也可能是 `link://...`（开了"自动转链接"时），
  适配器**两种都必须处理**，否则用户一开转链接就发不出图。详见 [§7.1](./references/07-image-and-media.md) 与 [§10 红线 3](./references/10-pitfalls.md)。
- **`is_tome` 靠 `at` 段触发**：上报时若把一条 `at` 段的 `data` 填成 `bot_self_id`，core 会判定"@了机器人"
  （`is_tome=True`）；私聊（`direct`）则 core 自动置 `is_tome=True`。详见 [§4.4](./references/04-report-message.md)。
- **命令前缀处理在两边都有**：core 端会按 `command_start` 削掉前缀；适配器上报前**不要**自作主张删命令前缀
  （除非平台特性需要，如 QQ 官方把 `/` 当指令）。详见 [§4.5](./references/04-report-message.md)。
- **`log_{LEVEL}` 是日志回显包**：core 想在适配器侧打日志时，发一条 `bot_id == 路由BOT_ID` 且
  `content[0].type` 为 `log_INFO/WARNING/ERROR/SUCCESS` 的包，适配器**只需按等级打印 `data`，不要当普通消息发**。详见 [§5.6](./references/05-send-message.md)。
- **双 ID 平台用 `-` 拼接 group_id**：米游社大别野、黑盒等需要两个 ID 才能定位会话的平台，上报时
  `group_id = f"{villa_id}-{room_id}"`，下发时再 `split('-')` 拆回。core 还会把 `group` 类型段附在末尾辅助定位。详见 [§8.1](./references/08-special-platforms.md)。
- **`node` 是合并转发，不能嵌套**：`node` 的 `data` 是 `List[Message]`，多数平台不支持原生合并转发，
  需要**遍历逐条发送**。详见 [§5.4](./references/05-send-message.md)。

## 关联文档（同仓库其他位置）

- 插件开发（core 内部业务逻辑，与适配器互补）：[`docs/skills/gscore-plugin-development/SKILL.md`](../gscore-plugin-development/SKILL.md)
- 协议原始描述（精简版，本 SKILL 是其超集）：`GenshinUID-docs/docs/CodeAdapter/Protocol.md`、`Pack.md`
- core 侧关键源码定位：
  - WebSocket 入口 / token 鉴权 / `/api/send_msg`：`gsuid_core/core.py`
  - 数据结构定义：`gsuid_core/models.py`、`gsuid_core/message_models.py`
  - 上报内容解析为 `Event`：`gsuid_core/handler.py` 的 `msg_process()` / `get_user_pml()`
  - 下发消息编码（`base64://`/`link://`/`node`/`image_size`）：`gsuid_core/segment.py`、`gsuid_core/bot.py` 的 `target_send()`
  - 日志回显包：`gsuid_core/gs_logger.py`
- **官方参考实现**（强烈建议对照阅读）：
  - 多平台全功能适配器：`GenshinUID/GenshinUID/client.py`（下发）+ `__init__.py`（上报）
  - 最小可运行测试客户端：`gsuid_core/client.py`
</content>
</invoke>
