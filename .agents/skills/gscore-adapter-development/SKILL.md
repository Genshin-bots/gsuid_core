---
name: gscore-adapter-development
description: >
  当用户要求"帮我写一个 GsCore 适配器"、"把 XXX 平台接入早柚核心 / gsuid-core"、
  "怎么连接 core 的 WebSocket"、"MessageReceive / MessageSend 怎么填"、
  "上报消息怎么写"、"core 发回来的消息怎么解析"、"base64:// 和 link:// 有什么区别"、
  "按钮 / Markdown 怎么适配到我的平台"、"node 合并转发怎么处理"、"双 ID 平台 group_id 怎么拼"、
  "QQ 官方 msg_id / msg_seq 时序问题"、"为什么我的命令前缀被吞了 / 没被识别"、
  "适配器 token 鉴权 / 断线重连怎么写"、"log_ 日志包是什么"、
  "进退群 / 戳一戳元事件怎么上报"、"wait_recall 回执 / unsend 撤回 / ban 禁言怎么在适配器实现"、
  "reply 和 reply_id 有什么区别"、"引用正文怎么上报"、"合并转发 / node 怎么上报给 core"、
  "旧适配器怎么改 reply / node"时触发此 SKILL。
  凡是"把某个聊天平台接入 GsCore"或"调试 core 与适配器之间通信"的任务都应优先读取此 SKILL。

  为 GsCore（早柚核心 / gsuid-core）机器人框架编写**平台适配器**的完整指南。适配器是运行在
  Bot 平台一侧、通过 WebSocket 与 core 通信的连接器（区别于运行在 core 内部的"插件"）。
  涵盖：早柚协议总览与二进制帧、三类数据结构（Message / MessageReceive / MessageSend / Button）、
  bot_id 的三层语义（路由 ID / 平台 ID / bot_self_id）、连接生命周期（token 鉴权 / 心跳 / 断线重连 /
  收发双协程骨架）、上报消息（平台→core，每种 content 类型如何构造、user_pm 映射、is_tome 机制、
  命令前缀处理）、发送消息（core→平台，recv 循环按 bot_id 路由、每种 type 的落地处理）、
  base64:// 与 link:// 双形态图片处理、按钮与 Markdown 跨平台映射、node 合并转发、双 ID 平台
  （villa / heybox）、QQ 官方时序、回调按钮上报、log_ 日志包、元事件上报（user_join_group /
  user_exit_group / poke 三种标准事件）、echo 撤回回执、撤回 / 禁言控制包、
  引用拆分（reply=正文 / reply_id=id）与合并转发 node 上报的新版本迁移、易错点红线清单与端到端完整示例。
---

# GsCore 适配器开发完整指南（核心入口）

> **什么是"适配器"？** 在 GsCore 生态里有两类扩展：
> - **插件（Plugin）**：跑在 core *内部*，用 `SV` + 触发器响应命令、画图、读写数据库——见同目录
>   [`gscore-plugin-development`](../gscore-plugin-development/SKILL.md)。
> - **适配器（Adapter）**：跑在 *某个聊天平台一侧*（NoneBot2 / Koishi / 原生 SDK / 你自己的 Bot 进程），
>   通过 **WebSocket** 把平台收到的消息上报给 core，再把 core 下发的消息翻译成平台 API 调用发出去。
>   **本 SKILL 只讲适配器。**
>
> 适配器负责协议转换与消息转发，不处理业务逻辑。

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
| 十一 | 元事件上报与控制消息（meta 标准三事件 进群/退群/戳一戳 上报、`echo` 撤回回执、`excute_delete_message` 主动撤回、`excute_ban_user` 禁言） | [references/11-meta-and-control.md](./references/11-meta-and-control.md) |
| 十二 | 新版本变更：引用拆分与合并转发上报（`reply`=正文 / `reply_id`=id、发送与引用查看 `node`、旧适配器推荐改法） | [references/12-reply-and-node.md](./references/12-reply-and-node.md) |

## 推荐开发流程（按需跳转）

1. **建立概念模型**：阅读 [一、协议总览](./references/01-protocol-overview.md)，明确适配器职责与数据流向。
2. **掌握数据结构**：阅读 [二、数据结构](./references/02-data-structures.md)，掌握 **bot_id 的三层语义**。
3. **建立基础连接**：依照 [三、连接生命周期](./references/03-connection-lifecycle.md) 的双协程骨架建立 WS 连接。
4. **打通上报链路**：阅读 [四、上报消息](./references/04-report-message.md)，将平台文本消息上报至 Core 并触发命令。
5. **打通下发链路**：阅读 [五、发送消息](./references/05-send-message.md)，将 Core 回复的文本与图片转发至平台。
6. **接入富媒体**：图片参考 [七、图片与多媒体](./references/07-image-and-media.md)，按钮与 Markdown 参考 [六、按钮与 Markdown](./references/06-buttons-and-markdown.md)。
7. **处理平台特性**：双 ID、QQ 时序、回调按钮参考 [八、特殊平台](./references/08-special-platforms.md)。
8. **接入元事件与控制指令**：若需上报进群、退群、戳一戳标准元事件，或支持插件 `wait_recall`、`unsend`、`ban`，参考 [十一、元事件与控制消息](./references/11-meta-and-control.md)。
9. **升级引用与合并转发**：若旧适配器仍将 ID 传入 `reply` 或未上报 `node`，依照 [十二、引用拆分与合并转发](./references/12-reply-and-node.md) 改造上报与下发。
10. **对照完整示例**：参考 [九、端到端示例](./references/09-full-adapter-example.md)。
11. **交付前自查**：逐条核对 [十、易错点红线](./references/10-pitfalls.md)、[十一、自查清单](./references/11-meta-and-control.md#116-自查清单) 与 [十二、自查清单](./references/12-reply-and-node.md#129-自查清单)。

## 关键概念速记（先看这一段再决定读哪一章）

- **两条独立链路**：上报（平台→Core，发送 `MessageReceive`）和下发（Core→平台，接收 `MessageSend`）。适配器通常维护两个并行协程：一个监听平台事件并推送到 Core，另一个监听 Core 下发并转发至平台。详见 [§1.3](./references/01-protocol-overview.md)。
- **帧必须为二进制**：Core 使用 `websocket.receive_bytes()` 读取、`send_bytes()` 发送，适配器必须发送二进制帧（使用 `msgspec.json.encode(...)` 获取 `bytes` 后通过 `ws.send(bytes)` 发送）。发送文本帧会导致解析失败。详见 [§1.2](./references/01-protocol-overview.md) 与 [§10 红线 1](./references/10-pitfalls.md)。
- **bot_id 分为三层语义**：① 路由 `/ws/{bot_id}`（连接级，如 `NoneBot2`，对应 `Event.WS_BOT_ID`）；② 每条消息的 `bot_id`（平台级，如 `onebot` / `qqgroup` / `onebot:red`，下发时据此路由到对应平台）；③ `bot_self_id`（机器人账号 ID）。详见 [§2.2](./references/02-data-structures.md)。
- **`bot_id` 含冒号会被 Core 拆分**：Core 将冒号前缀作为 `event.bot_id`，完整值保留在 `real_bot_id`（例如 `onebot:red` 解析为 `event.bot_id='onebot'`）。该机制用于多实现共用同一触发器。详见 [§2.2](./references/02-data-structures.md)。
- **图片三种形态**：Core 下发的 `image` 包含 `base64://`、`link://`（开启自动转链接）或 `file://`（协议端或本机路径，原样透传）。适配器必须同时支持三种形态。详见 [§7.1](./references/07-image-and-media.md) 与 [§10 红线 3](./references/10-pitfalls.md)。
- **`is_tome` 依赖 `at` 段触发**：上报消息时，若 `at` 段的 `data` 匹配 `bot_self_id`，Core 判定为提及机器人（`is_tome=True`）；私聊消息（`direct`）由 Core 自动设置 `is_tome=True`。详见 [§4.4](./references/04-report-message.md)。
- **引用拆分为 `reply` 与 `reply_id`**：上报时 `reply` 填入引用正文，`reply_id` 填入被引用消息 ID，引用图片作为 `image` 段一并上报。下发时 `reply` 与 `reply_id` 均作为消息 ID 转换为平台引用段。迁移指引见 [§12](./references/12-reply-and-node.md)，字段细节见 [§4.4](./references/04-report-message.md) 与 [§5.3](./references/05-send-message.md)。
- **合并转发需上报 `node`**：用户发送或引用转发卡片时，`data` 为扁平 `List[Message]`。引用转发时 `reply` 以 `[合并转发]` 开头并附带 `node`。禁止将节点正文拼入 `text`。展开规则见 [§12.4](./references/12-reply-and-node.md)，字段细节见 [§4.4](./references/04-report-message.md)。
- **命令前缀处理分工**：Core 会根据 `command_start` 剥离前缀；适配器在上报前禁止删除命令前缀（平台原生指令格式除外）。详见 [§4.5](./references/04-report-message.md)。
- **`log_{LEVEL}` 为日志回显包**：Core 向适配器输出日志时，发送 `bot_id == 路由BOT_ID` 且 `content[0].type` 为 `log_INFO/WARNING/ERROR/SUCCESS` 的数据包。适配器按对应级别打印 `data` 即可，禁止作为聊天消息发出。详见 [§5.6](./references/05-send-message.md)。
- **双 ID 平台使用连字符拼接 group_id**：针对需要双 ID 定位会话的平台（如米游社大别野、黑盒），上报时构造 `group_id = f"{villa_id}-{room_id}"`，下发时通过 `split('-')` 拆分还原。详见 [§8.1](./references/08-special-platforms.md)。
- **`node` 为合并转发且禁止嵌套**：`node` 的 `data` 为 `List[Message]`。若目标平台不支持原生合并转发，需遍历逐条发送。详见 [§5.4](./references/05-send-message.md)。
- **元事件、撤回与禁言通道**：标准元事件仅支持三种（`user_join_group`、`user_exit_group`、`poke`），上报格式为单段 `Message("meta-<事件名>", data)`；`MessageSend.echo` 非空时，发送完成后必须回执 `recall_message_id`；`excute_delete_message` 与 `excute_ban_user` 为下行控制包，适配器调用平台对应 API 执行操作，禁止作为普通消息发出。详见 [§11](./references/11-meta-and-control.md)。

## 关联文档（同仓库其他位置）

- 插件开发（Core 内部业务逻辑）：[`.agents/skills/gscore-plugin-development/SKILL.md`](../gscore-plugin-development/SKILL.md)
- 协议原始描述：`GenshinUID-docs/docs/CodeAdapter/Protocol.md`、`Pack.md`
- Core 侧关键源码定位：
  - WebSocket 入口 / token 鉴权 / `/api/send_msg`：`gsuid_core/core.py`
  - 数据结构定义：`gsuid_core/models.py`、`gsuid_core/message_models.py`
  - 上报内容解析为 `Event`：`gsuid_core/handler.py` 的 `msg_process()` / `get_user_pml()`
  - 下发消息编码：`gsuid_core/segment.py`、`gsuid_core/bot.py` 的 `target_send()`
  - 日志回显包：`gsuid_core/gs_logger.py`
- 参考实现：
  - 多平台适配器：`GenshinUID/GenshinUID/client.py`（下发 + 撤回回执）+ `__init__.py`（上报）
  - 元事件映射：`GenshinUID/GenshinUID/meta_event.py`
  - 撤回与禁言分支：`GenshinUID/GenshinUID/send_utils.py` 的 `del_msg` / `excute_ban_user`
  - 撤回与元事件协议契约：`gsuid_core/RECALL_AND_META_EVENTS.md`
  - 最小测试客户端：`gsuid_core/client.py`
  - 引用拆分与合并转发上报参考：`astrbot_plugin_gscore_adapter` 的 `main.py` / `send_utils.py`（见 [§12](./references/12-reply-and-node.md)）
