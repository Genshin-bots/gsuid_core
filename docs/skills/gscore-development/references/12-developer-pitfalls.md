# 十二、已知坑与开发注意事项

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[十一、统计 / 控制台 / 数据库 / 帮助](./11-statistics-webconsole-database.md)

**这一章是别人替你踩过的坑。改框架前过一遍能省大量返工。** 每条都标注了"为什么会踩、踩了什么
后果、正确做法"。源码已修复历史缺陷，这里登记的是**改动时不能破坏的不变量**。

## 12.1 改动前必读的代码红线（`docs/LLM.md`）

改任何 GsCore 代码前必读仓库根目录 [`docs/LLM.md`](../../../LLM.md)。绝对红线：

1. **禁止 `try/except` 兜底**吞类型/属性错误——从类型标注和逻辑上解决。
   - 唯一例外：解析 LLM 自由文本 / Qdrant payload / 加密报文等**不可信外部输入**处，为"绝不打断
     主链路"按需保留（`auth_crypto`、`extract_json`、记忆抽取、Heartbeat 决策、软触发沉默门等）。
2. **禁止 `cast()`** ——用 `Union` + `isinstance` 守卫或调整签名。
3. **禁止 `type: ignore`** 掩盖自身代码问题（仅第三方库类型标注错误且无法绕过时用）。
4. **禁止 `getattr/dict.get` 兜底语法** ——用 `TypedDict`/dataclass 明确类型 + `isinstance` 守卫 +
   `key in d` 显式取值。结构化数据用 `TypedDict`/`@dataclass`/`NamedTuple`。
5. **完全类型提示** ——所有函数参数/返回值都有注解。
6. **全异步** ——可能阻塞的都 `async def`；同步 CPU 用 `@to_thread`/专用线程池。

> 注释规范：`#` 注释**不超过两行、每行 ≤88 字**，精简直白，用最精确的注释给指导，而不是长篇
> 大论。详见 `docs/LLM.md`。

## 12.2 单进程 / 进程内存状态（多实例隐患）

Core 是**单进程单事件循环**。以下状态都是**进程内存 / 单进程密钥**，多进程/多实例水平扩展时
**不共享**：

| 状态 | 模块 |
|------|------|
| 免唤醒续聊窗口 | `followup_window.py` |
| 偏好记忆工具轨迹 + 即时 flush 去抖 | `memory/ingestion/tool_trace.py` |
| 认证密钥库 | `webconsole/auth_crypto.py`（`auth_keystore`） |
| Bot 实例 / 发送队列 | `gss.active_bot` / `_Bot._send_queue` |
| AI Session 注册表 | `AISessionRegistry._ai_sessions` |
| 记忆观察队列 | `memory/observer.py` 的 `queue.Queue` |
| 各类 Semaphore / mtime 缓存 | 全局模块变量 |

> **当前单进程事件循环模型下符合预期**。若将来要水平扩展，这些都需要外部化（Redis/DB/共享
> 存储）。现在写新状态时，默认"只有一个进程"，但心里要清楚这是个约束。

## 12.3 事件循环 / Windows 平台坑

### 🔴 IngestionWorker 必须跑在主事件循环（不要再尝试独立线程）

曾把 `IngestionWorker` 改成独立线程的事件循环，动机"避免 LLM 调用阻塞主循环"是**误判**——
LLM 调用是 `await` 的纯网络 I/O，等待期间不占用事件循环。双循环架构与主循环共享三个**循环
亲和资源**：pydantic_ai 缓存的 `httpx.AsyncClient`、全局 SQLAlchemy 引擎、全局
`AsyncQdrantClient`。批次超时的跨循环取消会**击穿主循环 Proactor 内核**（WinError 995 →
InvalidStateError → 主循环崩溃 → **WS 全线断连**）。现已回归主循环 `asyncio.create_task`。
**任何"把耗时 AI 任务搬去独立线程跑事件循环"的想法都要先验证它不碰这三个共享资源。**

### Windows SelectorEventLoop 不支持子进程

`core.py` 切到 `WindowsSelectorEventLoopPolicy` 规避 Proactor 关 socket 的 InvalidStateError。
代价：**SelectorEventLoop 不支持子进程**。任何跑 subprocess 的工具（`execute_shell_command` /
`execute_file`）必须分平台分支：Windows 走"同步 `subprocess.run` + `asyncio.to_thread`"，POSIX
走 `asyncio.create_subprocess_exec`，timeout 转 `asyncio.TimeoutError` 保持上层契约。

## 12.4 AI 总开关（D-21）必须贯穿

AI 关闭时**不该有任何 AI 逻辑在跑**。改 AI 模块时务必保留：

- 每个 `_init_*`（`_INIT_STEPS`）开头读 `ai_config.get_config("enable")`，关则 `return`。
- `scheduled_task/executor.py`、`heartbeat/inspector.py` 执行前查总开关。
- `create_core_tables` 跳过 AI 表创建。
- `handle_ai` 里 `enable_ai` **函数内动态读取**（不要缓存进模块级常量，否则切开关要重启）。

## 12.5 Bot 类型混淆（D-5）

- `gss.active_bot` 的 key 是 `WS_BOT_ID`（WS 连接 ID）**不是平台 `bot_id`**。后台取 `_Bot` 走
  `gss.active_bot[event.WS_BOT_ID]` 三级查找（见 [§05](./05-bot-classes.md)）。
- 需要 `Bot` 的地方必须传 `Bot`（含 `_Bot`+`Event`）而非裸 `_Bot`，否则 `send()` 崩。

## 12.6 群聊上下文割裂（D-1）

Session ID 群聊**不含 user_id**（`…:group:{group_id}`），群内共享 Session+记忆。配套
`HistoryManager` 群聊时把 `storage_event.user_id` 置空。改 Session 标识时不能破坏这个不变量，
否则群里每个人又会各聊各的。

## 12.7 历史截断必须保护 ToolCall/ToolReturn 配对

`deque(maxlen=40)` 只按条数截断（**隐形 Token 爆炸**：50 条长文可达 25 万字）。`GsCoreAIAgent`
用 `_truncate_history_with_tool_safety()` 按 Token 安全截断，并保证 `ToolCallPart` 与
`ToolReturnPart` **始终配对**，否则 pydantic-ai 报 "tool result's tool id not found"（400）。
另有 `_drop_orphan_tool_results` 自愈兜底（D-? "久聊必崩"）。改历史/截断逻辑时**必须**保留配对
保护，并把 `RetryPromptPart` 也纳入截断考虑。

## 12.8 强制总结偏离用户问题（D-20）

Agent 达 `UsageLimitExceeded`（思考轮数上限）时的 fallback 不能让 AI "自我总结思考过程"，必须
**直接回答用户原问题**。正确做法（`gs_agent.py` v4）：

- `_extract_run_context()` 按轮次提取"用户原问题 + 已知事实 + LLM 中间推理"打包成**一条干净
  消息**；`message_history=[]`（排除上一轮"工具调用模式"惯性）。
- fallback Agent `tools=[]`（从根源消除 schema 注入）、**不带** `deps_type/deps`、`retries=0`、
  `usage_limits=UsageLimits(request_limit=1)`。
- 错误处理一致性：有 `bot` 时 `bot.send()` 发最终错误并 `return ""`；无 `bot` 时返回字符串由
  调用方处理——**避免"安抚消息 + 错误消息"双发**。

**瞬时失败重试（核心回复请求）**：`_execute_run` 现为重试包装——单次执行落在 `_execute_run_once`，
网络/超时/5xx/529 等瞬时故障以异常冒泡，等 `_RUN_RETRY_DELAY`(3s) 后重试，至多 `_MAX_RUN_ATTEMPTS`(3)
次，全部失败才按异常类型记统计 + 返回 `执行出错: …`。每次重试复用**未被改写**的 `self.history`
（成功才 `extend`），从干净状态重跑；`download image` 自愈在重试前剥离过期远程图片。
`UsageLimitExceeded` 是**逻辑性**到顶（有 §12.8 兜底总结），在 `_execute_run_once` 内消化、
**不参与重试**——改这块勿把它并进通用 `except` 重试分支。

## 12.9 Heartbeat 并发雪崩（D-2）

巡检**必须**前置规则过滤（绝大多数会话不进 LLM）+ Semaphore(5) + 300s 超时。删掉前置过滤会
让 100 群 × 5 分钟 = 每 5 分钟 100 次并发 LLM（Rate Limit + Token 破产）。防刷屏靠
`metadata={"proactive": True}` + 最近 5 条检查。

## 12.10 续聊软触发与偏好记忆的成本（默认开）

- **续聊沉默门**：默认 30s 窗口内每条群消息触发一次沉默门 LLM 判定，群活跃时有额外开销。沉默门
  **默认偏沉默**：仅模型明确判定"接续/指向你"才放行；模型输出非 str / JSON 解析失败 / 缺
  `should_speak` 一律按沉默处理（不再回落放行），只有**真异常**（无历史 / 无人格 / LLM 调用崩溃）
  才放行交主 Agent 兜底。窗口/天花板按群活跃度观察调整。
  > 三道沉默关卡需口径一致：`run_reactive_gate`（门）/ `handle_ai` 软触发提示 / `prompts.py`
  > `## 沉默规则` 的"续聊场景"条款。历史上门放水 + 主 Agent 把续聊当"直接找你必须回应" → 几乎
  > 不沉默、一直输出。改其一务必同步另两处（见 [§04](./04-event-trigger-flow.md) §4.6）。
- **偏好记忆**：开箱启用工具轨迹 + 纠错探测 + **第二次蒸馏 LLM** + 置顶强约束注入。误抽偏好以
  强约束置顶可能过度约束工具调用（已有软停用 + salience 裁剪 + 精确能力域过滤兜底）。首次放量
  建议观察第二次蒸馏 Token 成本。

## 12.11 RF-Mem 阈值未标定即放量会退化（默认关）

`familiarity_theta_*` / `tau` 是论文英文模型经验值，中文本地模型通常需平移。回忆环强绑
`qdrant_provider=remote`（本地嵌入式 Qdrant O(N) 暴力扫，多轮成倍放大）。**保持默认关，标定后
再放量**。

## 12.12 `extract_json_from_text` 的健壮性（2026-06-15 重写）

`ai_core/utils.py` 由"正则 + repair"重写为"**括号配平** + repair"：跳过字符串字面量内括号与
转义、正确处理嵌套、先严后宽（合法 JSON 直接 `json.loads` 不经 repair）。被记忆抽取、Heartbeat
决策、软触发沉默门、偏好蒸馏**多处复用**（32 条回归测试 `tests/test_extract_json.py`）。

> 改任何"解析 LLM 返回的 JSON"的地方，优先复用 `extract_json_from_text`，不要各写各的正则。

## 12.13 沉默标记统一常量

`utils.py` 的 `SILENCE_MARKERS`（`frozenset`，含 `<SILENCE>`/`[SILENCE]`/`SILENCE`/`<end_turn>`）
是**唯一**沉默标记来源。`gs_agent.py`、`handle_ai.py`、`heartbeat/decision.py`、`send_chat_result`、
`extract_json_from_text` 全部引用它。**不要**再散落硬编码列表（曾因此漏掉 `<end_turn>` 被当普通
消息发出）。

## 12.14 记忆系统正确性细节（D-12~D-19）

改记忆系统时注意这些已修复的坑：

- **Edge 去重 key 拼接**：多个相邻 f-string 必须用括号包裹，否则 key 只含 `|` 字符、去重失效（D-12）。
- **entity 计数**：`increment_entity_count` 不能用 `len(entity_name_to_id)`（含已存在实体会虚高，
  导致 HierGraph 频繁重建）（D-13）。
- **新建 Category**：`_apply_entity_assignments` 的 `existing_ids` 要正确初始化（D-14）。
- **ORM Relationship** 用 `lazy='noload'` 显式加载，不是 `'selectin'`（避免 N+1）（D-17）。
- Entity 向量去重 / Reranker 三路用 `asyncio.gather` 并行，不要串行 await（D-15/D-16）。
- Speaker 强制 Layer-1 归类要有**代码层硬性保障**，不能只靠 LLM 遵守指令（D-18）。

## 12.15 配置/数据库/Schema 的约定

- 新配置项放进对应 `setup_config()`/`CONFIG_DEFAULT`，消费侧"每次用时读"即自动热重载（见
  [§03](./03-plugin-loading-and-config.md)）。
- SQLModel 不写 `__tablename__`；数据库方法写类里、用 `@with_session`；Schema 升级走
  `on_core_start_before` 的 `exec_list`/`trans_adapter`（见 [§11](./11-statistics-webconsole-database.md)）。
- AI 表要挂到受总开关控制的建表路径，不要无条件建。
- **ORM 查询类型安全**（别用 `cast`/`type:ignore`/`getattr` 糊弄 basedpyright，见
  [`docs/LLM.md`](../../../LLM.md) §3.5）：① `where`/`order_by`/`group_by` 里的列一律 `col()` 包裹
  （`col(cls.x) >= v` 才是 `ColumnElement[bool]`，裸 `cls.x >= v` 是 `bool`，`delete()/update().where()`
  会标红）；② `rowcount` 用 `isinstance(result, CursorResult)` 守卫取值；③ 不要 `select(*变长list)`，
  按分支写列数确定的 `select()` 让结果收敛成 `Select[tuple[...]]`。

## 12.16 RAG / 知识库的约定

- RAG 检索**不要改回前置强制**，由 `search_knowledge` 工具按需调（D-11）。
- 混合检索 score 是 RRF 名次分**非余弦**，不要按"余弦 ≥ 阈值"硬筛；过滤下推到 Qdrant
  `query_filter`，不要客户端二次筛 top-k（D-? / 2026-06-15-G）。
- 手动知识以 `AIKnowledgeChunk`（SQL）为真值源；长文必须分片（`chunking.py`）避免 512 token
  静默截断；深度对账是运维手动入口、非自动（见 [§10](./10-rag-knowledge-embedding.md)）。

## 12.17 历史缺陷速查表（D-1~D-21，全部已修复）

| ID | 模块 | 问题 | 详见 |
|----|------|------|------|
| D-1 | AI Router | Session ID 绑 user_id → 群聊上下文割裂 | §12.6 / [§06](./06-ai-session-and-persona.md) |
| D-2 | Heartbeat | 定时巡检并发雪崩 + Token 破产 | §12.9 / [§08](./08-heartbeat-scheduled-planning.md) |
| D-3 | Persona | Prompt 改后 Session 不更新 | [§06](./06-ai-session-and-persona.md) |
| D-4/D-9/D-10 | Handler | 长文本无长度保护 / 粗暴截断 / 无绝对上限 | [§04](./04-event-trigger-flow.md) |
| D-5 | Heartbeat | `_Bot`/`Bot` 混淆致 bot_self_id 缺失 | §12.5 / [§05](./05-bot-classes.md) |
| D-7 | WebConsole | 文件上传缺 MIME 检查 | [§11](./11-statistics-webconsole-database.md) |
| D-8 | Handler | 用户触发缺并发控制 → Rate Limit | [§04](./04-event-trigger-flow.md) |
| D-11 | handle_ai | RAG 强制前置检索 | §12.16 / [§10](./10-rag-knowledge-embedding.md) |
| D-12~D-19 | memory | 去重 key / 计数虚高 / N+1 / 并行化等 | §12.14 / [§09](./09-memory-system.md) |
| D-20 | gs_agent | 强制总结偏离用户问题 | §12.8 |
| D-21 | 全局 | AI 总开关关闭后仍跑 AI 逻辑 | §12.4 / [§02](./02-startup-lifecycle.md) |

## 12.18 改完代码的自查清单

1. 类型：无 `try/except` 兜底（除不可信外部输入）、无 `cast`、无 `type:ignore`、无 `getattr/
   dict.get` 兜底，参数返回值全标注。
2. 异步：可能阻塞的都 `async def`，CPU 密集走 `to_thread`/线程池，没在事件循环里同步跑。
3. AI 总开关：新加的 AI 初始化/定时任务/建表都查了 `enable`。
4. 状态：新加的进程内存状态知道多实例不共享；没碰 IngestionWorker 的独立线程禁区。
5. Bot：取 `_Bot` 用 `WS_BOT_ID`；需要 `Bot` 的地方没传裸 `_Bot`。
6. 历史/记忆：截断保留 ToolCall/ToolReturn 配对；记忆改动没踩 D-12~D-19。
7. 注释：`#` 注释 ≤2 行、每行 ≤88 字，精简直白。
8. 文档：改了核心机制，回头同步对应章节（源码是唯一事实源，但导航别让它过期）。
