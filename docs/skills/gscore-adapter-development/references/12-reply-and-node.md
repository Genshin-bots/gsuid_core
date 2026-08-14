# 十二、新版本变更：引用拆分与合并转发上报

> 适用读者：**已经上线的适配器作者**。core `94d2820`（「增加消息协议部分内容解析」）起，
> `msg_process()` 会把 `reply` / `reply_id` / `node` 提升到 `Event`，引用语义发生**不兼容变化**。
> 字段格式仍以 [§4.4](./04-report-message.md) / [§5.3](./05-send-message.md) / [§5.4](./05-send-message.md)
> 为准；本章只讲**旧适配器怎么改、为什么改、对照参考实现该抄哪几段**。
>
> 参考实现：`astrbot_plugin_gscore_adapter`（v0.5.5+）的 `main.py`（上报展开）与
> `send_utils.py`（下发落地）。插件侧读到的字段见插件 SKILL
> [`03-messaging.md`](../../gscore-plugin-development/references/03-messaging.md)。

---

## 12.1 这次改了什么

core 侧（`gsuid_core/handler.py` 的 `msg_process()` + `gsuid_core/models.py`）：

| 变化 | 说明 |
|------|------|
| `Event` 新增 `reply_id` | 被引用消息的**平台 id** |
| `Event` 新增 `node` | 当前消息或被引用消息里的合并转发，已展平为 `List[Message]` |
| `reply` 语义改成正文 | `event.reply` 现在是引用**纯文本**，不再是消息 id |
| `normalize_node_items()` | 若仍收到嵌套 `type=node`，core 会展平；超深度只留标记 |
| 引用 + node 同时存在 | core 保证 `event.reply` 以 `[合并转发]` 开头 |

适配器必须配合改的三件事：

1. **上报引用**：`reply` 填文字，`reply_id` 才填 id（旧写法把 id 塞进 `reply`，插件会把数字串当正文用）。
2. **上报合并转发**：用户**发送**或**引用**转发卡片时，都要带一段 `node`；不要把节点正文拼进顶层 `text`。
3. **下发引用**：同时认 `reply` 和 `reply_id`，**两者都按 msg_id** 落地成平台引用段（和上报语义相反）。

> 下发方向的 `node` 本来就能发，这次**没有改协议**。新要求是上报也要能把合并转发送进 core，
> 插件 / AI 才能读 `ev.node`、在引用场景里看到转发摘要。

---

## 12.2 对照表（旧 → 新）

### 上报（平台 → core）

| 场景 | 旧写法（不要再用） | 新写法 |
|------|-------------------|--------|
| 用户引用一条普通文字/图 | `Message("reply", msg_id)` | `Message("reply_id", msg_id)` + `Message("reply", 引用正文)`；引用图另报 `image` |
| 用户直接发合并转发 | 丢弃，或把节点文字拼进 `text` | `Message("node", List[Message])`，**不要**拼进顶层 `text` |
| 用户引用一张转发卡片 | 只报 `reply=id` | `reply_id` + `reply` 以 `[合并转发]` 开头 + `node` + 内部图片作 `image` |

### 下发（core → 平台）

| 段 | 旧适配器 | 新适配器 |
|----|----------|----------|
| `reply` | 按 **msg_id** 做成平台引用 | **仍然**按 msg_id（core 历史包只发这个） |
| `reply_id` | 没有 | **也**按 msg_id 做成平台引用 |
| `node` | QQ 原生转发 / 其它平台逐条发 | 不变 |

> **最容易写反的一点**：上报的 `reply` = 文字，下发的 `reply` = id。不要把上行刚解析出来的
> 引用正文再当成引用 id 发出去。

---

## 12.3 上报：`reply` = 文字，`reply_id` = id

插件读的是：

- `ev.reply`：被引用消息的纯文本（AI 上下文、"用户引用了什么"）。引用转发时以 `[合并转发]` 开头。
- `ev.reply_id`：被引用消息的平台 id（后续再引用、撤回、表情回应）。

### 推荐改法

```python
# 旧
if ev.reply:
    message.append(Message("reply", str(ev.reply.message_id)))

# 新：成对上报
if ev.reply:
    message.append(Message("reply_id", str(ev.reply.message_id)))
    message.append(Message("reply", extract_plain_text(ev.reply)))
    for seg in quoted_segments(ev.reply):
        if is_image(seg):
            message.append(Message("image", image_url_or_b64(seg)))
```

引用正文优先取平台已经解析好的纯文本；没有再用消息链里的 text 段拼：

```python
def extract_plain_text(quoted) -> str:
    if getattr(quoted, "message_str", None):
        return str(quoted.message_str)
    if getattr(quoted, "text", None):
        return str(quoted.text)
    parts = []
    for item in quoted_chain(quoted):
        if is_text(item):
            parts.append(item.text)
    return "".join(parts)
```

### 硬性约定

- **不要**再把 msg_id 填进 `reply`。旧 core 会把整段写进 `event.reply`，新 core 同样会——
  结果是插件拿到一串数字当"引用正文"。
- 引用图**始终**作为顶层 `image` 段上报（url 或 `base64://`），不要再加 `is_reply_img` 开关。
- 引用消息里经常夹着表情 / JSON 卡片 / 平台专有段：这些 core 不消费，**静默跳过**，
  不要因此丢掉当前消息里的命令文本。
- 平台给不出被引正文时，`reply` 可以是空串，但 **`reply_id` 仍要报**（有 id 总比没有强）。

---

## 12.4 上报：`node`（发送 + 引用查看）

`node.data` 与下发同形：扁平 `List[Message]`（JSON 里是 `[{"type":"...","data":"..."}, ...]`）。
进 `event.node`。**不要**把节点正文拼进顶层 `text`，否则会污染命令匹配。

两种入口都要覆盖：

| 入口 | 平台常见形态 | 适配器要做的 |
|------|-------------|--------------|
| 用户**直接发送**转发卡片 | OneBot `forward` / 已展开的 `node`/`nodes` | 展开后报一段 `Message("node", items)` |
| 用户**引用**一张转发卡片 | 引用链里带 `forward` / `node` | 同上，并让 `reply` 以 `[合并转发]` 开头 |

### 12.4.1 展开规则（建议直接抄）

常量与 core 对齐：

```python
NODE_MARK = "[合并转发]"
NODE_MAX_DEPTH = 3          # 与 gsuid_core.models.NODE_MAX_DEPTH 一致
```

1. 平台只给 forward id 时，先调 `get_forward_msg` / 等价 API 再转成 `List[Message]`。
2. 每条转发节点：有昵称先插 `text="{nickname}:"`，再跟该节点的 text / image / at / file。
3. **内层还可以再套合并转发**：按深度上限递归展开，全部扁平进**同一段** `node`；
   每进入一层先插一条 `text=NODE_MARK`。
4. 用 `seen: set[str]` 记已经拉过的 forward id，打破环。超深度、环、API 失败：
   **只留标记，不再请求**。
5. 展开失败也至少报 `Message("node", [Message("text", NODE_MARK)])`，不要整段丢弃。
6. 适配器侧先展平更好；即便你仍嵌套 `type=node`，core 的 `normalize_node_items` 会再展一次。

```python
async def fetch_forward_items(bot, forward_id: str, depth=0, seen=None) -> list[Message]:
    seen = seen if seen is not None else set()
    if not forward_id or forward_id in seen or depth >= NODE_MAX_DEPTH:
        return [Message("text", NODE_MARK)]
    seen.add(forward_id)
    try:
        raw = await bot.call_api("get_forward_msg", id=forward_id)
    except Exception:
        return [Message("text", NODE_MARK)]
    return await parse_forward_payload(raw, depth, seen)  # 见下
```

解析 OneBot `get_forward_msg` 返回值时，兼容两种外壳（`{"messages": [...]}` 或直接 list），
节点本身也可能再包一层 `{"type":"node","data":{...}}`：

```python
async def parse_forward_payload(raw, depth, seen) -> list[Message]:
    messages = raw["messages"] if isinstance(raw, dict) and "messages" in raw else raw
    if not isinstance(messages, list):
        return [Message("text", NODE_MARK)]
    items: list[Message] = []
    for entry in messages:
        payload = entry["data"] if entry.get("type") == "node" and isinstance(entry.get("data"), dict) else entry
        nickname = (
            (payload.get("sender") or {}).get("nickname")
            or payload.get("name")
            or ""
        )
        if nickname:
            items.append(Message("text", f"{nickname}:"))
        content = payload.get("content") or payload.get("message")
        if isinstance(content, str) and content:
            items.append(Message("text", content))
        elif isinstance(content, list):
            items.extend(await ob_dict_segs_to_gs(content, depth, seen))
    return items or [Message("text", NODE_MARK)]
```

内层段转换时，再遇到 `forward` / `forward_msg` 就插标记并递归，**不要**再包一层 `type=node`：

```python
async def ob_dict_segs_to_gs(segs, depth, seen) -> list[Message]:
    items = []
    for seg in segs:
        typ, data = seg.get("type") or "", seg.get("data") or {}
        if typ == "text":
            items.append(Message("text", str(data.get("text", ""))))
        elif typ == "image":
            url = data.get("url") or data.get("file") or ""
            if url:
                items.append(Message("image", str(url)))
        elif typ == "at":
            items.append(Message("at", str(data.get("qq", ""))))
        elif typ in {"forward", "forward_msg"}:
            items.append(Message("text", NODE_MARK))
            fid = str(data.get("id") or data.get("message_id") or "")
            items.extend(await fetch_forward_items(bot, fid, depth + 1, seen))
    return items
```

平台已经把转发展成 `Node` / `Nodes` 对象时（AstrBot 的 `Node`/`Nodes`），同样按深度扁平，
不要原样嵌套上报。

### 12.4.2 引用转发：`reply` 摘要 + `node` 正文

引用链里发现 `forward` / `node` 时，**除了**报 `node`，还要改 `reply` 文本：

```python
reply_id = str(quoted.id)
reply_text = extract_plain_text(quoted)
node_items = await flatten_quoted_forwards(quoted)   # 已是 List[Message]
if node_items:
    preview = format_node_preview(node_items)        # 首行 [合并转发]，随后文本/[图片]/…
    if not reply_text or NODE_MARK not in reply_text:
        reply_text = preview if not reply_text else f"{NODE_MARK}\n{reply_text}"

content.append(Message("reply_id", reply_id))
content.append(Message("reply", reply_text))
content.append(Message("node", node_items))
# 引用链里的图片仍作为顶层 image 上报（插件 ev.image_list 能看到）
```

摘要函数与 core `format_node_preview` 同形即可（适配器侧先写好，core 还会再兜一次）：

```python
def format_node_preview(items: list[Message]) -> str:
    lines = [NODE_MARK]
    for item in items:
        if item.type == "text" and item.data:
            text = str(item.data).strip()
            if text:
                lines.append(text)
        elif item.type == "image":
            lines.append("[图片]")
        elif item.type == "record":
            lines.append("[语音]")
        elif item.type == "video":
            lines.append("[视频]")
        elif item.type == "file":
            lines.append("[文件]")
    return "\n".join(lines)
```

core 兜底（适配器漏写标记时仍能用）：

```python
# handler.msg_process 末尾
if event.node is not None and (event.reply is not None or event.reply_id):
    preview = format_node_preview(event.node)
    if not event.reply:
        event.reply = preview
    elif NODE_MARK not in str(event.reply):
        event.reply = f"{NODE_MARK}\n{event.reply}"
```

> 只报 `node`、不报 `reply`/`reply_id`：这是「用户直接发了转发」，`event.node` 有值、
> `event.reply` 仍是 `None`。引用查看必须同时带 `reply_id`（以及尽量带 `reply`）。

### 12.4.3 段顺序：当前消息在前，引用 / node 在后

把当前气泡的 text / at / image / **发送用** node 放前面，把 `reply_id` / `reply` / 引用图 /
**引用用** node 附在末尾。命令匹配先看到用户正在说的话，而不是被引正文。

```python
current_message: list[Message] = []
quoted_context: list[Message] = []

for seg in event_segments:
    if is_quote(seg):
        quoted_context.extend(build_quote_segments(seg))   # reply_id + reply + image + node
    else:
        current_message.extend(build_current_segments(seg))  # text / at / image / 发送用 node

return current_message + quoted_context
```

AstrBot 参考实现就是这个顺序。

---

## 12.5 下发：`reply` / `reply_id` 都按 msg_id；`node` 照旧

```python
# 下发抽取 / 落地
elif _c.type in {"reply", "reply_id"}:
    # data 是平台 msg_id，不是引用正文
    message.append(platform_reply_segment(id=str(_c.data)))
```

- core **历史包**只发 `reply`（值仍是 msg_id）。新适配器必须继续认它，否则旧插件的
  「回复这条消息」会静默丢引用。
- 新 core / 新插件可能改发 `reply_id`。两个 type **同一套落地**，不要拆成两个分支。
- 不要把上行 `reply` 里的引用正文再当成引用 id 发出去。

`node` 下发没有新协议：QQ / OneBot 走原生 `send_group_forward_msg`，其它平台遍历逐条发。
详见 [§5.4](./05-send-message.md)。一帧被展开成多条时，`echo` 回执的 `id` 用 `List[str]`，
见 [§11.2](./11-meta-and-control.md)。

---

## 12.6 core 会帮你兜什么、不会帮你兜什么

| 事项 | core 会 | 适配器必须自己做 |
|------|---------|------------------|
| 嵌套 `type=node` | `normalize_node_items` 展平，超深度留 `[合并转发]` | 按深度 / 环自己展开，少打 API |
| 引用 + node 但 `reply` 没标记 | 自动给 `event.reply` 补上 `[合并转发]` | 最好自己写好摘要，预览更完整 |
| 把旧式 `reply=msg_id` 报上来 | 原样写入 `event.reply` | **不会**猜这是 id。插件会把数字串当正文 |
| 只报 `reply` 不报 `reply_id` | `event.reply_id` 为空 | 需要 id 的插件（再引用 / 撤回）失效 |
| 平台只给 forward id | 什么都不做 | 适配器侧 `get_forward_msg` |
| 把转发正文拼进顶层 `text` | 当命令文本解析 | **命令被污染**，必须自己避免 |

旧适配器不改也能连上（msgspec 忽略未知字段、未识别的段仍进 `event.content`），
但引用场景对插件 / AI **是错的**：`ev.reply` 变成 id，`ev.reply_id` / `ev.node` 永远是空。

---

## 12.7 推荐修改落点（按文件）

对照 `astrbot_plugin_gscore_adapter` 的拆分，旧适配器通常改两处即可。

### 上报入口（`__init__.py` / `main.py` / `convert_message`）

- [ ] 引用：拆成 `reply_id` + `reply(正文)`；引用图转 `image`。
- [ ] 当前消息遇到 `forward` / `node`：展开后追加 `Message("node", items)`。
- [ ] 引用链遇到 `forward` / `node`：展开进 `node`，并让 `reply` 以 `[合并转发]` 开头。
- [ ] 深度上限 3、`seen` 防环、失败也报标记。
- [ ] 当前消息段在前，引用 / node 上下文在后。
- [ ] 引用里的未知段静默跳过，不要 `warning` 到让人以为整条消息失败。

### 下发入口（`client.py` / `send_utils.py` / `*_send`）

- [ ] `type in {"reply", "reply_id"}` 都按 **msg_id** 做成平台引用段。
- [ ] `node`：有原生合并转发就发卡片，否则遍历逐条；`echo` 多气泡回 `List[str]`。

### 不必改

- `Message` / `MessageReceive` / `MessageSend` 的 Struct 定义（`node` / `reply` / `reply_id`
  都只是 `content` 里的 `Message`，不用加字段）。
- 连接、鉴权、心跳、`log_`、元事件、撤回 / 禁言控制包。

---

## 12.8 最小可粘贴骨架（OneBot 风格）

把这段嵌进现有 `convert_message`，再按平台 API 换掉 `get_forward_msg` 即可。

```python
NODE_MARK = "[合并转发]"
NODE_MAX_DEPTH = 3

async def append_quote_and_nodes(bot, ev, current: list[Message]) -> list[Message]:
    quoted: list[Message] = []
    if ev.reply:
        quoted.append(Message("reply_id", str(ev.reply.message_id)))
        reply_text = ev.reply.message.extract_plain_text()
        node_items: list[Message] = []
        for seg in ev.reply.message:
            if seg.type == "image" and seg.data:
                quoted.append(Message("image", seg.data.get("url") or ""))
            elif seg.type == "forward":
                fid = str((seg.data or {}).get("id") or "")
                node_items = await fetch_forward_items(bot, fid)
        if node_items:
            preview = format_node_preview(node_items)
            if not reply_text or NODE_MARK not in reply_text:
                reply_text = preview if not reply_text else f"{NODE_MARK}\n{reply_text}"
            quoted.append(Message("reply", reply_text))
            quoted.append(Message("node", node_items))
        else:
            quoted.append(Message("reply", reply_text))

    for seg in ev.original_message:
        if seg.type == "forward":
            fid = str((seg.data or {}).get("id") or "")
            current.append(Message("node", await fetch_forward_items(bot, fid)))
        # text / at / image / record / file … 保持原逻辑
    return current + quoted
```

完整上报 / 下发对照见 [§9.2](./09-full-adapter-example.md)。

---

## 12.9 自查清单

- [ ] 上报引用：`reply` = 正文，`reply_id` = 平台 id，**不再**把 id 塞进 `reply`
- [ ] 引用图始终作为 `image` 上报，无 `is_reply_img` 开关
- [ ] 用户发送合并转发时报 `node`，节点正文**不**进顶层 `text`
- [ ] 用户引用合并转发时：`reply_id` + `reply` 以 `[合并转发]` 开头 + `node`
- [ ] 展开：深度 ≤ 3、`seen` 防环、失败也报 `[合并转发]` 标记
- [ ] 内层转发扁平进同一段 `node`，上报不再嵌套 `type=node`（core 会再兜一次）
- [ ] 段顺序：当前消息在前，引用 / node 在后
- [ ] 下发：`reply` 与 `reply_id` 都按 msg_id 做成平台引用
- [ ] 下发 `node`：有原生转发走原生，否则逐条发；多气泡回执用 `List[str]`
- [ ] 用一条「引用文字」、一条「直接发转发」、一条「引用转发卡片」各打一遍，
      分别确认 `ev.reply` / `ev.reply_id` / `ev.node` 在插件日志里值正确
