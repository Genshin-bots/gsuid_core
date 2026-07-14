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
- **序号定位还不够——必须先快照 `seq`/`size` 再算下标，且要处理 `size == 0`**（2026-07-14 修）。
  `read_log` 会在 `yield` 处挂起（等客户端收字节），挂起期间**任何清空/淘汰**都会改变 deque 长度而
  `log_seq` 不变。当年 `clean_log()` 每 480s 的 `.clear()` 就是触发源：读者只要**落后一条以上**
  （控制台在排积压），恢复时就撞上 `len == 0` 而 `cursor < log_seq`：`oldest` 被算成 `log_seq`、游标
  抬到 `log_seq`，下标退化为 `log_history[0]` → **`IndexError: deque index out of range`，SSE 长连接
  直接断**，表现为网页控制台"开久了就报错"。正解是循环开头一次性快照
  `seq = log_seq; size = len(log_history)`（其间无 await，两者天然一致），再判 `cursor < seq` 才取
  下标——`size == 0` 时 `oldest == seq`，游标必被抬到 `seq`，自然走等待分支。
- **`log_seq` 只能增，绝不能重置**：序号一旦回退，在线读者的 `cursor` 会永久大于 `log_seq`，该连接
  静默到进程重启为止。淘汰只能从左侧走（`popleft` / deque 满自动淘汰），`oldest = seq - size` 天然兼容。
- **周期性 `.clear()` 已废除**（`clean_log` 现为空实现，不再挂 task）。它除了是上面 IndexError 的触发
  源，还会**抹掉控制台的回放积压**：前端重挂载（切路由回来 / 刷新）时 `allLogsRef` 归零，页面历史全靠
  SSE 从缓冲最旧一条回放；缓冲刚被清空就重连 → **控制台一片空白**，直到有新日志。内存改由三道上限
  保证有界：`reduce_event_dict` 单字段 4096 截断 + `LOG_HISTORY_MAXLEN` 条数 + `LOG_HISTORY_MAX_CHARS`
  总字符数（超预算左侧淘汰，正常负载不触发）。
- **缓冲存 `LogRecord`（level/gevent/timestamp 三个字符串），不存 `EventDict`**。旧实现把整个 event_dict
  `deepcopy` 一份入队，等于让缓冲长期持有 `Event`、消息列表等原始对象的引用——那才是 RSS 只涨不跌的大头，
  且字符预算按渲染后的 `gevent` 记账才准。顺带省掉了每条日志一次的 `deepcopy`（日志是热路径）。
- SSE 长连接还需**空闲心跳**（`SSE_KEEPALIVE_SEC = 15`，吐 `": keepalive\n\n"` 注释行，`EventSource`
  会忽略、不进 `onmessage`）：长时间零字节，反代（nginx `proxy_read_timeout` 默认 60s）会掐断连接。
  **心跳必须按"上次真正流出字节"计时，不能按"空闲轮次"计**：级别过滤下（如只订阅 ERROR 而满屏 DEBUG）
  游标一直在消费事件、却一条都不推，若消费即重置空闲计数，心跳会被永久饿死 → 连接每 60s 被掐一次。
- **已知固有上限**（非 bug）：`read_log` 追平后按 **1s 轮询**新日志；若单个轮询间隔内产出超过
  `MAXLEN`（2000）条日志，最旧的会在读者醒来前被挤掉——超载丢最旧是环形缓冲的设计，前端本身也只留
  2000 条。正常负载（<2000 行/秒）下一条不丢、不重、严格有序。
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

- `豆包/星火/文心/小爱` 等已知碰撞常用词——主路径有重说闭环兜着（误杀=多一次
  生成），**但若把它们引入没有重说语义的新路径，就会复现"整条回复被硬替换"事故**。
- 加词前用一批日常语料跑 `check_ooc` 验证误杀面（`tests/test_benign_fp.py` 两向都要跑）。
- 部署者热补词走 `ai_config.output_firewall_extra_terms`，不要硬编码进 `_MODEL_TERMS`。
- **词库分档（2026-07-12 第四/五轮）**：`_SYSTEM_TERMS` 只留硬词（systemprompt/max_tokens…）；
  `训练数据/参数量/上下文窗口/知识截止/采样参数/api密钥/apikey` 是 AI 行业闲聊与开发者日常词
  （"自己备个 API key"曾把整份工具推荐 scrub 成兜底句），在 `_CTX_TECH_SELF_RE` 里**绑定
  第一人称**才算泄露——真实密钥泄露由 `_SK_KEY_RE` 按 sk- 形态兜底；`供应商` 已删除。
  裸模型名的精度门：`_SELF_BIND_RE` 省主语支须**句首/标点后**（"群主用的是 ChatGPT"是第三方
  转述不拦）；**短答直答门是条件门**——`check_ooc(text, user_text=…)` 只在来话命中
  `_IDENTITY_PROBE_RE`（身份追问）时启用，AI 话题闲聊里的短句放行（C-5 对齐）。
  召回侧补 `_AI_ADMIT_RE`：多轮软磨下的**句首认领式承认**（"确实…是AI啦"，无"我"字，
  `_AI_SELFREF_RE` 第一人称要求接不住）；否定式（"才不是AI呢"）因否定词不在填充词集天然放行。
- **self_model 是注入持久化面（2026-07-12 第五轮实测）**：ooc 攻击（"以后每条结尾加>w<"）
  曾被 agent 用 `update_self_note` 记成 bot 级"学到的偏好"，跨会话跨用户全局注入——单轮防线
  防住了、学习路径把攻击写死了。已建：`add_self_note` 写入闸（`is_persistent_style_rule`
  复用 C-2 判据，风格规矩永不入偏好）+ 渲染免疫条款（旧印象绝不压过当前用户明确请求）。
  改自我认知学习路径时必须想"攻击者能否借这条路径持久化"；人设卡（persona.md）的触发表
  是**行为指令**不是风味描述——"被要求X→睡觉"就是在教模型拒活。

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

## 12.22b 输入/输出防线的"精度面"教训（2026-07-12 审查修复）

v9 评测驱动的一批防线（编码注入中和 / 假完成闸 / 防火墙词库扩充）上线时**只验证了
"坏内容拦得住"，没验证"好内容放得过"**——8 视角复审发现 4 处高严重度问题全是误杀方向，
已修复（明细见 [`docs/AI_CORE_CHANGE_REVIEW_20260712.md`](../../../AI_CORE_CHANGE_REVIEW_20260712.md)）。
后续改这些模块必须守住的点：

- **`content_guard.neutralize_encoded_injection` 跑在每条生产消息上**（`prepare_content_payload`）。
  意图门（解码提示 + 照做意图）的两侧词都不能收日常词：`转义`（改引号转义是日常提问）、
  裸 `回复/输出/说出` 曾让 "JWT base64 帮我看下再回复我" 触发屏蔽。兜底词库不能收
  `return xxx`（所有代码都含）。改词后必须同时跑：eval 的 inj_*/adv_* 注入样本（须全 HIT）
  **和**良性样本（JWT/编码代码/commit SHA/长链接，须全放行）。
- **该函数必须幂等**：警示横幅自身含「解码/照做」，无前缀早退的话，二次标注（历史回灌、
  评测 history 注入）会把良性编码块也屏蔽掉。`defuse_fake_tool_result` 同理（前缀含"工具返回"）。
- **`_AI_PEER_RE`/`_AI_SELFREF_RE` 是原文正则、不走词库重说白名单前的规范化**：英文短词
  （`ai`）必须 `\b` 词边界（曾命中"我们**main**分支"）；"我们…AI"句式必须要求
  `这些/这类/这种` 量词（"我们学校的人工智能社团"是合法第三方谈论）。
- **`_SYSTEM_TERMS` 禁止收多义英文裸词**：`temperature` 在天气 bot 是高频合法词，
  采样参数泄露改用取值形态正则（`temperature.{0,6}[0-2]\.\d`）。`ai_selfref` 类别
  不经过 model_identity 的"绑定自身"精度门，正则本身就要足够精确。
- **假完成闸（`gs_agent._claims_fake_done`）**：完成声明须带施动锚点（已/帮你/我），
  按句排除疑问/揣测语气。误触发的真实代价是**双倍 LLM 调用 + by_bot 下用户收到二连
  "改口"消息**（原文在流式迭代中已发出，闸在 run 末尾才检查）——不是"多一次生成"那么便宜。
  已知残留：纠正 nudge 会以 user turn 进入共享 history；架构级出路是把该检查并进
  pre-send 的 output_firewall 管线（见变更审查报告 §5.1），别在现有位置继续加正则域。
- 递归/重入护栏**用调用栈参数传递，不要用实例属性**——`GsCoreAIAgent` 是群共享对象，
  实例态会在并发 run 间互相压制（`fake_done_retry` 参数就是这么改出来的）。

## 12.22c 定时任务补偿与 APScheduler misfire（2026-07-12）

- 启动补偿（`reload_pending_tasks._schedule_overdue`）的错峰序号**取模回卷**铺满窗口；
  `min()` 截断会把溢出任务钉在窗口边界同一时刻，惊群只是被推迟 30 分钟。
- 全局 `job_defaults.misfire_grace_time=90`（`aps.py`）对"必须执行"的 date job 是隐形
  丢弃器：启动卡顿 >90s 该 job 被静默扔掉、任务永滞 pending。这类 job 要显式
  `misfire_grace_time=None`。排查"任务没跑"先看有没有 `Run time of job ... was missed` 日志。

## 12.22d 交互脚手架（C-1~C-4，`ai_core/interaction_scaffold.py`，2026-07-12）

评测把问题从"防线误杀"推进到"多步正确性 + 泛化"后，新增了一层**结构化交互脚手架**，
把过去全靠模型自觉的四件事变成框架层的显式约束/提示。只对**交互式主 Agent**生效
（`create_by ∈ {Chat, Agent, TEST}`，见 `_INTERACTIVE_CREATE_BY`），子 Agent / 后台链路不碰。

- **C-1 省略式跟进**（`detect_ellipsis_followup`）：当前是短句 + 闭类跟进动词（改成/取消/那X呢）
  且历史里有可跟进的动作时，注入"先 list 定位再 modify/cancel、别新建重复"的提示。
  治 `cross_turn_recall`/`planning` 里"改提醒建成新的"。"上一轮有动作"的**强证据是
  `has_recent_tool_call`（历史里的真实 ToolCallPart 轨迹）**，`_PRIOR_ACTION_RE` 名词表只兜
  跨 session 场景——名词表**不收数据域词**（天气/股价…），第四轮已把混入的 `天气` 清掉。
- **C-2 会话级漂移预算**（`count_style_pushes`）：统计当前 + 近几轮里"立持久说话规矩"的
  **意图**次数（时间持续量词 ∧ 人设核心宾语，两者必须同时命中）。判据是意图累积、不匹配
  具体措辞——**天然抗 paraphrase**。第四轮两处收敛：① **称呼偏好档摘出**——"以后叫我小王"
  是正常群社交（走群成员称呼机制、人设层自行决定），只有改说话方式/自称/人设/语言/格式的
  **核心档**才计入 push；② 注入语义在 gs_agent：**累积 ≥2 且比上轮增加**（session 字段
  `_last_drift_push_count` 去重）才注入提醒——单发交 prompt 层既有条款，一次 push 滞留
  recent 窗口不会逐轮重复唠叨。
- **C-3 寻址前置门**（`addressed_to_someone_else`）：当前消息带「@的是这位用户，不是你」标注、
  未同时点名自己、非 is_tome 时，**装配层直接把 `tools` 清空**（含 send_message_by_ai / find_tools
  / skills_toolset），把"不冲你来=零工具"从模型自觉变成硬约束。根治隐性指代域的 over-tooling。
  **@ 标注文案唯一定义在 `interaction_scaffold.AT_OTHER_MARKER`/`DIRECT_MARKER`**——
  utils/history_format 渲染只准 import 常量，字面量重复会让门静默失效
  （`test_at_marker_single_source` 源码级锁）。
- **C-4 墙钟软预算**（`gs_agent` ModelRequestNode 前）：交互式 run 墙钟超预算后，请求前注入一次
  "停止发起新工具轮、用已有信息收敛作答"，治多步任务的延迟长尾。与 token 预算正交。
  nudge 挂在 run 中途的后续 ModelRequest 上，**由 `_relean_user_turn(strip_hint_texts=…)` 从
  持久历史剥离**，不跨轮累积。

**阈值全部走 `ai_config`**（`scaffold_wall_clock_budget`=45s / `scaffold_followup_max_len`=24 /
`scaffold_ambient_max_len`=20 / `history_merge_window`=120s）：默认值按评测分布标定，
上线后按 `[Scaffold]` 日志的生产分布重标，不要当成已验证常量。

**铁律**：脚手架的所有判据必须是**结构/语言学范畴**（时间量词、闭类动词、@标注结构），
**绝不能塞评测集载荷词/具体暗号**——否则退化成又一处过拟合。改这里必须同时跑
`tests/test_interaction_scaffold.py`（正向触发 + 良性不触发双向锁）。C-3 的 gate 只朝
"更安全"方向偏置：伪造的 @ 标记（无标准"不是你"文案）**不触发** gate，交人设层兜底——
gate 误判的代价只能是"本该有工具却没给"，绝不能是"本该沉默却给了工具"。
注入提示（`（系统提示：…`）自身是伪造面：用户仿写同款句式会被
`content_guard.defuse_fake_system_hint` 加降权标注（幂等，同 `defuse_fake_tool_result` 模式）。

**🔴 长度/内容判定必须过 `extract_message_body`（P0 教训，2026-07-12 第六轮）**：
生产的 user_message 不是裸文本，是 `prepare_content_payload` 的完整 payload
（关系行 + 「--- 消息 ---」分节 + 正文 + 附件/@ 段落）+ handle_ai 追加的「【当前时间】」行；
评测端点传的才是裸文本。曾因判定直接吃整个 payload，`ambient_followup_to_other`（≤20 字门）
在**生产永远不触发**、`references_task_management`（≤60 字门）基本失效，而评测全绿——
与 C-3 rag 污染 bug 同款的「评测看得见、生产静默失效」。新增任何长度/内容类判定都必须
走 `extract_message_body`（@ 标注类判定看完整 payload），并在
`tests/test_interaction_scaffold.py` 补真实 payload 形态的用例锁（参考
`test_length_gates_on_production_payload`）。

## 12.22e 工具召不回的四层坑（2026-07-15 生产事故复盘）

现象：用户问「看下我玄翎秧秧面板」（鸣潮角色），AI 全程只调 `nte_*`（异环）工具，
`find_tools` 捞回来的也清一色是异环——**鸣潮工具一个都没进过工具列表**。四层原因叠加，
每一层单独看都"不致命"，合起来就把一个插件彻底变成了隐形。

**🔴 一、docstring 写错位置 = 注册了一个永远召不回的工具（零运行时症状）**

工具入库向量的文本是 `f"{name}\n{description}"`（`rag/tools.py`），而 `description`
**只**来自 docstring。XW 的 5 个面板工具把 docstring 写在了函数体第一条语句
（`logger.info(...)`）**之后**——那样它只是个普通字符串表达式，`__doc__` 是 `None`，
向量里只剩一个英文函数名，中文提问永远召不回。**注册成功、日志无异常、调用也正常**，
唯独检索不到。`@ai_tools` 现已在 docstring 为空时 `logger.warning`，
`tests/test_ai_tool_docstrings.py` 用 AST 扫全仓兜底。

**🔴 二、插件写 `category="self"` 会被降级，不是保底**

`self`/`buildin`/`meta` 是**框架特权分类**，插件声明时 `register.py` 会重定向到 `common`
（见 `_CORE_ONLY_CATEGORIES`）。插件工具因此**必须**靠向量检索召回——一旦踩了坑一，
就彻底没救。插件想被稳定召回，靠的是**好 docstring + `capability_domain` + `context_tags`**，
不是抢 `self`。

**🔴 三、能力族展开曾是"赢家通吃"**

`expand_tools_to_families` 旧实现：排名第一的族整族展开后一旦占满预算就 `break`。
`异环面板` 族有 9 个成员 > 附加池上限 8（`tool_extra_pool_max`），于是它**独占整个附加池**，
后面所有候选族（包括鸣潮的面板工具）连被看一眼的机会都没有。现改为：排名第一的族照旧
整族纳入（不回退），放不下的族**跳过而非中断**，并给**落选的种子**逐个补**兜底席位**
（至多 `_SEED_SEATS=4` 个，宁可小幅超预算）。

> **席位必须发给"种子"而不是"族"**（这一版差点写错）：只按族发席位，会把同族里排名靠后的
> 种子一并丢掉，**跨能力族的提问就会缺工具**——"看看我练度 + 这角色怎么提升"同时命中
> 「鸣潮面板」「鸣潮资料库」两族，资料库族整族放不下时只补 1 个席位，第 2、3 个种子
> （专武推荐等）就没了。种子是本轮的语义命中，一个都不该被大族挤掉。

**新增能力族时留意族大小**——族大于附加池上限时，它在旧逻辑下会挤掉所有人。反过来，
**单领域部署根本不必付这份检索开销**：persona `config.json` 的 `tool_packs` 可直接写
`capability_domain` 名，整族无条件常驻保底池（见 [§7.3](./07-tool-registry-and-agent.md)）。

**🔴 四、评测期配置遗留污染生产（本次真正的元凶，也是最容易复发的一类）**

`data/ai_core/local_embedding_config.json` 的 `embedding_model_name` 当时是
`BAAI/bge-small-en-v1.5`——**英文模型**。它是 2026-07-06 那轮英文语料记忆评测特意改的
（见 `docs/CHANGELOG_memory_eval_20260706.md` §A.1.5，同批还有 `enable_rerank=false`），
评测结束后**没改回来**。后果：**所有中文向量检索（工具 / 记忆 / 知识库）都退化成噪声**——
同一句中文 query，正确工具在英文模型下排 #40/58、在中文模型（框架默认，且是唯一合法
option）下排 **#1**；英文模型的相似度全挤在 0.058 的窄带里，`nte_explore` 能排在
`nte_character` 前面。

> **教训**：`data/ai_core/*.json` 是运行期配置、**不进 git**，评测为了跑分改的值不会被
> code review 拦住，也没有任何告警。**评测改了哪些运行期配置，必须在评测结束后逐条改回**，
> 并且改动本身要写进 changelog（这次幸好写了，否则根本查不出来）。排查"检索质量突然变差"
> 类问题时，**先确认嵌入模型和语料语言是否匹配**，再去怀疑业务代码。
>
> 切换嵌入模型会改向量维度（384 ↔ 512），重启时所有 Qdrant 集合维度不匹配 → 自动
> `force_recreate` + 从 payload 重嵌入（不丢数据，但启动会重跑全量嵌入，2C2G 上很慢）。

## 12.22f 记忆一条都没存下来的两个坑（2026-07-15 排查）

现象：生产库里**真实 QQ 流量的 Episode 数为 0**，偏好记忆表 46 条全是评测数据。
两个独立 bug 叠加，各自都不致命，合起来让整个记忆链路对真实用户**完全失效**。

**🔴 一、flush 是唯一的落库时机，而缓冲区在进程内存里**

`IngestionWorker` 把观察攒进 `self._buffers`（进程内存），旧实现只有两个落库出口：
攒满 `batch_max_size`(80) 条，或距上次 flush 满 `batch_interval_seconds`(**2 小时**)。
于是一段几轮的对话要在内存里躺两小时——core 在这期间重启/被强杀，这段记忆**永久消失**。

**能持久化的 Episode 全部来自 webconsole / 评测端点**，因为只有 `chat_with_history`
与 `batch_observe` 显式调了 `worker.flush_all()`；真实 WS/QQ 链路从不主动 flush。
排查时这个分布本身就是最强线索：**"只有走 API 的数据活下来了"= 落库依赖显式 flush**。

现已新增 `idle_flush_seconds`(默认 180)：**按「对话静默」而非固定周期触发**——对话进行中
一直有新消息 → 不算静默 → 不 flush，抽取仍是整段一次调用，**批量效率不受影响**；
对话结束 3 分钟后落库，记忆的最长在险时间从 2 小时降到 3 分钟。
`batch_interval_seconds` 退化为刷屏 scope 的兜底上限。

> 新增任何"先攒后落"的缓冲时，先问一句：**进程被 kill -9 会丢多少？** 攒批是为了省
> LLM 调用，不是为了省磁盘——落库和攒批应该解耦。

**🔴 二、`group_id or user_id` 把私聊记忆写进了 group scope**

`handler.py` / `handle_ai.py` 的 **4 个**调用点都写着：

```python
group_id=str(event.group_id or event.user_id)   # ← 私聊时 group_id 变成 user_id
```

而 `observer.py` 按 `ScopeType.GROUP if group_id else ScopeType.USER_GLOBAL` 定 scope，
**非空就走 GROUP**。于是私聊记忆落进 `group:{user_id}`。而：

- `AIMemPreference` 的 docstring 写死「**主存 USER_GLOBAL scope**」；
- `dual_route_retrieve` 注释着「私聊（group_id 为空）→ **user_global 是该用户记忆的主 scope**」。

**下游全都按"私聊 group_id=None"设计，只有调用点在回退**——写入落到一个谁也不认的
幻影 scope，偏好记忆因此永远为空。四处已统一改为私聊传 `None`，
`tests/test_memory_ingestion_durability.py` 用 AST 锁死该写法不得复活
（检测器只认 `X.group_id or X.user_id` 形态，不误伤黑名单的 `in ... or ... in ...`）。

## 12.22g 两条方法论教训（2026-07-15，比具体 bug 更值钱）

**🔴 一、测装配 / 注册表的评测，必须打「真实运行中的 core」**

第一版工具选择评测写成了**进程内**跑 `gss.load_plugins()`，结果注册表只有 **51 个工具**
（真实 core 里有 303）、实体索引缺一半插件，跑出来 Pool Recall 20%——**数字全是假的**。

根因：**插件的注册有两条路径**——`@ai_tools` / `ai_alias` 在 **import 期**注册，而不少插件
（如 XW 的 AI-RAG）走的是**启动钩子**（`on_core_start` → 资源下载完再 `reload_ai_rag()`）。
`load_plugins()` 只做 import，拿到的是**残缺注册表**。

> 凡是依赖"注册表全不全"的评测/脚本，一律通过 HTTP 打运行中的 core
> （`eval/tool_selection/` 的 `assemble_preview` / `entity_index` 两个 local-test 端点即为此
> 而设，默认 404，需 `GSUID_LOCAL_TEST_MODE=1`）。**别在进程内重建世界。**

**🔴 二、门控要做「过滤」，不要做「整轮开关」**

偏好注入曾用 `inject_preferences = intent != "闲聊"` 整轮关闭。这道门在**上游**跳过了整个
偏好查询，导致检索侧"`general` / 纠错规则永远保留"的设计**根本没机会执行**——文档里两句话
自相矛盾了几个月都没人发现，因为**没有任何报错**。

正确形态是**传空的过滤条件**（`preference_contexts=[]`），让下游那道**本就存在**的过滤自己
决定留什么。同理适用于任何"某某轮次不要 XX"的需求：

> **能用"缩小候选集"表达的，就不要用"整条链路跳过"表达。** 前者让下游的不变量继续生效，
> 后者会把下游所有精心设计的兜底一起废掉，且**静默失效**。
>
> 这个 bug 还叠加了第二层：意图分类器把"帮我查一下长离的练度好不好"判成**闲聊**（conf 0.8）。
> **任何挂在 `intent` 上的门控都要问一句：分类器错判时，代价是什么？** 若代价是"整个能力
> 静默消失"，那这个门控的形态就是错的。（`find_tools` 的渐进暴露也挂在同一个意图门上。）

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
9. 防线改动：注入/出戏/假完成相关的正则或词库改动，坏样本（eval inj_*/adv_*）与好样本
   （良性误杀集）**两个方向都要跑**（§12.22b）；处理用户文本的标注函数保持幂等。
10. 交互脚手架（§12.22d）：C-1~C-4 只对交互式主 Agent 生效；判据只用结构/语言学范畴、
    绝不塞评测载荷词；改动跑 `tests/test_interaction_scaffold.py` 双向锁。
11. 文档：改了核心机制，回头同步对应章节（源码是唯一事实源，但导航别让它过期）。
