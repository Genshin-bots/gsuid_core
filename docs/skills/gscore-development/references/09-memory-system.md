# 九、记忆系统（Mnemis 双路检索）

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[八、主动发言与任务编排](./08-heartbeat-scheduled-planning.md) · **下一章**：[十、RAG 知识库与嵌入](./10-rag-knowledge-embedding.md)

记忆系统（`ai_core/memory/`）让 AI"记住"群聊里发生的事。本章覆盖整条链路 + 偏好记忆 +
RF-Mem 双过程检索 + 生命周期 + 多模态摄入。

## 9.1 设计理念（先建立心智）

- **Observer 与发言决策正交**：AI 读所有消息构建认知，但不因此回复任何一条。**即使 Persona
  纯静默，记忆仍在后台积累**。
- **双路检索**：System-1（向量相似度快速匹配）+ System-2（分层图遍历全局选择），合并经
  Reranker 重排。
- **Scope Key 隔离**：群组间严格隔离，同时支持用户跨群全局画像。
- **门控纯规则零 LLM**：摄入质量门控 100% 正则/规则实现，绝不调 LLM。

**核心数据流**：

```
用户消息 → handler.py（observe 入队）
        → handle_ai.py（dual_route_retrieve 检索 → 注入上下文）
        → AI 回复 → handle_ai.py / bot.py（observe 入队）
        → IngestionWorker 后台消费 → LLM 提取 Entity/Edge → 写 SQLAlchemy + Qdrant → 分层图增量重建
```

## 9.2 Scope Key 隔离（`memory/scope.py`）

所有记忆节点带 `scope_key`：

| ScopeType | 格式 | 说明 |
|-----------|------|------|
| `GROUP` | `group:{group_id}` | 群组级，群内共享 |
| `USER_GLOBAL` | `user_global:{user_id}` | 用户跨群全局画像 |
| `USER_IN_GROUP` | `user_in_group:{user_id}@{group_id}` | 用户在特定群的局部档案 |
| `SELF` | `self:{bot_id}` | Bot 自身情景记忆与自我模型 |

> **`SELF` scope（C6）**：Bot 自身发言（`__assistant_*`）**不混入**群组事实图谱，改路由
> `self:{bot_id}` 做轻量摄入（仅 Episode、不抽 Entity/Edge），杜绝"Bot 戏言污染群记忆"。
> 隔离靠 SQL `WHERE scope_key = ?` + Qdrant payload filter。

## 9.3 Observer 观察者管道（`memory/observer.py`）

通过 `queue.Queue`（**线程安全**，非 `asyncio.Queue`）传递观察记录。

```python
@dataclass
class ObservationRecord:
    raw_content: str; speaker_id: str; group_id: str; scope_key: str
    timestamp: datetime; message_type: str   # group_msg / private_msg
    value_tier: str                           # C1：HIGH / LOW
```

**C1 摄入质量门控**（`_gate()`，100% 纯规则零 LLM）：① 过滤噪声；② 打 `value_tier`。

| 规则 | 说明 |
|------|------|
| 自身消息过滤 | `speaker_id == bot_self_id` 不入队 |
| 黑名单群组 | `group_id in observer_blacklist` 不入队 |
| 命令回显过滤 | 正则命中"请输入正确/功能名称"等框架报错回显 → 丢 |
| 注入特征过滤 | "忘记所有指令"/"ignore previous instructions" → 丢 |
| 复读/刷屏过滤 | 与本 scope 最近 12 条完全相同 → 丢（保留首次） |
| 重要性分级 | 含姓名自述/称呼偏好/承诺/数字日期 → HIGH；纯寒暄且 <10 字且无实体 → LOW；其余 HIGH |

`value_tier=LOW` 的记录 IngestionWorker 只写 Episode、跳过 Entity/Edge 抽取。

**三个入队点**（分属两条记忆路径 `memory_mode`，注意去重）：

1. `handler.py` 消息入口 —— **被动感知**：记所有群友发言。门控 `"被动感知" in memory_mode`。
2. `handle_ai.py` 入口 —— **主动会话**：记触发者发言。**去重**：`"主动会话" in memory_mode and
   "被动感知" not in memory_mode`（否则 ① 已记过会二次写入）。
3. `bot.py` 发送路径 —— **主动会话**：记 Bot 自身回复。`speaker_id=f"__assistant_{bot_id}__"`，
   `observe()` 据前缀路由到 SELF scope。

## 9.4 Ingestion 摄入引擎（`memory/ingestion/`）

`IngestionWorker`（`worker.py`）从队列消费，按 `scope_key` 分组缓冲，满足时间窗或数量阈值时
flush。

| 参数 | 默认 | 说明 |
|------|------|------|
| `batch_interval_seconds` | 1800（30min） | 聚合窗口，超时强制 flush |
| `batch_max_size` | 30 | 单次最大聚合条数 |
| `llm_semaphore_limit` | 2 | 并发 LLM 上限 |

Flush：`create_episode()` → `_llm_extract()` → `extract_and_upsert_entities()`（两阶段去重）→
`extract_and_upsert_edges()`（冲突检测）→ user_global 跨群属性 → `check_and_trigger_hierarchical_update()`。

- **Entity 两阶段去重**（`entity.py`）：Phase 1 精确名称匹配；未命中 Phase 2 Qdrant 向量相似，
  `similarity >= dedup_similarity_threshold(0.92)` 视为同一实体合并。
- **Edge 冲突检测**（`edge.py`）：向量搜同源同目标已有 Edge，`< edge_conflict_threshold(0.88)`
  判冲突，旧 Edge `invalid_at = now`。

> 🔴 **IngestionWorker 必须跑在主事件循环**（历史缺陷，必读 [§12](./12-developer-pitfalls.md)）。曾
> 改成独立线程双事件循环（动机"避免 LLM 调用阻塞主循环"是**误判**——LLM 调用是 `await` 的纯
> 网络 I/O，等待期间不占循环）。双循环与主循环共享三个循环亲和资源（pydantic_ai 缓存的
> `httpx.AsyncClient`、全局 SQLAlchemy 引擎、全局 `AsyncQdrantClient`），批次超时的跨循环取消
> 击穿主循环 Proactor（WinError 995 → InvalidStateError → 主循环崩溃 → **WS 全线断连**）。现已
> 回归主循环 `asyncio.create_task`（与 ImageUnderstandWorker 同架构）。**不要再尝试独立线程。**

## 9.5 双路检索引擎（`memory/retrieval/`）

- **System-1**（`system1.py`）：对 Episode/Entity/Edge 三个 Qdrant Collection 分别向量搜，
  **RRF（Reciprocal Rank Fusion）** 融合（`score = Σ 1/(k+rank_i)`, k=60）+ One-hop 邻居扩展。
- **System-2**（`system2.py`）：从顶层 Category BFS，每层 LLM 判哪些子节点相关、逐层深入到
  Entity 叶子。多次 LLM 调用，可 `enable_system2=False` 关。
- **合并 + Reranker**（`dual_route.py`）：`dual_route_retrieve()` 并行跑双路 → 合并去重 →
  Reranker 重排（三路 episodes/entities/edges `asyncio.gather` 并行）→ `MemoryContext`。

```python
@dataclass
class MemoryContext:
    episodes: list[dict]; entities: list[dict]; edges: list[dict]; retrieval_meta: dict
    def to_prompt_text(self, max_chars=3000) -> str: ...   # 【已知事实】+【历史对话片段】
```

## 9.6 分层语义图（`memory/ingestion/hiergraph.py`）

把大量 Entity 归纳为多层 Category，支撑 System-2 自顶向下遍历。`incremental_rebuild()` 增量构建，
关键优化（按存量收费 → 按新增收费）：

- 小 scope 跳过：总 Entity < `MIN_ENTITIES_FOR_HIERGRAPH(30)` → 仅更 Meta 返回（类目对小数据无收益）。
- 单轮上限：按 `created_at` 取最旧至多 `MAX_ENTITIES_PER_REBUILD(800)` 个，超额续轮（backlog 单调收敛）。
- 向量预分配：与已归类近邻 `summary_dense` 余弦 ≥ `VECTOR_ASSIGN_THRESHOLD(0.85)` → 直接并入，零 LLM。
- Layer-2/3 仅取"尚无父类目"的下层节点喂 LLM（`_filter_unparented`），消除高频复发 token。

## 9.7 数据库模型（`memory/database/models.py`）

记忆系统用 `SQLModel, table=True`（**非** `BaseIDModel`），主键 `uuid.uuid4()`。

| 模型 | 表名 | 说明 |
|------|------|------|
| `AIMemEpisode` | `aimemepisode` | 原始对话片段 |
| `AIMemEntity` | `aimementity` | 实体节点（唯一约束 `(scope_key, name)`） |
| `AIMemEdge` | `aimemedge` | 实体间关系边（`fact`/`valid_at`/`invalid_at`/`decay_score`/`mention_count`/`last_accessed`） |
| `AIMemCategory` | `aimemcategory` | 分层语义图节点 |
| `AIMemCategoryEdge` | `aimemcategoryedge` | Category↔Category 层次关联 |
| `AIMemHierarchicalGraphMeta` | `aimemhierarchicalgraphmeta` | 分层图构建状态（定义在 `hiergraph.py` 而非 models.py） |

关联表：`mem_episode_entity_mentions`、`mem_category_entity_members`。

> ⚠️ **ORM Relationship 用 `lazy='noload'` 显式加载**，不是 `'selectin'`（历史缺陷 D-17：N+1
> 查询）。向量去重用 `asyncio.gather` 并行而非 O(N) 串行 await（D-15）。

## 9.8 向量存储（`memory/vector/`）

复用 `rag/base.py` 的 Qdrant 客户端 + `embedding_provider`，3 个独立 Collection：
`memory_episodes` / `memory_entities` / `memory_edges`。用 `client.query_points()`（非弃用的
`client.search()`）。向量维度随启用嵌入模型动态变化（默认回退 512）。

## 9.9 偏好记忆（Procedural / Preference Memory，2026-06-15，默认开）

与 Episode/Entity/Edge 三层**陈述性**记忆正交，新增 `AIMemPreference` 表（**SQL-only、不写
向量**），承载"针对 Agent 未来行为的纠正 / 偏好规则"（"以后画图用竖图""按我时区"）。

链路：

- **门控探测**（`observer.py`）：纯规则零 LLM 的 `detect_correction_intent()` 命中纠错意图 →
  强制 `HIGH` + 即时 flush。
- **蒸馏门控**：实体抽取 LLM 顺手判 `pref` 布尔位（`prompts/extraction.py` 的
  `PREFERENCE_FLAG_INSTRUCTION`），命中才跑第二次独立蒸馏 LLM
  （`worker._extract_and_upsert_preferences`）。
- **轨迹背景**（`ingestion/tool_trace.py`）：有界 ring buffer 记最近工具调用，供蒸馏把"参数
  传错了"蒸成带具体参数的规则。
- **写入**：`AIMemPreference.upsert()`（语义等价强化 / 极性反转软停用 / 新建）。
- **注入**（`retrieval/dual_route.py`）：检索时 SQL 精确取活跃规则，**置顶强约束**注入。
- **选择性注入（精确能力域过滤）**：`handle_ai` 按**意图门**（纯闲聊不注入）+ **能力域过滤**
  传参；能力域信号 = `_relevant_preference_contexts(query)` 子串近似 **∪**
  `session.get_assembled_capability_domains()`。纠错规则与 `general` 通用规则**永远注入**。
- **生命周期**（`lifecycle/consolidation_worker.py`）：按 salience 裁剪，纠错类受保护。
- **清空联动**（`clear_ops.py`）：清空用户记忆一并删偏好规则。

**能力域精确化（"装配后回传"）**：`GsCoreAIAgent.run()` 装配工具后把本轮工具的
`capability_domain` 集合回填 `self._last_assembled_domains`，经
`get_assembled_capability_domains()` 暴露；`handle_ai` 下一轮检索读回，**只注入"本轮可用工具"
相关的软偏好**，避免无关规则挤占预算 / 分散工具调用注意力。

> ⚠️ **默认开的成本**：开箱启用工具轨迹记录 + 纠错探测 + 第二次蒸馏 LLM + 置顶强约束注入。
> 误抽偏好以强约束置顶可能过度约束工具调用（已有 WebConsole 软停用 + salience 裁剪 + 精确能力
> 域过滤兜底）。`tool_trace` 是进程内存，多实例不共享。

## 9.10 RF-Mem 双过程检索（2026-06-15，**默认关**）

`memory/retrieval/familiarity.py` 接入"回忆-熟悉度双过程理论"：

- **熟悉度探针**（`vector/ops.probe_episode_scores`）：一次纯 dense 查询取真实余弦分，算均分 s̄
  + 列表熵 H(p)，逐查询决定"检索多深"，把 System-2 从全局静态开关降为"按不确定性触发"。
- **回忆环**（零 LLM 的 KMeans + α-mix 多轮向量深检索）：低熟悉且 System-2 未触发时补召回，把
  召回 Episode 链**关系投影**成精准 Edge 事实。KMeans 走专用线程池，不阻塞事件循环。

> ⚠️ **默认关的原因**：阈值（`familiarity_theta_*` / `tau`）是论文英文模型经验值，中文本地模型
> 通常需平移、需离线标定后再放量；回忆环强绑 `qdrant_provider=remote`（本地嵌入式 Qdrant 是
> O(N) 暴力扫，多轮成倍放大成本）。配置描述已标注"需标定"。

## 9.11 记忆生命周期（C11，`memory/lifecycle/`）

`run_lifecycle_maintenance` 由 APScheduler **每周**触发，纯规则无 LLM：

- **巩固**：`mention_count ≥ 3` 的高频 Edge `decay_score` 回升 1.0。
- **衰减**：14 天未检索且非高频的 Edge `decay_score *= 0.85`。
- **遗忘**：`decay_score < 0.1` 的 Edge 物理删除（SQL + Qdrant）。
- **孤儿实体回收**（遗忘 Edge 后）：非 speaker、无 edge、`updated_at` 超 10 天的孤儿物理删除
  （SQL + Qdrant + 递减分层图计数，按 500 分块）。

`ingestion/edge.py` 增否定极性矛盾检测：同 src/tgt 高相似但极性相反 → 旧 Edge 软删除 + 记
`AIMemConflict`，不向 LLM 堆叠新旧矛盾。

## 9.12 多模态摄入（C9，`memory/ingestion/multimodal.py`）

Observer Hook 检测到图片 → `submit_image_observation` 纯规则过滤（URL 去重 + 按 scope 限流）→
投入独立 `_multimodal_queue`（与文本 `observation_queue` **物理隔离**）→ `ImageUnderstandWorker`
异步调 `understand_image` 转述 → 以 `[图片理解]` 前缀包装成观察记录推入主 `observe()` 管道。
**图片风暴不阻塞文本聊天。**

## 9.13 配置项（`memory/config.py`）

全局单例 `memory_config = MemoryConfig()`。要点：`observer_enabled`(True) /
`observer_blacklist` / `ingestion_enabled`(True) / `enable_retrieval`(True) /
`enable_system2`(True) / `enable_user_global_memory`(False) / `retrieval_top_k`(10) /
`dedup_similarity_threshold`(0.92) / `edge_conflict_threshold`(0.88)。

记忆运行统计集成在 AI Statistics（`record_memory_*`，7 项指标进 `AIDailyStatistics`，见
[§11](./11-statistics-webconsole-database.md)）。
