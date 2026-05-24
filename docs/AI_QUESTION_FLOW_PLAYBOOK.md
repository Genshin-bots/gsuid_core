# AI 通用问题流转剧本（Question Flow Playbook）

> 写作背景：本文以现行 `gsuid_core/ai_core/` 代码为唯一事实源，整合
> [`AI_TRIGGER_FLOW.md`](AI_TRIGGER_FLOW.md)、
> [`AGENT_CAPABILITY_AGENT_MERGED_20260521.md`](AGENT_CAPABILITY_AGENT_MERGED_20260521.md)、
> [`AGENT_MESH_KANBAN_IMPLEMENTATION_20260522.md`](AGENT_MESH_KANBAN_IMPLEMENTATION_20260522.md)
> 三份文档，回答一个具体问题——**当用户抛出 X 类问题时，框架理论上应当怎么走？**
>
> 不写"怎么实现"，只写"怎么走"。每条剧本都带：触发判据 → 关键节点 → 应当被
> 调用的工具 / 函数 → 落点（artifact / record / state / 直接回复）。
>
> 适用人格：所有 `Persona`；适用场景：群聊 + 私聊（差异在最末节专章对比）。

---

## 0. 目录

1. [统一前置流水线（任何问题都要走的步骤）](#1-统一前置流水线)
2. [问题分类总表](#2-问题分类总表)
3. [简单问题：寒暄 / 表达情绪](#3-剧本-a简单问题寒暄--表达情绪)
4. [信息查询：游戏 / 知识 / 实时事实](#4-剧本-b信息查询游戏--知识--实时事实)
5. [单工具任务（A 类：工具输出即答案）](#5-剧本-c单工具任务a-类工具输出即答案)
6. [追问溯源（问"为什么"）](#6-剧本-d追问溯源问为什么基于什么)
7. [专业域问题（B 类：金融 / 投资 / 医疗 / 法律 / 攻略评估）](#7-剧本-e专业域问题b-类需委派能力代理)
8. [复杂多步任务（Kanban 任务树）](#8-剧本-f复杂多步任务kanban-任务树)
9. [定时 / 周期任务](#9-剧本-g定时--周期任务-vs-周期复杂任务)
10. [虚拟账户 / 模拟交易 / N 元让你管理 N 天](#10-剧本-h虚拟账户--模拟交易--n-元让你管理-n-天)
11. [代码 / 脚本 / 数据处理 / 渲染](#11-剧本-i代码--脚本--数据处理--渲染)
12. [数据分析 / 复盘 / 周报](#12-剧本-j数据分析--复盘--周报)
13. [自我认知问题：你是谁 / 你能做什么 / 我之前说过什么](#13-剧本-k自我认知问题你是谁--你能做什么--我之前说过什么)
14. [群聊 vs 私聊：路径回环差异](#14-群聊-vs-私聊路径回环差异)
15. [历史记录 vs 调用工具：差异速查](#15-历史记录-vs-调用工具差异速查)

---

## 1. 统一前置流水线

任何一条用户消息进 AI 之前，`handler.py → handle_ai.py` 都会走同一条流水线。
画在最前面，后续每个剧本都默认从「步骤 7」往下分叉。

```
┌──────────────────────────────────────────────────────────────────────┐
│ handler.py  handle_event()                                            │
│   1. IS_HANDDLE 全局开关 / 黑白名单 / 命令前缀 / 触发器                │
│   2. 命中触发器 → 执行触发器；否则 → 走 AI                            │
│   3. AI 触发条件:                                                     │
│      - ai_config["enable"] = true                                     │
│      - persona_config_manager.get_persona_for_session(session_id)     │
│      - "提及应答" ∈ ai_mode 且 (event.is_tome 或 命中关键词)          │
│   4. ws.queue.put_nowait(TaskContext(coro=handle_ai_chat(...)))       │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│ handle_ai.py  handle_ai_chat(bot, event)                              │
│   5. 双层长度防护:                                                    │
│      - len > 60000  → 硬截断（防子 Agent Token 爆炸）                 │
│      - len > 15000  → 调 create_subagent 智能摘要                     │
│   6. 意图识别: classifier_service.predict_async(query)                │
│         intent ∈ {"闲聊", "工具", "问答"}                              │
│   7. session = await get_ai_session(event)                            │
│   8. 准备 user_messages（含好感度注入）                                │
│   9. 双路记忆检索（C4 寒暄门控判断是否跳过）:                          │
│         dual_route_retrieve(query, group_id, user_id, ...)            │
│  10. 拼装 context_parts:                                              │
│      - 【历史对话】format_history_for_agent(history, 30 条)            │
│      - 【当前群聊语境】format_context_injection(group_scope)           │
│      - （情绪状态）get_mood_description(persona, mood_key)             │
│      - 【关于我自己】build_self_cognition_context(bot_id, user_id)    │
│      - （口吻锚点）get_voice_anchor(persona_name)                     │
│      - （自我情景）retrieve_self_episodes(bot_id)  ← 仅命中 _SELF_RECALL_RE
│      - 【你正在为对方推进的 Kanban 任务】build_task_context(user_id)   │
│      - 【长期记忆】memory_context.to_prompt_text()                    │
│  11. session.run(user_message, bot, ev, rag_context=full_context,     │
│                  return_mode="by_bot")                                │
│  12. send_chat_result(bot, chat_result)                               │
│  13. 异步更新 mood: _update_persona_mood(...)                         │
└──────────────────────────────────────────────────────────────────────┘
```

**关键源码引用**：

```python
# F:/gsuid_core/gsuid_core/ai_core/handle_ai.py:105
if not ai_config.get_config("enable").data:
    logger.debug("🧠 [GsCore][AI] AI服务未启用，跳过处理")
    return

async with _ai_semaphore:                       # 全局 Semaphore(10) 并发上限
    ...
    res = await classifier_service.predict_async(query)
    intent = res["intent"]                      # 闲聊 / 工具 / 问答
    ...
    session = await get_ai_session(event)
    user_messages = await prepare_content_payload(event, favorability=favorability)
    ...
    if is_enable_memory and memory_config.enable_retrieval:
        if not _should_retrieve_memory(query, intent, str(event.user_id)):
            logger.debug("🧠 [Memory] 命中寒暄门控，跳过双路检索")
        else:
            mem_ctx = await dual_route_retrieve(query=query, group_id=..., user_id=..., ...)
            memory_context_text = mem_ctx.to_prompt_text(max_chars=..., priority_speakers=masters_set)
    ...
    chat_result = await session.run(
        user_message=user_messages,
        bot=bot, ev=event,
        rag_context=full_context,
        return_mode="by_bot",
    )
```

> **寒暄门控规则**（`handle_ai.py:64-82`）：只有「短 + 闲聊 + 无实体 + 无情绪 +
> 无回指 + 非任务引用 + 非主人」全部满足时才跳过双路记忆检索；命中任一条
> 件强制检索，避免漏掉重要背景。

```python
# F:/gsuid_core/gsuid_core/ai_core/handle_ai.py:53-82
_FORCE_RETRIEVE_RE  = re.compile(r"(之前|上次|上回|那个|那次|昨天|前几天|你说过|你不是说|记不记得|还记得|提到过|任务|计划|进度)")
_EMOTION_RETRIEVE_RE = re.compile(r"(难过|崩溃|沉船|破防|开心死|伤心|焦虑|想哭|绝望|委屈|孤独)")
_SELF_RECALL_RE = re.compile(r"(你之前|你上次|你不是说|你说过|你还记得|你刚才说|你答应)")
_ENTITY_HINT_RE = re.compile(r"([A-Za-z]{3,}|[「『\"“].+|[一-鿿]{6,})")
```

---

## 2. 问题分类总表

任何用户消息最终都会被映射到下表 11 个剧本之一。**判别从上到下找第一条匹配**。

| # | 触发判据（first-match） | 剧本 | 主要工具 / 落点 |
|---|---|---|---|
| A | 寒暄 / 纯情绪 / `<SILENCE>` 适用场景 | [§3](#3-剧本-a简单问题寒暄--表达情绪) | 不调工具，可能 `send_meme` |
| B | "怎么打 / 在哪 / 是什么"等纯知识查询 | [§4](#4-剧本-b信息查询游戏--知识--实时事实) | `search_knowledge` → `web_search_tool` |
| C | 单工具一击即得的私有 / 实时 / 个人数据 | [§5](#5-剧本-c单工具任务a-类工具输出即答案) | 单个 `by_trigger` 工具 |
| D | 主人追问"你为什么 / 凭什么这么说" | [§6](#6-剧本-d追问溯源问为什么基于什么) | `artifact_get_recent` / `artifact_list` |
| E | 落在专业代理 `when_to_use` 内、要"组合/评估/推荐" | [§7](#7-剧本-e专业域问题b-类需委派能力代理) | `create_subagent(agent_profile=...)` |
| F | 多种能力协作 / 多源汇总 / 持续做某事 | [§8](#8-剧本-f复杂多步任务kanban-任务树) | `evaluate_agent_mesh_capability` → `register_kanban_task` |
| G | "明天 / 每天 / 每隔 N 分钟"等定时触发 | [§9](#9-剧本-g定时--周期任务-vs-周期复杂任务) | `add_once_task` / `add_interval_task` / Kanban + `recurring_trigger` |
| H | 虚拟盘 / 模拟交易 / N 元让你管理 N 天 | [§10](#10-剧本-h虚拟账户--模拟交易--n-元让你管理-n-天) | Kanban + `recurring_trigger` + `record_*` |
| I | 写代码 / 跑脚本 / 渲染图 / 文件批处理 | [§11](#11-剧本-i代码--脚本--数据处理--渲染) | `code_agent` 能力代理 |
| J | 周报 / 复盘 / 趋势统计（基于内部数据） | [§12](#12-剧本-j数据分析--复盘--周报) | `internal_reporter` 能力代理 |
| K | "你是谁 / 你能做什么 / 你答应过我" | [§13](#13-剧本-k自我认知问题你是谁--你能做什么--我之前说过什么) | `get_self_info` / `query_user_memory` / self_cognition |

---

## 3. 剧本 A：简单问题（寒暄 / 表达情绪）

### 3.1 触发判据

- `classifier` 结果 `intent="闲聊"` 且未命中
  `_FORCE_RETRIEVE_RE` / `_EMOTION_RETRIEVE_RE` / `_SELF_RECALL_RE`；
- 决策树第 2 步「是否是日常寒暄 / 纯情绪回应」=> 是。
- 例：`你好`、`今天好热`、`嗯`、`哈哈哈`。

### 3.2 流程

```
classifier(query) → intent="闲聊"
       │
       ├── _should_retrieve_memory()  → False（短+闲聊+无实体）
       │     └── 跳过 dual_route_retrieve（省 Embedding + Reranker 开销）
       │
       ├── 拼装 context_parts:
       │     【历史对话】+ 群聊语境 + 情绪 + 自我认知 + 口吻锚点
       │     （不带【长期记忆】）
       │
       └── session.run(...)
              ├── Persona 系统提示词决策树 §2 命中 → 直接角色化回应
              ├── 群聊路过 → 极简（一个词 / 沉默 <SILENCE>）
              ├── 私聊主人 → 可适当展开
              └── （可选）回复中嵌入 `<meme: 情绪>` 标记
```

### 3.3 关键提示词节选

```python
# F:/gsuid_core/gsuid_core/ai_core/persona/prompts.py:189-191
2. **是否是日常寒暄 / 纯情绪回应（不涉及任何信息查询）？**
   是 → 直接用角色语言回应，结束。
```

```python
# F:/gsuid_core/gsuid_core/ai_core/persona/prompts.py:514-518
## 沉默规则
如果你判断此时不应该回应（话题与你无关 / 群里别人在聊 / @别人等），
请只输出以下文本，不要输出其他任何内容：
<SILENCE>
```

### 3.4 落点

- 无工具调用，无 artifact 写入；
- `handle_ai_chat` 末尾异步调 `_update_persona_mood(...)` 更新群组级情绪。

---

## 4. 剧本 B：信息查询（游戏 / 知识 / 实时事实）

### 4.1 触发判据

- 用户问"X 是什么 / 怎么打 / 在哪获得 / 怎么搭配"等**事实型**问题；
- 决策树第 3 步 A 类：「答案就是工具输出本身」。

### 4.2 知识库 / 记忆 / Web 三段式

```
┌─────────────────────────────────────────────────────────────────┐
│  按提示词「工具调用规范」(prompts.py:376-384) 的顺序:           │
│                                                                  │
│  ① 先查工具列表，看是否有领域专业工具（如 send_stock_info）       │
│       命中 → 直接调（A 类）                                      │
│  ② 涉及专业 / 未知内容 → search_knowledge（知识库）              │
│  ③ 知识库无结果 + 需要实时信息 → web_search_tool（Web 搜索）     │
│  ④ 全无 → 角色化告知用户，严禁编造                              │
└─────────────────────────────────────────────────────────────────┘
```

### 4.3 三种"库"是什么

| 库 | 介质 | 写入方 | 读取入口 | 适用场景 |
|---|---|---|---|---|
| **知识库** | Qdrant 向量库 + 元数据 | 插件通过 RAG 启动期写入（`ai_core/rag/knowledge.py`） | `search_knowledge(query, category?, plugin?, score_threshold=0.45)` | 稳定的攻略 / 角色资料 / 物品 / FAQ |
| **长期记忆** | Qdrant + SQLAlchemy（`MemEpisode` / `Entity` / `Edge` / `Category`） | `observer` 监听消息流 → `ingestion/worker.py` 异步摄入 | `dual_route_retrieve(query, group_id, user_id)`（System-1 向量 + System-2 图遍历 + Reranker） | 对话回溯、个人偏好、群内事件、关系图谱 |
| **Web** | Tavily / Exa / MCP 三选一 | 实时调用 | `web_search_tool(query)` / `web_fetch_tool(url)` | 时效信息、知识库未覆盖的问题 |

### 4.4 知识库工具签名

```python
# F:/gsuid_core/gsuid_core/ai_core/buildin_tools/rag_search.py:17
@ai_tools(category="buildin")
async def search_knowledge(
    ctx: RunContext[ToolContext],
    query: str,
    category: Optional[str] = None,
    plugin: Optional[str] = None,
    limit: int = 10,
    score_threshold: float = 0.45,
) -> str:
    """
    检索知识库内容
    当需要查询专业知识、游戏攻略、角色资料、技能效果、物品信息等时使用。
    """
```

### 4.5 记忆双路检索结构

```python
# F:/gsuid_core/gsuid_core/ai_core/memory/retrieval/dual_route.py:260
async def dual_route_retrieve(
    query: str, user_id: str, group_id: Optional[str] = None,
    top_k: int = 20, enable_system2: bool = True, enable_user_global: bool = True,
) -> MemoryContext:
    """
    System-1 (向量相似度) + System-2 (分层图遍历) 真并行 asyncio.gather
    群组 scope + 用户全局 scope 同时检索，最后 Reranker 重排
    """
```

注入文本形如：

```
【核心事实 - 与当前问题相关】
• 用户偏好被叫"老板"
• 用户上周提到要买 RTX 5090
【语义类目摘要】
• [L2] 硬件采购: 集中讨论显卡 / 主板选型
【相关对话片段】
[05-22 21:31] 用户: 显卡到底买不买 50 系
```

### 4.6 流程图

```
intent ∈ {"问答", "工具"} 且非寒暄
   │
   ├── handle_ai 在 §1 流水线已注入【长期记忆】+【当前群聊语境】
   │     ↑ 主人格读到时已经"自带背景知识"
   │
   ├── 主 Agent 决策（prompts.py:181-203 决策树 §3）
   │     ├── 工具列表有领域工具 → 直接调（A 类终结）
   │     └── 无 → search_knowledge(query)
   │             ├── 命中 → 角色化转译输出
   │             └── 0 命中且需实时 → web_search_tool(query)
   │                     └── 仍 0 → 角色化「找不到」
   │
   └── send_chat_result()
```

### 4.7 关键边界

- **知识库**是恒定的资料库，**不能**回答"我自己的数据"；
- 涉及"**我的** / **本群的**"等用户私有数据 → 必走 §5（单工具）或 §13；
- 知识库命中后**不要**再 `web_search` 兜底——避免冲突结论。

---

## 5. 剧本 C：单工具任务（A 类：工具输出即答案）

### 5.1 触发判据

- 决策树 §3 二选一回答「A：工具输出本身就是答案」；
- 典型：`我的好感度多少`、`我的自选股`、`NVDA 今天涨多少`、`查一下天气`。

### 5.2 流程

```
gs_agent._execute_run 装配工具时三层叠加:
  ① 保底池: get_main_agent_tools() ← self + buildin 分类，无条件全部加载
  ② 语境池: group_id → get_scope_context_tags() → get_tools_by_context_tags(max=8)
  ③ 查询池: search_tools(query, limit=6, non_category=["self","buildin"])
  附加池上限 MAX_EXTRA_TOOLS = 12

→ 主 Agent 在工具列表里能直接看到 by_trigger 工具（插件通过 to_ai 注册）
→ 单工具调用 → 工具内部 MockBot 拦截 send() → 资源 ID（如 img_a1b2c3d4）
→ 主 Agent 拿到工具返回 → send_message_by_ai(image_id=...) 把图发出去
```

### 5.3 关键源码

```python
# F:/gsuid_core/gsuid_core/ai_core/gs_agent.py:659-708
core_tools = await get_main_agent_tools()
core_names = {t.name for t in core_tools}
extra_tools: ToolList = []

if ev is not None and ev.group_id:
    scope_key = make_scope_key(ScopeType.GROUP, str(ev.group_id))
    ctx_tags = await get_scope_context_tags(scope_key)
    if ctx_tags:
        ctx_tools = get_tools_by_context_tags(ctx_tags, max_count=8)
        extra_tools += ctx_tools

if qy:
    extra_tools += await search_tools(
        query=qy, limit=6,
        non_category=["self", "buildin"],
    )

MAX_EXTRA_TOOLS = 12
tools = core_tools + deduped_extra[:MAX_EXTRA_TOOLS]
```

### 5.4 trigger_bridge 工作流

插件 `@sv.on_command(..., to_ai="...")` 自动把触发器函数包装为 AI 工具：

```
AI → 调触发器工具(text="证券ETF")
   ├── 权限检查（user_pm ≤ sv.pm）→ 不足 → 返回错误文本，AI 角色化告知
   ├── 通过 → MockBot 拦截 bot.send(im) → RM.register(im) → "img_a1b2c3d4"
   └── 工具返回 "查询完成\n[资源ID: img_a1b2c3d4，请调用 send_message_by_ai]"
AI → send_message_by_ai(image_id="img_a1b2c3d4")
   └── RM.get() → real_bot.send() → 图发出
```

### 5.5 落点

- 直接 `send_chat_result()` 把消息（含图片资源）发回会话；
- 不写 artifact，不开 Kanban 任务树，不写 `record_*`。

---

## 6. 剧本 D：追问溯源（问"为什么/基于什么"）

### 6.1 触发判据（决策树 §3.6）

- 主人格之前已经通过 Kanban 任务树交付过某个数据 / 决策；
- 主人继续问 `"你为什么选 X"` / `"凭什么这样推荐"` / `"那 30% 是怎么算的"`。

### 6.2 强制路径

```python
# F:/gsuid_core/gsuid_core/ai_core/persona/prompts.py:349-357
3.6 主人是否在追问"为什么/基于什么/怎么得出"某个先前 Kanban 任务结果中的具体细节？
   是 → **必须先调用 `artifact_get_recent`** 取回该任务树最近一份 artifact 的
        完整原文，再用角色口吻把原文里的真实理由转告主人；若需要逐份查阅，
        先用 `artifact_list` 列出再用 `artifact_get` 取具体 `res_xxx`。
        **严禁**自己重新 `web_search` / `search_knowledge` 拼凑一个解释。
        原文里若没有理由，就老实告诉主人当时代理也没写明。
```

### 6.3 流程

```
主人提"你为什么 X" + handle_ai 已注入【你正在为对方推进的 Kanban 任务】
   │
   ├── 主 Agent 识别任务编号 / 任务名
   ├── artifact_get_recent(task_ref="任务#3" 或自然语言)
   │     ↓
   │   读取 root_task_id 下最近一份 artifact
   │   payload_inline ≤ 4KB 直接返回；> 4KB 落盘读 payload_path
   │     ↓
   │   带 _format_artifact() 元数据返回主人格
   ├── （可选）artifact_list / artifact_get res_xxxxxx 进一步追问
   └── 角色口吻转告主人「当时代理是这么说的：……」
```

### 6.4 跨树读取边界

```
artifact_get / artifact_get_recent 强校验 plan_ctx.root_task_id 一致
→ 不允许跨 root_task_id 读取，防数据泄露
```

---

## 7. 剧本 E：专业域问题（B 类：需委派能力代理）

### 7.1 触发判据（决策树 §3.1）

满足全部条件：

1. 用户问题主题落入某已注册 `capability_agent_profile.when_to_use`；
2. 答案需要"组合多工具数据"或"工具数据 + 判断/总结/建议/推荐/评估"；
3. 用户没有明确说"你直接查"。

例：`点评一下华泰证券` / `这个值不值得买` / `帮我分析一下我的伤害循环`。

### 7.2 强制委派路径

```python
# F:/gsuid_core/gsuid_core/ai_core/persona/prompts.py:204-251 节选
3.1 专业域强制委派（反"工具池单点采摘"陷阱）
    第 1 步 · 识别专业域：用户问题主题是否落入已注册画像的 when_to_use？
    第 2 步 · 判定输出形态：
       - 单点查询（A 类）→ 主人格可直接调，再角色口吻包装
       - 需 ≥2 工具组合 / 加判断 → **必须** create_subagent(agent_profile=...)

    禁止：
      ① 自己拼一两个碎片工具 + web_search + 历史对话缝答案
      ② 用合规当借口装作执行了（"我不专业 / 这不能乱说"）
      ③ 从更早回合的工具 stdout 里捡数据当本轮答案
```

### 7.3 流程

```
主人格识别专业域 → create_subagent(agent_profile="stock_agent", task=...)
       │
       ▼
buildin_tools/subagent.py:48-63
       │
       ├── resolve_profile("stock_agent") → 注册表查 profile_id
       ├── run_capability_agent(profile_id, task, ev, bot, session_id_suffix)
       │     │
       │     ▼
       │   ┌────────────────────────────────────────────────────┐
       │   │ capability_agents/runner.py                         │
       │   │   ① 按 profile.tool_names 装配显式白名单            │
       │   │   ② _ALWAYS_TOOLS 永远追加:                         │
       │   │      artifact_put / artifact_get / artifact_list   │
       │   │      state_* / search_knowledge / web_*            │
       │   │   ③ create_by="CapabilityAgent" 无人格 Agent        │
       │   │   ④ return_mode="return" 不直接发给用户            │
       │   │   ⑤ 失败 → f"{CAPABILITY_AGENT_ERROR_PREFIX}: {e}" │
       │   └────────────────────────────────────────────────────┘
       │
       └── 主人格拿到代理结果 → 角色口吻转译 → send_message_by_ai
```

### 7.4 内置 5 个能力代理（profiles.py:249-405）

| profile_id | display_name | 核心工具白名单 |
|---|---|---|
| `research_agent` | 调研助手 | 空白名单（按 task 文本向量检索装配）+ `_ALWAYS_TOOLS` |
| `code_agent` | 代码助手 | `list_directory` / `read_file_content` / `write_file_content` / `diff_file_content` / `execute_file` / `execute_shell_command` / `render_html_to_image` / `render_markdown_to_image` / `get_current_date` / `state_*` / `record_*` |
| `internal_reporter` | 内部数据报告员 | `query_user_memory` / `query_user_favorability` / `record_get` / `record_list` / `record_summary` / `query_scheduled_task` / `list_scheduled_tasks` / `render_markdown_to_image` |
| `memory_curator` | 记忆管家 | `update_self_note` / `query_user_memory` / `get_current_date` |
| `scheduler_assistant` | 日程助手 | `add_once_task` / `add_interval_task` / `list_scheduled_tasks` / `query_scheduled_task` / `modify_scheduled_task` / `cancel_scheduled_task` / `pause_scheduled_task` / `resume_scheduled_task` |

> **业务画像由插件注册**——例：`SayuStock` 注册 `stock_agent`。框架未挂载
> 该域专业工具时，evaluator 会返回 `covered=false`，主人格按 §7.5 第 ②
> 条诚实告知主人。

### 7.5 失败处理

```
covered=false（评估代理判定缺能力）
   ├── ① 委派给已注册的专业代理（如果存在）
   ├── ② 诚实回："框架没有挂载这个域的专业工具，建议安装对应插件再做"
   └── ③ ❌ 禁止合规敷衍 / 拼凑答案 / 历史回溯捡数据
```

---

## 8. 剧本 F：复杂多步任务（Kanban 任务树）

### 8.1 触发判据（决策树 §3.5）

满足任一即走：

- 单一画像跑不动；要 2~N 个不同 `agent_profile` 接力 / 并行；
- 任务结论依赖多源数据汇总（先爬数据 → 再分析 → 出周报海报）；
- 用户要求"持续地 / 每天 / 自主地"做某事且单步搞不定；
- 虚拟盘 / 模拟交易 / 持续追踪 / N 元让你管理 / N 天后考察（→ §10）。

### 8.2 标准 6 步流程

```
① evaluate_agent_mesh_capability(user_goal)
       │
       │ 内部跑一次性无记忆 capability_evaluator
       │ 输出 JSON: {covered, missing_capabilities, suggested_subtasks, risk_notes}
       │ 15 分钟内缓存，按 (owner, goal前200字) 严格校验
       ▼
② covered=false → 角色化告知缺能力，结束
   covered=true → 继续 ③
       │
       ▼
③ register_kanban_task(
       goal=...,
       subtasks=[{description, agent_profile, depends_on, params_hint}, ...],
       broadcast_to_group=False,
       recurring_trigger=None,      # 一次性任务，§10 才用周期
       recurring_until=None,
   )
       │
       │ 落库 → AIAgentTask 根 + N 子节点（node_kind="root"/"subtask"）
       │ depends_on 索引回填为兄弟子任务 id
       │ 自动 kick_root → kanban_executor.execute_ready_tasks
       ▼
④ kanban_executor._run_one_task_node(root, child)
       ├── 取节点锁 asyncio.Lock + 条件 SQL 抢 pending→running
       ├── ensure_workspace → 绑定 PlanRunContext
       ├── _collect_upstream_artifacts → 拼装 prompt
       ├── run_capability_agent(profile_id, task, ev, bot)
       ├── 终态 → mark_subtask_completed/failed
       └── _persona_relay → 主人格口吻转告主人
       │
       ▼
⑤ 失败分支三选一：
   - respawn_subtask（≤3 次自动转 waiting_approval）
   - 等主人 webconsole 或对话审批 → respond_subtask_approval
   - fail_task_tree（明确终结）
       │
       ▼
⑥ 主人追问 → §6 artifact_get_recent
```

### 8.3 关键工具签名

```python
# F:/gsuid_core/gsuid_core/ai_core/planning/kanban_tools.py:57
@ai_tools(category="self")          # ← 仅主人格保底
async def evaluate_agent_mesh_capability(
    ctx, user_goal: str, ...
) -> str: ...

# F:/gsuid_core/gsuid_core/ai_core/planning/kanban_tools.py:117
@ai_tools(category="buildin")       # ← 主+代理保底
async def register_kanban_task(
    ctx,
    goal: str,
    subtasks: List[KanbanSubtaskSpec],
    broadcast_to_group: bool = False,
    recurring_trigger: Optional[str] = None,
    recurring_until: Optional[str] = None,
) -> str:
    """
    ⚠️ 周期任务直接传 recurring_trigger，不要枚举 add_once_task。
    ⚠️ 单轮 add_once_task ≥3 次会撞 PER_TURN_ONCE_TASK_LIMIT=2 硬节流。
    """
```

### 8.4 Kanban 5 列状态机

| 列 | status | 含义 |
|---|---|---|
| target | `pending` | 未启动 / 等依赖 |
| progress | `running` | 已派给代理执行中 |
| Done | `completed` / `skipped` | 子任务完成 |
| Blocked | `paused` / `waiting_approval` | 暂停或等主人审批 |
| failed | `failed` / `cancelled` | 终结 |

> **Kanban 没有定时器**——纯事件驱动。需要"明天/每天/N 小时后"触发请走
> §9 `add_once_task` / `add_interval_task`，或 §10 `recurring_trigger`。

### 8.5 Artifact Workspace 沙盒

```
data/ai_core/artifacts/{root_task_id}/{task_id}/
    workspace/             ← 唯一可写目录（按 agent_profile 再分子目录）
    {artifact_id}/payload.<ext>  ← ≥4KB 大工件落盘

工具层强制：
  file_manager._get_safe_path → resolve_safe_path(req, fallback, ctx)
  command_executor 执行前覆盖 work_dir = workspace
  累计 3 次越界 → record_violation → mark_subtask_failed
```

---

## 9. 剧本 G：定时 / 周期任务 vs 周期复杂任务

### 9.1 四象限选择表（决策树 §3.4）

| 任务性质 | 简单（单步无决策） | 复杂（多步含决策/记账/复盘） |
|---|---|---|
| **一次性** | `add_once_task` | `add_once_task` 唤醒 → `register_kanban_task`（无 recurring） |
| **周期性** | `add_interval_task` | `register_kanban_task(recurring_trigger="cron:..." 或 "interval:N")` |

### 9.2 5 秒判别口诀

- 「最终要交付决策 / 分析 / 报告 / 累计统计」→ Kanban
- 「只是提醒主人 / 输出固定模板内容 / 单工具就完事」→ scheduled_task

### 9.3 反枚举铁律

```python
# F:/gsuid_core/gsuid_core/ai_core/buildin_tools/scheduler.py:51
PER_TURN_ONCE_TASK_LIMIT = 2     # 单轮 add_once_task ≥3 次直接拒绝
MAX_PENDING_TASKS_PER_USER = 20  # 单用户 pending 上限
MAX_EXECUTION_LIMIT = 150        # 单循环任务最大执行次数
MIN_INTERVAL_SECONDS = 300       # 最小间隔 5 分钟
```

### 9.4 简单定时任务流程

```
用户："明早 6:30 叫我起床"
       │
       ▼
主 Agent → add_once_task(
              run_time="2026-05-24 06:30:00",
              task_prompt="叫主人起床，附上当日天气",
          )
       │
       ├── 安全检查（PER_TURN_ONCE_TASK_LIMIT / MAX_PENDING / MIN_INTERVAL）
       ├── 落库 AIScheduledTask(task_type="once", status="pending")
       └── APScheduler add_job(date 触发器)
              │
              │ ……时间到……
              ▼
       scheduled_task/executor.py::execute_scheduled_task(task_id)
              ├── 重新 get_ai_session(ev) 加载 persona + session
              ├── session.run(user_message=task_prompt, ...)
              ├── statistics_manager.record_trigger(trigger_type="scheduled")
              └── bot.send(result) → 主人收到角色口吻消息
```

### 9.5 简单周期任务流程

```
用户："每小时帮我看一下股市行情"
       │
       ▼
主 Agent → add_interval_task(
              interval_value=1, interval_type="hours",
              task_prompt="查 A 股大盘指数并简评",
              max_executions=10,
          )
       │
       └── APScheduler interval 触发器，每次执行后 current_executions+=1
            current_executions >= max_executions → status="executed" 结束调度
```

### 9.6 周期复杂任务 → §10

含「决策 / 持仓 / 账本 / 流水 / 复盘 / 多步骤 if-then-else」一律走 §10。

---

## 10. 剧本 H：虚拟账户 / 模拟交易 / N 元让你管理 N 天

### 10.1 触发判据（决策树 §3.5 关键词）

`虚拟盘` / `模拟交易` / `模拟运营` / `给你 N 元让你管理` / `N 天后考察` →
强制走 Kanban + `recurring_trigger` + `record_*`。

### 10.2 反模式（禁止）

```
❌ add_interval_task(...) + state_set("portfolio", json.dumps({...}))
```

> 该结构既无法被 webconsole 看到、也无法在主人追问时按字段溯源、也不会
> 自动结算。

### 10.3 正确模板

```python
# 第 1 步：评估
evaluate_agent_mesh_capability(
    user_goal="给我 10w 虚拟盘，每开盘日每半小时看盘买卖，30 天后考察收益率"
)
# evaluator 按 §2 重写后判 covered=true，给出 recurring_trigger 建议

# 第 2 步：注册周期 Kanban 模板
register_kanban_task(
    goal="10w 虚拟盘 30 天试运营",
    subtasks=[
        {
            "description": "首次初始化账户与持仓表（用 record_put）",
            "agent_profile": "code_agent",
            "depends_on": [],
            "params_hint": {"principal": 100000},
        },
        {
            "description": "查行情→决定买卖→写流水→更新持仓",
            "agent_profile": "stock_agent",   # 由 SayuStock 插件注册
            "depends_on": [0],
            "params_hint": {},
        },
    ],
    recurring_trigger="cron:0,30 9-14 * * 1-5",   # 周一-五 9-14 时每整点+半点
    recurring_until="2026-06-21T15:00:00",        # 30 天后
)
```

### 10.4 框架内部行为

```
register_kanban_task(recurring_trigger=...) 触发:
  ① 创建模板根任务 (recurring_status="armed", template_root_id=None)
  ② recurring.schedule_template → aps.scheduler.add_job(replace_existing=True)
  ③ **不**立即 kick_root（模板根永远不被调度）
       │
       │ ……每次开火……
       ▼
  recurring._fire_template(template_root_id) 回调:
       ├── 校验 armed + 未过期
       ├── clone_tree_for_fire → 复制整棵模板树
       │      ① 新建实例根（template_root_id=<模板 id>，recurring_trigger=None）
       │      ② 顺序新建子任务节点，构建 tpl_child.id → new_child.id 映射
       │      ③ 重映射 dependency_task_ids
       │      ④ fire_count += 1
       └── kick_root(instance_root.id)
            └── 普通 Kanban 执行流（§8.2 第 ④ 步）
```

### 10.5 业务持久化用 record_*

```python
# 注册画像里的代理在子任务里这样写:
record_put("stock:account_<owner>",
           {"principal": 100000, "cash": 100000, "started_at": "..."})
record_put("stock:position_<owner>",
           {"code": "601688", "qty": 1000, "avg_cost": 18.5},
           record_id="601688")
# 流水追加（每次开火 +1 条）
record_put("stock:trade_log_<owner>",
           {"side": "buy", "code": "601688", "price": 18.5, "qty": 1000, "at": "..."})
# 30 天后主人问"结算"：
record_list("stock:trade_log_<owner>", order_by="-at", limit=20)
record_summary("stock:trade_log_<owner>", sum_field="cash_flow")
```

> 跨实例的业务数据由 `record_*` 维护，**与 Kanban 树本身的生命周期解耦**——
> 实例 completed 不影响 record 集合。

### 10.6 record_* vs state_* 取舍

| 场景 | 推荐 |
|---|---|
| 单字段、单值（计数、最近一次执行时间） | `state_set` / `state_get` |
| 顺序追加但无需查询单条 | `state_append` |
| 多条结构化记录、按 id 更新 / 按字段过滤 / 聚合 | `record_*` |
| ≥ 1 万条 / 集合 | 按时间 / 主键分片成多个 `record_*` 集合 |

---

## 11. 剧本 I：代码 / 脚本 / 数据处理 / 渲染

### 11.1 触发判据

- 关键词：`写段代码 / 跑脚本 / 绘图 / PIL / matplotlib / 渲染 / 导出图片 /
  csv / json / 批处理 / 格式转换`；
- 任务最终需要一个"跑出来的产物"（图 / 文件 / 数据）。

### 11.2 流程

```
主人格 → create_subagent(agent_profile="code_agent", task=...)
       │
       ▼
run_capability_agent("code_agent", task, ev, bot)
       │
       ├── 装配白名单工具（无人格）：
       │     list_directory / read_file_content / write_file_content
       │     diff_file_content / execute_file / execute_shell_command
       │     render_html_to_image / render_markdown_to_image
       │     get_current_date / state_* / record_*
       │
       ├── 系统提示词强制 Plan-and-Solve：
       │     ① 先输出 <TODO_LIST>，拆 2~5 步
       │     ② 改文件前先 read_file_content，写完用 diff_file_content 自查
       │     ③ 沙盒脚本用 execute_file，一次性命令用 execute_shell_command
       │     ④ Windows: subprocess.run + asyncio.to_thread（避 SelectorEventLoop）
       │     ⑤ 跨平台路径用 pathlib.Path
       │     ⑥ Kanban 子任务执行时唯一可写目录 = Artifact Workspace
       │     ⑦ 高风险动作（rm -rf / 覆盖 git push）一律不自己执行
       │
       ├── 端到端跑出产物 → artifact_put 登记 res 句柄
       └── 主人格转译 → send_message_by_ai 把图发出
```

### 11.3 Windows subprocess 兼容（重要）

```python
# F:/gsuid_core/gsuid_core/ai_core/buildin_tools/command_executor.py
_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    stdout_bytes, returncode = await _run_subprocess_in_thread(...)
else:
    stdout_bytes, returncode = await _run_subprocess_async(...)
```

> Windows 启动时强制 SelectorEventLoop 以避免 ProactorEventLoop 关闭
> socket 时的 `InvalidStateError`，但 SelectorEventLoop **不支持子进程**——
> 必须用 `subprocess.run + asyncio.to_thread` 绕开。

### 11.4 落点

- 产物：workspace 文件 + artifact 登记；
- 回告：主人格用 `_persona_relay` 转译；
- 高风险动作：在交付摘要里显式列「需要主人决策的动作」，主人格转告主人定夺。

---

## 12. 剧本 J：数据分析 / 复盘 / 周报

### 12.1 触发判据

- 关键词：`周报 / 月报 / 复盘 / 对比 / 趋势 / 排名 / 累计`；
- 数据来源是**内部**（记忆、好感度、定时任务记录、record_* 集合）——不是
  外网。

### 12.2 流程

```
主人格 → create_subagent(agent_profile="internal_reporter", task=...)
       │
       ▼
run_capability_agent("internal_reporter", task, ev, bot)
       │
       ├── 装配白名单（与 research_agent 区别：无 web_* 工具）：
       │     query_user_memory / query_user_favorability
       │     record_get / record_list / record_summary
       │     query_scheduled_task / list_scheduled_tasks
       │     render_markdown_to_image / get_current_date
       │
       ├── 系统提示词：
       │     ① 第一步永远是**把数据拿到手**（query_user_memory / record_list）
       │     ② 结论必须能由工具数据复现
       │     ③ 强专业域无专业工具时显式告诉主人缺什么
       │     ④ 渲染：用 render_markdown_to_image 出图文报告
       │
       └── artifact_put 登记 → 主人格转译 → 发图
```

### 12.3 跨多步周报（如月报 + 海报）

如果"先爬数据 → 再分析 → 出周报海报"涉及 ≥2 个画像 →
**升级到 Kanban**（§8），由 `internal_reporter` + `code_agent` 接力。

---

## 13. 剧本 K：自我认知问题（你是谁 / 你能做什么 / 我之前说过什么）

### 13.1 三类问题路由

| 问题 | 工具 | 来源 |
|---|---|---|
| 「你是谁 / 你能做什么 / 你主人是谁」 | `get_self_info()` | `buildin_tools/self_info.py:92` |
| 「我之前说过 X 吗 / 还记得我喜欢什么吗」 | `query_user_memory()` + 流水线已注入的【长期记忆】 | `buildin_tools/database_query.py` + `dual_route_retrieve` |
| 「你答应过我什么 / 你之前怎么想的」 | self_cognition 注入（已在流水线 step 10）+ 命中 `_SELF_RECALL_RE` 时 `retrieve_self_episodes()` 召回自我情景 | `handle_ai.py:335` |

### 13.2 get_self_info 输出结构

```python
# F:/gsuid_core/gsuid_core/ai_core/buildin_tools/self_info.py:92
@ai_tools(category="buildin")
async def get_self_info(ctx: RunContext[ToolContext]) -> str:
    """
    返回身份、运行框架、能力边界（可用工具）、主人、当前会话语境等。
    """
```

输出节选：

```
【自我认知档案】
身份基本信息:
  Persona名称: 早柚
  运行框架: GsCore AI Core（PydanticAI Agent 架构）
我能做到的事（工具能力边界）:
  [核心能力] create_subagent、send_message_by_ai、...
  [基础工具] search_knowledge、web_search_tool、...
  [常用工具] send_meme、list_scheduled_tasks、...
我的主人（最高权限用户）: 12345678
当前会话:
  所在场景: 群聊 789012
  群组语境: 原神、星穹铁道、申鹤强度
```

### 13.3 self_cognition 自动注入

每轮对话 handle_ai 已经在 context_parts 里拼好：

```python
# F:/gsuid_core/gsuid_core/ai_core/self_cognition.py:240
async def build_self_cognition_context(bot_id, user_id, favorability) -> str:
    """
    内容 = 演化层 self_model 摘要 + 当前对话者关系 + 能力域清单
    不写入 persona 目录、不进 system_prompt——由 handle_ai 拼进 user message 侧
    """
```

注入示例：

```
【关于我自己（本轮动态注入，仅供参考）】
我的承诺: 答应每天 6 点提醒主人喝水；答应不在群里聊政治
我学到的偏好: 用户喜欢被叫"老板"；不喜欢 emoji
反复出现的话题: 原神、炒股、番剧
我最近的反思: 上次任务卡在没读 stderr，下次先读再修
当前对话者是我的主人（最高信任）。
我的能力域: 插件功能、子任务工具、基础工具、长期任务编排、核心能力
```

### 13.4 主动写入承诺 / 偏好 / 反思

```python
# 主人："以后叫我老板"
update_self_note(
    content="用户希望被称为'老板'",
    note_type="preference",  # preference / commitment / reflection
)
# 字段映射：
#   preference  → preferences_learned
#   commitment  → commitments
#   reflection  → self_notes
```

写入限流（`self_cognition.py`）：

- 单条 ≤200 字符；
- 同条文本去重（去掉旧的 append 到末尾）；
- 每字段最多 20 条，超出丢最早一条。

---

## 14. 群聊 vs 私聊：路径回环差异

### 14.1 Session ID

```python
# 群聊
session_id = f"{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}"
# 私聊
session_id = f"{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}"
```

> 群聊 Session **不**绑定 user_id——同群所有人共享一个 AI 会话，AI 才能
> "记住"群里发生的事而不是和每个人 1v1 单聊。

### 14.2 历史记录存储

| 场景 | `HistoryManager` 的 `storage_event` key | 效果 |
|---|---|---|
| 群聊 | user_id 置空，按 group_id+bot 唯一 | 同群所有用户消息共享同一 deque |
| 私聊 | 保留 user_id | 一对一隔离 |

每个 Session：`deque(maxlen=40)` + 单 session Token 上限
`MAX_HISTORY_TOKENS=160000`，AI 历史 30 条。

### 14.3 触发差异

| 模式 | 群聊触发 | 私聊触发 |
|---|---|---|
| 提及应答 | `event.is_tome=True`（@机器人）或命中 persona 配置 `keywords` | 任意消息都触发（不需要 @） |
| 定时巡检 | 启用 + `_pre_check_session` 4 条规则过滤 → LLM 决策是否开口 | 启用即对每个 1v1 私聊跑（同样有前置规则） |

### 14.4 极简原则按场景分级

```python
# F:/gsuid_core/gsuid_core/ai_core/persona/prompts.py:163-175
- 群聊路过 / 闲聊：能用一个词绝不用一句话，不使用严谨标点，尽量不换行。
- 私聊主人 / 被直接提问 / 汇报任务结果：可适当展开把事情说清楚，但保持角色口吻。
```

### 14.5 群组语境注入（仅群聊）

handle_ai 在群聊里额外注入【当前群聊语境】：

```python
# F:/gsuid_core/gsuid_core/ai_core/memory/group_profile.py:163
async def format_context_injection(scope_key: str, max_chars: int = 400) -> str:
    """
    【当前群聊语境】
    主要话题: 原神、星穹铁道、申鹤强度
    语境说明（群内特有词汇）:
      - "深渊" = 螺旋深渊
      - "狗熊" = 黄家三爷
    可能的别名歧义（请按上下文判断具体指代）:
      - "夜兰" 可能指: 夜兰（原神）、夜兰大人（外号）
    """
```

私聊场景不注入。

### 14.6 工具池差异

```python
# F:/gsuid_core/gsuid_core/ai_core/gs_agent.py:668
if ev is not None and ev.group_id:    # ← 只有群聊触发语境工具池
    scope_key = make_scope_key(ScopeType.GROUP, str(ev.group_id))
    ctx_tags = await get_scope_context_tags(scope_key)
    if ctx_tags:
        ctx_tools = get_tools_by_context_tags(ctx_tags, max_count=8)
        extra_tools += ctx_tools
```

群聊里如果群标签 = ["原神"]，会自动加载所有声明了 `context_tags=["原神"]`
的插件工具；私聊不走这一步。

### 14.7 心跳定时巡检（仅群聊 / 私聊均可启）

```
heartbeat/inspector.py 定时（5/10/15/30/60 分钟）
   ├── _pre_check_session(event):
   │     ① 无历史 → 跳过
   │     ② 最后消息来自 AI → 跳过
   │     ③ 群已 1+ 小时不活跃 → 跳过
   │     ④ AI 最近 5 条已发言 → 跳过
   │
   ├── _inspect_session_with_semaphore(event, persona_name)
   │     └── Semaphore(5) 控制并发，asyncio.wait_for timeout=300
   │
   └── decision.py:
       ① DECISION_PROMPT → {should_speak, mood, context_hook}
       ② should_speak=true → PROACTIVE_MESSAGE_PROMPT → 生成消息
       ③ 通过 _Bot.target_send 发出，metadata={"proactive": True}
```

---

## 15. 历史记录 vs 调用工具：差异速查

### 15.1 两者的本质区别

| 维度 | 历史记录（History） | 调用工具（Tool Call） |
|---|---|---|
| 存储 | `HistoryManager` deque + AI session `ModelMessage` 列表 | 实时执行，结果落 `ToolReturnPart` 进当前对话 history |
| 注入方式 | `format_history_for_agent` 拼进 `rag_context`（user message 侧） | PydanticAI 把 `ToolCallPart` / `ToolReturnPart` 加入 message_history |
| 时效性 | 过去（已发生） | 当下（实时查） |
| 上限 | session deque(maxlen=40) + token 160k + AI 30 条 + Agent 内部 50 条 | 单轮 `multi_agent_lenth` 次（默认 25） |
| 用途 | 让 AI 知道"刚才聊了什么" | 让 AI 拿到"用户当下需要的信息" |

### 15.2 决策树里的体现

```python
# F:/gsuid_core/gsuid_core/ai_core/persona/prompts.py:193-203
3. 当前工具列表是否有能直接处理此任务的工具？
   - A 类（输出即答案）→ 直接调用对应工具
   - B 类（答案要被推理出来）→ 进入 §3.1 判定流程
   - 完全没有相关工具 → 用 web_search / search_knowledge 兜底；
     若 B 类但无专业代理覆盖 → §3.1 ③ 诚实告知
```

历史记录**只是背景**，本轮要做"个体级决策 / 推荐 / 评估"必须**重新跑工具
流程**（决策树 §3.1 第 ③ 条铁律）。

### 15.3 历史截断的"自洽"保证

```python
# F:/gsuid_core/gsuid_core/ai_core/gs_agent.py:183
def _truncate_history_with_tool_safety(history, max_history):
    """
    保留消息中 ToolCallPart 和 ToolReturnPart 完全配对。
    PydanticAI 的 RetryPromptPart（tool_name 非空时）也算工具结果型消息，
    必须有配对的 ToolCallPart。
    截断不安全时向前移动截断点，直到所有保留的 return 都有对应的 call。
    """
```

不自洽会触发 OpenAI `400 ... tool id not found`。

### 15.4 "强制总结"兜底（达 UsageLimitExceeded 时）

```python
# F:/gsuid_core/gsuid_core/ai_core/gs_agent.py:113
def _extract_run_context(history, max_fact_len=2000) -> str:
    """
    按轮次提取 LLM 中间推理（TextPart）+ 工具返回（ToolReturnPart），
    组织成"第N轮 → 工具调用 → 返回"结构。
    """

# v4 最终方案：
# 1. 记录原始用户问题 _last_user_question
# 2. 从 history 抽事实 + 推理打包成一条 final_message
# 3. message_history=[] + tools=[] 起一个全新 fallback Agent
# 4. UsageLimits(request_limit=1) 强制一轮收尾
```

避免"工具调用模式的行为惯性"——LLM 以"全新会话"姿态回答用户原问题，
不会回去复盘工具调用过程。

### 15.5 历史 vs 工具的协同例子

| 用户原话 | 历史里有什么 | 工具调用 |
|---|---|---|
| 「刚才那张图保存在哪了？」 | 上一轮 `res_xxxxxx` | 不需要新工具，直接答历史里的 res |
| 「再画一张类似的」 | 上一轮调过 `code_agent` | 必须重新 `create_subagent(agent_profile="code_agent", task=...)` |
| 「华泰今天涨多少」 | 历史无 | `send_stock_info("华泰")` |
| 「华泰刚才那个数据准吗 / 怎么算的」 | 历史有上一份 artifact | `artifact_get_recent` 取原文 |
| 「你昨天答应我买点红薯了吗」 | 长期记忆 + self_cognition 已注入 | 不需要工具，直接看注入文本 |

---

## 附：完整决策树速查（必读）

```
收到消息
  │
  ├─ 1. 合规红线？ → 角色化拒绝
  │
  ├─ 2. 寒暄 / 纯情绪？ → 角色化回应（§3 剧本 A）
  │
  ├─ 3. 工具列表有可处理工具？
  │   ├─ A 类（输出即答案）→ 直接调（§5 剧本 C）
  │   ├─ B 类（要推理）→ §3.1
  │   └─ 完全无 → search_knowledge / web_search（§4 剧本 B）
  │
  ├─ 3.1 落在专业代理 when_to_use？
  │   └─ 是 + B 类 → create_subagent(agent_profile=...)
  │       （§7 剧本 E，对应 code §11 / data §12 / 金融 etc.）
  │
  ├─ 3.4 含时间触发？
  │   └─ 四象限选 scheduled_task vs Kanban
  │       （§9 剧本 G / §10 剧本 H）
  │
  ├─ 3.5 多能力协作 / 多源汇总 / 持续做某事？
  │   └─ ① evaluate_agent_mesh_capability
  │       ② covered=true → register_kanban_task（§8 剧本 F）
  │       ③ 含周期 → recurring_trigger（§10 剧本 H）
  │
  ├─ 3.6 追问 Kanban 任务结果细节？
  │   └─ artifact_get_recent（§6 剧本 D）
  │
  └─ 4. 全部不满足 → 角色化告知用户，严禁编造
```

---

## 关联文档

| 主题 | 文档 |
|---|---|
| AI 触发链 / 模块全景 | [`AI_TRIGGER_FLOW.md`](AI_TRIGGER_FLOW.md) |
| 能力代理与基础设施 | [`AGENT_CAPABILITY_AGENT_MERGED_20260521.md`](AGENT_CAPABILITY_AGENT_MERGED_20260521.md) |
| Kanban 任务树实施 | [`AGENT_MESH_KANBAN_IMPLEMENTATION_20260522.md`](AGENT_MESH_KANBAN_IMPLEMENTATION_20260522.md) |
| 插件接入 API | [`ai_core_api_for_plugins.md`](ai_core_api_for_plugins.md) |
| Kanban WebAPI | [`../gsuid_core/webconsole/docs/35-kanban.md`](../gsuid_core/webconsole/docs/35-kanban.md) |
| Artifact Hub WebAPI | [`../gsuid_core/webconsole/docs/36-artifacts.md`](../gsuid_core/webconsole/docs/36-artifacts.md) |
| 能力代理画像 WebAPI | [`../gsuid_core/webconsole/docs/34-capability-agents.md`](../gsuid_core/webconsole/docs/34-capability-agents.md) |
