# 十八、触发器 → AI 工具改造指南（to_ai 批量改造）

> 本章内容整合自 `gscore-to-ai-trigger-migration` SKILL。为保证单一入口，**关联引用全部改为本文件夹内路径**。
> 阅读本节前请先掌握 [十、to_ai 与 ai_return](./references/10-ai-to-ai-and-ai-return.md) 与 [十四、能力代理画像](./references/14-ai-capability-profile.md) 的基础概念。

## 18.1 背景：你要做的事

将现有插件的 `@sv.on_xxx(...)` 装饰器改造为支持 AI Tool Call 调用。改造后，插件的每个触发器命令：
- **用户直接发命令**：行为完全不变，走原有逻辑
- **AI 调用**：AI 按照 `to_ai` docstring 构建合适的 `text` 参数，触发器在 AI 上下文中执行，`ai_return()` 收集的文本内容返回给 AI 决策

改造至少涉及**两个独立层**，必须都做：
1. **触发器层**：在 `@sv.on_xxx(...)` 加 `to_ai="..."` 参数
2. **数据/渲染层**：在生成图片/结果前，调用 `ai_return()` 将结构化文本数据注入给 AI

如果插件提供的是金融、天气、游戏数据、运维、绘图等**专业业务能力**，还需要做第三层：
3. **能力代理画像层**：在插件启动入口注册 `CapabilityAgentProfile`，让 Kanban 多步任务和 `create_subagent(agent_profile="...")` 能把复杂任务派给插件自己的无人格专职代理。

---

## 18.2 改造前必须理解的核心机制

### 18.2.1 `to_ai` 参数

```python
# 改造前
@sv.on_command("个股")
async def send_stock_img(bot: Bot, ev: Event):
    ...

# 改造后
@sv.on_command(
    "个股",
    to_ai="""查询股票/ETF的K线图或分时图

    当用户询问某只股票走势、分时图、K线图时调用。

    Args:
        text: 查询内容，格式为 "[周期前缀] 股票名称或代码"
              - 无前缀：默认分时图，例如 "证券ETF"
              - "日k"/"周k"/"月k": K线图，例如 "日k 证券ETF"
              - 多个标的以空格分隔，例如 "证券ETF 白酒ETF"
    """,
)
async def send_stock_img(bot: Bot, ev: Event):
    ...
```

**`to_ai` 的本质**：这段字符串就是 AI 看到的工具 docstring。AI 依据它判断"什么时候调这个工具"以及"text 参数应该填什么"。

### 18.2.2 `ai_return(text)` 的作用与使用时机

```python
from gsuid_core.ai_core.trigger_bridge import ai_return
```

- **AI 调用时**：调用 `ai_return("某些文字")` 会将文字收集起来，作为工具的返回值传回给 AI
- **用户直接触发时**：`ai_return()` 什么都不做，完全透明，不影响原有逻辑

> **⚠️ 关键警告：AI 不会自动写 `ai_return`！**
>
> 实践中发现，AI（包括高级模型）在改造触发器时，**不会自动**在多层函数调用中添加 `ai_return()` 调用。AI 倾向于只在触发器函数本身做简单处理，而不会深入分析调用链去找到正确的数据注入点。
>
> **你必须手动完成以下工作**：
> 1. 逐层追踪触发器的完整调用链
> 2. 找到"数据已获取、图片未生成"的那个精确位置
> 3. 分析该位置的数据结构，提取关键字段
> 4. 编写 `_ai_return_xxx()` 辅助函数并注入
>
> **不要期望 AI 能自动完成这些分析，必须由你显式地在 SKILL 指令中引导 AI 做到。**

**`ai_return()` 的正确使用场景**：

| 场景 | 是否需要 `ai_return()` | 说明 |
|------|----------------------|------|
| `bot.send(str)` 纯文字 | ❌ 不需要 | MockBot 自动拦截，文字会被收集返回给 AI |
| `bot.send(bytes)` 图片字节 | ⚠️ 视情况 | 图片通过 `RM.register()` 注册，返回资源 ID。需要文字摘要时用 `ai_return()` |
| `bot.send(MessageSegment.image(...))` | ⚠️ 视情况 | 同上，`MessageSegment.image()` 返回 `Message(type="image")`，通过 `RM.register()` 注册 |
| `bot.send("base64://...")` 图片字符串 | ⚠️ 视情况 | 以 `base64://`、`http://`、`https://` 开头的字符串被识别为图片，通过 `RM.register()` 注册 |
| `bot.send([text_msg, image_msg])` 混合列表 | ⚠️ 注意 | 列表中**只要包含图片段**，整个列表被归为图片，文字部分**不会**返回给 AI |
| `return "string"` 不经过 bot.send | ✅ 需要 | 必须用 `ai_return()` 将结果注入给 AI |

**核心原则**：`ai_return()` 应该在**数据已经拿到、图片还没生成时**调用，传递的是结构化的文本数据摘要，让 AI 能够"读懂"这次查询的结果，从而决定如何向用户描述。

**典型模式**：
```python
# 模式1：纯文字返回 → 不需要 ai_return
await bot.send("绑定成功！UID: 123456")  # MockBot 自动拦截

# 模式2：返回图片，需要文字摘要 → 用 ai_return
ai_return("【证券ETF 分时行情】最新价: 1.234 涨跌幅: +2.5%")  # 给 AI 文字摘要
await bot.send(image_bytes)  # 图片通过 RM.register() 注册，返回资源 ID，AI 决定是否发送

# 模式3：直接 return → 需要 ai_return
ai_return("查询结果：今日运势 85 分")
return "运势结果已生成"
```

### 18.2.3 `MockBot` 拦截机制（自动处理，开发者无需干预）

当 AI 调用触发器时：
- `bot` 对象被自动替换为 `MockBot`
- `bot.send(bytes)` → 图片字节数据，通过 `RM.register()` 注册，返回资源 ID（如 `img_a1b2c3d4`）
- `bot.send(MessageSegment.image(...))` → 返回的 `Message(type="image")` 被检测为图片，通过 `RM.register()` 注册
- `bot.send("base64://...")` / `bot.send("https://...")` → 以这些前缀开头的字符串被识别为图片，通过 `RM.register()` 注册
- `bot.send(str)` 纯文字 → 文字被收集，作为工具返回值传回给 AI
- `bot.send([text_msg, image_msg])` 混合列表 → **只要包含图片段，整个列表归为图片**，文字部分不返回给 AI
- AI 收到工具返回值（含资源 ID）后，决定是否调用 `send_message_by_ai(image_id=...)` 发出图片
- 资源 ID 在 RM 中持久存储，AI 可在后续轮次中再次发送
- 用户直接触发时，`bot` 是真实 `Bot`，`bot.send` 立即发送，行为不变

**图片发送的常见形式**（开发者需要了解）：

| 传入类型 | 示例 | MockBot 处理 |
|---------|------|-------------|
| `bytes` | `await bot.send(image_bytes)` | ✅ `RM.register()` 注册，返回资源 ID |
| `MessageSegment.image()` | `await bot.send(MessageSegment.image(img))` | ✅ 返回 `Message(type="image")`，`RM.register()` 注册 |
| `Image.Image` 对象 | 通常先 `convert_img()` 转为 bytes 再 send | ✅ 转换后为 bytes，`RM.register()` 注册 |
| `base64://` 字符串 | `await bot.send("base64://iVBOR...")` | ✅ 检测为图片字符串，`RM.register()` 注册 |
| `http(s)://` URL | `await bot.send("https://example.com/img.png")` | ✅ 检测为图片 URL，`RM.register()` 注册 |
| 纯文字字符串 | `await bot.send("查询成功")` | ✅ 检测为文字，返回给 AI |
| `[text, image]` 混合列表 | `await bot.send([MessageSegment.text("结果"), MessageSegment.image(img)])` | ⚠️ 整体归为图片，`RM.register()` 注册，文字丢失 |

**⚠️ 混合列表的注意事项**：当 `bot.send()` 传入的列表中同时包含文字和图片 Message 时，MockBot 会将整个列表归类为图片。这意味着列表中的文字部分**不会**被返回给 AI。如果需要让 AI 同时获得文字信息，应在 `bot.send()` 之前单独调用 `ai_return()` 注入文字摘要。

---

## 18.3 改造流程

### Step 0：批量定位与前期准备

对于有大量触发器的项目（如 50+ 个触发器），建议采用批量处理策略：

**0.1 批量定位所有触发器**

```bash
# 使用 search_files 批量找出所有触发器装饰器
# 搜索模式：@sv.on_xxx 或 @sv_xxx.on_xxx
```

用 `search_files` 工具搜索 `\.on_(command|prefix|suffix|keyword|fullmatch|regex|file|message)\(` 模式，一次性获取所有触发器的位置和上下文。

**0.2 按模块分批处理**

将触发器按文件/模块分组，逐模块处理而非逐个触发器处理。每个模块的改造步骤：
1. 识别该模块所有触发器
2. 检查是否有重复的手动 AI 工具（见 Step 0.3）
3. 确认命令前缀格式（见 Step 0.4）
4. 批量添加 `to_ai` 参数
5. 批量注入 `ai_return()`

**0.3 检查已有的手动 AI 工具**

在改造前，**必须检查**目标插件中是否存在已手动注册的 `@ai_tools` 函数。这些手动工具与 `to_ai` 触发器功能可能重复，添加 `to_ai` 后会导致 AI 看到两个功能相同的工具。

检查方法：
```bash
# 搜索插件目录中是否有 @ai_tools 装饰器
# 搜索模式：@ai_tools
```

如果发现手动 AI 工具与触发器功能重复：
- **移除**手动 `@ai_tools` 函数（或注释掉）
- 保留 `to_ai` 触发器版本（因为它同时支持用户直接调用和 AI 调用）
- 如果手动工具提供了触发器不具备的额外功能，则保留两者但确保描述不重复

**0.4 判断是否需要注册 Capability Agent 画像**

在给触发器加 `to_ai` 之前，先判断插件是否只是"给主 Agent 多几个可直接调用的命令"，
还是提供了一个可独立承担多步任务的业务能力域。

需要注册画像的典型情况：
- 插件有一组同领域工具，需要组合调用才能完成任务，例如股票：行情、估值、自选、资金云图、VIX。
- 用户可能提出"每天复盘""帮我分析并生成报告""先查数据再给结论"这类多步任务。
- 专业域不能只靠通用 `research_agent` + `web_search` 兜底，否则会产生不可靠结论。
- 希望 Kanban 子任务里显式出现 `agent_profile="finance_agent"` / `weather_agent` / `game_data_agent`。

不需要注册画像的情况：
- 插件只有一两个简单查询命令，主 Agent 直接调用 `to_ai` 工具即可。
- 只是娱乐、随机图、纯文本小功能，不需要多步计划和专业工具组合。
- 功能只能由用户主动触发，不适合让 AI 自主委派。

> 结论：专业业务插件应同时提供 `to_ai` 工具 + Capability Agent 画像；`to_ai` 负责把
> 原有触发器暴露成工具，画像负责告诉 Kanban/子代理"什么时候、用哪些工具、按什么专业流程执行"。

**0.5 确认命令前缀格式**

不同插件使用不同的命令前缀。在撰写 `to_ai` 描述前，**必须确认**插件的实际前缀配置：

```python
from gsuid_core.sv import get_plugin_prefixs, get_plugin_prefix, get_plugin_available_prefix

# 获取插件的所有前缀
prefixes = get_plugin_prefixs("插件名")  # 例如 ["gs", ""]

# 获取插件的主前缀
prefix = get_plugin_prefix("插件名")  # 例如 "gs"

# 获取插件的可用前缀（考虑 force_prefix 和 allow_empty_prefix）
available = get_plugin_available_prefix("插件名")
```

**重要**：`to_ai` 描述中的命令示例应使用**实际的前缀格式**，而非假设的格式。例如：
- 如果插件前缀是 `"gs"`，命令示例应写 `"gs绑定uid"` 而非 `"/绑定uid"`
- 如果插件前缀是 `""`（空前缀），命令示例直接写命令名即可

### Step 0.6：注册 Capability Agent 画像（专业业务插件必做）

能力代理画像是**无人格专职执行者**：主人格负责识别、评估、创建 Kanban 任务树、转译结果；
画像负责按专业流程调用插件工具产出结果。插件注册画像后，主人格才能在
`evaluate_agent_mesh_capability` / `register_kanban_task` / `create_subagent` 中选择它。

**注册位置**：插件启动入口、`startup.py`、或会随插件加载导入的模块。注册表是进程内存数据，
晚于核心 `init_planning` 注册也可生效；同名 `profile_id` 后写覆盖前写。

**字段详细说明**与 [十四、能力代理画像](./references/14-ai-capability-profile.md) 一致——务必先读
那章的 §14.1 ~ §14.4（特别是 `_DELIVERY_BOUNDARY` 必拼、`record_*` 持久化、诚
实底线、`max_iterations` 等硬约束）。

### Step 1：阅读插件代码，识别所有触发器

找出所有 `@sv.on_command/on_prefix/on_fullmatch/on_keyword/on_suffix/on_regex/on_file/on_message` 装饰器，列出：
- 命令名称（keyword）
- 触发器类型（command/fullmatch/prefix/suffix/keyword/regex/file/message）
- 函数名
- 函数的实际功能（查什么数据、返回什么）
- 函数从 `ev.text` 里读取的参数格式

### Step 2：为每个触发器撰写 `to_ai` docstring

#### 2.1 `to_ai` 描述编写指南

**结构模板**：
```
<一句话功能描述，不加句号，18字以内，需标明所属游戏/功能模块>

<适用场景：AI 何时应该调用此工具，覆盖用户的多种说法>

Args:
    text: <参数格式说明，包括格式、示例、注意事项>
```

**撰写要点**：

1. **功能描述**（第一行）：简洁直白，18字以内，不加句号，必须标明所属游戏或功能模块
   - 第一行与第二行（适用场景）之间**必须有空行**
   - ✅ "查询原神角色详情和培养数据"
   - ✅ "查看A股大盘板块涨跌云图"
   - ✅ "查询股票/ETF的K线图或分时图"
   - ❌ "查询原神游戏中指定角色的详细信息和培养数据。"（超18字、有句号）
   - ❌ "这个功能可以帮用户查看股票信息"（不够简洁直白）

2. **适用场景**：描述 AI 在什么自然语言意图下应调用此工具
   - 覆盖用户的多种说法和表达方式
   - ✅ "当用户询问某只股票走势、分时图、K线图时调用"
   - ✅ "当用户说'帮我看看XX'、'XX怎么样'、'XX的行情'时调用"

3. **Args 部分**：
   - 说明参数的完整格式，包括可选前缀、分隔方式
   - 提供具体示例（至少 2-3 个）
   - 对于不需要参数的触发器（如 `on_fullmatch`），写"无需参数，留空即可"
   - 如果参数有多种格式，用列表逐项说明

4. **长度控制**：建议 5~15 行。太短 AI 无法正确构建参数，太长浪费 Token

**不同插件类型的描述风格**：

| 插件类型 | 描述风格示例 |
|---------|------------|
| 股票/行情 | "当用户询问某只股票今日走势、涨跌幅、K线图时调用" |
| 游戏查询 | "当用户查询原神/崩铁等游戏的角色、装备、副本信息时调用" |
| 娱乐功能 | "当用户想要...、请求...、发起...时调用" |
| 绑定/设置 | "当用户要绑定账号/UID/游戏ID时调用" |
| 无参数功能 | "...无需参数，留空即可" |

#### 2.2 不同装饰器类型的 `to_ai` 写法

GsCore 支持以下 8 种触发器装饰器，它们的 `to_ai` 写法有细微差异：

| 装饰器 | 匹配方式 | `text` 参数含义 | `to_ai` 写法要点 |
|--------|---------|----------------|-----------------|
| `on_command` | 前缀匹配命令名 | 命令后面的内容 | 描述命令后的参数格式 |
| `on_prefix` | 前缀匹配关键字 | 关键字后面的内容 | 同 `on_command`，描述关键字后的参数 |
| `on_fullmatch` | 完整匹配 | 无参数（`text` 为空） | 写"无需参数，留空即可" |
| `on_keyword` | 包含关键字 | 整条消息（含关键字） | 描述整条消息的格式 |
| `on_suffix` | 后缀匹配 | 关键字前面的内容 | 描述关键字前的参数格式 |
| `on_regex` | 正则匹配 | 整条消息 | 描述消息格式，说明正则捕获的模式 |
| `on_file` | 文件类型匹配 | 无 `text` 参数 | 通常不加 `to_ai`（AI 无法构建文件输入） |
| `on_message` | 消息匹配 | 整条消息 | 通常不加 `to_ai`（过于通用） |

**`on_prefix` 示例**：
```python
@sv.on_prefix(
    "查角色",
    to_ai="""查询原神角色详细信息

    当用户说"查角色 雷电将军"、"查角色 胡桃"时调用。

    Args:
        text: 角色名称，例如 "雷电将军"、"胡桃"、"纳西妲"
              支持角色昵称，例如 "雷神"、"影"、"小草神"
    """,
)
async def get_char_info(bot: Bot, ev: Event):
    char_name = ev.text.strip()  # "查角色" 后面的内容
    ...
```

**`on_suffix` 示例**：
```python
@sv.on_suffix(
    "怎么样",
    to_ai="""查询事物的评价或状态

    当用户说"XX怎么样"、"XX好不好"时调用。

    Args:
        text: 查询对象名称（"怎么样"前面的部分），例如 "雷电将军"、"这把武器"
    """,
)
async def query_evaluation(bot: Bot, ev: Event):
    subject = ev.text.replace("怎么样", "").strip()  # "怎么样" 前面的内容
    ...
```

**`on_keyword` 示例**：
```python
@sv.on_keyword(
    ("运势", "运气"),
    to_ai="""查看用户今日运势

    当用户消息中包含"运势"或"运气"时调用，例如"今天运势如何"、"我的运气怎么样"。
    无需额外参数，根据用户 ID 和日期自动生成。

    Args:
        text: 无需参数，留空即可
    """,
)
async def get_fortune(bot: Bot, ev: Event):
    ...
```

**`on_regex` 示例**：
```python
@sv.on_regex(
    r"(\d{9,10})的(uid|UID)",
    to_ai="""查询UID对应的用户信息

    当用户消息匹配"123456789的uid"这种格式时调用。

    Args:
        text: 包含 UID 的消息，格式为 "数字uid" 或 "数字UID"，例如 "123456789的uid"
    """,
)
async def query_uid(bot: Bot, ev: Event):
    uid = ev.regex_dict.get("uid")  # 从正则捕获组获取
    ...
```

### Step 3：逐层分析调用链，找出数据层，注入 `ai_return()`

这是改造中**最需要思考、也最容易被 AI 忽略**的步骤。

> **⚠️ 核心警告：必须逐层分析，不能只看触发器函数！**
>
> 实践中发现，AI 倾向于只分析触发器标注的函数本身，而**不会自动去追踪触发器内部调用的其他函数**。但实际的数据获取和渲染逻辑往往在更深层的函数调用中。
>
> **你必须做到**：
> 1. 从触发器函数出发，**逐层向下追踪**所有被调用的函数
> 2. 找到**真正拿到原始数据**的那一层（可能在第 2、3、4 层调用中）
> 3. 分析该层数据的**完整结构**，确定哪些字段是渲染图片所用的数据
> 4. 在数据获取之后、图片渲染之前，注入 `ai_return()`

**原则**：找到函数链中"已经拿到原始数据、但还没开始生成图片/发送消息"的那个位置，在那里提取关键信息并调用 `ai_return()`。

**逐层追踪的方法**：

```
触发器函数 send_xxx(bot, ev)
    └── 调用 render_image(...) 或 get_data(...) 等
        └── 调用 fetch_api(...) 或 get_xxx_data(...) 等
            └── 调用 parse_response(...) 或 build_chart_data(...) 等
                └── 这里才是真正拿到原始数据的地方！
```

**寻找注入点的步骤**：

1. **第一步**：从触发器函数出发，看它调用了什么函数（如 `render_image()`、`get_data()`）
2. **第二步**：进入被调用的函数，继续追踪，找到实际获取数据的 `get_xxx()` / `fetch_xxx()` 函数
3. **第三步**：确认数据获取后，找到图片生成的位置（`render_image_by_pw()` / `fig.write_html()` / `to_fig()` 等）
4. **第四步**：在数据获取之后、图片生成之前的位置注入 `ai_return()`

**注入位置选择**：

```python
# ✅ 正确：在渲染前注入（数据层函数内部）
async def render_html(market, sector, ...):
    raw_data = await get_xxx(...)   # 数据已拿到

    # 在这里注入 ai_return
    _ai_return_xxx(raw_data)        # ← 注入点

    fig = await to_fig(raw_data)    # 图片生成
    fig.write_html(file)
    return file

# ❌ 错误：在触发器函数内注入（通常拿不到原始数据）
async def send_xxx(bot, ev):
    im = await render_image(...)    # 数据和渲染都在里面，触发器层看不到原始数据
    await bot.send(im)
```

**⚠️ 图片场景的关键要求：分析渲染数据来源**

当触发器最终返回的是图片时，**必须分析图片是用什么数据渲染的**：

1. 找到图片渲染函数（如 `to_fig()`、`build_chart()`、`render_image_by_pw()` 等）
2. 分析该函数接收的参数是什么数据结构
3. 从这些数据中提取关键字段（名称、数值、状态等）
4. 将提取的字段格式化为纯文本，通过 `ai_return()` 返回给 AI

**示例：追踪多层调用找到注入点**

```python
# 触发器函数（第 1 层）
@sv.on_command(("个股"))
async def send_stock_img(bot: Bot, ev: Event):
    im = await render_stock(ev.text)  # 调用 render_stock
    await bot.send(im)

# 渲染函数（第 2 层）
async def render_stock(code: str):
    data = await fetch_stock_data(code)  # 调用 fetch_stock_data
    _ai_return_stock(data)               # ← 注入点在这里！
    fig = build_stock_chart(data)        # 用 data 渲染图片
    return fig_to_bytes(fig)

# 数据获取函数（第 3 层）
async def fetch_stock_data(code: str) -> dict:
    # 实际的 API 调用
    return {"name": "证券ETF", "price": 1.234, "change": 2.5, ...}
```

**不同数据类型的提取思路**：

| 数据类型 | 提取什么 |
|---------|--------|
| 股票行情 | 名称、最新价、涨跌幅、开/高/低、换手率、成交额 |
| K线数据 | 名称、周期、最近N条：日期、开/收/高/低、涨跌幅 |
| 排行榜/云图 | 领涨前N、领跌前N、涨/跌/平统计 |
| 游戏角色 | 名称、等级、核心数值、关键属性 |
| 游戏副本/任务 | 名称、进度、完成状态、剩余次数 |
| 娱乐数据 | 核心结果字段 |
| 错误情况 | 错误原因（`ai_return("错误：xxx")`） |

### Step 4：编写 `_ai_return_xxx()` 辅助函数

为每类数据类型各写一个辅助函数：

```python
def _ai_return_xxx(raw_data, ...):
    """从 xxx 数据中提取文本信息，通过 ai_return 返回给 AI 分析"""
    try:
        # 提取关键字段
        # 格式化为可读文本
        # 调用 ai_return(result)
    except Exception as e:
        logger.warning(f"[插件名] ai_return xxx数据提取失败: {e}")
```

**注意**：
- 用 `try/except` 包裹（这里允许，因为这不是业务逻辑，是辅助的观测代码，提取失败不影响图片生成）
- 错误只 `logger.warning`，不影响主流程
- 文本要简洁、结构化，用 `【标题】` 标注分区

---

## 18.4 完整改造示例（股票插件）

以下是改造前后的完整对比，覆盖了各种情况。

### 4.1 触发器层改造（`__init__.py` 或主逻辑文件）

**改造前：**
```python
from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

sv = SV("大盘云图")

@sv.on_command(("大盘云图"))
async def send_cloudmap_img(bot: Bot, ev: Event):
    im = await render_image("大盘云图", ev.text.strip())
    await bot.send(im)

@sv.on_fullmatch(("我的个股"))
async def send_my_stock_img(bot: Bot, ev: Event):
    uid = await SsBind.get_uid_list_by_game(ev.user_id, ev.bot_id)
    if not uid:
        return await bot.send("您还未添加自选呢~")
    txt = " ".join(convert_list(uid)[:5])
    im = await render_image(txt, "single-stock")
    await bot.send(im)

@sv.on_command(("个股"))
async def send_stock_img(bot: Bot, ev: Event):
    content = ev.text.strip().lower()
    if not content:
        return await bot.send("请后跟股票代码使用")
    # ... 逻辑 ...
    await bot.send(im)
```

**改造后：**
```python
from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

sv = SV("大盘云图")

@sv.on_command(
    ("大盘云图"),
    to_ai="""查看A股大盘板块涨跌云图

    当用户询问大盘行情、今日市场整体表现、行业板块涨跌分布、大盘热力图时调用。

    Args:
        text: 可选的板块筛选条件。留空显示全部行业板块的大盘云图。
              例如 "" 或 "医药" 或 "科技"
    """,
)
async def send_cloudmap_img(bot: Bot, ev: Event):
    im = await render_image("大盘云图", ev.text.strip())
    await bot.send(im)


@sv.on_fullmatch(
    ("我的个股"),
    to_ai="""查看自选股当日分时行情

    当用户询问"我的股票"、"自选股今天怎么样"、"帮我看看我的持仓"时调用。
    无需参数，自动读取当前用户的自选股列表。

    Args:
        text: 无需参数，留空即可
    """,
)
async def send_my_stock_img(bot: Bot, ev: Event):
    user_id = ev.at if ev.at else ev.user_id
    uid = await SsBind.get_uid_list_by_game(user_id, ev.bot_id)
    if not uid:
        return await bot.send("您还未添加自选呢~或者后跟具体股票代码")
    uid = convert_list(uid)
    if len(uid) > 5:
        uid = uid[:5]
    txt = " ".join(uid)
    im = await render_image(txt, "single-stock")
    await bot.send(im)


@sv.on_command(
    ("个股"),
    to_ai='''查询股票/ETF的K线图或分时图

    当用户询问某只股票/ETF今天走势、分时图、日K、周K、月K时调用。
    支持同时查询多只股票。

    Args:
        text: 查询内容，格式为 "[周期前缀] 股票名称或代码"
              - 无前缀：默认显示分时图，例如 "证券ETF"
              - "日k": 日K线，例如 "日k 证券ETF"
              - "周k": 周K线，例如 "周k 白酒ETF"
              - "月k"/"季k"/"年k": 对应周期K线
              - 多个标的以空格分隔，例如 "证券ETF 白酒ETF"
              - VIX指数：例如 "300vix"（仅支持分时，不支持K线）
    ''',
)
async def send_stock_img(bot: Bot, ev: Event):
    content = ev.text.strip().lower()
    if not content:
        return await bot.send("请后跟股票代码使用, 例如：个股 证券ETF")
    # ... 原有逻辑完全不变 ...
    await bot.send(im)
```

### 4.2 数据/渲染层改造（`get_cloudmap.py` 或渲染文件）

**新增 import：**
```python
from gsuid_core.ai_core.trigger_bridge import ai_return
```

**在 `render_html()` 中的注入点：**
```python
async def render_html(market, sector, start_time, end_time):
    # ... 数据获取逻辑 ...
    raw_data = await get_xxx(...)

    if sector == "single-stock":
        if raw_datas:
            fig = await to_multi_fig(raw_datas)
            _ai_return_single_stock(raw_datas, is_multi=True)   # ← 注入
        else:
            fig = await to_single_fig(raw_data)
            _ai_return_single_stock(raw_data)                    # ← 注入
    elif sector == "compare-stock":
        fig = await to_compare_fig(raw_datas)
        _ai_return_compare_stock(raw_datas)                      # ← 注入
    elif sector and sector.startswith("single-stock-kline"):
        fig = await to_single_fig_kline(raw_data)
        _ai_return_kline(raw_data, sector)                       # ← 注入
    else:
        fig = await to_fig(raw_data, market, sector, ...)
        _ai_return_cloudmap(raw_data, market, sector)            # ← 注入

    # ... 图片生成逻辑 ...
```

**各类辅助函数：**

```python
def _ai_return_single_stock(raw_data, is_multi: bool = False):
    """从个股分时数据中提取文本摘要，通过 ai_return 返回给 AI"""
    try:
        if is_multi:
            parts = []
            for rd in raw_data:
                if isinstance(rd, str):
                    continue
                d = rd.get("data", {})
                name = d.get("f58", "N/A")
                price = d.get("f43", "N/A")
                change = d.get("f170", "N/A")
                turnover = d.get("f168", "N/A")
                open_p = d.get("f60", "N/A")
                high = d.get("f44", "N/A")
                low = d.get("f45", "N/A")
                amount = d.get("f48", "N/A")
                parts.append(
                    f"【{name}】最新价: {price}  涨跌幅: {change}%  "
                    f"开盘: {open_p}  最高: {high}  最低: {low}  "
                    f"换手率: {turnover}%  成交额: {amount}"
                )
            if parts:
                ai_return("【多股分时行情对比】\n" + "\n".join(parts))
        else:
            d = raw_data.get("data", {})
            name = d.get("f58", "N/A")
            price = d.get("f43", "N/A")
            change = d.get("f170", "N/A")
            turnover = d.get("f168", "N/A")
            open_p = d.get("f60", "N/A")
            high = d.get("f44", "N/A")
            low = d.get("f45", "N/A")
            amount = d.get("f48", "N/A")
            ai_return(
                f"【{name} 分时行情】\n"
                f"最新价: {price}  涨跌幅: {change}%\n"
                f"开盘价: {open_p}  最高价: {high}  最低价: {low}\n"
                f"换手率: {turnover}%  成交额: {amount}"
            )
    except Exception as e:
        logger.warning(f"[插件名] ai_return 分时数据提取失败: {e}")


def _ai_return_kline(raw_data, sector: str):
    """从K线数据中提取文本摘要"""
    try:
        d = raw_data.get("data", {})
        name = d.get("name", "N/A")
        klines = d.get("klines", [])
        if not klines:
            return
        period_map = {"101": "日K", "102": "周K", "103": "月K", ...}
        code = sector.replace("single-stock-kline-", "")
        period_name = period_map.get(code, "K线")
        result = f"【{name} {period_name}数据（最近10条）】\n"
        result += "日期        开盘    收盘    最高    最低    涨跌幅\n"
        for line in klines[-10:]:
            values = line.split(",")
            if len(values) >= 9:
                result += f"{values[0]}  {values[1]:>8}  {values[2]:>8}  {values[3]:>8}  {values[4]:>8}  {values[8]:>6}%\n"
        ai_return(result)
    except Exception as e:
        logger.warning(f"[插件名] ai_return K线数据提取失败: {e}")


def _ai_return_cloudmap(raw_data, market: str, sector=None):
    """从大盘/板块云图数据中提取涨跌统计"""
    try:
        diff = raw_data.get("data", {}).get("diff", [])
        if not diff:
            return
        valid_items = [i for i in diff if i.get("f3") != "-" and i.get("f14")]
        valid_items.sort(key=lambda x: float(x.get("f3", 0)), reverse=True)
        result = f"【{market}涨跌分布】\n"
        result += "领涨:\n" + "".join(
            f"  {i.get('f14')}({i.get('f100', '')}): {i.get('f3')}%\n"
            for i in valid_items[:5]
        )
        result += "领跌:\n" + "".join(
            f"  {i.get('f14')}({i.get('f100', '')}): {i.get('f3')}%\n"
            for i in valid_items[-5:]
        )
        up = sum(1 for i in valid_items if float(i.get("f3", 0)) > 0)
        dn = sum(1 for i in valid_items if float(i.get("f3", 0) < 0)
        fl = len(valid_items) - up - dn
        result += f"统计：上涨 {up} 家  下跌 {dn} 家  平盘 {fl} 家"
        ai_return(result)
    except Exception as e:
        logger.warning(f"[插件名] ai_return 云图数据提取失败: {e}")
```

---

## 18.5 非股票插件的改造示例

### 5.1 游戏查询插件（有 UID 绑定，返回角色/账号数据）

```python
# 触发器层
@sv_genshin.on_command(
    ("查角色", "角色信息"),
    to_ai="""查询原神角色详情和培养数据

    当用户询问某个角色的命座、圣遗物、天赋等培养情况时调用。
    需要用户已绑定原神 UID。

    Args:
        text: 角色名称，例如 "雷电将军"、"胡桃"、"纳西妲"
              支持角色昵称，例如 "雷神"、"影"、"小草神"
    """,
)
async def get_char_info(bot: Bot, ev: Event):
    ...
```

```python
# 数据层注入（在拿到角色数据后、生成图片前）
async def render_char_image(uid: str, char_name: str):
    char_data = await fetch_char_data(uid, char_name)

    # AI 注入
    _ai_return_char(char_data, char_name)

    fig = build_char_figure(char_data)
    return await render_image_by_pw(fig)


def _ai_return_char(char_data: dict, char_name: str):
    """提取角色关键数据"""
    try:
        level = char_data.get("level", "N/A")
        const = char_data.get("constellation", 0)
        atk = char_data.get("fight_prop", {}).get("FIGHT_PROP_CUR_ATTACK", "N/A")
        crit_rate = char_data.get("fight_prop", {}).get("FIGHT_PROP_CRITICAL", "N/A")
        crit_dmg = char_data.get("fight_prop", {}).get("FIGHT_PROP_CRITICAL_HURT", "N/A")
        weapon = char_data.get("weapon", {}).get("name", "N/A")
        ai_return(
            f"【{char_name} 角色数据】\n"
            f"等级: {level}  命座: {const}命\n"
            f"攻击力: {atk:.0f}  暴击率: {crit_rate:.1%}  暴击伤害: {crit_dmg:.1%}\n"
            f"武器: {weapon}"
        )
    except Exception as e:
        logger.warning(f"[GenshinUID] ai_return 角色数据提取失败: {e}")
```

### 5.2 无返回数据的写操作（绑定/设置类）

绑定、设置等命令**不需要 `ai_return`**，因为 `bot.send(str)` 的文字会被 MockBot 自动拦截返回给 AI：

```python
@sv.on_command(
    ("绑定", "bind"),
    to_ai="""绑定游戏UID到账号

    当用户说"帮我绑定UID"、"我的uid是xxx"、"bind xxx"时调用。

    Args:
        text: 用户的游戏 UID，纯数字，例如 "123456789"
    """,
)
async def bind_uid(bot: Bot, ev: Event):
    uid = ev.text.strip()
    if not uid.isdigit():
        return await bot.send("UID 格式不正确，请输入纯数字")
    await GameDB.bind_uid(ev.user_id, uid)
    await bot.send(f"✅ 已成功绑定 UID: {uid}")
    # bot.send 的文字会被 MockBot 自动收集，AI 会知道"绑定成功"
    # 不需要额外调用 ai_return()
```

### 5.3 娱乐/随机类功能

```python
@sv_fun.on_fullmatch(
    ("今日运势", "运势"),
    to_ai="""查看用户今日运势

    当用户想看今天运势、问今天是否适合做某事时调用。
    无需参数，根据用户 ID 和日期生成唯一结果。

    Args:
        text: 无需参数，留空即可
    """,
)
async def get_fortune(bot: Bot, ev: Event):
    result = calculate_fortune(ev.user_id)
    im = await render_fortune_image(result)
    await bot.send(im)
```

```python
# 渲染层注入
async def render_fortune_image(result: dict):
    _ai_return_fortune(result)   # 注入
    fig = build_fortune_figure(result)
    return await render_image_by_pw(fig)


def _ai_return_fortune(result: dict):
    try:
        score = result.get("score", "N/A")
        lucky_color = result.get("lucky_color", "N/A")
        summary = result.get("summary", "")
        ai_return(
            f"【今日运势】\n"
            f"运势指数: {score}/100\n"
            f"幸运色: {lucky_color}\n"
            f"运势概述: {summary}"
        )
    except Exception as e:
        logger.warning(f"[FunPlugin] ai_return 运势数据提取失败: {e}")
```

---

## 18.6 不需要改造的触发器

以下类型的触发器**不加 `to_ai`**（保持 `to_ai=""` 默认值）：

| 情况 | 原因 |
|------|------|
| 管理员/超级用户专用命令 | 虽然系统会自动检查 `pm` 权限（低权限用户调用会返回"权限不足"），但 AI 对大多数用户都会收到权限错误，浪费 token |
| 系统维护命令（重载、清缓存等） | 危险操作，不开放给 AI |
| 需要多轮交互/Response 会话的命令 | `receive_resp` 在 AI 上下文中返回 `None`，交互流程会中断 |
| 纯文件上传/接收型命令（`on_file`） | AI 无法构建文件输入 |
| 功能过于单一且 AI 无法获得有效信息的命令 | 改造价值低 |

> **权限保障**：即使开发者错误地给高权限命令添加了 `to_ai`，系统也会在运行时检查 `plugins.pm` 和 `sv.pm`，低权限用户通过 AI 调用时会收到 "❌ 权限不足" 错误。配置通过 webconsole 修改后实时生效。

---

## 18.7 改造质量检查清单

改造完成后，逐项确认：

**前期准备：**
- [ ] 已用 `search_files` 批量定位所有触发器
- [ ] 已检查并移除与触发器功能重复的手动 `@ai_tools` 函数
- [ ] 已判断插件是否需要 Capability Agent 画像
- [ ] 专业业务插件已注册 `CapabilityAgentProfile`
- [ ] 画像 `system_prompt` 是无人格纯职能提示词，包含工具优先级、数据依据、Artifact、风险动作约束
- [ ] 画像 `tool_names` 只列插件专业工具，不重复列框架永远工具
- [ ] 画像 `match_keywords` 能覆盖用户自然语言 hint
- [ ] 已用 `get_plugin_prefixs()` 确认插件的实际命令前缀格式

**触发器层：**
- [ ] 所有应改造的 `on_xxx` 装饰器都已加 `to_ai` 参数
- [ ] `to_ai` 字符串的第一句话能让 AI 准确识别触发意图
- [ ] `text` 参数格式说明清晰，有具体例子
- [ ] `on_fullmatch` 无参数型已注明"无需参数，留空即可"
- [ ] `on_suffix` 的 `text` 参数描述的是关键字**前面**的内容
- [ ] `on_keyword` 的 `text` 参数描述的是**整条消息**
- [ ] `on_regex` 的 `text` 参数描述了正则匹配的消息格式
- [ ] 多 keyword 的 tuple 形式语法正确：`("命令1", "命令2")`
- [ ] `to_ai` 描述中的命令示例使用了正确的前缀格式

**调用链逐层分析（⚠️ 最容易遗漏的部分）：**
- [ ] **已逐层追踪**触发器函数内部调用的所有子函数，而非只看触发器本身
- [ ] **已找到真正获取原始数据的函数**（可能在第 2、3、4 层调用中）
- [ ] **已确认数据结构**：知道原始数据的字段名、类型、含义
- [ ] **已找到图片渲染函数**：确认图片是用哪些数据字段渲染的
- [ ] **注入点在正确的层级**：在数据获取之后、图片生成之前，而非在触发器函数内

**数据层：**
- [ ] 已 `from gsuid_core.ai_core.trigger_bridge import ai_return`
- [ ] 每类数据都有对应的 `_ai_return_xxx()` 辅助函数
- [ ] 注入点在数据获取后、图片生成前
- [ ] 辅助函数用 `try/except` 包裹，错误只 `logger.warning`
- [ ] `ai_return` 的文本内容包含足够的关键信息（数字、名称等）
- [ ] 错误分支（如数据为空）也有 `ai_return("错误：...")`
- [ ] 纯文字 `bot.send(str)` 场景没有重复调用 `ai_return()`
- [ ] 混合列表 `bot.send([text, image])` 场景已用 `ai_return()` 单独注入文字摘要
- [ ] **图片场景**：已分析图片渲染所用的数据来源，提取了关键字段作为文本摘要

**不破坏性检查：**
- [ ] 原触发器函数体**完全未修改**
- [ ] `ai_return()` 调用在辅助函数里，不在触发器函数里
- [ ] 没有给触发器函数添加任何额外参数

---

## 18.8 常见问题

**Q：`to_ai` 里能写多长？**
A：建议 5~15 行。太短 AI 无法正确构建参数，太长浪费 Token。核心是把 `text` 参数格式说清楚。

**Q：触发器函数本身有前置检查（如用户未绑定 UID），AI 调用时怎么处理？**
A：不用特殊处理。`bot.send("请先绑定UID")` 会被 MockBot 自动收集，作为工具返回值的一部分告知 AI，AI 会告诉用户"需要先绑定"。

**Q：某个触发器内部有多条 `await bot.send()`，这些都会被拦截吗？**
A：是的，MockBot 会拦截所有 `bot.send()`。纯文字的 `bot.send(str)` 会被自动收集返回给 AI，不需要额外调用 `ai_return()`。通常只有最后一条发图，中间的文字 send 也会被收集，AI 可以看到。

**Q：渲染层在另一个文件，我找不到合适的注入点怎么办？**
A：向上追踪调用链，找到 `raw_data = await get_xxx()` 之后的位置即可。如果渲染函数不经过这个流程（比如直接从缓存返回），可以在缓存命中分支之前加。

**Q：`on_prefix` 和 `on_command` 有什么区别，`to_ai` 的写法有不同吗？**
A：`on_prefix` 匹配以 keyword 开头的消息；`on_command` 通常也是前缀匹配但语义是命令。`to_ai` 写法相同，`text` 参数描述的都是命令后面的内容。`on_suffix` 则相反，`text` 描述的是关键字前面的内容。

**Q：多个触发器共享同一个渲染函数，我只注入一次就够了吗？**
A：是的。只要渲染函数内部按不同分支调用了不同的 `_ai_return_xxx()`，每条触发器路径都会被覆盖。

**Q：插件已经有手动注册的 `@ai_tools` 工具，加了 `to_ai` 后会冲突吗？**
A：会。两者都会注册为 AI 工具，导致功能重复。应该移除手动的 `@ai_tools` 函数，保留 `to_ai` 触发器版本（因为它同时支持用户直接调用和 AI 调用）。

**Q：如何确认插件的命令前缀？**
A：使用 `get_plugin_prefixs("插件名")` 获取所有前缀列表，或 `get_plugin_prefix("插件名")` 获取主前缀。`to_ai` 描述中的命令示例应使用实际前缀，而非假设的格式（如 `/命令`）。

**Q：`bot.send(str)` 的文字真的会被自动返回给 AI 吗？我还需要调用 `ai_return()` 吗？**
A：是的，`MockBot` 会自动拦截 `bot.send(str)` 并将文字收集到返回值中。对于纯文字场景，**不需要**额外调用 `ai_return()`。只有在需要返回图片的文字摘要（`bot.send(bytes)` 场景）或不经过 `bot.send` 直接 return 的场景才需要 `ai_return()`。

**Q：为什么必须逐层分析调用链，只看触发器函数不行吗？**
A：不行。实践中发现，触发器函数通常只是调用其他函数来获取数据和渲染图片，真正的数据获取逻辑在更深层的函数中。如果只看触发器函数，你无法知道：
1. 数据是从哪个 API 获取的
2. 数据的具体结构是什么
3. 图片是用哪些字段渲染的
4. 应该在哪个位置注入 `ai_return()`

**必须逐层追踪**：从触发器函数开始，进入它调用的每个函数，直到找到真正获取原始数据的地方。

**Q：图片场景如何分析渲染数据来源？**
A：当触发器返回图片时，必须：
1. 找到图片渲染函数（如 `to_fig()`、`build_chart()`、`render_image_by_pw()` 等）
2. 分析该函数接收的参数是什么数据结构
3. 从这些数据中提取关键字段（名称、数值、状态等）
4. 将提取的字段格式化为纯文本，通过 `ai_return()` 返回给 AI

**示例**：如果渲染函数是 `build_stock_chart(data)`，你需要查看 `data` 包含哪些字段（如 `name`、`price`、`change`），然后提取这些字段作为文本摘要。

**Q：AI 会自动帮我写 `ai_return()` 吗？**
A：**不会**。实践中发现，AI（包括高级模型）在改造触发器时，**不会自动**在多层函数调用中添加 `ai_return()` 调用。AI 倾向于只在触发器函数本身做简单处理，而不会深入分析调用链去找到正确的数据注入点。

**你必须手动完成以下工作**：
1. 逐层追踪触发器的完整调用链
2. 找到"数据已获取、图片未生成"的那个精确位置
3. 分析该位置的数据结构，提取关键字段
4. 编写 `_ai_return_xxx()` 辅助函数并注入

**不要期望 AI 能自动完成这些分析，必须由你显式地在 SKILL 指令中引导 AI 做到。**

**Q：`to_ai` 已经把触发器暴露成工具了，为什么还要注册 Capability Agent 画像？**
A：`to_ai` 只解决"AI 能不能调用某个命令"的问题；Capability Agent 画像解决"复杂任务该由哪个无人格专业执行者、按什么流程、用哪些专业工具组合完成"的问题。没有画像时，Kanban 的能力评估可能认为专业能力缺失，或者回退到 `research_agent`，在金融、医疗、法律等强专业域会触发诚实底线而拒绝给具体建议。

**Q：注册画像后，主人格会自动使用吗？**
A：会在两条链路中被使用：复合任务由主人格先调 `evaluate_agent_mesh_capability`，通过后 `register_kanban_task` 的子任务会带上 `agent_profile`；即时单步委派可由 `create_subagent(agent_profile="...")` 选择画像。前提是 `profile_id` / `match_keywords` 写得稳定且覆盖用户说法。

**Q：画像的 `tool_names` 要不要把 `artifact_put`、`web_search_tool`、`state_get` 都写进去？**
A：不要。插件只写业务专业工具；框架会自动附加 Artifact、state、知识检索和 web 兜底等永远工具。重复写不会增加能力，反而让画像维护变乱。
