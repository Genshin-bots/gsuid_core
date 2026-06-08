# GsCore 记忆系统全链路文档

> 本文档完整梳理 `gsuid_core/ai_core/memory/` 记忆系统的全部链路：
> **消息进入 → 门控分级 → 摄入队列 → LLM 抽取 → SQL/向量双写 → 分层图构建/重建 → 双路检索 → 生命周期维护**。
> 配套设计细节见 [`AI_TRIGGER_FLOW.md`](AI_TRIGGER_FLOW.md) 第 10/13 章；本文为面向全局的单文档总览。

---

## 目录

1. [系统概览](#1-系统概览)
2. [核心概念](#2-核心概念)
3. [数据模型（SQL + 向量）](#3-数据模型sql--向量)
4. [写入链路：从消息到图谱](#4-写入链路从消息到图谱)
5. [分层语义图：构建与重建](#5-分层语义图构建与重建)
6. [检索链路：双路召回与注入](#6-检索链路双路召回与注入)
7. [生命周期维护](#7-生命周期维护)
8. [配置项与关键常量](#8-配置项与关键常量)
9. [文件职责索引](#9-文件职责索引)
10. [关键设计约束与不变量](#10-关键设计约束与不变量)

---

## 1. 系统概览

记忆系统是一套**与 AI 发言决策正交的被动认知层**：它读取消息、沉淀长期记忆，但不决定是否回复。整体分三段：

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 写入链路（异步，独立线程）                                                    │
│                                                                            │
│  消息入口            门控+分级         摄入队列         IngestionWorker        │
│  handler.py    ┐                                      (独立线程事件循环)       │
│  bot.py        ├─► observe() ─► _gate() ─► queue.Queue ─► _ingest_batch()    │
│  handle_ai.py  ┘   (纯规则)     (HIGH/LOW)  (maxsize 1万)      │              │
│                                                               ▼              │
│                                          ┌── Episode（原文，始终写）           │
│                                          ├── LLM 抽取 Entity / Edge           │
│                                          ├── SQL 写入 + Qdrant 向量写入        │
│                                          └── 触发分层图增量重建（异步）          │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│ 分层图（按 scope 累积）                                                       │
│  Entity ──LLM/向量分类──► Layer-1 Category ──► Layer-2 ──► Layer-3 (顶层)     │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│ 检索链路（AI 回复前，主事件循环）                                              │
│  query ─► dual_route_retrieve ─┬─ System-1：向量相似度（episode/entity/edge）  │
│                                └─ System-2：分层图自顶向下遍历                  │
│            ─► Reranker 重排 ─► MemoryContext ─► 注入 Prompt                   │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│ 生命周期（APScheduler 每周一次，纯规则无 LLM）                                 │
│  巩固 Edge ─► 衰减 Edge ─► 遗忘 Edge ─► 回收孤儿 Entity                       │
└──────────────────────────────────────────────────────────────────────────┘
```

**两个存储后端协同**：
- **SQL（SQLModel/SQLite）**：结构化真值，存 Episode/Entity/Edge/Category/层次关联/冲突/分层图元数据。
- **Qdrant 向量库**：三个 Collection 存语义向量，供相似度检索与去重。SQL 是权威源，向量是其语义索引。

---

## 2. 核心概念

### 2.1 Scope（命名空间隔离）

所有记忆节点都携带 `scope_key`，实现严格隔离。定义见 [`scope.py`](../gsuid_core/ai_core/memory/scope.py)：

| ScopeType | 格式 | 含义 |
|---|---|---|
| `GROUP` | `group:{群号}` | 群组级记忆（群 A 对群 B 不可见） |
| `USER_GLOBAL` | `user_global:{用户ID}` | 用户**跨群**全局画像 |
| `USER_IN_GROUP` | `user_in_group:{用户ID}@{群号}` | 用户在特定群的局部档案（精细化，可选） |
| `SELF` | `self:{bot_self_id}` | Bot 自身情景记忆（"我说过/做过什么"） |

隔离由 SQL `WHERE scope_key=?` 与 Qdrant `scope_key` payload filter 共同保证。检索时可联合查询多个 scope（群组 + 用户全局）。

### 2.2 三层 Base Graph（论文 Section 2.1）

```
Episode（原始对话片段）──提及──► Entity（实体节点）──关系──► Edge（事实，有向边）
```

- **Episode**：聚合后的对话原文，永远保存（即便不抽实体）。是所有记忆的 durable 素材。
- **Entity**：从对话抽取的实体（人、物、概念）。`is_speaker=True` 的是发言者本身。
- **Edge**：两个 Entity 间的一条**可验证事实**（如"用户A 喜欢 咖啡"）。

### 2.3 分层语义图（Hierarchical Graph）

把海量 Entity 自底向上归纳为多层 Category（Layer-1 最具体，层号越大越抽象），供 System-2 自顶向下遍历检索。

### 2.4 记忆双开关（`memory_mode` / `memory_session`）

`memory_mode`（可多选）决定**记录谁的话**：

| 值 | 记录内容 | 入口 | 写入去向 |
|---|---|---|---|
| `被动感知` | 群内**所有成员**发言（无需触发命令/AI） | `handler.py` | GROUP/USER_GLOBAL，可抽实体 |
| `主动会话` | 仅 AI 实际参与的那轮：**触发者发言** + **Bot 自身回复** | `handle_ai.py` + `bot.py` | 触发者→群组图谱；Bot 回复→SELF（仅 Episode） |

`memory_session` 决定**被动感知的范围**：`全部群聊`（所有群）或 `按人格配置`（仅命中人格范围的 session）。

> 两路可同时开启。同时开启时，触发者发言已被"被动感知"在入口记录过一次，"主动会话"路径会自动跳过（`"被动感知" not in memory_mode` 守卫），避免二次写入。

---

## 3. 数据模型（SQL + 向量）

### 3.1 SQL 表（[`database/models.py`](../gsuid_core/ai_core/memory/database/models.py)）

| 表（类名） | 关键字段 | 说明 |
|---|---|---|
| `AIMemEpisode` | `scope_key, content, speaker_ids, valid_at, qdrant_id` | 对话片段原文 |
| `AIMemEntity` | `scope_key, name, summary, tag, is_speaker, user_id, created_at, updated_at, qdrant_id` | 实体节点；`UniqueConstraint(scope_key, name)` |
| `AIMemEdge` | `scope_key, fact, source_entity_id, target_entity_id, valid_at, invalid_at, mention_count, decay_score, last_accessed, qdrant_id` | 事实边（含时效/置信度字段） |
| `AIMemConflict` | `scope_key, fact_signature, old_edge_id, new_edge_id, summary` | 语义矛盾记录（极性相反的事实） |
| `AIMemCategory` | `scope_key, name, summary, tag, layer` | 分层图类目节点 |
| `AIMemCategoryEdge` | `parent_category_id, child_category_id` | 类目↔类目层次关联（多对多） |
| `AIMemHierarchicalGraphMeta` | `scope_key, max_layer, last_rebuild_at, entity_count_at_last_rebuild, current_entity_count, group_summary_cache` | 每 scope 的分层图状态追踪 |

**两张多对多关联表**：
- `mem_episode_entity_mentions(episode_id, entity_id)`：Episode 提及了哪些 Entity。
- `mem_category_entity_members(category_id, entity_id)`：Layer-1 Category 包含哪些 Entity。

### 3.2 Qdrant Collection（[`vector/collections.py`](../gsuid_core/ai_core/memory/vector/collections.py) / [`vector/startup.py`](../gsuid_core/ai_core/memory/vector/startup.py)）

| Collection | 向量结构 | 距离 | 用途 |
|---|---|---|---|
| `memory_episodes` | `dense` + `sparse(BM25)` | COSINE | Episode 内容检索 |
| `memory_entities` | `name_dense` + `summary_dense` + `sparse` | COSINE | Entity 去重 / 相似度归类 |
| `memory_edges` | `dense` + `sparse` | COSINE | Edge 事实检索 / 归并 |

- 向量 `point.id` == 对应 SQL 行的 `qdrant_id`（通常等于主键 id）。
- 检索默认走 **Hybrid（Dense + Sparse RRF 融合）**；Sparse 模型不可用时降级纯 Dense。
- Collection 维度随嵌入模型自动检测；维度变更会导出 payload→重建→重嵌入（迁移逻辑在 `vector/startup.py`）。

---

## 4. 写入链路：从消息到图谱

### 4.1 消息进入（三个入队点）

| # | 入口 | 触发条件 | `observe()` 入参 |
|---|---|---|---|
| ① | [`handler.py`](../gsuid_core/handler.py) `handle_event` | `enable_ai and not ai_scope_banned and (有文本/图片)` 且 `enable_memory + observer_enabled + "被动感知" in memory_mode`；范围再受 `memory_session` 约束 | `speaker_id=user_id` |
| ② | [`handle_ai.py`](../gsuid_core/ai_core/handle_ai.py) `handle_ai_chat` 入口 | `enable_memory and "主动会话" in memory_mode and "被动感知" not in memory_mode` | `speaker_id=user_id`（触发者） |
| ③ | [`bot.py`](../gsuid_core/bot.py) `_Bot.send()` 发送路径 | `enable_ai and enable_memory and "主动会话" in memory_mode` | `speaker_id=__assistant_{bot_id}__`（Bot 回复） |

### 4.2 门控与分级（[`observer.py`](../gsuid_core/ai_core/memory/observer.py)，纯规则，零 LLM）

`observe()` 先按发言人分流，再过门控 `_gate()`：

- **Bot 自身发言**（`__assistant_` 前缀）→ 路由到 `SELF` scope，强制 `value_tier=LOW`，**仅写 Episode、不进群组事实图谱**（C6：杜绝 Bot 戏言污染群记忆）。
- **普通用户消息** → 计算 GROUP/USER_GLOBAL scope，过 `_gate()`：
  - **丢弃**：自身数字 ID 消息、黑名单群、空消息、纯图片短消息、命令回显、用户命令/typo（按 `command_start` 匹配）、prompt 注入文本、复读（同 scope 最近 12 条重复）。
  - **分级 `_classify_value_tier`**（受 `extraction_value_gate` 档位控制）：含姓名自述/称呼偏好/承诺/数字日期/情绪词 → `HIGH`；其余按档位归档——`宽松`（等价旧行为）：纯寒暄且 <10 字且无实体特征 → `LOW`，其余 `HIGH`；`均衡`（默认）：无实体特征即 `LOW`；`严格`：仅强信号/情绪为 `HIGH`，其余全 `LOW`。**任何档位下 `LOW` 仍完整写 Episode，只省抽取 Token、不丢原文。**

放行的记录封装为 `ObservationRecord`，`put_nowait` 进 `queue.Queue`（线程安全，maxsize 10000，满则丢最老一条）。

### 4.3 摄入 Worker（[`ingestion/worker.py`](../gsuid_core/ai_core/memory/ingestion/worker.py)）

`IngestionWorker` 在**独立线程的事件循环**中运行（关键：LLM 抽取不阻塞主事件循环 → 不触发 WebSocket 心跳超时）：

- 按 `scope_key` 分桶缓冲；满足 **数量阈值（`batch_max_size`=80）** 或 **时间窗（`batch_interval_seconds`，默认 7200s，timer 每 30s 检查）** 时 `_flush`。批越大 / 窗越长，单次抽取覆盖的对话越完整、调用次数越少，固定 prompt 开销被摊薄。
- `_flush` 内以 `batch_max_size` 切批，每批 `_ingest_batch` 有 120s 超时；失败**以批为单位**退回缓冲重试（已成功批绝不重摄，避免重复 Episode）。
- 并发受 `llm_semaphore_limit`（默认 3）约束。

### 4.4 单批摄入 `_ingest_batch`

```
1. 拼接对话文本 → 写 Episode（含 LOW 价值消息，始终持久化 + 写向量）
2. 分流：SELF scope 或全 LOW 批 → 仅写 Episode，跳过抽取（return）
3. 否则 _extract_and_upsert_from_episode（best-effort，抽取异常不连累 Episode）
```

`_extract_and_upsert_from_episode` 步骤：
- 仅用 HIGH 价值消息拼接，并经 `_compact_high_records_dialogue` **折叠纯表情/标点/语气词等无实体信息行 + 合并相邻重复 + 合并连续同一发言者为一轮**后喂给 LLM（去掉重复的 `[speaker]:` 前缀、给 LLM 更连贯的发言；Episode 已存全文，此折叠只省抽取 Token）；折叠后为空则跳过 LLM 抽取。
- 附最近 `background_episode_count` 条 Episode 作背景上下文（默认 **1**，单条截断至 `background_episode_max_chars`=600 字符；置 0 则不注入背景、省去该查询与其 Token）。
- `_llm_extract`：注入"本群已知别名 + 已存在实体"指导 LLM，输出简写键 JSON，解析为 `{entities, edges}`；超长对话分片提取再合并去重。
- `_apply_master_tags`：主人（`core_config.masters`）对应 Speaker 实体打 `Master` 标签。
- `_apply_alias_redirection`：LLM 标注的别名实体合并到正式实体，重写 Edge 引用（实体消歧 Level-1）。
- 更新**群组画像**（`group_profile`）：记录别名映射 `term_mappings`、累计实体名频次作群组话题标签。
- `extract_and_upsert_entities` → `extract_and_upsert_edges` → 触发分层图检查。
- 增量维护 `current_entity_count`（`increment_entity_count`，O(1)，避免全表 COUNT）。
- **user_global 跨群属性**：`scope_hint == "user_global"` 的实体额外写入发言者的 `user_global:{uid}` scope。

### 4.5 Entity 去重写入（[`ingestion/entity.py`](../gsuid_core/ai_core/memory/ingestion/entity.py) + `AIMemEntity.extract_and_upsert`）

两阶段去重：
1. **精确名称匹配**（`scope_key + name` 唯一索引，O(1)）。
2. **向量混合检索**（BM25+向量 RRF）：相似度 ≥ `dedup_similarity_threshold`（0.92）视为同一实体。

命中既有实体 → 合并 summary（上限 2000 字符）/ 合并 tag / 刷新 `updated_at`；否则新建。最后批量写 Qdrant 向量（3 次指数退避重试 + 30s 全局超时，保障 SQL/向量一致）。

### 4.6 Edge 写入（[`ingestion/edge.py`](../gsuid_core/ai_core/memory/ingestion/edge.py)）

对每条 Edge，先并行检索语义等价的既有 Edge（同 src/tgt 且相似度 ≥ `edge_conflict_threshold`=0.88）：
- **可归并**（极性相同）→ 不写新边，既有边 `mention_count += 1` 并刷新 `valid_at`（C1 跨发言者归并）。
- **极性相反**（`_fact_polarity` 否定词奇偶判断）→ 语义矛盾：旧边软删除（`invalid_at`），记录 `AIMemConflict`，以新事实为准（C11）。
- **无等价** → 创建新 Edge + 收集向量，批量写 Qdrant。

### 4.7 群组画像与多模态

- **群组画像**（[`group_profile.py`](../gsuid_core/ai_core/memory/group_profile.py)）：累计 `term_mappings`（别名→正式名）、实体话题标签，并缓存 `group_summary`，供检索期"别名展开"和 Heartbeat 群摘要注入。
- **多模态**（[`ingestion/multimodal.py`](../gsuid_core/ai_core/memory/ingestion/multimodal.py)）：高价值图片走独立队列，`ImageUnderstandWorker` 异步转述为文本后，以 `[图片理解]` 前缀推回主 `observe()` 管道，不阻塞文本聊天。

---

## 5. 分层语义图：构建与重建

入口 [`ingestion/hiergraph.py`](../gsuid_core/ai_core/memory/ingestion/hiergraph.py) `HierarchicalGraphBuilder.incremental_rebuild()`。

> **构建消费方与 `hiergraph_build_mode` 门控（成本关键）**：分层类目树**只被 System-2 检索消费**（`dual_route` 中 `enable_system2` 门控）；唯一被"非 System-2"路径消费的产物是 `group_summary_cache`（Heartbeat 决策 + 人格群语境）。因此 `incremental_rebuild` 在小 scope 跳过后先判 `hiergraph_build_mode`：
> - `自动`（默认）：仅当 `enable_system2` 开启才构建整棵类目树；否则走 `_summary_only_rebuild`——**跳过 Layer-1/2/3 的全部 LLM 分类**，仅在新增实体 ≥ 50 时从高频实体名直接刷新群摘要（至多 1 次 LLM）。
> - `始终`：总是构建完整类目树（旧行为）。
> - `仅摘要`：从不建树，仅按需刷新群摘要。
> - `关闭`：既不建树也不刷摘要（重建零 LLM）。
>
> System-2 关闭时，类目树无任何消费方，构建它纯属浪费——这是大规模下重建 Token 的最大可削减项。Entity/Edge/Episode 等记忆本体不受任何模式影响。

### 5.1 触发判定（`_check_should_rebuild`）

满足任一即触发（增量计数，不扫全表）：
- 首次（无 meta）；
- `current_count > baseline × hiergraph_rebuild_ratio`（2.50）且新增 ≥ `MIN_DELTA`(20)；
- 距上次重建 > `hiergraph_rebuild_interval_seconds`（48h）。

由 `rebuild_task` 加**每 scope 锁**串行执行，防并发重建。

### 5.2 重建主流程（含四项成本优化）

```
incremental_rebuild()
 ├ [小 scope 跳过] 总实体 < hiergraph_min_entities(默认30,可配) → 仅更新 Meta 返回
 │     （类目对小数据集无收益，召回靠 System-1 向量 + edges）
 │
 ├ 取未分配实体 _get_unassigned_entities(limit=MAX_ENTITIES_PER_REBUILD)
 │     ├ [入口过滤] 仅喂 is_speaker 或"至少有一条 edge"的实体
 │     │     （无 edge 噪声实体不进 LLM；它们留作去重锚点，由孤儿 GC 回收）
 │     └ [单轮上限] 按 created_at 取最旧的至多 800 个，超额留待续轮
 │
 ├ Layer-1 归类
 │     ├ [向量预分配] 与"已归类近邻"summary_dense 余弦 ≥ 阈值(默认0.85,可配)
 │     │     → 直接并入近邻所在 Category（_vector_pre_assign，零 LLM）
 │     └ 残余（speaker + 未命中）才走 LLM（_llm_categorize / _apply_entity_assignments）
 │           · speaker 由 M-06 强制归入 "Speaker" Category
 │
 ├ Layer-2/3 逐层增量构建
 │     ├ [增量化] 仅取"尚无父类目"的下层节点喂 LLM（_filter_unparented）
 │     │     （把"按存量收费"降为"按新增收费"）
 │     ├ 下层全部已归类 → 跳过 LLM，推进到上层
 │     └ 违反 node count reduction rule（本层 ≥ 下层）→ 回滚本层并终止
 │
 ├ 更新 Meta（max_layer 取 valid_prev_layer，rollback 安全）
 ├ 按需重算群组摘要缓存（_should_regen_group_summary 命中才发 LLM）
 └ [续清] 本轮达单轮上限 → 再调度一次 rebuild_task（backlog 单调收敛）
```

**LLM 分类 `_llm_categorize`**：按 `hiergraph_batch_size`（默认 20，可配置）分批；已有类目数限制 `hiergraph_max_existing_cats`（默认 50，可配置）防 prompt 爆炸；每节点附带的实体摘要长度由 `hiergraph_node_summary_chars`（默认 60，可配置，0=不带）控制；失败兜底为"每节点单独成类"。

> **建树期 token 压缩（need_tree 路径）**：① 移除冗余的"待分类节点示例"（原是 `nodes_info` 前 5 条的重复，抽象粒度已由 `layer_hint` 一行类比给出）；② 现有类目**只发名称**（复用按名称匹配，逐条 summary 每批重发收益低）；③ `hiergraph_batch_size` 调大 → 单轮调用更少、摊薄每批固定开销；④ `hiergraph_vector_assign_threshold` 调低 → 更多实体走零 LLM 的向量预分配路径。

> 这些优化的目的：实体规模上 10 万级后，旧逻辑每轮重建按存量收费、token 几何级上升。优化后一次常规重建的 LLM 调用从"几百~几千次"降到"个位数甚至 0"。

---

## 6. 检索链路：双路召回与注入

入口 [`retrieval/dual_route.py`](../gsuid_core/ai_core/memory/retrieval/dual_route.py) `dual_route_retrieve()`，在 [`handle_ai.py`](../gsuid_core/ai_core/handle_ai.py) AI 回复前调用。

### 6.1 触发与门控

`handle_ai.py` 先过 **C4 寒暄门控** `_should_retrieve_memory`：主人 / 回指词（"之前/上次/你说过…"）/ 情绪词 / 实体 → 强制检索；纯短寒暄（闲聊 + <12 字 + 无实体）→ 跳过，省向量+Reranker 开销。

### 6.2 双路并行

- **System-1（向量相似度，[`system1.py`](../gsuid_core/ai_core/memory/retrieval/system1.py)）**：对所有 scope（群组 + 用户全局）并行检索 episode/entity/edge（Hybrid RRF），再做 **One-hop 邻居扩展**（沿 Edge 把另一端 Entity 纳入）。
- **System-2（分层图遍历，[`system2.py`](../gsuid_core/ai_core/memory/retrieval/system2.py)）**：从顶层 Category 出发，每层 LLM 选择相关节点（可"取全部子孙"快捷），逐层向下到 Layer-1，收集成员 Entity 及其 Edge/Episode（用 Recursive CTE 一次性取子孙，带深度熔断防环）。可由 `enable_system2` 关闭以省成本。

### 6.3 合并、重排、注入

1. S1 + S2 结果按 id 合并去重。
2. **类型隔离 Rerank**：episode/entity/edge 三路并行过 Reranker；**Category 跳过 Reranker**（字面重合度低，给固定最高优先级，保证 LLM 总能看到大纲）。
3. **置信度富集**：命中的 Edge 从 DB 取 `mention_count`/`decay_score`，折算 `weight = 佐证×新鲜度`（`compute_edge_confidence`）；同时刷新 `last_accessed`（供衰减判定）。
4. `MemoryContext.to_prompt_text(max_chars)` 按预算注入：
   - **核心事实（edges）≈55%**：过滤失效边/低置信边，按 fact 签名去重，主人 edge 上浮、事件型 trivia 下沉。
   - **语义类目摘要（categories）≈15%**：话题大纲。
   - **相关对话片段（episodes）≈30%**：少量最相关轮次。

> 注意：**entities 被检索/重排但不直接注入 Prompt**——它们通过 edges（事实）和 categories（大纲）间接发挥作用。这也是孤儿实体（无 edge）可安全回收的依据。

---

## 7. 生命周期维护

[`lifecycle/consolidation_worker.py`](../gsuid_core/ai_core/memory/lifecycle/consolidation_worker.py) `run_lifecycle_maintenance`，由 APScheduler **每周一次**触发，**纯规则、零 LLM**：

| 步骤 | 规则 | 常量 |
|---|---|---|
| **巩固** | `mention_count ≥ 3` 的高频 Edge `decay_score` 回升 1.0 | `PROTECT_MENTION_COUNT=3` |
| **衰减** | 超 14 天未被检索且非高频的 Edge `decay_score *= 0.85` | `DECAY_STALE_DAYS=14, DECAY_FACTOR=0.85` |
| **遗忘** | `decay_score < 0.1` 的 Edge 物理删除（SQL + Qdrant） | `FORGET_THRESHOLD=0.1` |
| **孤儿实体回收** | 遗忘 Edge 后：非 speaker、无任何 edge、`updated_at` 超 10 天的实体物理删除（SQL + Qdrant + 递减分层图计数，按 500 分块） | `ORPHAN_ENTITY_TTL_DAYS=10` |

> 遗忘 Edge 是孤儿实体的主要来源，故孤儿 GC 紧随其后，防止实体只增不减膨胀分类成本。

衰减结果在检索期被 `reranker_score × decay_score` 加权消费，使活跃记忆始终优先。

**清空操作**（[`database/clear_ops.py`](../gsuid_core/ai_core/memory/database/clear_ops.py)）：按 scope 精确/前缀/后缀匹配批量清空（群级、用户全局级），同步删 SQL + Qdrant，支持 `dry_run`。

---

## 8. 配置项与关键常量

### 8.1 运行时配置（[`config.py`](../gsuid_core/ai_core/memory/config.py) + [`ai_config.py`](../gsuid_core/ai_core/configs/ai_config.py)）

| 配置 | 默认 | 说明 |
|---|---|---|
| `observer_enabled` | True | 消息观察者总开关 |
| `observer_blacklist` | [] | 不记忆的群组 ID |
| `ingestion_enabled` | True | 摄入引擎开关 |
| `batch_interval_seconds` | 7200 | 聚合窗口（超时强制 flush）；越长越省 Token |
| `batch_max_size` | 80 | 单次最大聚合条数；越大调用越少、摊薄固定开销 |
| `background_episode_count` | 1 | 抽取时注入的近期 Episode 背景数（0=不注入） |
| `background_episode_max_chars` | 600 | 每条背景 Episode 在抽取提示词中的字符上限 |
| `extraction_value_gate` | 均衡 | 抽取价值门控档位（宽松/均衡/严格），调严省 Token 不丢原文 |
| `hiergraph_build_mode` | 自动 | 分层图构建模式（自动/始终/仅摘要/关闭）；自动模式下 System-2 关闭即跳过整棵类目树 |
| `llm_semaphore_limit` | 3 | 并发 LLM 调用上限 |
| `enable_retrieval` | True | 检索注入开关 |
| `enable_user_global_memory` | True | 联合用户跨群画像 |
| `enable_heartbeat_memory` | True | Heartbeat 注入群摘要 |
| `search_edge_count` | 30 | Edge 注入上限 |
| `min_edge_weight` | 0.0 | 置信度轴过滤阈值 |
| `min_edge_rerank_score` | 0.0 | 相关性轴过滤阈值 |
| `dedup_similarity_threshold` | 0.92 | Entity 去重阈值 |
| `edge_conflict_threshold` | 0.88 | Edge 归并/冲突阈值 |
| `min_children_per_category` | 3 | 每类目最少子节点 |
| `max_layers` | 3 | 分层图最大层数 |
| `hiergraph_rebuild_ratio` | 2.50 | 实体增长触发比例 |
| `hiergraph_rebuild_interval_seconds` | 172800 | 重建时间窗（48h） |
| `retrieval_top_k` | 15 | 最终检索数（ai_config） |
| `memory_inject_max_chars` | — | 注入字符预算（ai_config） |
| `enable_system2` / `eval_mode` | — | System-2 开关 / 评测模式（ai_config） |
| `memory_mode` | `[被动感知,主动会话]` | 记忆路径（ai_config） |
| `memory_session` | `按人格配置` | 被动感知范围（ai_config） |

### 8.2 代码内常量

| 常量 | 值 | 位置 | 作用 |
|---|---|---|---|
| 队列 maxsize | 10000 | observer.py | 观察队列容量 |
| `_REPEAT_WINDOW` | 12 | observer.py | 复读检测窗口 |
| `_LOW_TIER_MAX_LEN` | 10 | observer.py | 短句降级 LOW 阈值 |
| `hiergraph_min_entities` | 30 | ai_config（可配置） | 小 scope 跳过分层图（含轻量摘要）；调大省 token |
| `MAX_ENTITIES_PER_REBUILD` | 800 | hiergraph.py | 单轮重建实体上限 |
| `hiergraph_vector_assign_threshold` | 0.85 | ai_config（可配置） | 向量预分配余弦阈值；调低省 token（更多实体走零 LLM 预分配） |
| `VECTOR_ASSIGN_TOP_K` | 5 | hiergraph.py | 预分配检索近邻数 |
| `hiergraph_batch_size` | 20 | ai_config（可配置） | LLM 分类批大小；调大省 token（更少调用、摊薄固定开销） |
| `hiergraph_max_existing_cats` | 50 | ai_config（可配置） | 喂入的已有类目上限（仅名称）；调小省 token |
| `hiergraph_node_summary_chars` | 60 | ai_config（可配置） | 分类输入中每节点的实体摘要字符上限（0=不带） |
| `hiergraph_summary_delta` | 50 | ai_config（可配置） | 群摘要刷新的新增实体阈值；调大省 token |
| `MIN_DELTA` | 20 | hiergraph.py | 重建最小增量 |
| `DECAY_STALE_DAYS` | 14 | consolidation_worker.py | 衰减判定天数 |
| `DECAY_FACTOR` | 0.85 | consolidation_worker.py | 单次衰减系数 |
| `PROTECT_MENTION_COUNT` | 3 | consolidation_worker.py | 高频保护阈值 |
| `FORGET_THRESHOLD` | 0.1 | consolidation_worker.py | 遗忘阈值 |
| `ORPHAN_ENTITY_TTL_DAYS` | 10 | consolidation_worker.py | 孤儿实体回收 TTL |

---

## 9. 文件职责索引

| 文件 | 职责 |
|---|---|
| `memory/scope.py` | Scope 类型与 `make_scope_key` |
| `memory/config.py` | `MemoryConfig` 运行时配置门面 |
| `memory/startup.py` | 记忆系统初始化（Collection + Worker + 生命周期定时任务注册） |
| `memory/observer.py` | 观察入口 `observe()` + 纯规则门控 `_gate()` + 分级 + SELF 路由 |
| `memory/ingestion/worker.py` | `IngestionWorker`（独立线程缓冲/flush）+ `_ingest_batch` + `_llm_extract` + 别名/主人标签 |
| `memory/ingestion/entity.py` | Entity 两阶段去重写入（SQL + 向量） |
| `memory/ingestion/edge.py` | Edge 归并/极性冲突检测/写入 |
| `memory/ingestion/hiergraph.py` | 分层图构建/增量重建 + 触发判定 + 向量预分配 + 元数据 |
| `memory/ingestion/multimodal.py` | 多模态（图片）异步转述入管道 |
| `memory/database/models.py` | 全部 SQLModel 模型 + 业务方法（衰减/遗忘/孤儿回收等） |
| `memory/database/clear_ops.py` | 按 scope 批量清空（SQL + Qdrant） |
| `memory/vector/collections.py` | Collection 名称常量 |
| `memory/vector/startup.py` | Collection 创建/维度迁移/重嵌入 |
| `memory/vector/ops.py` | Episode/Entity/Edge 向量 upsert/search + 近邻检索 |
| `memory/retrieval/dual_route.py` | 双路检索编排 + Reranker + `MemoryContext` 注入 |
| `memory/retrieval/system1.py` | 向量相似度检索 + One-hop 扩展 |
| `memory/retrieval/system2.py` | 分层图自顶向下遍历选择 |
| `memory/retrieval/types.py` | 检索层 TypedDict（Episode/Entity/Edge/Category） |
| `memory/lifecycle/consolidation_worker.py` | 巩固/衰减/遗忘 Edge + 孤儿 Entity 回收 |
| `memory/group_profile.py` | 群组画像（别名映射、话题标签、群摘要缓存） |
| `memory/prompts/*.py` | 抽取/分类/选择/摘要/类别归纳的 Prompt 模板 |

---

## 10. 关键设计约束与不变量

1. **门控与生命周期 100% 纯规则，绝不调用 LLM**：噪声过滤、分级、衰减、遗忘、孤儿回收均为正则/数值运算，杜绝在低价值环节烧 token。
2. **Episode 永远先持久化、durable**：抽取阶段（Entity/Edge/LLM）失败仅记日志、不退回 Episode 重试——Episode 无幂等键，重试会重复写入并使计数虚高。
3. **Scope 严格隔离**：群间记忆不可见；Bot 自身发言只进 `SELF` scope（仅 Episode、`LOW`），永不污染群组事实图谱（C6）。
4. **SQL 是权威、向量是索引**：写入先 SQL 后向量并带重试/超时；删除（遗忘/孤儿/清空）同步删两侧。
5. **记忆与发言决策正交**：即便人格纯静默，记忆仍在后台积累；`observe()` 永远 fire-and-forget，不阻塞主流程。
6. **entities 不直接注入 Prompt**：只通过 edges/categories 间接生效——这是无 edge 孤儿实体可安全回收的根据。
7. **重建成本随"新增"而非"存量"增长**：入口过滤 + 向量预分配 + Layer-2/3 增量化 + 单轮上限 + 小 scope 跳过五项叠加，保证大规模下重建仍可控。
8. **类型与异常**（遵循 [`LLM.md`](LLM.md)）：完整类型提示；不用 `cast`/`type:ignore`/`getattr`/`.get` 兜底掩盖类型问题；try-except 仅用于 Qdrant/LLM 等外部 I/O 边界的优雅降级。

---

> 维护提示：本系统的写入/检索/重建/维护四段相互解耦，改动某段时请对照第 10 章的不变量，并同步更新本文件与 [`AI_TRIGGER_FLOW.md`](AI_TRIGGER_FLOW.md) 的对应章节。
