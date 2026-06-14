# GsCore 记忆系统全链路文档（Mnemis 适配版）

> 本文档梳理 `gsuid_core/ai_core/memory/` 模块的完整链路：
> **消息进入 → 门控分级 → 观察队列 → IngestionWorker 摄入 → LLM 抽取 → SQL/向量双写 → 分层语义图构建/重建 → 双路检索 → 生命周期维护**。
>
> **本文以 [`gsuid_core/ai_core/memory/`](../gsuid_core/ai_core/memory/) 源码为唯一事实源**，定期与代码对齐；引用文件均给出相对路径与行号。
>
> 配套文档：
> - [`gsuid_core/webconsole/docs/22-ai-memory.md`](gsuid_core/webconsole/docs/22-ai-memory.md) — `/api/ai/memory/*` 接口详情。**注意：该文件部分默认值已与代码漂移，请以本文档第 8 章为准**。
> - [`docs/AI_QUESTION_FLOW_PLAYBOOK.md`](AI_QUESTION_FLOW_PLAYBOOK.md) — 触发链路整体视角。
>
> 相邻但**不属于本系统**的模块（本文不展开）：
> - [`gsuid_core/ai_core/persona/`](../gsuid_core/ai_core/persona/) — 主人格人设、情绪（`mood.py`）、群语境（`group_context.py`）、口吻锚点（`voice_anchor`），与记忆系统的群摘要缓存有 1 处耦合（见 §5.4）。
> - [`gsuid_core/ai_core/state_store/`](../gsuid_core/ai_core/state_store/) — **通用**持久 KV / 结构化集合工具，记忆系统**复用**其底层（`AIPersistentState` 表）来存群组画像，详见 §5.4。
> - [`gsuid_core/message_history/`](../gsuid_core/message_history/) — 会话级短期消息历史（与本系统的"长期图记忆"正交，不存盘、不进向量库）。

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

记忆系统是一套**与 AI 发言决策正交的被动认知层**：它读取消息、沉淀长期记忆，但不决定是否回复。整体分四段：

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 写入链路（主事件循环上的 IngestionWorker 后台 task）                       │
│                                                                            │
│  消息入口            门控+分级         观察队列         摄入 Worker         │
│  handler.py    ┐                                      (主循环 task)         │
│  handle_ai.py  ├─► observe() ─► _gate() ─► queue.Queue ─► _ingest_batch()   │
│  bot.py        ┘   (纯规则)     (HIGH/LOW)  (maxsize 1万)       │          │
│                                                          (LLM 抽取/写库)    │
│                                              ┌── Episode（始终写）           │
│                                              ├── LLM 抽取 Entity / Edge      │
│                                              ├── SQL + Qdrant 向量双写       │
│                                              └── 触发分层图增量重建            │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│ 分层图（按 scope 累积）                                                       │
│  Entity ──LLM/向量分类──► Layer-1 Category ──► Layer-2 ──► Layer-3 (顶层)     │
│  + 群摘要缓存（被 Persona/Heartbeat 消费）                                      │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│ 检索链路（AI 回复前，主事件循环）                                              │
│  query ─► dual_route_retrieve ─┬─ System-1：向量相似度（episode/entity/edge）  │
│                                └─ System-2：分层图自顶向下遍历                  │
│            ─► Reranker 重排 ─► MemoryContext ─► 注入 Prompt                   │
└──────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────┐
│ 生命周期（APScheduler 每周一次，纯规则无 LLM）                                 │
│  巩固 Edge ─► 衰减 Edge ─► 遗忘 Edge ─► Edge 容量裁剪 ─► 回收孤儿 Entity        │
│  ─► Entity 容量裁剪 ─► Episode 保留/降级(热→冷) ─► SQL↔Qdrant 悬空向量对账        │
└──────────────────────────────────────────────────────────────────────────┘
```

**两个存储后端协同**：
- **SQL（SQLModel/SQLite）**：结构化真值，存 Episode / Entity / Edge / Category / 层次关联 / 矛盾 / 分层图元数据。
- **Qdrant 向量库**：三个 Collection 存语义向量，供相似度检索与去重。SQL 是权威源，向量是其语义索引。

**Worker 运行拓扑（重要，与早期文档不同）**：

[`gsuid_core/ai_core/memory/ingestion/worker.py:5-14`](../gsuid_core/ai_core/memory/ingestion/worker.py:5) 明确：

> IngestionWorker 以**主事件循环**上的后台 task 运行。LLM 调用是 `await` 的纯异步网络 I/O，不阻塞事件循环；embedding 推理走 `vector/ops.py` 的独立线程池。
>
> **历史的"独立线程 + 双事件循环"架构** 因跨循环取消击穿主循环（Windows `WinError 995` → `InvalidStateError` → `run_forever` 崩溃 → WS 全线断连）已**废弃**，请勿复活。

这也是为什么观察队列仍是线程安全的 `queue.Queue`（可能被非事件循环线程投递），但 `_consume_loop` 与 `_flush_timer_loop` 都跑在主循环上。

---

## 2. 核心概念

### 2.1 Scope（命名空间隔离）

所有记忆节点都携带 `scope_key`，实现严格隔离。定义见 [`memory/scope.py`](../gsuid_core/ai_core/memory/scope.py:11)：

| ScopeType          | 格式                          | 含义                                   | 主要消费者                           |
|--------------------|-------------------------------|----------------------------------------|--------------------------------------|
| `GROUP`            | `group:{群号}`                | 群组级记忆（群 A 对群 B 不可见）       | `handler.py` 被动感知                |
| `USER_GLOBAL`      | `user_global:{用户ID}`        | 用户**跨群**全局画像                   | `dual_route_retrieve` 联合检索        |
| `USER_IN_GROUP`    | `user_in_group:{用户ID}@{群号}` | 用户在特定群的局部档案（可选）         | `webconsole/ai_memory_api` 按需清空 |
| `SELF`             | `self:{bot_self_id}`          | Bot 自身情景记忆（"我说过/做过什么"）   | `bot.py` 主动会话                     |

隔离由 SQL `WHERE scope_key=?` 与 Qdrant `scope_key` payload filter 共同保证。检索时可联合查询多个 scope（群组 + 用户全局）。

### 2.2 三层 Base Graph（论文 Section 2.1）

```
Episode（原始对话片段）──提及──► Entity（实体节点）──关系──► Edge（事实，有向边）
```

- **Episode**：聚合后的对话原文，永远保存（即便不抽实体）。是所有记忆的 durable 素材。
- **Entity**：从对话抽取的实体（人、物、概念）。`is_speaker=True` 的是发言者本身。
- **Edge**：两个 Entity 间的一条**可验证事实**（如"用户A 喜欢 咖啡"）。

### 2.3 分层语义图（Hierarchical Graph）

把海量 Entity 自底向上归纳为多层 Category（Layer-1 最具体，层号越大越抽象），供 System-2 自顶向下遍历检索。同时维护 `group_summary_cache` 供 Persona 的群语境注入与 Heartbeat 决策共同消费。

### 2.4 记忆双开关（`memory_mode` / `memory_session`）

两路都存在 `gsuid_core/ai_core/configs/ai_config.py` 的 `MEMORY_CONFIG`（用户可在 WebConsole 修改），并在代码内被 [`memory/config.py`](../gsuid_core/ai_core/memory/config.py:120) 通过 `@property` 转发到 `memory_config.memory_mode / memory_session`。

`memory_mode`（**多选**，`GsListStrConfig`）决定**记录谁的话**：

| 值           | 记录内容                                                                 | 入口                              | 写入去向                                                  |
|--------------|--------------------------------------------------------------------------|-----------------------------------|-----------------------------------------------------------|
| `被动感知`   | 群内**所有成员**发言（无需触发命令/AI）                                   | `handler.py` `handle_event`       | GROUP/USER_GLOBAL，可抽实体                                |
| `主动会话`   | 仅 AI 实际参与的那轮：**触发者发言** + **Bot 自身回复**                   | `handle_ai.py` + `bot.py`         | 触发者 → 群组图谱；Bot 回复 → SELF scope（仅 Episode）      |

`memory_session`（**单选**，`GsStrConfig`）决定**被动感知的范围**：

| 值                | 含义                                                            |
|-------------------|-----------------------------------------------------------------|
| `全部群聊`        | 群内所有人的消息都入记忆                                         |
| `按人格配置`      | 仅"该群匹配到某 Persona 配置"时记录（默认）                      |

> **去重守卫**（[`handle_ai.py:139-156`](../gsuid_core/ai_core/handle_ai.py:139)）：当两路同时开启时，触发者发言已被"被动感知"在 `handler.py` 入口记录过一次；"主动会话"路径会以 `"被动感知" not in memory_mode` 守卫自动跳过，避免二次写入。

### 2.5 `memory_mode` 与 `enable_retrieval` 的正交性

- `memory_mode` 控制**写**哪些消息；
- `enable_retrieval` 控制 AI 回复时**是否注入**记忆上下文（[`dual_route.py`](../gsuid_core/ai_core/memory/retrieval/dual_route.py) 调用前置门控）；
- 两者可独立开关——可只写不读（后台积累但不干预回复）、也可只读不写（用旧的或外部导入的记忆）。

---

## 3. 数据模型（SQL + 向量）

### 3.1 SQL 表（[`memory/database/models.py`](../gsuid_core/ai_core/memory/database/models.py)）

| 表（类名）                    | 关键字段                                                                                                            | 说明                                                                       |
|-------------------------------|---------------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| `AIMemEpisode`                | `scope_key, content, speaker_ids, valid_at, qdrant_id`                                                              | 对话片段原文；含 `created_at`                                              |
| `AIMemEntity`                 | `scope_key, name, summary, tag, is_speaker, user_id, created_at, updated_at, qdrant_id`                            | 实体节点；`UniqueConstraint(scope_key, name)`                              |
| `AIMemEdge`                   | `scope_key, fact, source_entity_id, target_entity_id, valid_at, invalid_at, created_at, qdrant_id, mention_count, decay_score, last_accessed` | 事实边（含时效/置信度/被检索时间）；`mention_count` 默认 1（启动 ALTER 补列） |
| `AIMemConflict`               | `scope_key, fact_signature, old_edge_id, new_edge_id, summary`                                                    | 语义矛盾记录（极性相反的事实，以新事实为准）                                |
| `AIMemCategory`               | `scope_key, name, summary, tag, layer, created_at, updated_at`                                                    | 分层语义图类目节点；`UniqueConstraint(scope_key, layer, name)`             |
| `AIMemCategoryEdge`           | `parent_category_id, child_category_id`                                                                            | 类目↔类目层次关联（多对多）                                                  |
| `AIMemHierarchicalGraphMeta`  | `scope_key, max_layer, last_rebuild_at, entity_count_at_last_rebuild, current_entity_count, group_summary_cache, group_summary_updated_at` | 每 scope 的分层图状态追踪 + 群摘要缓存                              |
| `AIMemPreference`             | `scope_key, user_id, target_context, preference_rule, polarity(do/dont), is_correction, is_active, source_episode_id, mention_count, last_applied_at` | **程序性/偏好记忆规则**（"该/不该如何调工具/排版/选参数"）。**默认开**（`enable_preference_memory`，WebConsole 可关）；SQL-only **不写向量**；主存 `USER_GLOBAL`。复合索引 `(scope_key,target_context)` / `(scope_key,user_id)`。详见 [`docs/PROCEDURAL_PREFERENCE_AND_RFMEM_IMPLEMENTATION_20260614.md`](PROCEDURAL_PREFERENCE_AND_RFMEM_IMPLEMENTATION_20260614.md) |

**两张多对多关联表**：
- `mem_episode_entity_mentions(episode_id, entity_id)`：Episode 提及了哪些 Entity。
- `mem_category_entity_members(category_id, entity_id)`：Layer-1 Category 包含哪些 Entity。

### 3.2 Qdrant Collection（[`memory/vector/collections.py`](../gsuid_core/ai_core/memory/vector/collections.py)）

| Collection          | 向量结构                       | 距离    | 用途                         |
|---------------------|--------------------------------|---------|------------------------------|
| `memory_episodes`   | `dense` + `sparse(BM25)`       | COSINE  | Episode 内容检索（**热集**：System-1 只查此集合） |
| `memory_episodes_cold` | `dense` + `sparse`          | COSINE  | 降级后的**冷** Episode 归档（不参与在线检索，可审计） |
| `memory_entities`   | `name_dense` + `summary_dense` + `sparse` | COSINE | Entity 去重 / 相似度归类  |
| `memory_edges`      | `dense` + `sparse`             | COSINE  | Edge 事实检索 / 归并         |

- **冷热分集合（§3.2① / P0-2）**：生命周期维护把"无引用 + 超龄 + 超每 scope 最近 M 条"的冷 Episode 向量从 `memory_episodes` 迁到 `memory_episodes_cold` 并标记 SQL `is_archived=True`，使热集合规模可控、退出本地向量库的暴力扫描；冷集合是从热集合迁移的派生数据，真值始终在 SQL，维度变更时直接重建为空即可（不影响任何记忆事实）。
- 向量 `point.id` == 对应 SQL 行的 `qdrant_id`（通常等于主键 id）。
- 检索默认走 **Hybrid（Dense + Sparse RRF 融合）**；Sparse 模型不可用时降级纯 Dense。
- Collection 维度随嵌入模型自动检测；维度变更会导出 payload → 重建 → 重嵌入（迁移逻辑在 [`memory/vector/startup.py`](../gsuid_core/ai_core/memory/vector/startup.py)）。

---

## 4. 写入链路：从消息到图谱

### 4.1 消息进入（三个入队点）

| #  | 入口                                                                                                | 触发条件                                                                                                                                   | `observe()` 入参                              |
|----|-----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------|
| ①  | [`handler.py`](../gsuid_core/handler.py:331) `handle_event` → 内部 `asyncio.create_task(observe(...))` | `enable_ai and not ai_scope_banned and (有文本/图片)` **且** `enable_memory + observer_enabled + "被动感知" in memory_mode`；范围再受 `memory_session` 约束 | `speaker_id=str(event.user_id)`                |
| ②  | [`handle_ai.py`](../gsuid_core/ai_core/handle_ai.py:139) `handle_ai_chat` 入口                       | `enable_memory and "主动会话" in memory_mode and "被动感知" not in memory_mode`                                                            | `speaker_id=str(event.user_id)`（触发者）     |
| ③  | [`bot.py`](../gsuid_core/bot.py:460) `_Bot.send()` 发送路径                                          | `enable_ai and enable_memory and "主动会话" in memory_mode`                                                                                | `speaker_id=f"__assistant_{bot_id}__"`（Bot） |

> 三处最终都调用 [`memory/observer.py`](../gsuid_core/ai_core/memory/observer.py) 的 `observe()`，该函数本身**纯规则无 LLM**。

### 4.2 门控与分级（[`memory/observer.py`](../gsuid_core/ai_core/memory/observer.py)，纯规则，零 LLM）

`observe()` 先按发言人分流，再过门控 `_gate()`：

- **Bot 自身发言**（`__assistant_` 前缀）→ 路由到 `SELF` scope（`self:{bot_self_id}`），强制 `value_tier=LOW`，**仅写 Episode、不进群组事实图谱**（C6：杜绝 Bot 戏言污染群记忆）。
- **普通用户消息** → 计算 GROUP / USER_GLOBAL scope，过 `_gate()`：
  - **丢弃**：自身数字 ID 消息、黑名单群、空消息、纯图片短消息、命令回显（`_COMMAND_ECHO_RE`）、用户命令 / typo 命令（`_USER_COMMAND_RE`，按 `command_start` 配置精确匹配 + 1~2 个命令式标点 + 短文本启发式兜底）、prompt 注入文本（`_INJECTION_RE`）、复读（同 scope 最近 12 条重复，`_REPEAT_WINDOW=12`）。
  - **分级 `_classify_value_tier`**（受 `extraction_value_gate` 档位控制）：
    - 强信号（姓名自述 / 称呼偏好 / 承诺 / 数字日期 / 情绪词）→ `HIGH`（含 `_HIGH_SIGNAL_RE` / `_EMOTION_RE` 命中即升级，拥有否决权）
    - **档位语义**（任何档位下 `LOW` 仍完整写 Episode，只省抽取 Token、不丢原文）：
      - `宽松`（默认）：纯寒暄且 < 10 字（`_LOW_TIER_MAX_LEN=10`）且无实体特征 → `LOW`，其余 `HIGH`
      - `均衡`：无实体特征 → `LOW`
      - `严格`：仅强信号 / 情绪 → `HIGH`，其余全 `LOW`

放行的记录封装为 `ObservationRecord`，`put_nowait` 进 `queue.Queue`（线程安全，maxsize **10000**，满则丢最老一条）。

### 4.3 摄入 Worker（[`memory/ingestion/worker.py`](../gsuid_core/ai_core/memory/ingestion/worker.py)）

`IngestionWorker` 在**主事件循环上的后台 task** 中运行（**关键**：LLM 抽取是 `await` 网络 I/O，不阻塞事件循环；embedding 走 `vector/ops.py` 独立线程池；旧"独立线程 + 双事件循环"架构已废弃——见 §1）：

- 按 `scope_key` 分桶缓冲；满足 **数量阈值（`batch_max_size`=80）** 或 **时间窗（`batch_interval_seconds`，默认 7200s，timer 每 30s 检查）** 时 `_flush`。批越大 / 窗越长，单次抽取覆盖的对话越完整、调用次数越少，固定 prompt 开销被摊薄。
- `_flush` 内以 `batch_max_size` 切批，每批 `_ingest_batch` 有 120s 超时；失败**以批为单位**退回缓冲重试（已成功批绝不重摄，避免重复 Episode）。`_flushing` 集合标记防止并发 flush。
- 并发受 `llm_semaphore_limit`（默认 3）约束。

### 4.4 单批摄入 `_ingest_batch`

```
1. 拼接对话文本 → 写 Episode（含 LOW 价值消息，始终持久化 + 写向量）
2. 分流：SELF scope 或全 LOW 批 → 仅写 Episode，跳过抽取（return）
3. 否则 _extract_and_upsert_from_episode（best-effort，抽取异常不连累 Episode）
```

`_extract_and_upsert_from_episode` 步骤（[`worker.py:447-561`](../gsuid_core/ai_core/memory/ingestion/worker.py:447)）：

- 仅用 HIGH 价值消息拼接，并经 `_compact_high_records_dialogue` **折叠纯表情/标点/语气词等无实体信息行 + 合并相邻重复 + 合并连续同一发言者为一轮**后喂给 LLM（去掉重复的 `[speaker]:` 前缀、给 LLM 更连贯的发言；Episode 已存全文，此折叠只省抽取 Token）；折叠后为空则跳过 LLM 抽取。
- 附最近 `background_episode_count` 条 Episode 作背景上下文（默认 **1**，单条截断至 `background_episode_max_chars`=600 字符；置 0 则不注入背景、省去该查询与其 Token）。
- `_llm_extract`：注入"本群已知别名 + 已存在实体"指导 LLM，输出简写键 JSON（`n/s/t/u/a/src/tgt/f`），由 `_restore_keys` 还原完整键；超长对话（> 14000 字符）按对话行分片提取、合并去重；调用 `agent.create_by="MemEntityExtraction"` 的通用 Agent（**不传 `output_type`**，避免 thinking trace）；单次 180s 超时。
- `_apply_master_tags`：主人（`core_config.masters`）对应 Speaker 实体打 `Master` 标签。
- `_apply_alias_redirection`：LLM 标注的别名实体合并到正式实体，重写 Edge 引用（实体消歧 Level-1）；同时累计群组画像。
- `extract_and_upsert_entities`（精确名称匹配 + 混合检索相似度合并） → `extract_and_upsert_edges`（相似度阈值归并 + 极性反转检测） → 触发分层图检查。
- 增量维护 `current_entity_count`（`increment_entity_count`，O(1)，避免全表 COUNT）。
- **user_global 跨群属性**：`scope_hint == "user_global"` 的实体额外写入发言者的 `user_global:{uid}` scope。

### 4.5 Entity 去重写入（[`memory/ingestion/entity.py`](../gsuid_core/ai_core/memory/ingestion/entity.py)）

两阶段去重（[`memory/database/models.py:170-198`](../gsuid_core/ai_core/memory/database/models.py:170)）：
1. **精确名称匹配**（`scope_key + name` 唯一索引，O(1)）。
2. **向量混合检索**（BM25+向量 RRF）：相似度 ≥ `dedup_similarity_threshold`（**0.92**）视为同一实体。

命中既有实体 → 合并 summary（上限 **2000** 字符，超限保留头部 + 截断提示）/ 合并 tag / 刷新 `updated_at`；否则新建。最后批量写 Qdrant 向量（**3 次指数退避重试 + 30s 全局超时**，保障 SQL/向量一致）。

### 4.6 Edge 写入（[`memory/ingestion/edge.py`](../gsuid_core/ai_core/memory/ingestion/edge.py)）

对每条 Edge，先并行检索语义等价的既有 Edge（同 src/tgt 且相似度 ≥ `edge_conflict_threshold`=0.88）：

- **可归并**（极性相同）→ 不写新边，既有边 `mention_count += 1` 并刷新 `valid_at`（C1 跨发言者归并，避免重复 Edge 噪声）。
- **极性相反**（`_fact_polarity` 含奇数个否定标记词 `("不", "没", "无", "非", "别", "讨厌", "拒绝", "反对", "停止")`）→ 语义矛盾：旧边软删除（`invalid_at`），记录 `AIMemConflict`，以新事实为准（C11）。
- **无等价** → 创建新 Edge + 收集向量，批量写 Qdrant。

### 4.7 群组画像与多模态

- **群组画像**（[`memory/group_profile.py`](../gsuid_core/ai_core/memory/group_profile.py)）：累计 `term_mappings`（别名→正式名）、`tag_counts`（实体名频次，作为群组话题标签）、`member_alias_ids`（群成员称呼 → 用户ID，单候选直接给、多候选降级为歧义由 Agent 消歧）；存于通用持久状态存储 `AIPersistentState` 表（`scope = "__gscore_group_profile__"`，**双下划线保留命名**避免与插件冲突）；容量上限：`term_mappings` 60、`tags` 40、`member_aliases` 60、单称呼最多绑 5 个用户。详见 §5.4。
- **多模态**（[`memory/ingestion/multimodal.py`](../gsuid_core/ai_core/memory/ingestion/multimodal.py)）：高价值图片走独立队列，`ImageUnderstandWorker` 异步转述为文本后，以 `[图片理解]` 前缀推回主 `observe()` 管道，不阻塞文本聊天。在 `handler.py` 中**文本门控之外**触发，纯图片消息也能进入。

### 4.8 同步强制刷写 `IngestionWorker.flush_all()`

`IngestionWorker` 还提供 `flush_all()`（[`worker.py:180-196`](../gsuid_core/ai_core/memory/ingestion/worker.py:180)），由 [`memory/startup.py:103`](../gsuid_core/ai_core/memory/startup.py:103) 的 `get_ingestion_worker()` 暴露给 `/api/chat_with_history` 等需要"同步等待记忆构建完成"的场景。最多等待 120s，超时则放弃。`flush_all` 完成后由 `rebuild_task` 在后台异步触发分层图重建（除非 `eval_mode=True`，评测模式统一外部触发）。

---

## 5. 分层语义图：构建与重建

入口 [`memory/ingestion/hiergraph.py`](../gsuid_core/ai_core/memory/ingestion/hiergraph.py) `HierarchicalGraphBuilder.incremental_rebuild()`。

### 5.0 构建消费方与 `hiergraph_build_mode` 门控（成本关键）

分层类目树**只被 System-2 检索消费**（`dual_route` 中 `enable_system2` 门控）；唯一被"非 System-2"路径消费的产物是 `group_summary_cache`（Heartbeat 决策 + 人格群语境）。因此 `incremental_rebuild` 在小 scope 跳过后先判 `hiergraph_build_mode`：

| 模式     | 行为                                                                                                                  |
|----------|-----------------------------------------------------------------------------------------------------------------------|
| `自动`（默认） | 仅当 `enable_system2=True` 时才构建整棵类目树；否则走 `_summary_only_rebuild`——**跳过 Layer-1/2/3 全部 LLM 分类**，仅在新增实体 ≥ `hiergraph_summary_delta` 时从高频实体名直接刷新群摘要（至多 1 次 LLM） |
| `始终`   | 总是构建完整类目树（旧行为）                                                                                          |
| `仅摘要` | 从不建树，仅按需刷新群摘要                                                                                            |
| `关闭`   | 既不建树也不刷摘要（重建零 LLM）                                                                                      |

> System-2 关闭时，类目树无任何消费方，构建它纯属浪费——这是大规模下重建 Token 的最大可削减项。Entity / Edge / Episode 等记忆本体不受任何模式影响。

### 5.1 触发判定（`_check_should_rebuild`）

满足任一即触发（增量计数，不扫全表）：
- 首次（`meta` 不存在）；
- `current_count > baseline × hiergraph_rebuild_ratio`（**2.50**）且 `delta >= MIN_DELTA`（**20**，避免冷启动反复重建）；
- 距上次重建 > `hiergraph_rebuild_interval_seconds`（**172800s = 48h**）。

由 `rebuild_task` 加**每 scope 锁**串行执行，防并发重建。

### 5.2 重建主流程（含五项成本优化）

```
incremental_rebuild()
 ├ [小 scope 跳过] 总实体 < hiergraph_min_entities(30) → 仅更新 Meta 返回
 │     （类目对小数据集无收益，召回靠 System-1 向量 + edges）
 │
 ├ 取未分配实体 _get_unassigned_entities(limit=MAX_ENTITIES_PER_REBUILD=800)
 │     ├ [入口过滤] 仅喂 is_speaker 或"至少有一条 edge"的实体
 │     │     （无 edge 噪声实体不进 LLM；它们留作去重锚点，由孤儿 GC 回收）
 │     └ [单轮上限] 按 created_at 取最旧的至多 800 个，超额留待续轮
 │
 ├ Layer-1 归类
 │     ├ [向量预分配] 与"已归类近邻"summary_dense 余弦 ≥ hiergraph_vector_assign_threshold(0.85)
 │     │     → 直接并入近邻所在 Category（_vector_pre_assign，零 LLM；VECTOR_ASSIGN_TOP_K=5）
 │     └ 残余（speaker + 未命中）才走 LLM（_llm_categorize / _apply_entity_assignments）
 │           · speaker 由 M-06 强制归入 "Speaker" Category（Many-to-Many，允许同时归入其他类目）
 │
 ├ Layer-2/3 逐层增量构建
 │     ├ [增量化] 仅取"尚无父类目"的下层节点喂 LLM（_filter_unparented）
 │     │     （把"按存量收费"降为"按新增收费"）
 │     ├ 下层全部已归类 → 跳过 LLM，推进到已存在的上层继续检查
 │     └ 违反 node count reduction rule（本层 ≥ 下层）→ 回滚本层新建 Category 并终止
 │
 ├ 更新 Meta（max_layer 从 valid_prev_layer 计算，rollback 安全）
 ├ 按需重算群组摘要缓存（_should_regen_group_summary 命中才发 LLM）
 └ [续清] 本轮达单轮上限 → 再调度一次 rebuild_task（backlog 单调收敛）
```

**LLM 分类 `_llm_categorize`**：按 `hiergraph_batch_size`（默认 20，可配置）分批；已有类目数限制 `hiergraph_max_existing_cats`（默认 50，可配置）防 prompt 爆炸；每节点附带的实体摘要长度由 `hiergraph_node_summary_chars`（默认 60，可配置，0=不带）控制；每批超时 180s，兜底为"每节点单独成类"（论文 Section 2.2 例外规则）。

> **建树期 token 压缩（need_tree 路径）**：① 移除冗余的"待分类节点示例"（原是 `nodes_info` 前 5 条的重复，抽象粒度已由 `layer_hint` 一行类比给出）；② 现有类目**只发名称**（复用按名称匹配，逐条 summary 每批重发收益低）；③ `hiergraph_batch_size` 调大 → 单轮调用更少、摊薄每批固定开销；④ `hiergraph_vector_assign_threshold` 调低 → 更多实体走零 LLM 的向量预分配路径。
>
> 这些优化的目的：实体规模上 10 万级后，旧逻辑每轮重建按存量收费、token 几何级上升。优化后一次常规重建的 LLM 调用从"几百~几千次"降到"个位数甚至 0"。

### 5.3 群摘要缓存 `group_summary_cache`

由 `HierarchicalGraphBuilder` 在重建成功后**按需**重算（`_run_group_summary_llm`），写入 `AIMemHierarchicalGraphMeta.group_summary_cache` 字段。**两种来源**：
- **类目树来源**（`_update_group_summary_cache`，`_summary_only_rebuild` 关闭）：将顶层 Category 的 `name(layer, summary[:100])` 喂 LLM 生成 ≤500 字符的群摘要。
- **高频实体来源**（`_update_group_summary_from_entities`，`_summary_only_rebuild` 的轻量替身）：System-2 关闭时直接取本 scope 最近活跃的非 speaker 实体名（`AIMemEntity.get_frequent_names` limit=20）作为输入，避开无类目树场景。

触发刷新条件（`_should_regen_group_summary`）：①首次或缓存空；②`max_layer` 与本次实际不一致；③`current_count - baseline >= hiergraph_summary_delta`（默认 50）。

### 5.4 群摘要与跨模块耦合

`group_summary_cache` 是记忆系统的"对外产物"，被两个非记忆模块共同消费：

- **Persona 群语境**（[`persona/group_context.py:93-119`](../gsuid_core/ai_core/persona/group_context.py:93)）：`_get_group_summary_from_memory(group_id)` 直接读 `AIMemHierarchicalGraphMeta.get_or_none(scope_key="group:{group_id}").group_summary_cache`，与群名拼接为【当前群聊环境】注入 [`build_persona_prompt`](../gsuid_core/ai_core/persona/processor.py:14) 的 system_prompt。
- **Heartbeat 决策**（`enable_heartbeat_memory` 开关控制）：读同一字段。

> **故障耦合**：`persona/group_context.py` 内部对 `memory.ingestion.hiergraph` 的 import 用了 try/except 兜底，因此即使记忆系统未启用，Persona 仍可正常工作（仅失去群摘要）。这意味着记忆系统是 Persona 群语境的**可选增强**而非硬依赖。

### 5.5 群组画像（`group_profile.py`）

与 `group_summary_cache` 平行的另一套"群画像"机制，存于通用 `AIPersistentState` 表（**不**在记忆表内）：
- `term_mappings` / `tag_counts` 由摄入链路（`_apply_alias_redirection` / `record_entity_tags`）累计；
- `member_alias_ids` 由 [`persona/prompts.py`](../gsuid_core/ai_core/persona/prompts.py) 决策树中"以后叫她小C"之类的指令触发 `remember_user_alias` 工具写入；
- 检索期 `expand_query_with_aliases` 把 query 中出现的别名展开成正式名，提升召回；
- 注入期 `format_context_injection` 拼成【当前群聊语境】文本（含成员称呼、主要话题、词汇映射），见 [`handle_ai.py:348-358`](../gsuid_core/ai_core/handle_ai.py:348)。

---

## 6. 检索链路：双路召回与注入

入口 [`memory/retrieval/dual_route.py`](../gsuid_core/ai_core/memory/retrieval/dual_route.py) `dual_route_retrieve()`，在 [`handle_ai.py:259-280`](../gsuid_core/ai_core/handle_ai.py:259) AI 回复前调用。

### 6.1 触发与门控

`handle_ai.py` 先过 **C4 寒暄门控** `_should_retrieve_memory`（纯规则）：
- 主人 / 回指词（之前/上次/你说过…/记不记得…）/ 任务引用词 / 明显情绪词 / 含实体 → **强制检索**。
- 纯短寒暄（`intent=="闲聊"` 且 `< 12` 字且无实体特征）→ 跳过，省向量+Reranker 开销。
- 其余 → 照常检索。

调用参数：`top_k = memory_config.retrieval_top_k`（**15**，用户可改 5/10/15/20）、`enable_system2 = memory_config.enable_system2`（**默认 False**）、`enable_user_global = memory_config.enable_user_global_memory`（**默认 True**）。

### 6.2 双路并行（`asyncio.gather`）

- **System-1**（[`retrieval/system1.py`](../gsuid_core/ai_core/memory/retrieval/system1.py)：向量相似度）：对所有 scope（群组 + 用户全局）并行检索 episode/entity/edge（Hybrid RRF），再做 **One-hop 邻居扩展**（沿 Edge 把另一端 Entity 纳入）。
- **System-2**（[`retrieval/system2.py`](../gsuid_core/ai_core/memory/retrieval/system2.py)：分层图遍历）：从顶层 Category 出发，每层 LLM 选择相关节点（可"取全部子孙"快捷），逐层向下到 Layer-1，收集成员 Entity 及其 Edge/Episode（用 Recursive CTE 一次性取子孙，带深度熔断防环）。可由 `enable_system2=False` 关闭以省成本。

> 双路任务由 `asyncio.create_task` 真实并行（OPT-02），任一失败不连累另一路（`return_exceptions=True`）。

### 6.3 合并、重排、注入

1. S1 + S2 结果按 id 合并去重。
2. **类型隔离 Rerank**：episode / entity / edge 三路并行（`asyncio.gather`）过 Reranker；**Category 跳过 Reranker**（字面重合度低，给固定最高优先级，保证 LLM 总能看到大纲）。Reranker 用 `ThreadPoolExecutor(max_workers=3)` 隔离 ONNX 推理，不阻塞事件循环。
3. **置信度富集**：命中的 Edge 从 DB 取 `mention_count` / `decay_score`，折算 `weight = 佐证×新鲜度`（`compute_edge_confidence`，见 [`dual_route.py:76-90`](../gsuid_core/ai_core/memory/retrieval/dual_route.py:76)）；同时刷新 `last_accessed`（供衰减判定，后台 fire-and-forget）。
4. `MemoryContext.to_prompt_text(max_chars=memory_config.memory_inject_max_chars=2000)` 按预算注入：
   - **核心事实（edges）≈55%**：过滤失效边 / 低置信边（`min_edge_weight`）/ 低相关性边（`min_edge_rerank_score`），按 fact 签名去重，主人 edge 上浮、事件型 trivia（`提及/提到/在唱/演唱/询问/聊到/讨论/说起/谈到`）下沉，身份等价类 fact 标"（记忆·待证）"。
   - **语义类目摘要（categories）≈15%**：话题大纲。
   - **相关对话片段（episodes）≈30%**：少量最相关轮次（按 `valid_at` 排序，前 3 条）。
   - 主语补全：缺主语的 fact 由 `_complete_fact_subject` 用 `source_name` 补成"用户xxx建议关注…"；身份等价类 fact 禁止强补主语。

> 注意：**entities 被检索/重排但不直接注入 Prompt**——它们通过 edges（事实）和 categories（大纲）间接发挥作用。这也是孤儿实体（无 edge）可安全回收的依据。

### 6.4 RF-Mem 双过程检索：熟悉度路由 + 回忆环（**默认全关**）

在 §6.2 并行启动 S1/S2 之前插入一层**零 LLM 的探针路由**（论文 `plans/2603.09250v1.pdf`：Recollection–Familiarity 双过程检索）。**默认关，关闭时检索行为逐字节不变。**

- **探针**（`vector/ops.probe_episode_scores`）：对 `memory_episodes` 发一次**纯 dense、`score_threshold=0`** 查询取真实余弦分（不可与混合检索的 RRF 融合分混用）。
- **信号 / 路由**（`retrieval/familiarity.py`）：`compute_familiarity_signal` 算均分 `s̄` + 温度 softmax 列表熵 `H`；`decide_route` 按论文 **Eq. 3 两阈值 + 熵裁决**（`s̄≥θ_high`→Familiarity；`s̄≤θ_low`→Recollection；中段 `H>τ`→Recollection）。
- **路由只 gate 深检索，不动 System-1**：`effective_enable_system2 = enable_system2 AND route==Recollection`——高熟悉查询**抑制 System-2** 省 LLM；**永远受用户 `enable_system2` 总开关约束**。
- **回忆环**（`recollection_search`，System-1.5）：仅当 `enable_recollection_path=True` **且** System-2 未实际触发 **且** `qdrant_provider=remote` 时跑。零 LLM 的 KMeans+α-mix 多轮深检索（论文 Algorithm 3，beam 按 `Σ<x',z_i>` 留 top-B），召回 Episode 再 `project_episodes_to_edges` **关系投影**成精准 Edge，与 S1 取并集统一进 Reranker。

> **重要落地约束（见实现文档 §7.1）**：本库 System-1 永远先跑，故路由开启在**默认本地 Qdrant** 下仅是"按不确定性抑制 System-2"的**成本优化、不增召回**；论文式召回提升只在 **remote Qdrant + 开回忆环 + 阈值已标定** 三者同时满足时才出现。`familiarity_theta_*` 随嵌入模型/语料漂移，**放量前必须按 `embedding_provider` 标定**（实现文档 §7.4 给出标定程序），否则可能全路由深检索而增本。

### 6.5 程序性 / 偏好记忆注入（**默认开**）

`enable_preference_memory`（**默认开**，WebConsole 可关）开启时，`dual_route_retrieve` 额外 SQL 查 `AIMemPreference.get_active(scope_keys)`（O(log n) 复合索引 seek，不走向量），把活跃规则填入 `MemoryContext.preferences`；`to_prompt_text` 在**最前**渲染**置顶、强约束**区块 `【用户偏好/纠错 - 须严格遵守】`（独立小预算 `preference_inject_budget_ratio`）。命中规则后台 fire-and-forget 刷新 `last_applied_at`。写入端门控/蒸馏链路见实现文档 §2。**`preferences` 为空时不渲染**——绝大多数用户无偏好规则时本区块零成本。

**选择性注入（默认开 → 不是每轮都注入）**：`dual_route_retrieve(inject_preferences, preference_contexts)`。`handle_ai` 按**意图门**（`intent=="闲聊"` 整轮跳过偏好）+ **能力域过滤**传参，能力域信号 = `_relevant_preference_contexts(query)` 子串近似 **∪ `session.get_assembled_capability_domains()`**（gs_agent 上一轮**实际装配**工具的能力域，精确"装配后回传"）。检索侧策略：**纠错规则与 `general` 永远注入**（紧扣"刚纠正完的下一轮"），软偏好仅当 `target_context` 命中本轮能力域才注入，避免每条回复都注入全部规则挤占预算/分散工具调用注意力。`preference_contexts=None` = 不过滤（旧行为）。

**门控（写入端，实现文档 §2.2）**：蒸馏门控自 2026-06-14 由实体抽取 LLM 顺手判的 `has_preference` 标志位裁决（取代脆弱纯正则）；观察期纠错正则降级为"召回预过滤 + 即时 flush 时机"。

---

## 7. 生命周期维护

[`memory/lifecycle/consolidation_worker.py`](../gsuid_core/ai_core/memory/lifecycle/consolidation_worker.py) `run_lifecycle_maintenance`，由 [`memory/startup.py:88-95`](../gsuid_core/ai_core/memory/startup.py:88) 通过 APScheduler **每周一次**触发（`trigger="interval", weeks=1`，job id `ai_memory_lifecycle_maintenance`），**纯规则、零 LLM**：

维护按下表**顺序**执行（编号即执行序）：

| 步骤       | 规则                                                                                                  | 常量                                |
|------------|-------------------------------------------------------------------------------------------------------|-------------------------------------|
| ① **巩固**   | `mention_count ≥ 3` 的高频 Edge `decay_score` 回升 1.0                                                | `PROTECT_MENTION_COUNT=3`           |
| ② **衰减**   | 超 14 天未被检索且非高频的 Edge `decay_score *= 0.85`（**单条集合式 UPDATE**：`SET decay_score = coalesce(decay_score,1.0)*factor WHERE ...`，不再逐行 UPDATE） | `DECAY_STALE_DAYS=14, DECAY_FACTOR=0.85` |
| ③ **遗忘**   | `decay_score < 0.1` 的 Edge 物理删除（SQL + Qdrant，`PointIdsList` 单次批量删除）                       | `FORGET_THRESHOLD=0.1`              |
| ④ **Edge 容量裁剪** | 边数超每 scope 软上限的 scope，按 salience（有效性→`mention_count`→`decay_score`→最近访问）降序保留 top-N、淘汰长尾（SQL + Qdrant，按 **500** 分块） | `EDGE_MAX_PER_SCOPE=50000` |
| ⑤ **孤儿实体回收** | 非 speaker、无任何 edge、`updated_at` 超 10 天的实体物理删除（SQL + Qdrant + 递减分层图计数，按 **500** 分块） | `ORPHAN_ENTITY_TTL_DAYS=10, CHUNK=500` |
| ⑥ **Entity 容量裁剪** | 实体数超每 scope 软上限的 scope，淘汰最弱的"非 speaker、无 edge"实体（FK 安全，复用孤儿回收路径） | `ENTITY_MAX_PER_SCOPE=50000` |
| ⑦ **Episode 保留/降级** | **降级**：无 Entity 引用 + 超 `hot_days` + 超每 scope 最近 `hot_per_scope` 条的热 Episode，向量迁入冷集合 `memory_episodes_cold`、SQL 标记 `is_archived`（退出热检索）；**物理上限**：每 scope 总量超上限时物理删最老的"冷且无引用"Episode（SQL + 热/冷向量） | `EPISODE_HOT_DAYS=30, EPISODE_HOT_PER_SCOPE=2000, EPISODE_MAX_PER_SCOPE=20000` |
| ⑧ **悬空向量对账** | 分页 `scroll` 各 Collection 的 point id，与 SQL 对应表 `qdrant_id` 集合比对，删除 SQL 已无对应行的悬空向量（删除半失败残留）。覆盖 episodes 热/冷集、entities、edges | `RECONCILE_SCROLL_BATCH=500` |
| ⑨ **偏好规则裁剪**（默认开） | `enable_preference_memory` 开启时：每 `(scope, user, target_context)` 仅保留 salience 最高的 N 条活跃规则，其余**非纠错**规则软停用（`is_active=False`，纠错类受保护）。纯规则、零 LLM、SQL-only；关闭时 no-op | `preference_max_per_context=5` |

> 顺序要点：遗忘 / Edge 裁剪在前——它们是孤儿实体的主要来源，故孤儿 GC（⑤）与 Entity 裁剪（⑥）紧随其后；对账（⑧）收尾，清理前序各步可能遗留的 SQL↔Qdrant 不一致。容量裁剪与 Episode 保留均为**容量驱动**（针对千人群超大 scope），与原有的**时间/访问驱动**衰减互补；阈值默认极大、对普通部署是 no-op。

衰减结果在检索期被 `weight = compute_edge_confidence(mc, decay)` 加权消费，使活跃记忆始终优先。**注**：检索阶段 r2.3 之前 `reranker_score × decay_score` 双重加权；当前实现是 `compute_edge_confidence` 在 DB 侧一次性折算为 `weight`，与 Reranker 分数正交。

**清空操作**（[`memory/database/clear_ops.py`](../gsuid_core/ai_core/memory/database/clear_ops.py)）：按 scope 精确 / 前缀 / 后缀匹配批量清空（群级、用户全局级、用户群内档案），同步删 SQL + Qdrant，支持 `dry_run`，对外通过 `clear_group_memories` / `clear_user_global_memories` / `clear_memories_for_scope_async` 三个函数暴露给 [`webconsole/ai_memory_api.py`](../gsuid_core/webconsole/ai_memory_api.py)。**清空联动**：同 scope 的 `AIMemPreference` 行一并物理删除（SQL-only，返回 `deleted_preferences`），否则"清空用户记忆后旧偏好规则仍硬约束工具调用"。

---

## 8. 配置项与关键常量

### 8.1 `memory_config.MemoryConfig` 直接字段（[`memory/config.py`](../gsuid_core/ai_core/memory/config.py)）

运行时字段，默认值即代码硬编码；修改通过 `memory_config.<field>` 直接赋值即可（仅影响进程内单例，重启后归位）。

| 字段                                | 默认值 | 含义                                                                                                       |
|-------------------------------------|--------|------------------------------------------------------------------------------------------------------------|
| `observer_enabled`                  | True   | 观察者总开关；关闭后 `observe()` 静默返回                                                                    |
| `observer_blacklist`                | []     | 不记忆的群组 ID 列表                                                                                         |
| `ingestion_enabled`                 | True   | 摄入引擎开关                                                                                                |
| `batch_interval_seconds`            | **7200** | 聚合窗口（超时强制 flush）；越长越省 Token                                                                  |
| `batch_max_size`                    | **80** | 单次最大聚合条数；越大调用越少、摊薄固定开销                                                                 |
| `llm_semaphore_limit`               | **3**  | 并发 LLM 调用上限（`_llm_semaphore` 信号量）                                                                 |
| `enable_retrieval`                  | True   | 检索注入开关                                                                                                |
| `enable_user_global_memory`         | True   | 联合用户跨群画像                                                                                            |
| `enable_heartbeat_memory`           | True   | Heartbeat 决策中注入群组摘要缓存                                                                            |
| `search_edge_count`                 | **30** | Edge 搜索结果数量上限                                                                                       |
| `min_edge_weight`                   | **0.0** | 【置信度轴】过滤 `weight` 低于此值的 Edge（`compute_edge_confidence` 折算后）                              |
| `min_edge_rerank_score`             | **0.0** | 【相关性轴】过滤 Reranker 相关性分数低于此值的 Edge（无 Reranker 时无相关性信号）                          |
| `dedup_similarity_threshold`        | **0.92** | Entity 去重阈值                                                                                            |
| `edge_conflict_threshold`           | **0.88** | Edge 归并 / 极性冲突阈值                                                                                   |
| `min_children_per_category`         | **3**  | 每类目最少子节点数（`node count reduction rule` 与 `_filter_unparented` 用）                                |
| `max_layers`                        | **3**  | 分层图最大层数                                                                                              |
| `hiergraph_rebuild_ratio`           | **2.50** | Entity 增长触发比例（需 `delta >= 20` 才生效）                                                              |
| `hiergraph_rebuild_interval_seconds`| **172800** | 重建时间窗（48h）                                                                                          |

**RF-Mem 回忆环内部超参（运行时字段；总开关与标定阈值已上 `MEMORY_CONFIG`，见 §8.2）**：

| 字段 | 默认 | 含义 |
|---|---|---|
| `recollection_beam` / `_fanout` / `_rounds` / `_alpha` | **3 / 2 / 3 / 0.5** | 回忆环 B / F / R / α（较少需按部署调整，保留为运行时字段） |

**程序性 / 偏好记忆即时 flush 去抖（运行时字段；总开关与注入/裁剪阈值已上 `MEMORY_CONFIG`，见 §8.2）**：

| 字段 | 默认 | 含义 |
|---|---|---|
| `preference_flush_debounce_seconds` | **60.0** | 即时 flush 的 per-scope debounce（防"连环纠正→flush 风暴"） |

> 另：`memory_config.qdrant_provider`（只读 `@property`，转发 `ai_config`）作回忆环前置校验。
> RF-Mem / 偏好记忆的总开关（`enable_familiarity_routing` / `enable_recollection_path` / `enable_preference_memory`）与标定阈值（`familiarity_theta_*` / `tau` / `lambda` / `probe_k`、`preference_*` 注入项）自 2026-06-14 起改为 `MEMORY_CONFIG` 项、WebConsole 可调，详见 §8.2。

### 8.2 `memory_config` 上的 `@property`（转发自 `ai_config.memory_config`）

可由用户在 WebConsole 的"GsCore AI 记忆配置"分组修改，持久化到 `data/ai_core/memory_config.json`：

| 属性                                | 默认值    | 来源（`MEMORY_CONFIG`）                  | 含义                                                                       |
|-------------------------------------|-----------|------------------------------------------|----------------------------------------------------------------------------|
| `memory_mode`                       | `["被动感知","主动会话"]` | `GsListStrConfig` | 记忆路径（多选）                                                            |
| `memory_session`                    | `按人格配置` | `GsStrConfig`（二选一）              | 被动感知范围                                                                |
| `retrieval_top_k`                   | **15**    | `GsIntConfig`（5/10/15/20）              | 最终检索数量                                                               |
| `memory_inject_max_chars`           | **2000**  | `GsIntConfig`（1000~16000）              | 单次注入 Prompt 的记忆字符预算                                             |
| `enable_system2`                    | **False** | `enable_system2get`（`GsBoolConfig`）    | 是否启用 System-2（成本较高）                                              |
| `background_episode_count`          | **1**     | `GsIntConfig`（0~3）                     | 抽取时注入的近期 Episode 数量；0 = 不注入背景                              |
| `background_episode_max_chars`      | **600**   | `GsIntConfig`（300~2000）                | 每条背景 Episode 在抽取提示词中的字符上限                                  |
| `extraction_value_gate`             | `均衡`    | `GsStrConfig`（宽松/均衡/严格）          | 抽取价值门控档位                                                           |
| `hiergraph_build_mode`              | `自动`    | `GsStrConfig`（自动/始终/仅摘要/关闭）  | 分层图构建模式                                                             |
| `hiergraph_batch_size`              | **20**    | `GsIntConfig`（15/20/30/40）             | 建树 LLM 分类单批节点数                                                    |
| `hiergraph_vector_assign_threshold` | **0.85**  | `GsStrConfig`（存为字符串，运行时 `float()`） | 向量预分配余弦阈值；调低让更多实体走零 LLM 预分配                          |
| `hiergraph_min_entities`            | **30**    | `GsIntConfig`（30~200）                  | 小 scope 跳过分层图的实体门槛（含轻量摘要）                                |
| `hiergraph_max_existing_cats`       | **50**    | `GsIntConfig`（20/30/50/80）             | 分类输入每批最多带入的已有类目数（仅名称）                                 |
| `hiergraph_node_summary_chars`      | **60**    | `GsIntConfig`（0/30/60/100）             | 每个待分类节点附带的实体摘要字符上限                                       |
| `hiergraph_summary_delta`           | **50**    | `GsIntConfig`（50/100/200/500）          | 群摘要刷新的新增实体阈值；调大省 Token                                     |
| `eval_mode`                         | **False** | `GsBoolConfig`                           | 评测模式：禁用 System-2 和 Rerank；摄入时不自动触发分层图重建，由外部 `rebuild_task` 统一触发 |
| `enable_preference_memory`          | **True**  | `GsBoolConfig`                           | 程序性/偏好记忆总开关（**默认开**）；关闭后写入/蒸馏/注入/即时 flush 全部停用 |
| `preference_max_inject`             | **12**    | `GsIntConfig`（5/8/12/20）               | 单次注入偏好条数上限 |
| `preference_max_per_context`        | **5**     | `GsIntConfig`（3/5/8/12）                | 单 `target_context` 活跃规则上限（生命周期裁剪用） |
| `preference_inject_budget_ratio`    | **0.10**  | `GsFloatConfig`（0.0~0.5）               | 偏好区块占注入预算比例（置顶、强约束） |
| `preference_immediate_flush`        | **True**  | `GsBoolConfig`                           | 纠错命中即时 flush（受总开关前置） |
| `enable_familiarity_routing`        | **False** | `GsBoolConfig`                           | RF-Mem 探针熟悉度路由总开关（关时不发探针、行为不变） |
| `enable_recollection_path`          | **False** | `GsBoolConfig`                           | 回忆环开关（仅 `qdrant_provider=remote` + 路由判低熟悉 + System-2 未触发时生效） |
| `familiarity_theta_high`            | **0.6**   | `GsFloatConfig`（0.0~1.0）               | 熟悉度上阈 θ_high（**余弦语义，须按嵌入模型标定**，见实现文档 §7.4） |
| `familiarity_theta_low`             | **0.3**   | `GsFloatConfig`（0.0~1.0）               | 熟悉度下阈 θ_low（同上需标定） |
| `familiarity_tau`                   | **0.22**  | `GsFloatConfig`（0.0~1.0）               | 列表熵阈 τ（中段由熵裁决；论文 0.2~0.25） |
| `familiarity_lambda`                | **20.0**  | `GsFloatConfig`（1.0~100.0）             | 温度 softmax 锐度 λ（论文 20~30，不敏感） |
| `familiarity_probe_k`               | **15**    | `GsIntConfig`（10/15/20/30）             | 探针候选数 |

> **注意**：`enable_system2get` 是 ai_config 中的**真实配置键名**（早期文档中写的 `enable_system2` 是 `memory_config` 上的 `property` 名）；两者含义一致——后者只是前者的别名映射。
> RF-Mem 回忆环内部超参（`recollection_beam/_fanout/_rounds/_alpha`）与即时 flush 去抖（`preference_flush_debounce_seconds`）仍为 §8.1 运行时字段，未上 WebConsole（较少需调整）。

### 8.3 代码内硬编码常量

| 常量                                  | 值      | 位置                                                              | 作用                                                            |
|---------------------------------------|---------|-------------------------------------------------------------------|-----------------------------------------------------------------|
| 观察队列 maxsize                      | 10000   | [`memory/observer.py:28`](../gsuid_core/ai_core/memory/observer.py:28) | 队列容量，满则丢最老一条                                        |
| `_REPEAT_WINDOW`                      | 12      | observer.py                                                      | 复读检测窗口                                                    |
| `_LOW_TIER_MAX_LEN`                   | 10      | observer.py                                                      | 短句降级 LOW 阈值（仅宽松档位生效）                              |
| `MAX_ENTITIES_PER_REBUILD`            | 800     | hiergraph.py                                                     | 单轮重建实体上限（**非** ai_config 可配，硬编码）                |
| `VECTOR_ASSIGN_TOP_K`                 | 5       | hiergraph.py                                                     | 向量预分配检索近邻数                                            |
| `MIN_DELTA`                           | 20      | hiergraph.py                                                     | 重建最小增量（避免冷启动反复重建）                              |
| `_APPEND_MAX_RETRY`                   | 5       | [`state_store/store.py:20`](../gsuid_core/ai_core/state_store/store.py:20) | state_store 乐观锁最大重试次数（与记忆正交，列在此方便对照）    |
| `_RERANK_EXECUTOR` max_workers        | 3       | dual_route.py                                                    | Reranker 线程池大小                                              |
| `Fact budget / Cat budget / Ep budget` | 55% / 15% / 30% | dual_route.py `to_prompt_text`                            | Prompt 注入预算分配                                              |
| `_PROFILE_SCOPE`                      | `__gscore_group_profile__` | group_profile.py                                       | 群组画像在 state_store 的 scope（双下划线保留命名）              |
| `_MAX_TERM_MAPPINGS`                  | 60      | group_profile.py                                                 | 词汇映射表容量上限                                              |
| `_MAX_TAGS`                           | 40      | group_profile.py                                                 | 群标签容量上限                                                  |
| `_MAX_MEMBER_ALIASES`                 | 60      | group_profile.py                                                 | 群成员称呼表容量上限                                            |
| `_MAX_IDS_PER_ALIAS`                  | 5       | group_profile.py                                                 | 单称呼最多绑定的用户数                                          |
| `DECAY_STALE_DAYS`                    | 14      | consolidation_worker.py                                          | 衰减判定天数                                                    |
| `DECAY_FACTOR`                        | 0.85    | consolidation_worker.py                                          | 单次衰减系数                                                    |
| `PROTECT_MENTION_COUNT`               | 3       | consolidation_worker.py                                          | 高频保护阈值                                                    |
| `FORGET_THRESHOLD`                    | 0.1     | consolidation_worker.py                                          | 遗忘阈值                                                        |
| `ORPHAN_ENTITY_TTL_DAYS`              | 10      | consolidation_worker.py                                          | 孤儿实体回收 TTL                                                |
| 孤儿 GC CHUNK                         | 500     | consolidation_worker.py                                          | 孤儿回收分块大小（防 SQLite 变量上限 + Qdrant 超时）             |
| `EPISODE_HOT_DAYS`                    | 30      | consolidation_worker.py                                          | Episode 保留为热的最小年龄窗（§3.2①）                           |
| `EPISODE_HOT_PER_SCOPE`               | 2000    | consolidation_worker.py                                          | 每 scope 保留为热的最近 Episode 条数                            |
| `EPISODE_MAX_PER_SCOPE`               | 20000   | consolidation_worker.py                                          | 每 scope Episode 物理上限（超限删最老的冷且无引用）             |
| `EDGE_MAX_PER_SCOPE`                  | 50000   | consolidation_worker.py                                          | 每 scope Edge 软上限（salience 驱动裁剪，§3.2③）                |
| `ENTITY_MAX_PER_SCOPE`                | 50000   | consolidation_worker.py                                          | 每 scope Entity 软上限（淘汰无边非 speaker 实体）               |
| `RECONCILE_SCROLL_BATCH`              | 500     | consolidation_worker.py                                          | SQL↔Qdrant 悬空向量对账分页批大小（§2）                         |

### 8.4 上层 ai_config 联动项

| 字段 (`AI_CONFIG`)            | 默认值 | 含义                                                       |
|-------------------------------|--------|------------------------------------------------------------|
| `enable`                      | False  | AI 总开关；关闭后记忆系统初始化直接跳过                    |
| `enable_memory`               | True   | 记忆总开关                                                  |
| `embedding_provider`          | `local` | 嵌入模型服务提供方（`local` / `openai`），决定 Qdrant 写入路径 |
| `rerank_provider`             | `local` | Rerank 服务提供方；`eval_mode=True` 时强制不用             |
| `qdrant_provider`             | `local` | Qdrant 部署方式（`local` 嵌入式 / `remote` 远程）        |

---

## 9. 文件职责索引

| 文件                                                                                | 职责                                                                                                |
|-------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| [`memory/__init__.py`](../gsuid_core/ai_core/memory/__init__.py)                   | 模块导出；`observe`、`dual_route_retrieve`、`MemoryContext`、`memory_config`、`clear_*`            |
| [`memory/scope.py`](../gsuid_core/ai_core/memory/scope.py)                         | `ScopeType` 与 `make_scope_key`                                                                    |
| [`memory/config.py`](../gsuid_core/ai_core/memory/config.py)                       | `MemoryConfig` 运行时配置门面（直接字段 + `@property` 转发 ai_config）                            |
| [`memory/startup.py`](../gsuid_core/ai_core/memory/startup.py)                     | `init_memory_system`（Collection + IngestionWorker + 多模态 Worker + 生命周期定时任务）+ `shutdown` + `get_ingestion_worker` |
| [`memory/observer.py`](../gsuid_core/ai_core/memory/observer.py)                   | 观察入口 `observe()` + 纯规则门控 `_gate()` + 分级 `_classify_value_tier` + SELF scope 路由       |
| [`memory/group_profile.py`](../gsuid_core/ai_core/memory/group_profile.py)         | 群组画像（`term_mappings` / `tag_counts` / `member_alias_ids`），复用 `AIPersistentState`          |
| [`memory/database/models.py`](../gsuid_core/ai_core/memory/database/models.py)     | 全部 SQLModel 模型 + 多对多关联表 + 业务方法（去重、衰减、遗忘、孤儿 GC、置信度折算输入）          |
| [`memory/database/clear_ops.py`](../gsuid_core/ai_core/memory/database/clear_ops.py) | 按 scope 精确/前缀清空（SQL + Qdrant，支持 `dry_run`）                                              |
| [`memory/vector/collections.py`](../gsuid_core/ai_core/memory/vector/collections.py) | 三个 Collection 名常量                                                                            |
| [`memory/vector/startup.py`](../gsuid_core/ai_core/memory/vector/startup.py)       | Collection 创建 / 维度迁移 / 重嵌入                                                                  |
| [`memory/vector/ops.py`](../gsuid_core/ai_core/memory/vector/ops.py)               | Episode / Entity / Edge 向量 upsert/search + `_hybrid_search_entities` + `search_categorized_neighbors` |
| [`memory/ingestion/worker.py`](../gsuid_core/ai_core/memory/ingestion/worker.py)   | `IngestionWorker`（主循环后台 task，buffer/flush）+ `_ingest_batch` + `_llm_extract` + 别名 / 主人标签 / 折叠 |
| [`memory/ingestion/entity.py`](../gsuid_core/ai_core/memory/ingestion/entity.py)   | Entity 两阶段去重写入（SQL + 向量，含 3 次指数退避 + 30s 全局超时）                                |
| [`memory/ingestion/edge.py`](../gsuid_core/ai_core/memory/ingestion/edge.py)       | Edge 归并 / 极性冲突检测 / `AIMemConflict` 记录 / 写入                                              |
| [`memory/ingestion/hiergraph.py`](../gsuid_core/ai_core/memory/ingestion/hiergraph.py) | `AIMemHierarchicalGraphMeta` + `HierarchicalGraphBuilder.incremental_rebuild` + `_vector_pre_assign` + `_run_group_summary_llm` + `increment_entity_count` |
| [`memory/ingestion/multimodal.py`](../gsuid_core/ai_core/memory/ingestion/multimodal.py) | `ImageUnderstandWorker`（独立队列，异步图片转述）+ `submit_image_observation` 入队                |
| [`memory/retrieval/types.py`](../gsuid_core/ai_core/memory/retrieval/types.py)     | TypedDict（Episode / Entity / Edge / Category / RetrievalMeta）                                     |
| [`memory/retrieval/system1.py`](../gsuid_core/ai_core/memory/retrieval/system1.py) | 向量相似度检索 + One-hop 邻居扩展                                                                  |
| [`memory/retrieval/system2.py`](../gsuid_core/ai_core/memory/retrieval/system2.py) | 分层图自顶向下遍历选择（含 Recursive CTE、深度熔断）                                                |
| [`memory/retrieval/dual_route.py`](../gsuid_core/ai_core/memory/retrieval/dual_route.py) | 双路编排 + 并行 Reranker + `MemoryContext.to_prompt_text`（含偏好置顶区块）+ `compute_edge_confidence` + 探针路由/回忆环编排（RF-Mem，默认关）+ 偏好检索注入编排（默认开） |
| [`memory/retrieval/familiarity.py`](../gsuid_core/ai_core/memory/retrieval/familiarity.py) | **RF-Mem（默认关）**：熟悉度信号 `compute_familiarity_signal` / 路由 `decide_route`（Eq. 3）/ 回忆环 `recollection_search`（KMeans+α-mix，beam 按 `Σ<x',z_i>` 留 top-B）/ 关系投影 `project_episodes_to_edges`；KMeans 走专用线程池 |
| [`memory/ingestion/tool_trace.py`](../gsuid_core/ai_core/memory/ingestion/tool_trace.py) | **程序性记忆（默认开）**：按 user_id 分桶的有界 ring buffer，记近期工具调用轨迹供偏好蒸馏作背景（纯内存、TTL 过期、零持久化） |
| [`memory/lifecycle/consolidation_worker.py`](../gsuid_core/ai_core/memory/lifecycle/consolidation_worker.py) | `run_lifecycle_maintenance`（巩固 / 衰减 / 遗忘 / Edge·Entity 容量裁剪 / 孤儿 GC / Episode 保留降级 / 悬空向量对账） |
| [`memory/prompts/extraction.py`](../gsuid_core/ai_core/memory/prompts/extraction.py) | Entity/Edge 抽取 System + User prompt                                                            |
| [`memory/prompts/categorization.py`](../gsuid_core/ai_core/memory/prompts/categorization.py) | 分层分类 System + User prompt（含 `LAYER_HINTS`）                                              |
| [`memory/prompts/selection.py`](../gsuid_core/ai_core/memory/prompts/selection.py) | System-2 选择 prompt                                                                                |
| [`memory/prompts/summary.py`](../gsuid_core/ai_core/memory/prompts/summary.py)     | 群摘要生成 prompt                                                                                  |
| [`memory/prompts/output_models.py`](../gsuid_core/ai_core/memory/prompts/output_models.py) | 抽取/分类的 Pydantic 输出模型（备用，当前 `_llm_extract` / `_llm_categorize` 走 JSON 解析）     |

---

## 10. 关键设计约束与不变量

1. **门控与生命周期 100% 纯规则，绝不调用 LLM**：噪声过滤、分级、衰减、遗忘、孤儿回收均为正则/数值运算，杜绝在低价值环节烧 token。`extraction_value_gate` 是档位，不是"调低 LLM 频率"。
2. **Episode 永远先持久化、durable**：抽取阶段（Entity / Edge / LLM）失败仅记日志、不退回 Episode 重试——Episode 无幂等键，重试会重复写入并使计数虚高（[`worker.py:433-444`](../gsuid_core/ai_core/memory/ingestion/worker.py:433) N-2 修复）。
3. **Scope 严格隔离**：群间记忆不可见；Bot 自身发言只进 `SELF` scope（仅 Episode、`LOW`），永不污染群组事实图谱（C6）。`memory_session == "按人格配置"` 时还需命中 `persona_config_manager.get_persona_for_session` 才入队。
4. **SQL 是权威、向量是索引**：写入先 SQL 后向量（Entity 3 次指数退避 + 30s 全局超时）；删除（遗忘/孤儿/清空）同步删两侧。
5. **记忆与发言决策正交**：即便人格纯静默、或者 AI 总开关 `enable=False`，记忆只在 `init_memory_system` 失败时静默跳过；只要初始化成功，`observe()` 永远 fire-and-forget，不阻塞主流程。
6. **entities 不直接注入 Prompt**：只通过 edges / categories 间接生效——这是无 edge 孤儿实体可安全回收的根据。
7. **重建成本随"新增"而非"存量"增长**：入口过滤 + 向量预分配 + Layer-2/3 增量化 + 单轮上限（800）+ 小 scope 跳过 五项叠加，保证大规模下重建仍可控（一次常规重建 LLM 调用从"几百~几千次"降到"个位数甚至 0"）。
8. **IngestionWorker 在主事件循环上跑**：LLM `await` 是网络 I/O 不阻塞循环；embedding 走 `vector/ops.py` 独立线程池。**禁止**复活"独立线程 + 双事件循环"架构——WinError 995 → InvalidStateError → run_forever 崩溃 → WS 全线断连。
9. **分层类目树的唯一消费方是 System-2 检索 + 群摘要缓存**：`hiergraph_build_mode != "始终"` 且 `enable_system2=False` 时，类目树无任何消费方，应跳过整棵 Layer-1/2/3 的 LLM 分类以省 Token。Entity / Edge / Episode 等记忆本体不受任何模式影响。
10. **类型与异常**（遵循 [`LLM.md`](LLM.md)）：完整类型提示；不用 `cast` / `type: ignore` / `getattr` / `.get` 兜底掩盖类型问题；try-except 仅用于 Qdrant / LLM 等外部 I/O 边界的优雅降级（典型：`extracted["entities"] if "entities" in extracted else []`）。

---

> **维护提示**：
> - 本系统的写入 / 检索 / 重建 / 维护四段相互解耦，改动某段时请对照本章的不变量。
> - 改动任何 ai_config `MEMORY_CONFIG` 字段的默认值时，请同步更新本文档 §8.2 与 [`gsuid_core/webconsole/docs/22-ai-memory.md`](gsuid_core/webconsole/docs/22-ai-memory.md) §22.15。
> - 改动任何 `memory_config.MemoryConfig` 字段默认值时，请同步更新本文档 §8.1。
> - 改动硬编码常量（§8.3）时，请在该常量所在文件顶部 docstring 解释设计取舍，并在本文档同步列出。
