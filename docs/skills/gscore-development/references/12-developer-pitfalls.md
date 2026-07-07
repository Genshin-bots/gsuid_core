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

## 12.17 流式 usage 累计语义膨胀（D-22，2026-07-04 修复）

**踩坑**：部署者看板 token 消耗（58M/29M）比服务商实扣（~300K）虚高数十倍。根因不在统计/
DB/前端，而在 usage 源头：vLLM/SGLang 系网关（SiliconFlow 等）流式响应**每个 chunk 都携带
累计 usage**（prompt_tokens 恒定、completion_tokens 单调不减），而 pydantic_ai 默认按
「仅最后 chunk 带 usage」的 OpenAI 标准语义逐 chunk 累加 → input ≈ 真实值×chunk 数、
output 近似平方级膨胀。Moonshot/OpenAI/DeepSeek 官方等标准网关不受影响。

**修复**（`ai_core/configs/models.py` + `openai_config/config_manager.py`）：

- OpenAI 配置新增 `usage_stats_mode`：`auto`（默认）/ `incremental` / `cumulative`。
- `auto` = 白名单（`_CUMULATIVE_USAGE_URL_KEYWORDS`）+ 进程内探测注册表
  （`_detected_cumulative_urls`）预置 + **在线探测**：`AutoUsageOpenAIChatModel` 经
  `_streamed_response_cls` 钩子挂 `_AutoUsageStreamedResponse`（覆写 pydantic_ai 文档
  标明可覆写的 `_validate_response`），观测到第 2 个带 usage 的 chunk 且符合累计特征即
  翻转 `openai_continuous_usage_stats`（该设置在流循环内逐 chunk 读取，翻转后「替换」
  语义覆盖先前误加的和）；流结束**终局对账**——证据链完整则定格最后累计值，翻转后特征
  破坏则用增量语义影子和回退，误判不丢数。探测当次请求的数值即已精确。

**不变量（改动时不能破坏）**：

1. 标准网关在 auto 下入库数值与旧行为**逐字节一致**（仅 1 个 usage chunk 永不触发翻转）。
2. 请求参数 `stream_options.continuous_usage_stats` **仅显式配 cumulative 时发送**；auto 的
   白名单/探测只作用于响应对象（构造时请求已发出），绝不改请求体——防网关拒收未知字段。
3. 终局对账的注册表登记/告警以 `usage_seen >= 2` 为前提，预置命中的标准网关不得误记。
4. 统计（`record_token_usage`/`record_hourly_performance`）、预算（`record_usage_scope`）、
   session 日志共用 `result.usage()` 这一个源头，修 usage 语义只改 models.py，不要在
   下游各自打补丁。
5. 依赖 pydantic_ai（1.77.0）的 `_validate_response` / `_streamed_response_cls` 覆写钩子，
   升级该库需回归验证探测逻辑。

## 12.18 历史缺陷速查表（D-1~D-22，全部已修复）

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
| D-22 | configs/models | 累计语义网关流式 usage 被逐 chunk 累加致统计膨胀数十倍 | §12.17 / [§11](./11-statistics-webconsole-database.md) |

## 12.19 记忆 / 嵌入的性能·内存瓶颈（2C2G 部署必读）

排"记忆系统内存爆 / 摄入越来越慢"前先看这里，别错怪泄漏或 Qdrant：

- **瓶颈是本地嵌入不是 LLM**：实测单条 ~491-turn haystack 嵌入 ~68s（CPU-bound）vs 作答 LLM ~8s。
  摄入慢 / CPU 高几乎全在 fastembed ONNX。旋钮见 [§10.5.1](./10-rag-knowledge-embedding.md)：
  `GSUID_EMBED_THREADS`（默认 `cpu//2`，别在 2 核机设）、`GSUID_EMBED_BATCH`（默认 64）、
  `GSUID_EMBED_BATCH_WORKERS`（默认 `cpu//4`）。
- **onnxruntime arena 只增不减**：一次大 batch 后不释放，**峰值即稳态**——`GSUID_EMBED_BATCH` 降峰值
  是永久生效，不是压瞬时尖峰。这**不是泄漏**。
- **空载 ~4.6GB 的大头是游戏插件（~4GB），不是记忆系统**（精简 core 无插件仅 ~624MB；嵌入模型
  ~150MB 加载→单批 ~300–500MB）。排"记忆泄漏"先确认：跑测中 RSS 随题数**稳定不上行**（每题
  episode/向量/session 逐题释放）就不是泄漏，只是启动斜坡到平台。
- **Qdrant 随规模退化的是"共享集合上的过滤搜索速度"，不是内存**：向量 `on_disk=True`（memmap，
  不进 RAM），只有 HNSW 图在 RAM 且亚 GB；但**所有 scope 共享一个 collection** 靠 `scope_key` 过滤，
  总量涨到百万级后 filtered-HNSW 变慢（**这是"评测越跑越慢"根因**——上百 scope 堆一集合且不清理）。
  生产靠冷热分集合 + 容量裁剪（[§09 / MEMORY_SYSTEM §7](./09-memory-system.md)）压住热集合；
  **建议补 `valid_at_ts` payload 索引**（`vector/startup.py` 目前只建 `scope_key`），否则 `ts_range`
  时间过滤随 scope 增大扫描变慢。详见 `MEMORY_SYSTEM.md` §3.2.2。
- **2C2G 清单**：`embedding_provider=openai`（远程嵌入，消 CPU-bound + 最大动态内存）+
  `enable_rerank=false`（本地 reranker ONNX 是另一大内存项）+ `qdrant_provider=remote` + 精简插件
  → 空载可压到 ~0.6–1GB。本地全栈跑 2C2G 会周期打满核 + 逼近 2GB。

## 12.20 大语料回灌 / 图谱评测的摄入架构（§14：Episode 粒度与抽取批次解耦）

BEAM-10M / LongMemEval 这类"单题灌数百~上千 turn"的大语料，会撞穿原 `observe → worker` 摄入链路
的两个隐性假设。**动 `batch_observe` / 摄入链路前必读**：

- **坑①：巨型 Episode 召回恒空**。原链路按 `batch_max_size` 把连续消息拼成一条 Episode，大语料下
  单条可达数十万字符。本地 bge-small 向量只覆盖头部 ~512 token、注入又被预算截断 → **问到具体
  数字/版本永远召回不到**。
- **坑②：抽取批次撞子超时丢图谱**。每批实体/边抽取一次 LLM 调用必撞 120s 子超时被丢 →
  **0 entity / 0 edge**，System-1 图形同虚设。
- **解法（评测/回灌专用，`eval_mode` / 新端点字段闸门保护，线上行为不变）**：
  1. **Episode 粒度**：`AIMemEpisode.create_episodes_bulk` **每 turn 一条 granular Episode**（`_chunk_text`
     按句子边界切 ≤900 字符、同 turn 块共享时间戳），纯嵌入零 LLM、可被 System-1 精确召回。
  2. **抽取批次粒度**：`batch_observe(extract=true)` → `worker.extract_window` 把**连续若干 turn 拼成
     抽取窗口**（字符/turn 数先到者收口），每窗口一次 LLM 抽取，复用
     `_extract_and_upsert_from_episode` 全下游。**绝不**复活 observer 队列 + 80-turn 聚合。
  3. **窗口宽松超时只跳过不丢整 plan**：`_run_extract_pass` 每窗口 `asyncio.wait_for`，超时/异常只
     `stats["failed"]++`、不取消父任务（避开 pydantic_ai 跨 Context 取消报错）。
- **valid_at 污染（生产也相关的时序坑）**：抽取写边时 `valid_at` 必须取**本窗口 turn 的最新对话
  时间戳**（`extract_and_upsert_edges(valid_at=...)`），不传则落成**抽取时刻**，"同属性取最新值"类
  时序推理被抽取顺序污染。`ObservationRecord.timestamp` 是必填 aware datetime，直接
  `max((r.timestamp for r in records), default=None)`——**别用 `getattr`+`try/except` 兜底**（违反红线）。
- **assistant 侧事实别丢**：`observe(force_scope_key=...)` / `batch_observe` 下 user 与 assistant **都**
  按普通会话摄入到目标 scope 并参与抽取。回放语料里 assistant 是对话内容、非"本机 Bot 戏言"，
  **不走 C6 SELF 轻量路由**，否则半数事实被跳过抽取、探针召回不到。
- **SQLite 写并发**：窗口化并发多路写会撞 `UNIQUE(scope_key,name)` / `database is locked`。
  `entity.py`/`edge.py` 用**乐观重试**（`IntegrityError`/`OperationalError` 退避 6 次）+
  `eval_write_lock.eval_write_guard()`（`eval_mode` 下进程内写锁把快速写事务排队，LLM/嵌入仍锁外
  并发）。线上按 scope 串行 flush、锁恒不竞争，行为不变。
- **`write_episodes=False`**：对已摄入 Episode 的 scope 只补抽取，避免重复嵌入 6 万+ 条、规避高并发
  重嵌入丢向量。**`trigger_rebuild=true` 仍同步 `await rebuild_task(scope_key)`**（曾被误删致静默
  失效、响应谎报 `rebuild:true`，现已恢复）——rebuild 要在 episodes/实体/边都落库后才看得到最新图。
- **注入侧配套**：`chat_with_history` 必须传 `to_prompt_text(max_chars=memory_config.memory_inject_max_chars)`
  （默认 `2000` 只够 ~2 条 Episode，是长对话事实"检索到却答不出"的暗坑）；纯 episode-RAG（无图谱）时
  `to_memory_text` / `to_prompt_text` 都要带上 episodes，否则 `memory` 字段恒空。

驱动脚本见 `eval/BEAM_10M/ingest_graph.py`（逐 plan 断点续跑 + 统一 rebuild 轮询）/ `quick_eval.py`
（复用已摄入记忆、调参后分钟级子集重测）。

## 12.21 WebConsole 实时日志缓冲：有界 deque 的 SSE 游标陷阱

`logger.log_history` 是 **`deque(maxlen=2000)`**（有界，防无界 list 在高吞吐下堆出数 GB 堆、RSS
只涨不跌）。**改 `read_log`（`webconsole/logs_api.py` 的 SSE 实时日志源）时切记**：

- **不能用绝对下标 `log_history[index]` 配单调 `index` 推进**。deque 写满 2000 后每次 append 淘汰最
  左元素、`len` 封顶，`index` 越过 2000 即 `index <= len-1` 永假 → 该 SSE 连接**永久收不到新日志**
  （`clean_log` 每 480s `.clear()` 也救不回，因 `len` 永不超过 maxlen）。这是有界化一度引入的回归。
- **正解**：用模块级单调 `log_seq`（每条 append 自增），游标按序号定位
  `log_history[len - (log_seq - cursor)]`，落后到被淘汰区间时跳到最旧可用条。序号与 deque 淘汰
  解耦，写满后照常推送、`clear()` 后照常从新日志续推。
- 同理，任何"按绝对位置消费有界 deque"的代码都有此陷阱；有界缓冲的消费者一律用**单调序号 + 落后
  截断**，别用下标。

## 12.22 输出安全防线的不变量（出戏防火墙 / 内容守卫，2026-07-08）

两个模块：`ai_core/output_firewall.py`（AI 输出侧"出戏"检测）+ `ai_core/content_guard.py`
（不可信内容包裹 / 伪造工具返回降权）。背景与全部日志实证见
[`docs/SESSION_LOG_SECURITY_FINDINGS_20260707.md`](../../../SESSION_LOG_SECURITY_FINDINGS_20260707.md)。
改这两个模块或任何输出/发送链路时，以下不变量**不能破坏**：

### 🔴 核心语义是"提醒一次 → 重说 → 放行"，不是"命中即封禁"

误杀的代价被设计为"多一次生成"，所以词库可以高召回；一旦把任何路径改成"命中即永久
拦截/替换"，误杀就会直接吃掉用户可见的回复（历史事故："早餐吃了个豆包"命中 model 词
`豆包` 被整条替换）。三条路径各自的语义：

| 路径 | 检测点 | 命中行为 |
|------|--------|----------|
| 主输出（`gs_agent` TextPart） | 发送前预检 `check_ooc` | 不发送 → 记入 `_ooc_blocked` → iter 结束后 `_ooc_rewrite_and_send` 用轻量无工具 Agent 带警告重写一次 → 产物经 `send_chat_result(ooc_check=False)` **无检放行**；重写失败才退 `PERSONA_FALLBACK_TEXT`，且 history 中被拦原文换成重写版 |
| 工具发送（`send_message_by_ai`） | `output_firewall.gate_warn_once(tool_ctx.extra, text)` | 同轮首次命中 return 重写警告（模型重写重发）；**同轮第二次仍命中放行**（`ooc_warned:{turn_id}` 键），防"警告↔重试"死循环 |
| 无重说通道（proactive / 兜底总结等一切默认走 `send_chat_result` 的） | `send_chat_result` 内末端兜底 | 命中替换为 `PERSONA_FALLBACK_TEXT`（底线：绝不把模型名/AI身份发出去） |

- **`ooc_check=False` 只允许用于重说产物**。新增发送路径时默认让它带检；想跳过检测先想清楚
  该路径的文本是否已经走过一次反馈闭环。
- `check_ooc(tier="plain")` 生产**尚无调用方**（预留给将来的非角色扮演出口），别当它已接线。

### 🔴 低俗谐音 / 钓鱼识别是 prompt 层防线，不要复活词库

初版曾做过 `_LEWD_TERMS` 词库 + scheduler 内容闸门，**已于 2026-07-08 评审整体移除**：
手工词库在真实俚语空间（谐音/缩写/拆字/emoji 变体持续演化）覆盖率≈0，而常用词碰撞
误杀实测严重（`几把`→"推荐几把武器"、`导管`→"领**导管**理"跨词拼出、`豆包`→食物）。
现行防线：

- **表达/执行纪律在 `persona/prompts.py` 合规层**：谐音"怀疑先验"（"提醒我XX"式请求中
  XX 疑似身体/性谐音→按梗对待）+ **绝不为其调用任何工具** + 钓鱼连锁信不参与不传播；
  heartbeat 决策 prompt 另有同款免疫条款。`tests/test_security_guard.py` 的
  `test_lewd_phishing_lexicon_removed` / `test_prompt_contains_lewd_phishing_discipline`
  锁住"词库不复活 + prompt 纪律不丢"。
- 若将来 prompt 纪律实证不够（看生产日志），升级方向是**副作用工具入口的低成本 LLM 语义
  判定**（low_level provider + normalize 后 hash 缓存 + fail-open），而不是回到词库。

### 🔴 output_firewall 词库（模型名/AI自指）新增条目前必须过"规范化碰撞"检查

出戏防火墙的 `_MODEL_TERMS` 词库**保留**（身份泄露是公开事故面，且 prompt 防线已被
"2.5"事故实证穿透，必须代码级兜底）。但 `normalize_for_match` 删词内分隔符（空格/点/
连字符/**逗号**）+ 全角转半角后做**子串匹配**，加词须知：

- `豆包/星火/文心/小爱/供应商` 等已知碰撞常用词——主路径有重说闭环兜着（误杀=多一次
  生成），**但若把它们引入没有重说语义的新路径，就会复现"整条回复被硬替换"事故**。
- 加词前用一批日常语料跑 `check_ooc` 验证误杀面。
- 部署者热补词走 `ai_config.output_firewall_extra_terms`，不要硬编码进 `_MODEL_TERMS`。

### 🔴 框架代码禁止硬编码具体人格台词

历史事故 ×2：内容闸门拒绝文案写死"早柚才不记呢"（生产 persona 是达妮娅→自我指涉错乱）；
框架级前摇台词模块（已整体移除，见下）。规矩：**框架层给用户的文本要么人格中性，要么是
"给 Agent 的指令"让它自己组织语言**（工具 return 天然是反馈通道）。唯一例外是
`PERSONA_FALLBACK_TEXT` 这类语气中性的末端兜底。

### 工具"前摇台词"模块已整体移除（2026-07-08）

`_FRAMEWORK_PRE_TOOL_EXPRESSIONS`（const.py）、persona `config.json` 的 `pre_tool_expressions`
字段及其缓存/失效/发送逻辑已全部删除，**不要再加回任何"框架替 AI 说固定话"的机制**。
耗时工具前的告知由 `persona/prompts.py::TOOL_ORCHESTRATION_CONSTRAINTS` 的"耗时工具处理"
条款驱动 Agent 自行组织语言——流式 TextPart 本就先于工具结果发出，Agent 的话天然能到达用户。
旧 persona 配置里残留的 `pre_tool_expressions` 字段是惰性数据，读到也当不存在。

### 持久历史是"精简版"，别把要留存的内容塞进 rag_context

Token 优化 O-1：`gs_agent` 在拼 rag_context **之前**快照 `_lean_user_message`，run 结束后用
`_relean_user_turn` 把写入 `self.history` 的 user turn 换成精简版。因此 rag_context（【历史
对话】/记忆/群语境）只存在于**当前轮**，不进持久 history——需要跨轮留存的内容必须放进
`user_message` 本体而不是 rag_context。同理 O-2：`system_prompt` 的时间戳刻意只到**日**级
（跨会话命中 provider 前缀缓存），别把秒/分级时间加回去——精确时间在 user_message 侧的
【当前时间】。

## 12.23 改完代码的自查清单

1. 类型：无 `try/except` 兜底（除不可信外部输入）、无 `cast`、无 `type:ignore`、无 `getattr/
   dict.get` 兜底，参数返回值全标注。
2. 异步：可能阻塞的都 `async def`，CPU 密集走 `to_thread`/线程池，没在事件循环里同步跑。
3. AI 总开关：新加的 AI 初始化/定时任务/建表都查了 `enable`。
4. 状态：新加的进程内存状态知道多实例不共享；没碰 IngestionWorker 的独立线程禁区。
5. Bot：取 `_Bot` 用 `WS_BOT_ID`；需要 `Bot` 的地方没传裸 `_Bot`。
6. 历史/记忆：截断保留 ToolCall/ToolReturn 配对；记忆改动没踩 D-12~D-19。
7. 注释：`#` 注释 ≤2 行、每行 ≤88 字，精简直白。
8. 输出链路：新发送路径默认过出戏防火墙（`ooc_check=False` 仅限重说产物）；框架层文本
   人格中性；词库加词先过规范化碰撞检查 + 误杀回归用例（§12.22）。
9. 文档：改了核心机制，回头同步对应章节（源码是唯一事实源，但导航别让它过期）。
