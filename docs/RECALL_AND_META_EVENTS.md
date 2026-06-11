# `recall_message_id` 回执 与 `on_meta` 元事件 —— 实现要点

> 状态：**已实现并通过自测**（2026-06-10；2026-06-11 增补 `bot.unsend` 主动撤回 API，见 §1.7）
> 关联设计：`plans/recall_message_id_design_20260610.md`、`plans/meta_event_trigger_design_20260610.md`
> 本文记录两特性最终落地的契约、协议、配置、边界与**有意未实现的开放项**，供插件 / adapter 作者与后续维护者查阅。

---

## 一、`recall_message_id` 回执机制

### 1.1 插件侧契约

```python
recall_ids = await bot.send(msg, wait_recall=True)
if recall_ids:
    for mid in recall_ids:
        ...  # 每个气泡一个真实出站 id，可分别撤回 / 编辑 / 表情回应 / 置顶
```

- 返回类型恒为 `Optional[List[str]]`。
- **`wait_recall` 默认 `False`**（opt-in）。不传时返回 `None`，行为与改动前**逐字节一致、零额外开销**。
- 一旦 `wait_recall=True`：
  - 正常：返回**按帧序**排列的 id 列表，**哪怕只有一个气泡也是单元素 list**（`["<id>"]`，**绝不解包成裸 str**）。
  - 旧 adapter 不回执 / 超时：返回 `[]`（空 list）。
  - 多帧（markdown 拆分、按钮独立帧、转发展开）：返回**全部气泡** id；某帧未回执则其 id 被**过滤**出 list（元素类型恒为 `str`）。
- `list[0]` 恒为主气泡，顺序与气泡出站顺序一致，**与回执到达顺序无关**（乱序回执 → 有序返回）。

支持 `wait_recall` 的入口：`Bot.send`、`Bot.target_send`（跨会话）。

### 1.2 协议

- **下行**（core → adapter）：`MessageSend` 新增 `echo: Optional[str]` 字段。
  - `echo is None` ⇒ adapter **不回执**（纯 fire-and-forget，零回程流量）。
  - `echo` 非空 ⇒ adapter 发完真实消息后**必须回执**。每帧各带不同 `echo`。
- **上行**（adapter → core）：复用 `MessageReceive` 承载，**单独成包**：

```python
MessageReceive(
    bot_id=...,
    content=[Message(type="recall_message_id",
                     data={"echo": "<原样回传>", "id": "<平台出站msg_id>"})],
)
```

> adapter 规则（无需理解"多帧"概念）：**每个**带 `echo` 的 `MessageSend`，发送平台消息成功后回传一个 `recall_message_id` 控制消息，`echo` 原样、`id` 填平台返回 id。一次 `send()` 拆出的 N 帧自然回 N 次。
>
> core 收到回执后会对 `echo` 与 `id` 做 `str()` 归一——OneBot 等平台 `message_id` 为 int，adapter 不必自行转字符串，插件侧拿到的元素类型恒为 `str`。

### 1.3 内部机制

- 一次 `send()` 的**每一帧**盖各自 `echo`（`_Bot._echo_seq` 逐帧自增，连接内唯一），对应一个 `asyncio.Future`，登记在 `_Bot._recall_waiters: Dict[str, Future]`（**发帧前登记**，避免回执先于登记到达的竞态）。
- core 在 **WS 收包处**（`core.py`）调用 `_Bot.resolve_recall(msg)` 优先拦截回执：按 `echo` O(1) 唤醒对应 future，**不进入 `handle_event` 正常管道**。只要 `type=="recall_message_id"` 即拦截（即便 echo 未命中），防止迟到 / 重复回执误入消息处理。
- 发送循环后用**一次** `asyncio.wait(futs, timeout=RECALL_WAIT_TIMEOUT)` 覆盖全部帧；`try/finally` 保证任何路径（正常 / 部分超时 / 全超时 / 异常 / 取消）都清空本次登记项，**杜绝泄漏**。
- 收集时**遍历登记顺序**（= 帧序）而非 `done` 集合，过滤 `None`。

### 1.4 能力探测（latch）与降级

- `_Bot._supports_recall`：`None`=未知 / `True`=支持 / `False`=已确认不支持。
- 任意一帧成功回执 ⇒ 判定支持并清零超时连击数。
- 连续 `RECALL_DISABLE_AFTER_TIMEOUTS` 次「整次调用零回执」且当前为 `None` ⇒ latch 为 `False`，此后 `wait_recall=True` **立即返回 `[]` 不再等待**（避免旧 adapter 反复吃满超时）。
- latch 是连接级属性：断连重连（≤5 分钟复用实例）保持；>5 分钟丢弃重建的新实例回到 `None` 重新探测。

### 1.5 配置（模块常量，`gsuid_core/bot.py`）

| 常量 | 默认 | 含义 |
|---|---|---|
| `RECALL_WAIT_TIMEOUT` | `10.0` | 回执等待上限（秒），仅 `wait_recall=True` 生效；多帧共享一个窗口 |
| `RECALL_DISABLE_AFTER_TIMEOUTS` | `3` | 连续整次零回执达此次数后 latch 为不支持 |

> 按需求方决定：**不**提升为热配置（`sp_config`）。

### 1.6 边界与兼容性

| 场景 | 行为 |
|---|---|
| 旧插件 `bot.send(msg)`（不传 flag） | 返回 `None`，与改动前逐字节一致 |
| 旧 adapter + `wait_recall=True` | 超时 → `[]`；连续整次零回执后 latch，后续即时 `[]` |
| 新 adapter 单帧 / 多帧 | `["<id>"]` / `["<id0>", "<id1>", ...]`（按帧序） |
| HTTP `/api/send_msg`（`task_event` 非空） | recall 关闭，`target_send` 返回 `None`；响应 JSON 多一个无害的 `echo: null` |
| AI 上下文（`MockBot`） | 拦截不真实发送，`wait_recall` 被忽略、返回 `None`（语义正确，签名已兼容防 `TypeError`） |
| 断连 | `disconnect` 将在途 future `set_result(None)` 并清空登记表，调用方拿到已到达部分（可能为空 list） |

> 旧 adapter 用自己那份不含 `echo` 的 `MessageSend` 解码时，msgspec 忽略未知字段 ⇒ **对旧 adapter 无影响**。

### 1.7 `bot.unsend` 主动撤回 API（2026-06-11 增补）

§五原开放项「`bot.recall(message_ids)` 高层撤回 API」已由需求方确定下行协议并落地，方法名定为 `unsend`。

#### 插件侧契约

```python
recall_ids = await bot.send(msg, wait_recall=True)
...
await bot.unsend(recall_ids)        # 撤回本次全部气泡；List[str]
await bot.unsend(recall_ids[0])    # 也可单个撤回；str / int 均可（int 会 str() 归一）
```

- 签名：`Bot.unsend(message_id, target_type=None, target_id=None) -> None`。
- `message_id` 接受 `str | int | List[str | int] | None`：
  - 传 `None` / 空列表 ⇒ **静默忽略**（可直接透传 `wait_recall` 的返回值，无需判空）。
  - 列表 ⇒ 逐 id 按序各发一个撤回请求包。
- `target_type` / `target_id` 缺省取**当前事件所在会话**；撤回 `target_send` 发往其他会话的
  消息时需**连同 `target_id` 一起显式传入**（OneBot 撤回仅需 id，但 Telegram / Discord 等
  平台撤回需要会话定位，故协议恒携带 target 字段）。
- fire-and-forget：**无回执、无返回值**，不保证平台侧一定成功（超时窗口、权限等由平台决定）。
- 底层入口：`_Bot.unsend(message_id, target_type, target_id, bot_id, bot_self_id)`（框架内部 / 无 `Event` 场景用）。

#### 协议（下行 core → adapter）

每个待撤回 id **单独成包**，复用 `MessageSend` 承载：

```python
MessageSend(
    bot_id=...,                      # 照常填
    bot_self_id=...,
    target_type="group" | "direct" | ...,   # 消息所在会话
    target_id="<会话 id>",
    content=[Message(type="excute_delete_message", data="<待撤回消息 id>")],
)
```

- `data` 即待撤回消息 id，core 侧已 `str()` 归一。
- `msg_id` 留空、`echo` 为 `None`（撤回请求本身不参与回执机制）。
- adapter 规则：`content` 单段且 `type == "excute_delete_message"` ⇒ 调用平台撤回 API，**不当作普通消息发送**。
- 撤回包与普通消息**共用同一发送队列**：与在途消息保持相对顺序，断连时同样暂存、重连后发出。

#### 边界

| 场景 | 行为 |
|---|---|
| `message_id=None` / `[]` | 静默忽略，不构包不入队 |
| HTTP `/api/send_msg`（`task_event` 非空） | 忽略并记 debug 日志（HTTP 无 adapter WS 连接，且 HTTP `_Bot` 无发送 worker，入队即泄漏，故前置拦截） |
| 旧 adapter（不认识该 type） | 收到 content 单段未知 type 的包；按其现有「未知段忽略」逻辑不发送即无副作用（adapter 责任） |
| AI 上下文（`MockBot`） | `unsend` 未被拦截，经 `__getattr__` 代理到真实 `Bot` ⇒ 真实撤回。语义正确：id 必然指向真实出站消息；且 AI 上下文 `wait_recall` 返回 `None`，常规「发了就撤」流程在 `None` 守卫处已短路 |
| 断连期间调用 | 撤回包入队暂存，重连后发出（与普通消息一致） |
| Core AI 禁言（`is_scope_banned`） | **有意不拦截**：撤回不产生新内容，禁言状态下仍允许清理已发消息 |
| 统计 / 历史 | **有意不计入** `send` 计数、不写历史记录（非消息） |

---

## 二、`on_meta` 元事件触发器

### 2.1 插件侧用法

```python
sv = SV("群管理")

@sv.on_meta("user_exit_group")
async def _(bot: Bot, ev: Event):
    uid = ev.get_meta("user_id")          # 便捷访问器，等价 ev.meta_event_data.get(...)
    gid = ev.get_meta("group_id")
    await bot.send(f"{uid} 退群了")        # 交互式发送照常可用，发往 ev 的群/私聊

# 批量订阅多个事件（传 sequence）
@sv.on_meta(("user_join_group", "user_exit_group"))
async def _(bot: Bot, ev: Event):
    ...
```

- `on_meta(event_name, block=False)`：`event_name` 不含 `meta-` 前缀，可传 `str` 或 `Tuple[str, ...]` 批量订阅。
- 触发器函数内 `ev.meta_event_type`（事件名）、`ev.meta_event_data`（dict）、`ev.get_meta(key, default=None)` 可用。
- meta 触发器**完整继承** SV/Plugins 的权限（pm）、黑白名单、area、enabled 限制——与命令路径**同口径**（见 §2.4）。

### 2.2 协议

adapter 检测到平台通知类事件（非聊天消息）时，照常构造 `MessageReceive`：

- 顶层字段照常填：`bot_id`、`bot_self_id`、`user_type`（群相关→`group`，私聊/好友相关→`direct`），**尽量填** `user_id`/`group_id`。
- `content` 放**单个** `Message(type="meta-<事件名>", data={...})`，`data` 为该事件特有字段。
- 若顶层未填 `user_id`/`group_id`，core 会从 `data` 回填（仅在顶层为空时，不覆盖显式值），但**推荐顶层也填**以保证 area / 黑白名单准确。

建议事件命名（开放，可由 adapter 扩展）：`user_join_group` / `user_exit_group` / `group_admin_change` / `group_ban` / `group_recall` / `friend_add` / `friend_request` / `group_request` / `poke` / `bot_online` / `bot_offline` 等。

### 2.3 分发路径

```
handle_event(ws, msg)
  ├─ if not IS_HANDDLE: return
  ├─ if _extract_meta_segment(msg) is not None:        ← 最优先，早于 BlackList 与一切常规处理
  │     return await handle_meta_event(ws, msg)         ← 独立路径
  └─ （以下为现有常规流程，原样不动）
```

`handle_meta_event` 内：`msg_process`（填充 meta 字段 + 回填 id）→ `get_user_pml` → 全局 BlackList → **`_sv_authorized` 鉴权级联** → 仅遍历 `sv.TL["meta"]` → `Trigger._check_meta`（事件名精确匹配）→ 按优先级 `deepcopy → Bot → TaskContext → ws.queue`（与命令路径一致，**支持 `block` 跨 SV 阻断**）。

- meta 路径**不**走文本 / AI / 历史 / 记忆 / Meme observer / `command_start` 前缀剥离 / `SameUserEventCD`。
- 双向隔离：`_check_meta` 对普通消息（`meta_event_type is None`）恒 `False`，meta 触发器不误触发普通消息；meta 走独立路径，普通触发器（用 `raw_text`）不被 meta 事件触发。

### 2.4 鉴权口径统一（重要重构）

新增 `handler._sv_authorized(sv, event, user_pm) -> bool`，复刻 Plugins/SV 级联鉴权（enabled / pm / 黑名单 / area / 白名单）。**命令路径与 meta 路径共用同一 helper**，命令循环已重构为 `if not _sv_authorized(...): continue`，消除两处鉴权随时间漂移的风险。全局 BlackList 仍由各调用方在循环外统一判断。

### 2.5 触发器类型 `"meta"`

- `Trigger`：`type` Literal 增加 `"meta"`，`check_command` 分派到 `_check_meta`（`ev.meta_event_type is not None and == keyword`）。
- `SV.on_meta` → `_on("meta", event_name, block=block, to_me=False, prefix=False)`。**`prefix=False`**：事件名不参与命令前缀展开（否则 `#user_exit_group` 永不匹配）；不声明 `to_ai` ⇒ 不注册为 AI 工具。

### 2.6 边界与兼容性

| 场景 | 行为 |
|---|---|
| 普通文本/图片消息 | 无 `meta-*` 段 ⇒ 早返回扫描后原样进常规流程，逐字节不变 |
| 旧插件（无 `on_meta`） | `TL` 无 `meta` 桶 ⇒ meta 路径静默忽略 |
| 旧 adapter（不发 meta） | 永不进 meta 路径 |
| `data` 非 dict（畸形包） | 仍记录 `meta_event_type`，`meta_event_data` 留空 dict，`ev.get_meta(k, d)` 返回 `d` 不报错 |
| HTTP 发 meta | 同样独立分发；`handle_meta_event` 返回 `None` ⇒ HTTP 响应 `status_code:-100`（meta 主要面向 WS） |
| webconsole 命令列表 / 启动 `trigger_count` | 多出 `type:"meta"` 项 / 含 meta 桶，均为良性 |

---

## 三、改动文件清单

| 文件 | 改动 |
|---|---|
| `gsuid_core/models.py` | `MessageSend.echo`；`Event.meta_event_type` / `meta_event_data` / `get_meta()` |
| `gsuid_core/bot.py` | `RECALL_WAIT_TIMEOUT` / `RECALL_DISABLE_AFTER_TIMEOUTS` 常量；`_Bot` 4 个 recall 实例字段；`resolve_recall()`；`target_send` 增 `wait_recall` 与回执登记/等待/收集；`Bot.send` / `Bot.target_send` 透传；`_Bot.unsend` / `Bot.unsend` 主动撤回（§1.7） |
| `gsuid_core/core.py` | WS 收包处 `if bot.resolve_recall(msg): continue` 拦截回执 |
| `gsuid_core/server.py` | `disconnect` 唤醒并清空 `_recall_waiters`、复位 `_recall_timeout_streak` |
| `gsuid_core/ai_core/trigger_bridge.py` | `MockBot.send` / `reply` 签名增 `wait_recall`（忽略，返回 `None`），防 `TypeError` |
| `gsuid_core/trigger.py` | `type` Literal 增 `"meta"`；`check_command` 分支；`_check_meta` |
| `gsuid_core/sv.py` | `_on` Literal 增 `"meta"`；新增 `on_meta` |
| `gsuid_core/handler.py` | `_sv_authorized` helper + 命令循环复用；`msg_process` 解析 meta 段并回填 id；`_extract_meta_segment` + `handle_meta_event` + `handle_event` 顶部拦截 |

两特性互不冲突：recall 在 **core 收包处**拦截 `recall_message_id`，meta 在 **`handle_event` 顶部**拦截 `meta-*`，作用于不同 `type`。

---

## 四、自测覆盖

已通过脚本化运行验证：

- **recall**：`resolve_recall`（命中/未命中/多段忽略）、单帧返回单元素 list、两次调用 id 互不相交、`wait_recall=False` 返回 `None`、超时降级 `[]` 且登记表清空、连续 3 次超时后 latch 且后续立即 `[]` 不等待、断连唤醒在途 future。
- **unsend**：包形状（content 单段 `excute_delete_message`、target/bot 字段、`echo:null`）、int → str 归一、列表逐包按序、群聊/私聊目标推导、显式 target 覆盖、`None`/空列表静默忽略、HTTP 守卫不入队、ws 未连接丢弃路径不抛异常。
- **meta**：`_extract_meta_segment` 普通/meta 区分、`msg_process` 填充 meta 字段、id 回填（含 `str()` 归一）、畸形 `data` 容错、`_check_meta` 双向隔离、`handle_meta_event` 基础触发 + disabled/pm/area/黑名单/无 meta 桶各拒绝路径 + 入队 coro 的 `ev` 接线正确。
- 全部改动文件 `py_compile` 与 `ruff check` 通过。

---

## 五、有意未实现的开放项（与各自设计 §待办 一致）

以下条目在设计文档中被标注为待办/开放项。基于「无引入可能的 bug、无冗余代码」原则，**核心机制已完整落地**，下列项**有意推迟**，理由如下：

| 开放项 | 处置 | 理由 |
|---|---|---|
| `bot.recall(message_ids)` 高层撤回 API | ~~未实现~~ → **已实现**（2026-06-11，方法名 `unsend`，见 §1.7） | 下行协议已由需求方确定（`content` 单段 `Message(type="excute_delete_message", data="<id>")`），按此落地。 |
| 严格「帧↔id 位置对齐」模式（保留 `None`、返回 `List[Optional[str]]`） | **未实现** | 与设计 §1「已与需求方确认」的硬约束冲突（元素恒为 `str`、绝不解包、过滤缺失）。保留已确认的默认「过滤」语义；如确需对齐模式，应作为显式开关单独评审，避免放宽返回类型影响所有现有调用方。 |
| meta 事件独立统计维度 | **未实现** | 需改 `PlatformVal` TypedDict + `CoreDataSummary` 数据库模型 + 迁移，属 schema 级改动，风险与本次「不破坏现有数据」目标不匹配。设计 §4.6 的 `handle_meta_event` 本就不计数。建议后续作为独立的统计特性单独评审落地。 |
| `meta_events.py` 固化事件名常量 | **未实现** | 设计文档注明「后续再处理」。当前事件名为开放字符串约定，adapter 可自由扩展。 |

已纳入本次实现的开放项：**sequence 批量订阅**（`on_meta` 接受 `Tuple`）、**`ev.get_meta` 便捷访问器**、**命令循环重构为 `_sv_authorized`**（两路鉴权口径统一）。

---

## 六、审查意见（2026-06-11）

对本次全部改动（8 个文件）做了逐行复审，对照 `docs/LLM.md` 红线、E501（line-length=120）与本文档声明的契约。**结论：核心机制实现正确，发现并已修复 3 类问题，无遗留阻塞项。**

### 6.1 已修复

| 问题 | 等级 | 修复 |
|---|---|---|
| `resolve_recall` 将回执 `id` 原样 `set_result`，OneBot 等平台 `message_id` 为 int，返回列表会混入非 `str` 元素，违反 §1.1「元素类型恒为 `str`」契约 | **bug** | `id` 与 `echo` 在 core 侧统一 `str()` 归一（`bot.py`），§1.2 已同步补充协议说明 |
| `bot.py` 模块常量 `RECALL_WAIT_TIMEOUT` 上方注释为 3 行 | 风格 | 压缩为 2 行，信息不变 |
| `resolve_recall` / `msg_process` meta 回填使用 `dict.get` 兜底（LLM.md §1.4）；`handle_meta_event` 缺返回值注解（§2.1）；`_check_meta` 中 `is not None` 与 `==` 判断冗余 | 风格 | 改为 `isinstance` + `in` 成员判断后直接下标访问；补 `-> None`；删冗余判断并压缩注释为 1 行 |

### 6.2 复审确认无问题的要点

- **竞态与泄漏**：future 发帧前登记；`try/finally` 兜底清空登记项；超时后 `cancel` 的 future 不参与收集；迟到/重复回执被拦截不入消息管道；断连唤醒 + 清表 + streak 复位齐全。
- **零开销兼容**：`wait_recall` 缺省 `False` 时不登记 future、`MessageSend.echo=None`，发送路径行为与改动前一致；`target_send` 新参数追加在末尾，全仓现有调用方（均 ≤6 个位置参数）不受影响。
- **latch 正确性**：仅在 `_supports_recall is None` 时累计零回执 streak；断连导致的 `set_result(None)` 不会误判为「支持」；`_echo_seq` 连接实例内单调递增，重连复用实例也不会撞 key。
- **鉴权一致性**：`_sv_authorized` 与原命令循环逐条件比对一致（含 `_plugins_area == "ALL"` 参与 SV 级 area 判断这一原有行为）；meta 路径黑名单、`get_user_pml` 时序（先回填 id 再算 pm）正确。
- **双向隔离**：`_check_meta` 对普通消息恒 `False`；meta 走独立路径不会触发文本/AI/历史/记忆管道；`Event` 新字段位于末尾，`msg_process` 的位置参数构造不受影响。

### 6.3 保留的写法（有意不改）

- `getattr(coro, "__qualname__", str(coro))` 与 `check_command` 外层 `try/except Exception`：与既有命令路径完全同款，属插件故障隔离而非类型兜底；统一修改应另起重构，不在本次范围。
- `Event.get_meta(key, default)`：meta `data` 为开放 schema 的协议字典，`get` 语义即设计本身（§2.1 已声明）。
- MockBot `wait_recall` 恒返回 `None`（而非 `[]`）：已与 §1.6 表格声明一致，AI 上下文无真实出站消息，语义正确。

### 6.4 已知边界（非本次引入，记录备查）

- HTTP `_Bot("HTTP")` 的 `queue` 无 `_process` 消费者，HTTP 下发 meta 事件会入队但不执行（HTTP 命令路径现状相同）；meta 本就面向 WS（§2.6），如需 HTTP 支持应单独立项。
- adapter 若违反「meta 段单独成包」契约、把 `meta-*` 段与文本混发，整包会被劫持进 meta 路径而跳过文本触发器——按 §2.2 契约属 adapter 责任。

### 6.5 验证

修复后 8 个改动文件 `ruff check`（E/F/I/W，含 E501@120）与 `py_compile` 全部通过；`resolve_recall` 冒烟脚本覆盖 int/str `id` 归一、int `echo` 命中、缺 `id`、echo 未命中、非 dict `data`、普通消息不拦截、登记表清空，全部断言通过。

---

## 七、`unsend` 增补 与 第二轮复审（2026-06-11）

新增 `bot.unsend`（§1.7）后对全部未提交改动做了第二轮逐行复审。**结论：未发现新 bug；§六的修复均仍然成立；以下为本轮确认/补录的边界。**

### 7.1 `unsend` 实现要点复核

- `_do_send` 闭包以默认参数绑定 `body`，不捕获循环变量；执行时动态读 `self.bot`，重连后自动用新 ws——与 `target_send` 同款模式。
- HTTP 守卫（`ev.task_event is not None`）**必须前置**：HTTP `_Bot("HTTP")` 从不启动发送 worker，入队的协程永远无人消费，等于内存泄漏；守卫直接 return 规避。
- `Bot.unsend` 的 `bot_id` / `bot_self_id` 取值（`ev.real_bot_id` / `self.bot_self_id`）与 `Bot.send` 完全一致；私聊 `target_id = user_id`、群聊 `= group_id` 的推导也与 `Bot.send` 同口径。
- 显式 `target_type` 覆盖时 `target_id` 原样使用（不回退 ev 字段，避免「显式类型 + 隐式错误会话 id」的组合）；契约要求两者成对传入。

### 7.2 本轮补录的已知边界（均非 bug，记录备查）

- **断连与 latch 的微小窗口**：断连把在途 future `set_result(None)`，该次 `wait_recall` 调用收集到空 ids，仍会使 `_recall_timeout_streak` +1（disconnect 复位为 0 在前、调用方 +1 在后，最终为 1）。理论上「该连接从未成功回执过（`_supports_recall is None`）+ 连续 3 次调用均恰跨断连」才会误 latch 为不支持；后果仅是该连接实例不再等待回执（功能性降级、`[]` 返回，不影响消息发送），>5 分钟重连重建实例后自愈。概率与代价均可接受，不做额外处理。
- **meta 事件名与触发器类型名碰撞**（既有代码行为）：`SV._on` 注册时以 `if _k not in self.TL` 对外层「类型桶」dict 做了排重，若事件名恰好等于九种触发器类型名之一（`prefix`/`suffix`/`keyword`/`fullmatch`/`command`/`file`/`regex`/`message`/`meta`）且该类型桶已存在，该触发器会**静默不注册**。此为 `_on` 既有行为（对 `on_command("command")` 等同样成立），非本次引入；meta 事件命名请避开这九个词。
- **`/api/chat_with_history`**：函数体首行 `return None`（已停用），其 `_Bot("HTTP")` 路径不构成 `unsend` / recall 的实际边角。
- **HTTP 上行不在 recall 契约内**：`resolve_recall` 拦截只挂在 WS 收包处（`core.py`）；若有人通过 HTTP `/api/send_msg` 上行伪造 `recall_message_id` 包，会走常规消息管道（无触发器命中则无副作用）。回执协议本就仅约定 WS 上行（§1.2）。

### 7.3 验证

- 8 个改动文件 `ruff check` 与 `py_compile` 通过（`core.py:34` 的 SyntaxWarning 为既有 `noqa: W605` 文档字符串，与本次无关）。
- `unsend` 冒烟脚本 8 项断言全部通过（覆盖项见 §四）。
