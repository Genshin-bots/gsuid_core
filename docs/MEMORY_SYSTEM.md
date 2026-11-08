# 记忆系统（schema_v2）

> 2026-11-08 起，记忆系统进入 schema_v2：四表 + 偏好、两阶段摄入、四态代数、五模态检索。
> 旧的 `AIMemSession` / `AIMemTurnGist` / `AIMemEvent` / `AIMemConflict` / `AIMemCategory*` /
> `AIMemHierarchicalGraphMeta` 已物理删除，旧 System-2 图遍历与分层类目树不再存在。

---

## 1. 总体链路

```
入站消息
  ├─ TurnRouter（一次无思考低档调用）→ TurnRouteDecision
  │    intent / memory_intent / soft_speak / is_live_lookup / needs_committed_work
  ├─ MemoryObserver（富元数据 + 环形窗口）
  └─ IngestionWorker（28 条 / 180s 空闲 / 1800s 上限）
       ├─ Pass 1 WindowRouter：话题分诊 → 多 Episode 或 open_lines
       └─ Pass 2 Algebra：实体 / 四态边 / 偏好 / 故事线里程碑

检索
  └─ dual_route_retrieve(memory_intent=...)：FTS5 + 稠密 S1 + 线程时间轴
       ├─ point_latest / version_diff / timeline_order / broad_summary / none
       └─ 边过滤、版本链展开、升序时间线、线程/会话均衡节选
```

**不再有**：正则意图分类、System-2 LLM 问答、LLM 压缩摘要、决策蒸馏二次 LLM 调用、
分层图定时重建、`AIMemCategory` 真实表。

---

## 2. 数据模型（四表 + 偏好）

| 表 | 作用 | 关键字段 |
|----|------|----------|
| `AIMemThread` | 故事线 / 专题 | `scope_key`、`title`、`aliases`、`summary`、`status`、`first_at`、`last_at`、`milestone_count`、`watermark_valid_at` |
| `AIMemEpisode` | 原始对话片段 | `scope_key`、`content`、`speaker_ids`、`valid_at`、`session_id`、`turn_index`、`thread_id`、`qdrant_id` |
| `AIMemEntity` | 人或物 | `scope_key`、`name`、`summary`、`tag`、`entity_type`、`user_id`、`is_speaker`、`qdrant_id` |
| `AIMemEdge` | 四态事实边 | `scope_key`、`fact`、`source_entity_id`、`target_entity_id`、`edge_kind`、`slot_key`、`resolution_state`、`superseded_by_id`、`contradicts_edge_id`、`thread_id`、`source_episode_id`、`event_at`、`valid_at`、`invalid_at` |
| `AIMemPreference` | 用户偏好 | `user_id`、`target_context`、`preference_rule`、`polarity`、`is_correction` |

- `resolution_state` 只允许 `active / staging / superseded / contradicted`。
- `staging` = 转述、传闻、第三人称未证实信息；本人自述直接 `active`。
- `superseded` 保留 `invalid_at` 与 `superseded_by_id`；`contradicted` 双边互相记录 `contradicts_edge_id`，不设 `invalid_at`。
- `AIMemThread` 是**追加式里程碑脊柱**：`AIMemEdge.edge_kind == "milestone"` 的边按 `event_at` 追加，不改写旧里程碑。
- Episode 关联实体通过 `mem_episode_entity_mentions` 多对多表。

SQLite 另有 FTS5 虚表 `aimemepisode_fts`（`unicode61`），用于 Episode 词面召回；非 SQLite 退化为参数化 LIKE。

---

## 3. 前置路由 TurnRouter

`gsuid_core/ai_core/classifier/turn_router.py`

- CLASSIFY 点位**只调用一次** `route_turn()`，结果写入 `AgentHookContext.route_decision`。
- 模型必须是低档无思考通道：`get_fast_model_for_task("low")`，隔离用户为 `max` 通道配置。
- 输出：`intent`、`memory_intent`、`soft_speak`、`is_live_lookup`、`needs_committed_work`、`time_start/end`。
- 离线 / 超时 / 非法 JSON → `structural_route()`：
  - 空串 → `闲聊 / none`
  - 句末问号 → `问答 / point_latest`
  - 上一轮用过工具且 ≤24 字 → `工具 / point_latest`
  - 其余 → `闲聊 / none`
- 下游 ReactiveGate、MemoryKit、ScaffoldKit、Visibility、`search_cognition` 只消费结论，不再扫文本猜语义。

`mode_classifier.py` 仅保留旧 `IntentService` 调用形状，内部走结构退化。

---

## 4. 两阶段摄入

### 4.1 观察与窗口

`memory/observer.py` 的 `ObservationRecord` 记录：

- `speaker_id` / `speaker_name` / `message_id`
- `reply_to_msg_id` / `reply_preview` / `at_targets`
- `timestamp` / `timestamp_ms` / `command_hit` / `raw_content`

`_ScopeWindowBuffer` 按 scope 维护：

- 容量 28 条立即 flush
- 空闲 180s 且至少 4 条 flush
- 窗口最老消息 1800s 强制 settle
- `flush_all()` 在关闭 / 评测前强制 settle

### 4.2 Pass 1 WindowRouter

`memory/ingestion/window_router.py`

- 输入：本窗对话 + 最近活跃 Thread 卡片。
- 输出：`clusters[]`（行号区间、`action=discard|episode_only|extract`、`thread_ref`、`topic_title`）与 `open_lines[]`。
- 规则：`open_lines` 只能是窗口尾部最多 8 行；模型漏行按 `episode_only` 收口，不静默丢消息。
- LLM 不可用 / 超时 / 非法输出 → 整窗 `episode_only`，**不会**回退到固定 JSON 抽取。

### 4.3 Pass 2 Algebra

`memory/ingestion/algebra.py`

1. 解析实体（姓名、类型、摘要），经 `AIMemEntity.extract_and_upsert` 两阶段去重写入。
2. 解析边操作：
   - `ADD` → 新建边
   - `MERGE(target)` → 合并事实、`mention_count + 1`
   - `SUPERSEDE(old)` → 新边 active，旧边 superseded + invalid_at + superseded_by_id
   - `CONTRADICT(old)` → 双边 contradicted，互相记录 `contradicts_edge_id`
3. `provenance_tier`：本人自述或 `subject_user_id` 为空 → active；他人转述 → staging。
4. 偏好写入 `USER_GLOBAL` scope。
5. `milestone` 边绑定 `thread_id`、`event_at`、`source_episode_id`，并推进 Thread 的 `milestone_count / last_at`。
6. Pass 2 整体受 `db_write_guard` 保护，单条坏输出只丢该条。

---

## 5. 检索

`memory/retrieval/dual_route.py`

`dual_route_retrieve(..., memory_intent=...)`：

| memory_intent | Episode | Edge |
|---------------|---------|------|
| `none` | 不重检索，仅偏好 + 高频实体卡 | 无 |
| `point_latest` | FTS5 + 稠密 + 近邻扩展 | 只取 active / contradicted；同 slot 有 active 时丢弃 staging / superseded；按 `event_at, valid_at` 倒序 |
| `version_diff` | 常规召回 | 展开 superseded 版本链，按 `valid_at` 升序 |
| `timeline_order` | Thread 里程碑脊柱 + Episode 时间轴；无 Thread 时按 session 分桶、每桶最多 2 条、严格升序 | 里程碑 + 相关事实，按时间升序 |
| `broad_summary` | 线程/会话均衡节选，保留原文，不调 LLM 压缩 | active 属性/关系骨架，不含 milestone |

`MemoryContext.to_prompt_text()`：

- 偏好块置顶。
- contradicted 边加 `⚠️[存在未消解矛盾]`，staging 加 `[待证实传闻]`，version_diff 的 superseded 加 `[已于后续更新]`。
- `timeline_order` 加 `MEMORY_TIMELINE_ORDER_NOTE`，`broad_summary` 保持原文节选。
- 所有召回正文经 `wrap_untrusted("memory_recall", ...)` 包裹。

---

## 6. schema_v2 迁移

`memory/database/migration_v2.py` 在 `init_memory_system()` 最前面执行：

1. 读 `data/ai_core/memory/schema_version.json`；已是 v2 只确保 FTS 虚表与查询索引（`ix_mem_episode_valid_at` 等）。
2. SQLite 备份到 `data/ai_core/memory/backup_v1/GsData_pre_v2.db.bak`，并复制 `group_profiles/`。
3. DROP 旧表（含 `mem_episode_entity_mentions` / `aimemepisode_fts`）；`aimempreference.source_episode_id` 置空。
4. `SQLModel.metadata.create_all` 建四表 + 偏好 + 关联表，建 FTS5。
5. 删除旧 Qdrant 集合（`memory_sessions / memory_gists / memory_events / memory_categories / memory_hiergraph / memory_conflicts`）及 payload 备份。
6. 清理 `group_profiles` 的 `term_mappings / pending_terms / tag_counts`。
7. 写版本标记。迁移失败则记忆初始化中止，不带着半套 schema 运行。

**保留**：`AIMemPreference`、`UserFavorability`、`AIPersistentState`、全部插件业务表。

---

## 7. Qdrant 集合

`memory/vector/collections.py` 只保留四个活跃集合：

| 集合 | 向量 |
|------|------|
| `memory_episodes` | `dense` |
| `memory_entities` | `name_dense` + `summary_dense` |
| `memory_edges` | `dense` |
| `memory_threads` | `dense` |

统一 Payload 索引：`scope_key`（KEYWORD）、`valid_at_ts`（FLOAT）。

---

## 8. WebConsole 与前端

- `GET /api/ai/memory/categories`：零 Token 虚拟投影。
  - `virt:L1:attribute|relation|milestone|conflict` → 四大原语
  - `virt:thread:{thread_id}` → 故事线
- `GET /api/ai/memory/categories/{category_id}`：上述虚拟节点详情。
- `GET /api/ai/memory/edges?include_history=true`：包含 staging / superseded。
- `GET /api/ai/memory/threads`：故事线分页。
- `POST /api/ai/memory/hiergraph/rebuild`：零 LLM，只做 FTS 与 Qdrant 补偿。
- 前端 `gsuid_hub` 的 `src/pages/AIMemoryPage.tsx` 已适配虚拟分类、四态边标签、故事线页签与历史边开关。

---

## 9. 文件职责索引

| 文件 | 职责 |
|------|------|
| `classifier/turn_router.py` | `TurnRouteDecision` + 唯一前置路由调用 + 结构退化 |
| `classifier/mode_classifier.py` | 旧 `IntentService` 兼容壳 |
| `hooks/models.py` | `AgentHookContext.route_decision` |
| `kits/classifier/kit.py` | CLASSIFY 点位调用 TurnRouter |
| `kits/reactive_gate/kit.py` | 消费 `soft_speak` |
| `kits/decision_distill/kit.py` | 缓冲 + 规则摘要，零 LLM |
| `memory/observer.py` | 富元数据观察、噪声/命令门控、环形窗口 |
| `memory/ingestion/window_router.py` | Pass 1 话题分诊 |
| `memory/ingestion/algebra.py` | Pass 2 四态代数 + 偏好 + 线程里程碑 |
| `memory/ingestion/worker.py` | 窗口消费、Episode 落库、Pass 1/2 调度 |
| `memory/database/fts.py` | FTS5 索引 + LIKE 回退 |
| `memory/database/models.py` | 四表 + 偏好 + 业务方法 |
| `memory/database/migration_v2.py` | 一次性净室迁移 |
| `memory/retrieval/dual_route.py` | 五模态检索与 Prompt 装配 |
| `memory/retrieval/thread_recall.py` | Thread / session 时间轴召回 |
| `memory/lifecycle/sleep_extract.py` | 睡眠期补线程：无 Thread 的 session 归入故事线 |
| `memory/lifecycle/consolidation_worker.py` | 衰减、遗忘、容量裁剪、孤儿 GC、向量对账 |
| `rag/tool_lexical.py` | 工具中文 CJK-BM25 + RRF |
| `webconsole/ai_memory_api.py` | 记忆控制台 API（虚拟分类 / 线程 / 四态边） |

---

## 10. 不变量

1. **TurnRouter 是语义判断的唯一入口**；检索、门控、脚手架只消费结论。
2. **Episode 先持久化**：Pass 2 抽取失败只丢抽取结果，不重试写 Episode。
3. **schema_v2 不允许旧表回灌**：迁移后任何 `AIMemSession` / `AIMemTurnGist` / `AIMemEvent` / `AIMemConflict` / `AIMemCategory` / `AIMemHierarchicalGraphMeta` 引用都是事故。
4. **SQL 是权威，向量是索引**：写入 SQL 后再写 Qdrant；删除两侧同步。
5. **前缀缓存红线**：系统提示运行中不改写；动态记忆只进 user 侧，落盘前剥掉。
6. **人格 / 能力不锁定**：框架不写死口癖与业务词表；能力由插件 `covers` / `aliases` 自描述。
7. **当轮控制句唯一来源**：`control/lines.py`。
8. **测试规模**：`tests/` 收集用例 ≤200；一次性验证脚本用后即删。
