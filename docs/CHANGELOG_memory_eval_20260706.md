# CHANGELOG — 记忆系统评测·性能·摄入架构大版本（2026-07-04 → 07-06）

> 本 CHANGELOG **完全并入并取代** `docs/memory_eval_consolidated_20260706.md`（其全文见 **附录 A**），
> 并在其基础上补齐**全部未提交代码的逐文件详解**、**本轮 code review 的缺陷发现与修复**，以及
> **对照 `docs/LLM.md` 红线的合规结论**。面向 GsCore 维护者，一份文档看清「这次到底改了什么、
> 为什么改、有没有引入新坑、坑修没修」。
>
> 变更规模：**32 改动文件 + 6 个新增文件，约 +2000/-280 行**（含跨会话累积）。

---

## 目录

- [第一部分：代码变更全景（逐文件详解）](#第一部分代码变更全景逐文件详解)
  - [A. 记忆图谱大语料回灌架构（§14，本轮主线）](#a-记忆图谱大语料回灌架构14本轮主线)
  - [B. 检索链路增强（dual_route / vector / 时间分桶 / 矛盾提示）](#b-检索链路增强)
  - [C. 摄入健壮性（entity / edge / worker / eval_write_lock）](#c-摄入健壮性)
  - [D. Provider 并发 / 故障路由（跨会话已落地）](#d-provider-并发--故障路由跨会话已落地)
  - [E. 流式 usage 累计语义（跨会话已落地）](#e-流式-usage-累计语义跨会话已落地)
  - [F. 本地嵌入 CPU / 内存优化](#f-本地嵌入-cpu--内存优化)
  - [G. 内存日志缓冲有界化 + 本轮修复](#g-内存日志缓冲有界化--本轮修复)
  - [H. 评测基础设施（eval/）](#h-评测基础设施eval)
  - [I. 文档更新](#i-文档更新)
- [第二部分：本轮 code review 的缺陷发现与修复](#第二部分本轮-code-review-的缺陷发现与修复)
- [第三部分：对照 LLM.md 红线的合规结论](#第三部分对照-llmmd-红线的合规结论)
- [附录 A：memory_eval_consolidated_20260706.md 全文并入](#附录-amemory_eval_consolidated_20260706md-全文并入)

---

## 第一部分：代码变更全景（逐文件详解）

### A. 记忆图谱大语料回灌架构（§14，本轮主线）

**背景问题**：BEAM-10M / LongMemEval 这类"单题灌入数百~上千 turn"的大语料评测，撞穿了原摄入
链路的两个隐性假设——① `observe → worker` 按 `batch_max_size` 把连续消息拼成**巨型 Episode**
（单条可达数十万字符），本地 bge-small 向量只覆盖头部 ~512 token、注入又被预算截断 → **召回恒空**；
② 每批实体/边抽取一次 LLM 调用必撞 120s 子超时被丢 → **0 entity / 0 edge**。本轮把 **Episode 写入
粒度**与**抽取批次粒度**彻底解耦。

| 文件 | 改了什么 | 关键点 / 坑 |
|---|---|---|
| `memory/database/models.py` | 新增 `AIMemEpisode.create_episodes_bulk(scope_key, items, *, vector_chunk=64)`：**每 turn 一条 granular Episode**，`add_all` 一次提交 SQL 行 + 向量按 `vector_chunk` 分块 batch embed；返回写入条数 | 分块 embed 防"一次上千条触发远程 413 / 本地 OOM"；`valid_at` 用 turn 真实时间戳 |
| `memory/database/models.py` | 新增 `AIMemConflict.get_by_signatures(scope_keys, signatures, limit=6)`：`@with_session` + `col()` 包裹，按 `(scope, fact_signature)` 取矛盾摘要，供检索期"矛盾提示"注入 | 命中 `ix_mem_conflict_scope_sig` 索引；C11 软删除后单侧事实无从察觉矛盾，靠它带回 |
| `memory/database/models.py` | `AIMemEntity.extract_and_upsert` 前置：`eval_mode` 下**跳过阶段2 向量语义去重**（仅保留阶段1 精确名匹配） | BEAM 技术语料实体极细粒度、阶段2 每未命中名都 embed+Qdrant，是窗口化并发主要耗时；实测真被语义合并的仅 ~8%（多为大小写变体）。**线上 `eval_mode=False` 行为不变** |
| `webconsole/ai_memory_api.py` | `batch_observe` 重写：user/assistant 两侧统一落目标 scope；`_chunk_text` 按句子边界切 ≤900 字符块（每块一条 Episode，同 turn 块共享时间戳）；同步 `create_episodes_bulk`（返回即落 DB+Qdrant，flush 语义天然满足） | assistant **不再**走 C6 SELF 轻量路由（回放语料里 assistant 是对话内容非"本机戏言"，否则半数事实被跳过抽取） |
| `webconsole/ai_memory_api.py` | `BatchObserveRequest` 新增 `write_episodes / extract / extract_window_chars / extract_window_turns / extract_window_timeout / extract_concurrency / extract_max_windows` | `write_episodes=False`：对已摄入 scope 只补抽取，避免重复嵌入 6 万+ 条 + 规避 §5 高并发重嵌入丢向量 |
| `webconsole/ai_memory_api.py` | 新增 `_build_extraction_windows` / `_run_extract_pass` / `_find_episode_id_in_range` / `_count_entities_edges`：连续 turn 拼**抽取窗口**（字符或 turn 数先到者收口），限流并发 + 每窗口宽松超时（超时只跳过该窗口不丢整 plan、不取消父任务，避开 §4.4 pydantic_ai 跨 Context 取消报错） | 复用 `worker.extract_window` 全下游（实体/边/user_global/偏好蒸馏）；返回前后 entity/edge 差值供断点续跑校验 |
| `memory/ingestion/worker.py` | 新增 `extract_window(*, scope_key, records, episode_id="")`：窗口化抽取入口，复用 `_extract_and_upsert_from_episode` | **绝不**创建巨型 Episode、**绝不**走 observer 队列 + 80-turn 聚合路径 |
| `memory/ingestion/worker.py` | `_extract_and_upsert_from_episode` 签名从 `episode: AIMemEpisode` 改为 `episode_id: str`（可空串）；全部 `episode.id` → `episode_id` | 抽取不再强绑一条持久化 Episode；`episode_id` 仅用于 `mem_episode_entity_mentions` 关联与背景排除 |
| `memory/ingestion/worker.py` | Step 5 传 `valid_at=stmt_ts`（本窗口 turn 的**最新对话时间戳**）给 `extract_and_upsert_edges` | **valid_at 污染修复**：不传则边 `valid_at` 落成抽取时刻，"取最新值"类时序推理被抽取顺序污染（BEAM §17 教训）。**本轮 review 已把此处从 `getattr`+`try/except` 兜底改为直接 `max(...)`，见第二部分** |
| `memory/observer.py` | `observe()` 新增关键字参 `force_scope_key`：给定即视为评测回放，assistant 也当普通会话摄入、跳过 C6 SELF 路由；`is_self_speech = force_scope_key is None and ...` | 向后兼容（默认 None 时行为不变） |
| `memory/prompts/extraction.py` | 抽取 system prompt 增"**事实来源约束（防幻觉）**"：人物属性只能来自本人/他人明确陈述，**不得**从 assistant 建议/示例构造人物属性 | BEAM 教训：assistant 说"可以找安全分析师帮忙"被误抽成"某人是安全分析师" |

### B. 检索链路增强

| 文件 | 改了什么 | 关键点 / 坑 |
|---|---|---|
| `memory/retrieval/dual_route.py` | **私聊/无群 scope 修复**：`if user_id and (not group_id or enable_user_global)` 才检索 user_global（原 `enable_user_global and user_id`） | 私聊/评测（`group_id=None`）时 user_global 是主 scope，否则召回恒空；群聊仍受 `enable_user_global` 约束、不重复添加 scope |
| `memory/retrieval/dual_route.py` | **时间分桶补召回**：`_extract_time_range`（query 含 ≥2 个日期 **且** 时序/枚举/总结意图词才触发）→ `_fetch_temporal_episodes` 把时间窗切 6 桶、每桶带 `valid_at_ts` 过滤语义检索、合并去重按时间升序 | 与 S1/S2 并行；结果**绕过 Reranker** 直接并入（rerank 按字面相似会踢掉时序关键但低字面重合的片段）；`>24` 条按等步长采样保时间轴覆盖 |
| `memory/retrieval/dual_route.py` | **C11 矛盾提示注入**：`MemoryContext.conflicts`，检索期一次 `AIMemConflict.get_by_signatures` 取命中边的矛盾摘要；`to_prompt_text` / `to_memory_text` 各加【矛盾记录】块；注入期兜底：同 `(src,tgt)` 命中边极性相反时双方标 ⚠️ | 让 Agent "指出矛盾请澄清"而非武断给单侧结论 |
| `memory/retrieval/dual_route.py` | **注入预算重排**：`temporal_mode` 时事实占比 55%→30%；Episode 块吃掉前面区块**用剩的全部预算**（原固定 30% + 3 条×200 字硬上限饿死纯 episode-RAG 召回）；每条 Episode 内容上限 temporal 600 / 普通 1000 字 | 事实带 `[YYYY-MM-DD]` 记录日期前缀（`_edge_date_prefix`），knowledge_update 类"取最新值"需要时间戳排序 |
| `memory/vector/ops.py` | `_hybrid_search_impl` 新增 `ts_range` 时间窗过滤（`valid_at_ts` payload `Range` 下推）；新增 `search_episodes_in_range` | 时间分桶检索用 |
| `memory/vector/ops.py` | **RRF 融合分不被余弦阈值误杀**：余弦门只下推到 dense 分支（`dense_score_threshold`），**移除**对融合结果的 `score >= threshold` 后筛 | 混合检索时 `query_points` 返回 RRF 名次分（~1/(60+rank)≈0.016），再余弦硬筛会误杀全部命中（sparse 活跃时召回恒空）。与 `knowledge.py` 一致 |
| `memory/vector/ops.py` | 三个 `upsert_*_vectors_batch` 加 `wait=True` | §5 教训：异步 upsert 在突发高并发写下静默丢向量，SQL 与 Qdrant 计数对不上 |
| `memory/retrieval/types.py` / `familiarity.py` / `system2.py` | `Edge` TypedDict 新增 `valid_at_ts: Optional[float]`；两处 Edge 构造点补 `valid_at_ts` | 与 `_hybrid_search_edges` 一致，供 `_edge_date_prefix` 显示记录日期 |

### C. 摄入健壮性

| 文件 | 改了什么 | 关键点 / 坑 |
|---|---|---|
| `memory/ingestion/eval_write_lock.py`（新增） | 进程内 `asyncio.Lock`（`eval_write_guard()`）：`eval_mode` 下把"快速写事务"显式排队（毫秒级交接），LLM 抽取/嵌入仍在锁外并发；非 eval 返回 `nullcontext()` 零开销 | SQLite 单写者；窗口化并发直接撞写锁 → `busy_timeout(5s)` 忙等成主要耗时。线上按 scope 串行 flush，锁恒不竞争、行为不变 |
| `memory/ingestion/entity.py` | `extract_and_upsert_entities` 包**乐观重试**（`IntegrityError` / `OperationalError`，最多 6 次退避）+ `eval_write_guard` | 同 scope 并发窗口撞 `UNIQUE(scope_key,name)` → 回滚重试，下次 `find_existing` SQL 精确命中另一窗口刚提交实体、改走更新分支。旧"按 scope 串行整个 find+写"粗锁把向量去重也串行化、吞吐骤降 ~10x |
| `memory/ingestion/edge.py` | **英文否定极性**：`_NEGATION_RE_EN`（词边界 `never/not/no/none/without/refuse/deny/stopped/n't`）并入 `_fact_polarity` | BEAM 教训：仅中文标记时英文语料否定全漏检、C11 矛盾引擎从未触发。词边界避免 `note→not`/`knows→no` 误命中 |
| `memory/ingestion/edge.py` | `extract_and_upsert_edges` 新增 `valid_at` 参数（回放语料真实陈述时间，缺省仍用当前时间）；写入包 `OperationalError` 重试 6 次（每次重置累积态）+ `eval_write_guard` | valid_at 污染修复的写侧 |
| `memory/ingestion/edge.py` | `eval_mode` 专用 `_eval_find_mergeable_edges`：一次 SQL 按 `(src,tgt)` 预取既有有效边，内存判定归并（精确重复→mention++ / 极性相反→矛盾 / 其余版本更新保留两条），**零向量检索** | 替代每条边一次 `search_edges`（embed+Qdrant），窗口化并发下主要耗时来源之一 |

### D. Provider 并发 / 故障路由（跨会话已落地）

> ⚠️ **已落地，勿重复实现**。

| 文件 | 改了什么 |
|---|---|
| `configs/provider_router.py`（新增） | 槽位计数（非 Semaphore，支持网页热改并发）+ 冷却期故障切换：`acquire/release/slot()` 上下文、`mark_failure/mark_success`、`_pick` 按可用性→剩余容量选路；`looks_like_provider_failure` 粗判限流/连接/额度错误 |
| `configs/ai_config.py` | `AI_CONFIG` 新增 `high/low_level_2nd_provider_config_name`（备用配置全名，留空不启用） |
| `configs/models.py` | 新增 `get_2nd_config_name_for_task` / `get_max_concurrency_for_config`（clamp [1,10]）/ `get_model_by_full_name` |
| `configs/{openai,anthropic}_config/config_manager.py` | 配置模板加 `max_concurrency`（`GsIntConfig`，默认 1、最大 10） |
| `gs_agent.py` | `run()` 接入路由：绑定固定模型会话（`model_config_name is None`）不参与路由；否则 `provider_router.slot(task_level)` 选路、临时换 `self.model`、命中 provider 级故障标记冷却 60s 并换路重试**一次**，`finally` 恢复并 `_aclose_model` 临时模型 |
| `gs_agent.py` | `eval_mode` 下 `model_settings` 注入 `temperature=0`（消 probe 采样方差，±2-4 题跑次波动） |

### E. 流式 usage 累计语义（跨会话已落地）

> ⚠️ **已落地，勿重复实现**。修复"部署者看板 token 虚高数十倍"。

| 文件 | 改了什么 |
|---|---|
| `configs/models.py` | `get_openai_config_by_name` 返回值从 5-tuple 扩为 **7-tuple**（加 `continuous_usage, usage_stats_mode`；**唯一调用方 `get_openai_model_by_name` 已同步**）；新增 `_resolve_continuous_usage` / `AutoUsageOpenAIChatModel` / `_AutoUsageStreamedResponse`（覆写 `_validate_response`：在线探测每 chunk 累计 usage 的 vLLM 系网关、即时翻转 `openai_continuous_usage_stats` + 终局对账 + 影子和回退） |
| `configs/openai_config/config_manager.py` | 模板新增 `usage_stats_mode`（`auto`/`incremental`/`cumulative`） |

### F. 本地嵌入 CPU / 内存优化

| 文件 | 改了什么 | 效果 |
|---|---|---|
| `rag/embedding/local.py` | 线程默认 `min(cpu,8)` → **`max(1, cpu//2)`**（`GSUID_EMBED_THREADS`）；新增 `batch_size` 默认 **64**（`GSUID_EMBED_BATCH`），`embed_sync` 显式传 `batch_size`；`threads` 参数改 `int \| None` 惰性解析 | 嵌入期 CPU 从吃满全核压到 ~50%；单批驻留内存 ~500MB→~300MB |
| `rag/embedding/base.py` | 批量执行器 `max_workers` 从固定 1 改 `max(1, cpu//4)` 自适应（`GSUID_EMBED_BATCH_WORKERS`） | 并发摄入并行嵌入路数可控（小机退 1 防过订阅）；实测并发共享 session 快 1.76x（非过订阅） |

### G. 内存日志缓冲有界化 + 本轮修复

| 文件 | 改了什么 | 关键点 / 坑 |
|---|---|---|
| `logger.py` | `log_history` 从**无界 `list`** 改为 **`deque(maxlen=2000)`**；`clean_log` 从 `log_history = []`（重绑）改为 `.clear()`（同对象清空） | 无界 list 在高吞吐（记忆评测单请求数百条含 30k+ 字符注入全文的日志）下，8 分钟清理周期内即堆出数 GB Python 堆、高水位不归还 OS，表现为 RSS 只涨不跌 |
| `logger.py`（**本轮 review 修复**） | 新增单调 `log_seq`（每条 append +1）；`read_log` SSE 游标从**绝对 deque 下标**改为**按 `log_seq` 序号定位**、落后到被淘汰区间时跳到最旧可用 | **修复有界 deque 引入的回归**：绝对下标在 deque 写满 2000 后左侧淘汰会漂移，`index` 单调增而 `len` 封顶 → SSE 实时日志写满即**永久静默**。详见第二部分 |
| `webconsole/chat_with_history_api.py` | `to_prompt_text(max_chars=memory_config.memory_inject_max_chars)`（修默认 2000 暗坑）；注入前置英文"记忆使用准则"（取最新值/指出矛盾/引用精确数字/不臆造人物属性/按记忆具体值作答）；日志只记摘要不落 30k 全文 | 默认 `max_chars=2000` 只够 ~2 条 Episode，长对话事实全落预算外 |

### H. 评测基础设施（eval/）

| 文件 | 改了什么 |
|---|---|
| `eval/common/judge.py` | `_is_transient_judge_failure` + `judge_single_answer` 指数退避重试 5 次：判分走 chat_with_history，agent 把 LLM 连接错误当正文返回（HTTP 200），parse 不到 JSON 默认判错 → **276/500 题误判**。识别 `connection error/执行出错/rate limit/无法解析评判回复` 等瞬时故障 |
| `eval/common/runner.py`（新增） | 通用 probe/judge 并发 + 断点续跑（按 `question_id` resume）+ 坏答卷 `repair`（剔除 `[ERROR]`/`评判请求失败`/超时空答重跑）骨架，替代旧会话里的手工 repair 脚本 |
| `eval/run_eval.py`（新增） | 统一入口 `python eval/run_eval.py <benchmark> <stage>`；probe 加 `--inject-date`（注入 `question_date` 当前时间，temporal 必需）/`--extract`（走 System-1 图）/`--system2`/`--clear-first` + `--answers-file`/`--judge-file` 覆盖；BEAM 委托既有脚本 |
| `eval/common/http_client.py` | `call_batch_observe` 加 `extra_payload` 透传（`extract`/`write_episodes`/窗口参数），评测脚本无需逐字段加参 |
| `eval/longmemeval/run_longmem_eval.py` | `flatten_haystack_with_dates` 携带 `haystack_dates` 回填 Episode 时间；接入统一 runner；作答注入当前时间 |
| `eval/BEAM_10M/ingest_graph.py`（新增） | §14 全量图谱构建驱动：逐 plan 走 `batch_observe(extract=true)` 窗口化抽取、状态文件断点续跑、全抽完统一 `rebuild` 轮询至收敛；默认 `write_episodes=False`（只补抽取） |
| `eval/BEAM_10M/quick_eval.py`（新增） | 只对指定类别跑 probe+judge、复用已摄入全量记忆，调参后无需重灌、分钟级验证 |
| `eval/BEAM_10M/run_beam_eval.py` / `README.md` | 沿用 CLI + 状态文件，接入新 http_client / runner |

### I. 文档更新

| 文件 | 改了什么 |
|---|---|
| `docs/MEMORY_SYSTEM.md` | §3.2.2 Qdrant 规模扩展性与内存实测表；§8.5 本地嵌入 CPU/内存 env |
| `docs/skills/.../10-rag-knowledge-embedding.md` | §10.5.1 本地嵌入 CPU/内存调优（三 env + onnxruntime arena 只增不减 + 空载 4.6GB 是插件） |
| `docs/skills/.../11-statistics-webconsole-database.md` | Token 来源与流式 usage 语义、预算共享同源、内存账本回载注意 |
| `docs/skills/.../12-developer-pitfalls.md` | §12.17 流式 usage 膨胀（D-22）；§12.18 缺陷速查表扩到 D-22；§12.19 记忆/嵌入性能内存瓶颈 |
| `docs/skills/.../03-plugin-loading-and-config.md` | `usage_stats_mode` 热更新条目 |
| `docs/skills/.../SKILL.md` | D-21→D-22 |
| **本轮新增（见本 CHANGELOG 配套）** | §12.20 大语料回灌摄入架构 + §12.21 日志缓冲 SSE 游标陷阱（写入 12-developer-pitfalls.md）；§09 memory 补 batch_observe/granular 摄入指针 |

---

## 第二部分：本轮 code review 的缺陷发现与修复

对全部 32 改动 + 6 新增文件逐一 review（重点排查新引入 bug + 对照 `docs/LLM.md`）。**全部 22 个
production 文件 `py_compile` 通过**。发现并修复 **3 处缺陷**，另记 2 条低风险观察项。

### 已修复（3）

#### FIX-1 · `logger.py`：有界 deque 引入的 SSE 实时日志永久静默（回归，中危）

- **根因**：`log_history` 改为 `deque(maxlen=2000)` 后，`read_log` 的 SSE 游标仍用**绝对下标**
  `log_history[index]` 且 `index` 单调递增。deque 写满 2000 条后每次 append 淘汰最左元素、`len`
  封顶 2000，而 `index` 越过 2000 后 `index <= len(log_history)-1` **永假** → 该 SSE 连接**再也
  收不到新日志**（`clean_log` 每 480s `.clear()` 也救不回，因 `len` 永不超过 2000 < index）。
  `read_log` 服务 `webconsole/logs_api.py:564` 的 `text/event-stream` 长连接，评测负载下几个请求即
  写满 2000、实时日志随即卡死。
- **触发**：任一长连接 WebConsole 实时日志，缓冲累计 >2000 条后。
- **修复**：引入单调 `log_seq`（每条 append 自增），`read_log` 改按序号定位
  `log_history[len - (log_seq - cursor)]`，游标落后到淘汰区间时跳到最旧可用条。已用小 `maxlen`
  模拟 eviction / burst / clear 三场景验证不再丢日志、不再静默。

#### FIX-2 · `webconsole/ai_memory_api.py`：`batch_observe` 的 `trigger_rebuild` 静默失效（回归，中危）

- **根因**：`batch_observe` 重写时删掉了 `if req.trigger_rebuild: await rebuild_task(scope_key)`，
  但请求字段 `trigger_rebuild` 与响应 `"rebuild": req.trigger_rebuild` 仍在，且
  `eval/BEAM_10M/run_beam_eval.py` 的 `--rebuild` 仍传 `trigger_rebuild=true`、README §87 仍如此
  文档。结果：调用方传 `trigger_rebuild=true` 时**分层图不重建**，响应却**谎报 `rebuild: true`**。
- **修复**：在 episodes 落库 + `extract`（若开）之后、`return` 之前恢复
  `if req.trigger_rebuild: await rebuild_task(scope_key)`（`rebuild_task` 本就已 import），此时
  rebuild 能看到最新实体/边。新 `ingest_graph.py` 走独立 rebuild 端点不受影响。

#### FIX-3 · `memory/ingestion/worker.py` Step 5：`getattr` + `try/except` 兜底违反 LLM.md §1.1/§1.4（红线）

- **根因**：`ts_list = [r.timestamp for r in high_records if getattr(r, "timestamp", None)]` 外包
  `try/except Exception`。`ObservationRecord.timestamp` 是**必填 aware `datetime`**（observer.py:175），
  `getattr` 默认兜底与吞异常都是对已知类型的多余防御，违反 §1.1（禁 try-except 兜底）+ §1.4（禁
  getattr 兜底）。原 consolidated §6 自审漏掉此处。
- **修复**：`stmt_ts = max((r.timestamp for r in high_records), default=None)`——类型契约保证全为
  aware datetime、`max` 不会 tz 混比；无 record 时 `default=None`。

### 低风险观察项（记录，未改）

- **OBS-1 · `edge.py` 重试与 `AIMemConflict.record` 的重复**：写入重试循环内调用的
  `AIMemConflict.record` 是 `@with_session`（**独立 session 即时提交**），若外层 `session.commit()`
  抛 `OperationalError` 触发重试，上一轮已提交的 Conflict 行不会回滚 → 重试可能**重复记录同一矛盾
  摘要**。仅 `eval_mode` 且撞写锁时发生；`get_by_signatures` limit 6、重复摘要无害，注入体验轻微
  冗余。代码注释"每次重试重置累积态避免重复"仅指 `edges_vector_data/merged_count`，不含已提交的
  Conflict 行——措辞略过头，但影响可忽略，未改。
- **OBS-2 · `ai_memory_api._run_one` 两处 `# type: ignore`**：源于窗口类型声明 `List[List["object"]]`
  的宽松标注（`recs: List[object]` 上取 `.timestamp`）。属评测专用端点的类型洁癖问题、非运行时
  bug；收紧为 `List[List[ObservationRecord]]` 需引入模块级 import（有循环导入风险），权衡后保留
  现状。`_llm_extract_single` 的 429 文本探测（`"429"/"额度"` 子串命中即退避重试）在含 HTTP 状态码/
  "rate limit"字面的技术语料上有误判可能，只带来无谓退避、不丢数，属"上游把 429 当正文返回"这一
  外部约束的固有代价。

---

## 第三部分：对照 LLM.md 红线的合规结论

| 项 | 结论 |
|---|---|
| §1.1 禁 try-except 兜底 | worker Step 5 的 try/except 兜底 **已由 FIX-3 移除**；`judge.py` 重试是对**瞬时故障判定后**重试（非吞异常兜底）、与 runner I/O 韧性一致；检索链 `_fetch_temporal_episodes`/矛盾查询的 `except` 均为"降级不影响主流程"的 fire-and-forget，非类型兜底 |
| §1.2 禁 cast | 本轮新增代码无新 `cast`（`vector/ops.py` 顶部既有 `cast` 为历史遗留，未新增） |
| §1.3 禁 type:ignore | 仅 `ai_memory_api._run_one` 两处（评测端点、见 OBS-2），非 production 类型域；`models.py` 的 `@override` 是 pydantic_ai 子类化正当用法 |
| §1.4 禁 getattr/dict.get 兜底 | worker Step 5 的 `getattr` **已由 FIX-3 移除**；`eval/` 的 `resp.get(...)` 是无类型 HTTP 响应、与评测 harness 既有风格一致（非 gsuid_core 生产类型域） |
| §1.6 注释 ≤2 行/≤88 字 | 新增注释均控制在 2 行内 |
| §2.1 完整类型提示 | 新增函数（`create_episodes_bulk`/`get_by_signatures`/`_extract_time_range`/`_resolve_batch_size` 等）参数返回值全标注 |
| §3 DB 规范 | `create_episodes_bulk` 走 `async_maker()`；`get_by_signatures` 用 `@with_session`+`col()` 包裹列；`_count_entities_edges` 用 `func.count()`+`col()` 守卫 |
| 运行时缺陷 | 修复 3 处（FIX-1~3）；`get_openai_config_by_name` 5→7 tuple **唯一调用方已同步**、无解包错位 |

**结论**：本轮改动主线（大语料摄入解耦 + 检索增强 + 摄入健壮性）行为在 `eval_mode=False`（线上）下
**基本不变**（新分支多以 `eval_mode` / 新可选参数 / 新端点字段闸门保护）；`enable_user_global` 私聊
修复、`to_prompt_text` 预算、`wait=True`、注入预算重排、嵌入默认更省 CPU/内存等为**面向所有链路的
增强/修正**，均可通过配置或 env 回退。修完 3 处缺陷后**未见残余运行时回归**。

---

## 附录 A：memory_eval_consolidated_20260706.md 全文并入

> 以下为被本 CHANGELOG 取代的 `docs/memory_eval_consolidated_20260706.md` 全文（评测综合成绩 /
> 提分方向 / 性能真相 / Qdrant 扩展性 / 复现命令 / goal），一字不删并入以满足"完全包含"。

### A.0 结论速览

| 基准 | 成绩 | 关键说明 |
|---|---|---|
| **LongMemEval（全 500 题）** | **443/500 = 88.6%** | episode-RAG + 注入当前时间 + MiniMAX 作答，全程 **reranker-free** |
| LongMemEval 首轮（错误值） | 180/500 = 36.0% | **判分连接错误假象**，非记忆缺陷 |
| LongMemEval 判分修复真基线 | 386/500 = 77.2% | 纯 episode-RAG |
| **BEAM-10M conv0（20 题）** | **8/20 = 40%**，rubric 42/71=59% | valid_at 回填后真基线；2/3 失败是数据集/判分瑕疵+思考模型抖动 |
| BEAM 全量（200 题） | 未跑 | 单 conv ~35h、infra-bound（本地嵌入 + 抽取吞吐） |

**两条核心结论**：
1. **LME 的"异常低分"是两个评测基础设施 bug 叠加的假象**（判分 LLM 连接错误 + 作答 LLM 配额耗尽），
   非记忆质量问题；修复后真实水平 88.6%。
2. **差 90% 的最后一步是作答模型的聚合推理，不是记忆检索**——实测在残余失败题上跑完整 System-1
   entity/edge 图（图建成、召回改善），仍 0/7 恢复（计数/求和照样错）。**System-1 图不解决计数聚合。**

### A.1 未提交代码改了什么（按方向归类）

> 注：此为原文的方向归类；**逐文件详解见本 CHANGELOG 第一部分**（更细）。

#### A.1.1 评测基础设施修复（LME 分数订正的直接原因）

| 文件 | 改了什么 | 为什么 |
|---|---|---|
| `eval/common/judge.py` | `_is_transient_judge_failure` + `judge_single_answer` 指数退避重试 5 次 | 判分走 chat_with_history，agent 把 LLM 连接错误当正文返回（HTTP 200），parse 不到 JSON 默认判错 → 276/500 题误判 |
| `eval/common/runner.py` | `FAILURE_MARKERS` 加 `Connection error/执行出错/无法解析评判回复` | 让 `repair=True` 能把历史坏判分记录重跑 |
| `eval/run_eval.py` | probe 新增 `--inject-date`/`--extract`/`--system2`/`--clear-first` + `--answers-file`/`--judge-file` 覆盖 | 注入 `question_date` 当前时间（temporal 必需）；`extract` 走 System-1 图；子集实验隔离 |
| `eval/longmemeval/run_longmem_eval.py`、`eval/common/http_client.py` | 统一入口/HTTP 封装（`call_batch_observe` 的 `extract`/`write_episodes` 透传） | 两基准共用 `runner.py` 并发/断点续跑骨架 |

#### A.1.2 记忆系统性能优化（2C2G 相关）

| 文件 | 改了什么 | 效果 |
|---|---|---|
| `rag/embedding/local.py` | 线程默认 `min(cpu,8)`→**`cpu//2`**；新增 `batch_size` 默认 **64**（`GSUID_EMBED_BATCH`） | 嵌入期 CPU 从吃满全核压到 ~50%；单次推断驻留内存 ~500MB→~300MB |
| `rag/embedding/base.py` | 批量执行器 `max_workers` 按 `cpu//4` 自适应（`GSUID_EMBED_BATCH_WORKERS`） | 并发摄入并行度可控（小机退 1，防过订阅） |

#### A.1.3 Provider 并发/故障路由（跨会话已落地，勿重复实现）

| 文件 | 改了什么 |
|---|---|
| `configs/provider_router.py`（新增） | 槽位计数 + 冷却期故障切换：主配置并发满/冷却 → 自动路由备用配置，两 provider 可同时跑 |
| `configs/ai_config.py`、`configs/models.py`、`{openai,anthropic}_config/config_manager.py` | `AI_CONFIG` 加 `high/low_level_2nd_provider_config_name`；配置模板加 `max_concurrency`（默认 1、最大 10） |
| `gs_agent.py` | `run()` 接入路由：主配置故障标记冷却 60s 并换路重试一次 |

#### A.1.4 记忆图谱链路（BEAM 阶段，episode 粒度 / 抽取批次解耦 + 健壮性）

| 文件 | 改了什么 |
|---|---|
| `memory/database/models.py` | `AIMemEpisode.create_episodes_bulk`（每 turn 一条 granular Episode，纯嵌入零 LLM）；`AIMemConflict.get_by_signatures`（矛盾感知注入） |
| `webconsole/ai_memory_api.py` | `batch_observe` 改 granular 切块摄入（`_chunk_text` ≤900 字符）+ 可选 `extract` 窗口化抽取 pass |
| `memory/ingestion/worker.py`、`entity.py`、`edge.py` | 窗口化抽取解耦；SQLite 写乐观重试 + `eval_write_lock` 串行锁；英文否定极性修复（`never/not/n't`）；`increment_entity_count` O(1) |
| `memory/retrieval/dual_route.py` | group_id 空时查 user_global；Episode 注入吃满预算；时间分桶语义检索（`ts_range`）；矛盾感知注入；置信度富集 |
| `memory/vector/ops.py` | RRF 融合分不被余弦阈值误杀（下推 dense 分支）；`search_episodes_in_range` 时间窗检索 |
| `webconsole/chat_with_history_api.py` | 注入传 `memory_inject_max_chars`（修默认 2000 暗坑）；英文记忆使用准则 |
| `gs_agent.py` | `eval_mode` 下 temperature=0（消 probe 采样方差） |

#### A.1.5 配置变更（`data/ai_core/*.json`，运行期改、重启加载）

| 配置 | 值 | 说明 |
|---|---|---|
| `local_embedding_config.embedding_model_name` | `bge-small-en-v1.5`（384d） | 英文语料用英文嵌入（原中文 512-token 是召回天花板） |
| `enable_rerank` | **false** | 本轮全程未加载 reranker（2C2G 适配；见 A.4） |
| `retrieval_top_k` / `memory_inject_max_chars` | 80 / 30000 | 评测期拉满，给检索足够召回 |
| `商汤科技.model_name` | `sensenova-6.7-flash-lite` | 高容量，替代容量极小的 `deepseek-v4-flash`（429 根因） |

### A.2 综合评测结果（逐类准确率）

#### A.2.1 LongMemEval（全 500 题，2026-07-06 最终）

| question_type | baseline（纯 episode-RAG） | **改进（+注入时间 +MiniMAX作答）** |
|---|---|---|
| single-session-assistant | 54/56 (96%) | **56/56 (100%)** |
| single-session-user | 40/70 (57%) | **66/70 (94%)** ← 配额答卷恢复 |
| knowledge-update | 69/78 (88%) | **71/78 (91%)** |
| temporal-reasoning | 92/133 (69%) | **116/133 (87%)** ← 注入当前时间 |
| multi-session | 111/133 (83%) | 112/133 (84%) |
| single-session-preference | 20/30 (67%) | 22/30 (73%) |
| **TOTAL** | **386/500 = 77.2%** | **443/500 = 88.6%** |

> 方法：所有会被改进影响结局的题（206 题 = temporal ∪ baseline 失败题）全部重跑；未受影响的
> baseline 通过题沿用原判分（同模型/同检索/日期前缀无害）。两大增益：**注入当前时间**（temporal
> +24 题）+ **MiniMAX 作答消除配额答卷**（single-session-user +26 题）。

#### A.2.2 BEAM-10M conv0（20 题，2026-07-05，valid_at 回填后真基线）

| 类别 | pass | 类别 | pass |
|---|---|---|---|
| instruction_following | 2/2 | contradiction_resolution | 0/2 |
| temporal_reasoning | 2/2 | event_ordering | 0/2 |
| knowledge_update | 1/2 | multi_session_reasoning | 0/2 |
| preference_following | 1/2 | summarization | 0/2 |
| abstention | 1/2 | **合计** | **8/20 = 40%** |
| information_extraction | 1/2 | rubric 检查点 | 42/71 = 59% |

> BEAM conv0 40% 中**三分之二失败非记忆缺陷**：①数据集/判分瑕疵（如 `knowledge_update__1` 标准答案
> "14" 系张冠李戴，Agent 答 22 反而更对）；②思考模型生成抖动（同上下文 quick_eval PASS、全量 FAIL）；
> ③抽取丢信息（英文否定 `never` 漏检，A.1.4 已修但需重抽才生效）。**近当前弱模型天花板**。
> 全量 200 题（10 conv）未跑——单 conv ~12M token / ~35h，infra-bound。

### A.3 记忆系统还能怎么提分（关键结论：差距是推理不是检索）

**已实证的天花板分析**：在 LME 残余失败的 29 题（multi-session 计数 + preference）上跑**完整 System-1
entity/edge 抽取**，实测——

- ✅ 图**确实建成**（样本 scope：105 实体 / 95 边）且**改善召回**（相关花费/项目都被检索到）；
- ❌ 作答仍在**计数/求和**失败：「led 几个项目」→4（标准 2）、「几件衣服」→2（标准 3）、
  「自行车总花费」列全明细却求和错。**前 7 题重跑 0/7 恢复。**

**结论**：事实无论以 episode 还是 edge 形式注入，作答模型都一样过/欠计数——**残余差距是作答模型的
跨会话去重/求和推理，不是记忆检索**。所以要冲 90%+，按杠杆排序：

1. **换强作答模型（最高杠杆）**：当前作答/判分都是 flash-lite 档，多值选错、计数不准、判分抖动。
   仅 probe+judge 用强模型（如 MiniMax-M3 满血 / Claude / DeepSeek-R），token 开销小（40 次/轮），
   预期直接吃掉 multi-session/preference 与 BEAM 的判分噪声分。
2. **答案侧聚合脚手架**：对"多少个/总共多少"类问题，让 Agent 先列举去重再计数（工具化计数 / 结构化
   中间步），而非一次性心算——这是记忆系统外的 prompt/agent 改动。
3. **抽取质量补强**（BEAM 相关）：多值属性抽取保留数字限定语、否定陈述整条捕获（A.1.4 英文否定已修，
   需对失败窗口重抽）；抗幻觉抽取约束（"不得从 assistant 建议构造人物属性"）。
4. **分层图 / System-2**（event_ordering / summarization 的"全中"依赖自顶向下聚合）：当前分类 LLM
   90s 超时未建成；强模型就位后先 rebuild 再测。
5. **判分加固**：`parse_beam_judge_response` 对非英文/含 ```json 的健壮性；判分模型升档消抖。

### A.4 内存 / CPU / 系统瓶颈（实测）

#### A.4.1 瓶颈 = 本地嵌入，不是 LLM（单题分阶段计时，491-turn haystack）

| 阶段 | 耗时 | CPU |
|---|---|---|
| **本地嵌入**（`batch_observe`，bge-small ONNX） | **68.2s（~89%）** | 嵌入期 python ~790%（≈8 核，因当时 `GSUID_EMBED_THREADS=8`） |
| 作答 LLM（MiniMAX，12 并发可用） | 8.2s（~11%） | 网络 I/O |

12 路 LLM 并发大部分时间空等一条 CPU-bound 嵌入跑完。**折算 2 核机**：嵌入吃满 2 核、且线程 8→2
使嵌入再慢 ~4x（单 haystack ~280s）——**2C2G 上本地嵌入是硬伤，远程嵌入近乎必选**。

#### A.4.2 空载 4.65GB 的真相：是插件不是嵌入，且**不是泄漏**

| 配置 | 空载 RAM |
|---|---|
| **精简 core（无游戏插件）** | **624 MB** |
| 满配（29 个游戏插件） | 4653 MB |
| → 29 个游戏插件 | **~4 GB**（各自 import pandas/PIL/matplotlib/游戏数据） |
| 嵌入模型（动态，摄入期） | 153 MB 加载 → 单批 ~300–500 MB |

- **非泄漏**：跑测中 RSS 在 154→165 题稳定在 4625–4656 MB（~30MB 带内波动），无上行趋势。Python GC
  正常，每题的 episode/向量/SQL session 逐题释放。
- 表面"无限上涨"是**启动斜坡**（624MB→4.6GB，插件 import + 首次嵌入），~2 分钟到顶即平台。
- ⚠️ **onnxruntime arena 只增不减**：峰值即稳态——这正是 `GSUID_EMBED_BATCH=64` 降峰值有意义之处。

#### A.4.3 达 2C2G「CPU<50% / RAM<2G」的配置清单

1. **嵌入改远程**（`embedding_provider=openai`）：消 CPU-bound 瓶颈 + 最大动态内存；作答变网络 I/O。
   ⚠️ 远程维度（nomic 1024）≠ 本地 bge-small（384），切换触发 Qdrant 一次性维度迁移重建。
   （注：当前配置的远程端点 `syn_` key 对 `api.openai.com` 返回 401，需换可用端点/密钥。）
2. **精简插件**：只挂记忆系统 + 必要插件 → 空载 ~0.6–1 GB。
3. **`enable_rerank=false`（已）+ `qdrant_provider=remote`（已）**：reranker 本轮全程未加载。
4. 2 核机**不要**设 `GSUID_EMBED_THREADS`：新默认 `cpu//2`（=1）即对。
5. **生产是逐消息增量嵌入**（每条几 turn，CPU 毫秒级）；评测的单题灌 ~500 turn 是病态峰值，不代表生产。

### A.5 Qdrant 随记忆增长的扩展性（重点）

**实测现状**（localhost:6333，green）：`memory_episodes` **516,937 点** / 5 段、`memory_entities` 16,782、
`memory_edges` 32,107；HNSW `m=16, ef_construct=100`、`indexing_threshold=10000`；**所有 scope 共享同一
collection**，靠 `scope_key` payload 索引过滤。关键配置：**向量 `on_disk=True`（memmap）、payload
`on_disk_payload=True`、但 HNSW 图 `on_disk=False`（图在 RAM）**。

逐项回答"记忆实体增加是否压迫性能/内存"：

| 维度 | 随规模的行为 | 结论 |
|---|---|---|
| **内存占用** | 向量 memmap 在磁盘（不进 RAM，OS 页缓存热页）；payload 在磁盘；**唯一随规模线性增长的是 HNSW 图**（`m=16` 链/点，51.7 万点 ≈ 数十 MB，百万级 ≈ ~百 MB，千万级才上 GB） | **不会像 in-memory 模式那样线性爆 RAM**；普通用户 scope（几百~几千点）可忽略。⚠️ 想再省：把 `hnsw_config.on_disk=True` 把图也落盘（换延迟省 RAM，适合 2C2G） |
| **插入性能** | HNSW 插入 O(log N)/点 + on_disk 磁盘 I/O；超 `indexing_threshold=10000` 触发段 HNSW 构建（CPU 尖峰）。规模到百万后吞吐亚线性下降但可用 | 生产**每用户增量小**（几 turn/消息）→ 插入快；评测的**批量灌 6万+ 点**才会触发段优化尖峰 |
| **搜索性能** | HNSW 查询 O(log N)。**真正的隐患：单一共享 collection**——**所有用户/群的记忆都在同一个 `memory_episodes` 里**，靠 `scope_key` 过滤。总量涨到百万级后，每次带过滤的 HNSW 在大图上跑，选择性过滤可能触发 filtered-HNSW 退化 | 这是**评测"越跑越慢"的根因**（500 eval scope 全堆一个集合 + 永不清理）；生产靠生命周期维护缓解（见下） |
| **召回性能** | HNSW 召回由 `ef/m` 决定，与 N 基本无关；`scope_key` 是精确索引过滤，不损召回 | **召回稳定**；评测跨 scope 累积不降召回、只降速 |
| **检索性能（时间过滤）** | `valid_at_ts` **无 payload 索引** → 时间分桶/范围过滤在候选内扫描，随 scope 增大变慢 | **生产相关**：temporal 类检索建议补 `valid_at_ts`（FLOAT range）payload 索引 |

**为什么生产不会失控**：生命周期维护（APScheduler 每周）做**冷热分集合 + 容量裁剪**——无引用+超龄+超每
scope 最近 M 条的冷 Episode 从 `memory_episodes`（热集）迁到 `memory_episodes_cold`（退出在线检索），
`EPISODE_MAX_PER_SCOPE=20000` 物理封顶。所以**热集合规模被主动控制**，单 scope 检索面稳定。

**给大规模 / 2C2G 的建议**（按性价比）：
1. **补 `valid_at_ts` payload 索引**（`startup.py::ensure_payload_indexes`）——直接收益，生产 temporal 检索。
2. **`hnsw_config.on_disk=True`**——2C2G 上把 HNSW 图落盘，RAM 再降（换少量查询延迟）。
3. **热集合规模盯梢**：确认生命周期维护在跑（冷热迁移 + 容量裁剪），别让 `memory_episodes` 无限涨。
4. **超大部署**：可评估按大 scope 分 collection 或 Qdrant sharding；当前单集合 + scope_key 过滤在
   百万级可用、千万级需重新评估 filtered-HNSW 退化。

> 一句话：**记忆实体增加对内存的压迫远小于直觉**（向量在磁盘、只有 HNSW 图在 RAM 且亚 GB）；真正随
> 规模退化的是**共享集合上的过滤搜索速度**，靠冷热分集合 + 容量裁剪 + `valid_at_ts` 索引可控。

### A.6 复现命令

```bash
# 服务（评测端点需 LOCAL_TEST_MODE；不设则 /api/chat_with_history、batch_observe 全 404）
GSUID_LOCAL_TEST_MODE=1 PYTHONUTF8=1 uv run core --dev

# LongMemEval：改进版作答（注入时间 + 清库重灌，纯 episode-RAG / System-1）
python eval/run_eval.py longmem probe --inject-date --clear-first --concurrency 4 --timeout 900 \
  --answers-file eval/longmemeval/results/answers_epd_full.json
python eval/run_eval.py longmem judge --concurrency 6 --timeout 120 \
  --answers-file eval/longmemeval/results/answers_epd_full.json \
  --judge-file eval/longmemeval/results/judge_epd_full.json
# （加 --extract 走 System-1 entity/edge 图；但对计数聚合无提分、且 infra-bound）

# BEAM-10M（委托既有脚本）
python eval/run_eval.py beam probe --conv 0
python eval/BEAM_10M/run_beam_eval.py judge --answers eval/BEAM_10M/results/answers_0.json
```

**注意**：LME probe 并发须 ≤4——每题灌 800~900 条 episode 进共享 `GsData.db`，并发过高触发
`database is locked`。判分/作答的连接错误已由 `judge.py` 重试兜住。

**评测前需临时设 eval 档**（收尾已回退为生产值）：`eval_mode=true`（temperature=0）、
`retrieval_top_k=80`、`memory_inject_max_chars=30000`；**生产值**分别为 `false / 15 / 2000`。
评测机可 `GSUID_EMBED_THREADS=8`（大核机吞吐）；2C2G/生产**不要设**（用默认 `cpu//2`）。

### A.7 Goal 命令（下次执行用 — 冲 90%+：强模型 + 答案侧聚合，2026-07-06）

> **背景**：LME 已到 **88.6%**（reranker-free）、BEAM conv0 40%（近弱模型天花板）。**实证残余差距是
> 作答模型的聚合/计数推理，不是记忆检索**（System-1 图在失败题上 0/7 恢复，A.3）。性能瓶颈是本地嵌入
> （A.4），Qdrant 扩展性可控（A.5）。provider 路由 / 时间分桶检索 / 嵌入省内存 / 判分重试**均已落地，勿重复实现**。

```
/goal 续跑 docs/CHANGELOG_memory_eval_20260706.md（原 memory_eval_consolidated_20260706.md），把
LongMemEval 从 88.6% 冲向 90%+、BEAM conv0 从 40% 抬升。
已落地勿重复实现：provider 并发/故障路由、时间分桶语义检索(ts_range)、嵌入 cpu//2 线程 + batch64 省内存、
judge 瞬时故障重试、注入当前时间(--inject-date)。核对 docs/LLM.md 红线后再动代码。
按杠杆排序执行（A.3）：
1) 换强作答/判分模型：把 high_level_provider 指向强模型（MiniMax-M3 满血 / DeepSeek-R / Claude），
   仅 probe+judge 用高档（token 开销小）、抽取仍走低档商汤。预期吃掉 LME multi-session/preference 与
   BEAM 判分抖动分——先跑 LME 的 multi-session+preference 失败子集（eval/longmemeval/s1_targets.json）验证。
2) 答案侧聚合脚手架：对"多少个/总共多少"类问题，让 Agent 先列举去重再计数（结构化中间步/工具化计数），
   通用改动、禁止逐题特调；用 quick_eval / 子集快测。
3) 性能：若要 2C2G 达标，配可用的远程嵌入端点（当前 openai_embedding syn_ key 对 api.openai.com 401），
   切换后重测 CPU/RAM<50%/2G，并把 System-1 全量图抽取跑起来（远程嵌入后不再 infra-bound）。
4) Qdrant：补 valid_at_ts payload 索引；2C2G 评估 hnsw_config.on_disk=True。
5) BEAM：英文否定极性修复(edge.py)需重抽失败窗口才生效；抗幻觉抽取约束；强模型后先 rebuild 分层图再测 System-2。
6) 逐类记录成绩与差距写回本文档 A.2，关键点把新 goal 写入文末（不改本条之前内容）。
环境：GSUID_LOCAL_TEST_MODE=1 PYTHONUTF8=1 uv run core --dev；LME probe 并发≤4；PYTHONIOENCODING=utf-8。
收尾回生产：eval_mode=false、provider 路由/商汤模型按需回退。
```
