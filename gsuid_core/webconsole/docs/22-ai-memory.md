# 22. AI Memory API - /api/ai/memory

> **架构版本：schema_v2（2026-11-08 起）**
> 记忆系统已升级为 **四表 + 偏好、两阶段摄入（WindowRouter + Algebra）、四态事实代数、五模态检索（`dual_route_retrieve`）**。
> 旧 System-2 分层语义图、`AIMemCategory*` 与 `AIMemHierarchicalGraphMeta` 已物理移除；`/categories` 改为零 Token 虚拟分类投影，`/hiergraph/rebuild` 改为零 LLM 的 FTS5 与 Qdrant 向量补偿对账。
> 完整内部架构详见 [`docs/MEMORY_SYSTEM.md`](../../../docs/MEMORY_SYSTEM.md)。

提供 AI 记忆系统的管理接口，包括五模态记忆检索、Episode/Entity/Edge/Thread/Preference 浏览与治理、虚拟分类投影、索引对账、配置与统计信息等。

## 系统概述

记忆系统采用 **关系数据库（SQLAlchemy + SQLite FTS5）+ 向量数据库（Qdrant）** 混合存储与 **五模态检索** 架构（SQL 是权威真值源，Qdrant 与 FTS5 是索引）：

- **前置路由（`TurnRouter`）**：入站消息在 `CLASSIFY` 点位经一次无思考低档调用产出 `memory_intent`（`none` / `point_latest` / `version_diff` / `timeline_order` / `broad_summary`）。
- **两阶段摄入（`IngestionWorker`）**：
  - **Pass 1 `WindowRouter`**：按话题切分对话窗口，归入已有或新建的 `AIMemThread` 故事线，或标记为 `episode_only` / `open_lines`。
  - **Pass 2 `Algebra`**：抽取实体、执行四态边代数操作（`ADD` / `MERGE` / `SUPERSEDE` / `CONTRADICT`）、写入 `USER_GLOBAL` 偏好规则，并将 `milestone` 边追加至故事线脊柱。
- **五模态检索（`dual_route_retrieve`）**：结合 FTS5 词面召回、Qdrant 稠密向量检索与 Thread/Session 时间轴，按 `memory_intent` 执行针对性的边状态过滤、版本链展开、升序时间线或跨会话均衡节选。

### 核心数据模型（四表 + 偏好）

| 模型 | 说明 | 存储层 |
|------|------|--------|
| **Thread** (`AIMemThread`) | 故事线 / 专题（追加式里程碑脊柱，记录起止时间与里程碑数） | DB + Qdrant (`memory_threads`) |
| **Episode** (`AIMemEpisode`) | 原始对话片段（关联 `session_id`、`turn_index`、`thread_id`） | DB + FTS5 (`aimemepisode_fts`) + Qdrant (`memory_episodes`) |
| **Entity** (`AIMemEntity`) | 提取的实体节点（人物/事物，含 `name_dense` + `summary_dense` 双向量） | DB + Qdrant (`memory_entities`) |
| **Edge** (`AIMemEdge`) | 四态事实边（`edge_kind`: `attribute`/`relation`/`milestone`；`resolution_state`: `active`/`staging`/`superseded`/`contradicted`） | DB + Qdrant (`memory_edges`) |
| **Preference** (`AIMemPreference`) | 程序性/偏好规则（纠正 Agent 未来行为，如"调 `generate_image` 用竖图"），**硬约束**工具调用 | DB（SQL-only，无向量） |

> **四态边状态说明**：
> - `active`：当前有效事实（本人自述或无特定主语归属）。
> - `staging`：转述、传闻或第三人称未证实信息（检索注入时标记 `[待证实传闻]`）。
> - `superseded`：已被后续新事实替代的历史版本（记录 `invalid_at` 与 `superseded_by_id`，在 `version_diff` 模态下展开）。
> - `contradicted`：存在未消解矛盾的事实（双边互相记录 `contradicts_edge_id`，注入时标记 `⚠️[存在未消解矛盾]`）。

### Scope Key 隔离体系

所有记忆节点均携带 `scope_key` 字段实现群组与用户间严格隔离：

| Scope 类型 | 格式 | 说明 |
|------------|------|------|
| `group` | `group:789012` | 群组级共享记忆 |
| `user_global` | `user_global:12345` | 用户跨群全局画像与偏好规则主存储 |
| `user_in_group` | `user_in_group:12345@789012` | 用户在特定群组内的局部档案 |

---

## 22.1 记忆检索

```
POST /api/ai/memory/search
```

按 `memory_intent` 执行五模态记忆检索（FTS5 词面 + Qdrant 稠密向量 + 故事线时间轴），返回结构化 `MemoryContext` 与组装后的 `prompt_text`。

**请求体**:
```json
{
    "query": "用户查询文本",
    "group_id": "789012",
    "user_id": "12345",
    "top_k": 10,
    "enable_system2": false,
    "enable_user_global": false,
    "memory_intent": "point_latest"
}
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | 是 | - | 查询文本，1-2000 字符 |
| `group_id` | string | 否 | null | 群组 ID |
| `user_id` | string | 是 | - | 用户 ID，用于定位用户记忆或联合跨群画像 |
| `top_k` | int | 否 | 10 | 返回结果数量上限，1-50 |
| `enable_system2` | bool | 否 | false | 兼容保留字段（schema_v2 已无分层图遍历） |
| `enable_user_global` | bool | 否 | false | 是否联合查询用户跨群画像 |
| `memory_intent` | string | 否 | `"point_latest"` | 检索模态：`none` / `point_latest` / `version_diff` / `timeline_order` / `broad_summary` |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "episodes": [
            {
                "id": "uuid-string",
                "content": "对话片段内容",
                "valid_at": "2026-11-08 10:30:00",
                "score": 0.92
            }
        ],
        "entities": [
            {
                "id": "uuid-string",
                "name": "实体名称",
                "summary": "实体摘要",
                "tag": ["标签1", "标签2"],
                "score": 0.88
            }
        ],
        "edges": [
            {
                "id": "uuid-string",
                "fact": "实体间关系或属性描述",
                "valid_at": "2026-11-08 10:30:00",
                "score": 0.85
            }
        ],
        "preferences": [
            {
                "id": "uuid-string",
                "target_context": "general",
                "preference_rule": "调用生成图片类工具时默认用竖图",
                "polarity": "do",
                "is_correction": true,
                "mention_count": 2
            }
        ],
        "retrieval_meta": {
            "memory_intent": "point_latest",
            "scope_keys": ["group:789012"]
        },
        "prompt_text": "<untrusted_content source=\"memory_recall\">...</untrusted_content>"
    }
}
```

> `preferences` 为**本次检索命中并注入的程序性/偏好硬约束**。规则治理见 [22.13–22.16](#2213-偏好规则列表)。该字段在 `enable_preference_memory` 关闭时为空数组。

---

## 22.2 Episode 列表

```
GET /api/ai/memory/episodes
```

获取 Episode（对话片段）列表，支持按群组或 `scope_key` 过滤和分页。设置 `all_scopes=true` 可返回所有范围的 Episode。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `group_id` | string | 否 | - | 群组 ID（优先级低于 `scope_key`） |
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 `group_id`） |
| `all_scopes` | bool | 否 | false | 是否返回所有范围的 Episode |
| `page` | int | 否 | 1 | 页码，从 1 开始 |
| `page_size` | int | 否 | 20 | 每页数量 |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [
            {
                "id": "uuid-string",
                "scope_key": "group:789012",
                "content": "聚合后的对话文本",
                "speaker_ids": ["user001", "user002"],
                "valid_at": "2026-11-08 10:30:00",
                "created_at": "2026-11-08 10:30:00",
                "qdrant_id": "uuid-string"
            }
        ],
        "total": 150,
        "page": 1,
        "page_size": 20
    }
}
```

---

## 22.3 Episode 详情

```
GET /api/ai/memory/episodes/{episode_id}
```

获取单个 Episode 详情，包含关联的 Entity 列表。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `episode_id` | string | Episode ID |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "uuid-string",
        "scope_key": "group:789012",
        "content": "聚合后的对话文本",
        "speaker_ids": ["user001", "user002"],
        "valid_at": "2026-11-08 10:30:00",
        "created_at": "2026-11-08 10:30:00",
        "qdrant_id": "uuid-string",
        "mentioned_entities": [
            {
                "id": "uuid-string",
                "name": "实体名称",
                "summary": "实体摘要",
                "tag": ["标签1"],
                "is_speaker": false
            }
        ]
    }
}
```

---

## 22.4 删除 Episode

```
DELETE /api/ai/memory/episodes/{episode_id}
```

删除单个 Episode，同时删除数据库记录和 Qdrant `memory_episodes` 集合中的向量。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `episode_id` | string | Episode ID |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": null
}
```

---

## 22.5 Entity 列表

```
GET /api/ai/memory/entities
```

获取 Entity（实体节点）列表，支持按群组、是否为说话者、名称搜索过滤。设置 `all_scopes=true` 可返回所有范围的 Entity。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `group_id` | string | 否 | - | 群组 ID |
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 `group_id`） |
| `all_scopes` | bool | 否 | false | 是否返回所有范围的 Entity |
| `is_speaker` | bool | 否 | - | 是否为说话者 |
| `search` | string | 否 | - | 名称搜索关键词 |
| `page` | int | 否 | 1 | 页码 |
| `page_size` | int | 否 | 20 | 每页数量 |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [
            {
                "id": "uuid-string",
                "scope_key": "group:789012",
                "name": "实体名称",
                "summary": "实体摘要描述",
                "tag": ["标签1", "标签2"],
                "is_speaker": false,
                "user_id": null,
                "created_at": "2026-11-08 10:30:00",
                "updated_at": "2026-11-08 12:00:00"
            }
        ],
        "total": 80,
        "page": 1,
        "page_size": 20
    }
}
```

---

## 22.6 Entity 详情

```
GET /api/ai/memory/entities/{entity_id}
```

获取单个 Entity 详情，包含关联的 Episode 和出入向 Edge 信息（各最多 20 条）。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `entity_id` | string | Entity ID |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "uuid-string",
        "scope_key": "group:789012",
        "name": "实体名称",
        "summary": "实体摘要描述",
        "tag": ["标签1", "标签2"],
        "is_speaker": false,
        "user_id": null,
        "created_at": "2026-11-08 10:30:00",
        "updated_at": "2026-11-08 12:00:00",
        "qdrant_id": "uuid-string",
        "episodes": [
            {
                "id": "uuid-string",
                "content": "对话片段内容（前200字）...",
                "valid_at": "2026-11-08 10:30:00"
            }
        ],
        "edges": [
            {
                "id": "uuid-string",
                "fact": "关系描述",
                "target_entity_id": "uuid-string",
                "valid_at": "2026-11-08 10:30:00",
                "direction": "outgoing"
            },
            {
                "id": "uuid-string",
                "fact": "关系描述",
                "source_entity_id": "uuid-string",
                "valid_at": "2026-11-08 10:30:00",
                "direction": "incoming"
            }
        ]
    }
}
```

---

## 22.7 删除 Entity

```
DELETE /api/ai/memory/entities/{entity_id}
```

删除单个 Entity，同时删除数据库记录和 Qdrant `memory_entities` 集合中的向量。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `entity_id` | string | Entity ID |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": null
}
```

---

## 22.8 Edge 列表

```
GET /api/ai/memory/edges
```

获取 Edge（四态事实边）列表，支持按群组、关联 Entity 过滤。默认仅返回 `active` 与 `contradicted` 状态的当前边；设置 `include_history=true` 可包含 `staging`（待证实传闻）与 `superseded`（已替代历史版本）。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `group_id` | string | 否 | - | 群组 ID |
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 `group_id`） |
| `all_scopes` | bool | 否 | false | 是否返回所有范围的 Edge |
| `entity_id` | string | 否 | - | 关联的 Entity ID（返回该 Entity 的出边和入边） |
| `include_history` | bool | 否 | false | 是否包含 `staging` 和 `superseded` 状态的边 |
| `page` | int | 否 | 1 | 页码 |
| `page_size` | int | 否 | 20 | 每页数量 |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [
            {
                "id": "uuid-string",
                "scope_key": "group:789012",
                "fact": "实体A喜欢实体B",
                "source_entity_id": "uuid-string",
                "target_entity_id": "uuid-string",
                "valid_at": "2026-11-08 10:30:00",
                "invalid_at": null,
                "created_at": "2026-11-08 10:30:00",
                "edge_kind": "relation",
                "slot_key": "like_target",
                "resolution_state": "active",
                "provenance_tier": "self",
                "superseded_by_id": null,
                "contradicts_edge_id": null,
                "thread_id": null,
                "event_at": "2026-11-08 10:30:00",
                "mention_count": 2,
                "decay_score": 1.0,
                "last_accessed": null
            }
        ],
        "total": 50,
        "page": 1,
        "page_size": 20
    }
}
```

---

## 22.9 Edge 详情

```
GET /api/ai/memory/edges/{edge_id}
```

获取单个 Edge 详情，包含四态代数元数据以及源 Entity 和目标 Entity 信息。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `edge_id` | string | Edge ID |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "uuid-string",
        "scope_key": "group:789012",
        "fact": "实体A喜欢实体B",
        "source_entity_id": "uuid-string",
        "target_entity_id": "uuid-string",
        "valid_at": "2026-11-08 10:30:00",
        "invalid_at": null,
        "created_at": "2026-11-08 10:30:00",
        "qdrant_id": "uuid-string",
        "edge_kind": "relation",
        "slot_key": "like_target",
        "resolution_state": "active",
        "provenance_tier": "self",
        "superseded_by_id": null,
        "contradicts_edge_id": null,
        "thread_id": null,
        "event_at": "2026-11-08 10:30:00",
        "mention_count": 2,
        "decay_score": 1.0,
        "source_entity": {
            "id": "uuid-string",
            "name": "实体A",
            "summary": "实体A的摘要"
        },
        "target_entity": {
            "id": "uuid-string",
            "name": "实体B",
            "summary": "实体B的摘要"
        }
    }
}
```

---

## 22.10 删除 Edge

```
DELETE /api/ai/memory/edges/{edge_id}
```

删除单个 Edge，同时删除数据库记录和 Qdrant `memory_edges` 集合中的向量。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `edge_id` | string | Edge ID |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": null
}
```

---

## 22.11 虚拟分类投影列表（Categories）

```
GET /api/ai/memory/categories
```

零 Token 虚拟分类投影（替代旧 `AIMemCategory` 表）：

- **`layer=0`（故事线专题）**：投影 `AIMemThread` 表，节点 ID 形如 `virt:thread:{thread_id}`，`member_entities_count` 对应故事线的 `milestone_count`。
- **`layer=1`（四大原语）**：实时 SQL 聚合投影 `AIMemEdge` 表：
  - `virt:L1:attribute`：人物与实体属性（`edge_kind == "attribute"` 且 `active`）
  - `virt:L1:relation`：跨实体关系网（`edge_kind == "relation"` 且 `active`）
  - `virt:L1:milestone`：关键事件里程碑（`edge_kind == "milestone"` 且 `active`）
  - `virt:L1:conflict`：待核实传闻与冲突（`resolution_state in ("contradicted", "staging")`）

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `group_id` | string | 否 | - | 群组 ID |
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 `group_id`） |
| `all_scopes` | bool | 否 | false | 是否返回所有范围的投影 |
| `layer` | int | 否 | - | 层级过滤：`0` = 故事线，`1` = 四大原语，不传则合并返回 |
| `page` | int | 否 | 1 | 页码 |
| `page_size` | int | 否 | 20 | 每页数量 |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [
            {
                "id": "virt:thread:uuid-string",
                "scope_key": "group:789012",
                "name": "模拟盘交易计划",
                "summary": "讨论模拟盘建仓与每日收盘总结安排",
                "tag": ["thread", "active"],
                "layer": 0,
                "parent_id": null,
                "child_categories_count": 0,
                "member_entities_count": 4,
                "created_at": "2026-11-08 10:00:00",
                "updated_at": "2026-11-08 12:00:00"
            },
            {
                "id": "virt:L1:attribute",
                "scope_key": "group:789012",
                "name": "人物与实体属性",
                "summary": "实时 SQL 投影，共 42 条",
                "tag": ["virtual", "attribute"],
                "layer": 1,
                "parent_id": null,
                "child_categories_count": 0,
                "member_entities_count": 42,
                "created_at": null,
                "updated_at": null
            }
        ],
        "total": 5,
        "page": 1,
        "page_size": 20
    }
}
```

---

## 22.12 虚拟分类详情

```
GET /api/ai/memory/categories/{category_id}
```

下钻查看虚拟分类详情：
- `virt:thread:{thread_id}`：返回该故事线的里程碑边（`edges`）、关联实体（`member_entities`）与源对话片段（`episodes`）。
- `virt:L1:{kind}`：返回对应原语下的边（最多 200 条）与涉及的实体列表。

**路径与 Query 参数**:

| 参数 | 位置 | 类型 | 说明 |
|------|------|------|------|
| `category_id` | path | string | 虚拟分类 ID（`virt:thread:...` 或 `virt:L1:...`） |
| `scope_key` | query | string | 可选 Scope Key（用于 `virt:L1:*` 过滤范围） |
| `group_id` | query | string | 可选群组 ID（优先级低于 `scope_key`） |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "virt:L1:attribute",
        "scope_key": "group:789012",
        "name": "人物与实体属性",
        "summary": "实时 SQL 投影，共 42 条",
        "tag": ["virtual", "attribute"],
        "layer": 1,
        "parent_id": null,
        "created_at": null,
        "updated_at": null,
        "parent_categories": [],
        "child_categories": [],
        "member_entities": [
            {
                "id": "uuid-string",
                "name": "实体名称",
                "summary": "实体摘要",
                "is_speaker": false
            }
        ],
        "edges": [
            {
                "id": "uuid-string",
                "fact": "属性事实描述",
                "resolution_state": "active",
                "valid_at": "2026-11-08 10:30:00"
            }
        ],
        "episodes": []
    }
}
```

---

## 22.13 偏好规则列表

```
GET /api/ai/memory/preferences
```

获取程序性/偏好规则列表，支持多维过滤、关键词搜索与分页。排序：**纠错优先 → 高频强化 → 最近更新**。

> 偏好规则以**置顶强约束**注入对话、**硬约束**工具调用。偏好为 **SQL-only（无向量）**，主存 `user_global:{uid}`。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `scope_key` | string | 否 | - | 完整 Scope Key（若传入无冒号字符串，会同时匹配 `user_global:{val}` 与 `user_id`） |
| `user_id` | string | 否 | - | 用户 ID；仅传 `user_id` 时自动按 `user_global:{user_id}` 推导 |
| `target_context` | string | 否 | - | 绑定上下文：能力域/工具名，或 `general`（跨能力通用约定，永远注入） |
| `is_correction` | bool | 否 | - | 是否为纠错规则 |
| `polarity` | string | 否 | - | 极性：`do`（应当） / `dont`（禁止） |
| `is_active` | bool | 否 | - | 是否启用（软停用规则 `is_active=false`，保留审计） |
| `keyword` | string | 否 | - | 模糊匹配 `preference_rule` / `target_context` / `user_id` / `scope_key` |
| `all_scopes` | bool | 否 | false | 是否返回所有范围 |
| `page` | int | 否 | 1 | 页码 |
| `page_size` | int | 否 | 20 | 每页数量 |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [
            {
                "id": "uuid-string",
                "scope_key": "user_global:12345",
                "user_id": "12345",
                "target_context": "general",
                "preference_rule": "调用生成图片类工具时默认用竖图",
                "polarity": "do",
                "is_correction": true,
                "is_active": true,
                "mention_count": 2,
                "source_episode_id": "uuid-string",
                "created_at": "2026-11-08 10:30:00",
                "updated_at": "2026-11-08 12:00:00",
                "last_applied_at": "2026-11-08 12:30:00"
            }
        ],
        "total": 7,
        "page": 1,
        "page_size": 20
    }
}
```

---

## 22.14 偏好规则详情

```
GET /api/ai/memory/preferences/{pref_id}
```

获取单条偏好规则详情（字段同列表项）。

---

## 22.15 更新偏好规则（人工纠偏）

```
PATCH /api/ai/memory/preferences/{pref_id}
```

人工纠偏：修改规则正文 / 极性 / 绑定上下文 / 启停。**停用为软停用（`is_active=false`）而非删除，保留审计。**

**请求体**:
```json
{
    "preference_rule": "调用 generate_image 时默认使用竖图（9:16）",
    "polarity": "do",
    "target_context": "generate_image",
    "is_active": true
}
```

---

## 22.16 删除偏好规则

```
DELETE /api/ai/memory/preferences/{pref_id}
```

删除单条误抽的偏好规则（SQL-only）。

---

## 22.17 故事线（Threads）浏览与详情

### 22.17.1 故事线列表

```
GET /api/ai/memory/threads
```

分页获取 `AIMemThread` 故事线列表，按 `last_at` 倒序排列。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `group_id` | string | 否 | - | 群组 ID |
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 `group_id`） |
| `status` | string | 否 | - | 状态过滤（如 `active` / `archived`） |
| `page` | int | 否 | 1 | 页码 |
| `page_size` | int | 否 | 20 | 每页数量 |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [
            {
                "id": "uuid-string",
                "scope_key": "group:789012",
                "title": "模拟盘交易计划",
                "aliases": ["A股模拟盘"],
                "summary": "讨论模拟盘建仓与每日收盘总结安排",
                "status": "active",
                "milestone_count": 4,
                "first_at": "2026-11-08 10:00:00",
                "last_at": "2026-11-08 12:00:00"
            }
        ],
        "total": 1,
        "page": 1,
        "page_size": 20
    }
}
```

### 22.17.2 故事线详情

```
GET /api/ai/memory/threads/{thread_id}
```

获取单个故事线详情，包含按时间升序排列的里程碑边（`milestones`）、关联对话片段（`episodes`）与涉及的实体（`entities`）。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "uuid-string",
        "scope_key": "group:789012",
        "title": "模拟盘交易计划",
        "aliases": ["A股模拟盘"],
        "summary": "讨论模拟盘建仓与每日收盘总结安排",
        "status": "active",
        "milestone_count": 4,
        "first_at": "2026-11-08 10:00:00",
        "last_at": "2026-11-08 12:00:00",
        "milestones": [
            {
                "id": "edge-uuid",
                "fact": "创建百万起步默认模拟盘",
                "slot_key": "milestone",
                "source_entity_id": "ent-uuid-1",
                "target_entity_id": "ent-uuid-2",
                "event_at": "2026-11-08 10:05:00",
                "valid_at": "2026-11-08 10:05:00"
            }
        ],
        "episodes": [
            {
                "id": "ep-uuid",
                "content": "对话内容...",
                "valid_at": "2026-11-08 10:05:00",
                "session_id": "sess-1"
            }
        ],
        "entities": [
            {
                "id": "ent-uuid-1",
                "name": "维拉",
                "summary": "群主",
                "entity_type": "person",
                "is_speaker": true
            }
        ]
    }
}
```

---

## 22.18 虚拟分类投影状态与零 LLM 索引对账

### 22.18.1 虚拟分类投影状态

```
GET /api/ai/memory/hiergraph/status
```

获取指定范围的虚拟分类投影统计（兼容原 `/hiergraph/status` 路径）。

**Query 参数**: `group_id` 或 `scope_key`（均可选，不传则统计全局）。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "enabled": true,
        "mode": "virtual_projection_v2",
        "scope_key": "group:789012",
        "initialized": true,
        "l1_count": 4,
        "thread_count": 6,
        "active_edges": 120,
        "max_layer": 1,
        "last_rebuild_at": null,
        "entity_count_at_last_rebuild": 0,
        "current_entity_count": 120,
        "group_summary_cache": null,
        "group_summary_updated_at": null
    }
}
```

### 22.18.2 零 LLM 索引对账（FTS5 + Qdrant 补偿）

```
POST /api/ai/memory/hiergraph/rebuild
```

不调用 LLM，对指定 `scope_key`（或 `group_id`）执行：
1. 重建 SQLite `aimemepisode_fts` 全文检索索引；
2. 检查并补齐 Qdrant 中缺失的 Episode / Entity / Edge 向量。

**Query 参数**: 必须提供 `group_id` 或 `scope_key` 之一。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "scope_key": "group:789012",
        "fts_rows": 150,
        "vectors_repaired": 3,
        "elapsed_ms": 42.15
    }
}
```

---

## 22.19 记忆统计

```
GET /api/ai/memory/stats
```

获取指定 scope 或全局的各类记忆节点数量、故事线数与四态边状态分布统计。

**Query 参数**: `group_id`（可选）、`scope_key`（可选，优先级高于 `group_id`）。不传则返回全局统计。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "scope_key": null,
        "episode_count": 150,
        "entity_count": 80,
        "speaker_entity_count": 25,
        "edge_count": 120,
        "active_edge_count": 100,
        "category_count": 0,
        "thread_count": 12,
        "edge_state_counts": {
            "active": 95,
            "staging": 5,
            "superseded": 18,
            "contradicted": 2
        },
        "preference_count": 7,
        "preference_correction_count": 4,
        "observation_queue_size": 0,
        "scope_keys": ["group:789012", "user_global:12345"]
    }
}
```

---

## 22.20 获取与更新记忆配置

### 22.20.1 获取记忆配置

```
GET /api/ai/memory/config
```

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "observer_enabled": true,
        "observer_blacklist": [],
        "ingestion_enabled": true,
        "batch_interval_seconds": 7200,
        "batch_max_size": 80,
        "llm_semaphore_limit": 3,
        "idle_flush_seconds": 180,
        "enable_retrieval": true,
        "enable_user_global_memory": true,
        "enable_heartbeat_memory": true,
        "retrieval_top_k": 10,
        "memory_inject_max_chars": 3200,
        "memory_inject_wide_chars": 48000,
        "session_gap_seconds": 1800,
        "fact_max_inject": 18,
        "dedup_similarity_threshold": 0.92,
        "enable_preference_memory": true,
        "preference_max_inject": 12,
        "preference_max_per_context": 5,
        "preference_inject_budget_ratio": 0.1,
        "preference_immediate_flush": true
    }
}
```

### 22.20.2 更新运行时记忆配置

```
PUT /api/ai/memory/config
```

更新运行时内存配置（重启后恢复配置文件默认值；持久化配置请在 WebConsole「GsCore AI 记忆配置」修改）。

**可更新字段**:
- `observer_enabled` (bool)
- `observer_blacklist` (string[])
- `ingestion_enabled` (bool)
- `batch_interval_seconds` (int, 60–86400)
- `batch_max_size` (int, 5–100)
- `llm_semaphore_limit` (int, 1–10)
- `idle_flush_seconds` (int, 0–86400)
- `enable_retrieval` (bool)
- `enable_user_global_memory` (bool)
- `enable_heartbeat_memory` (bool)
- `retrieval_top_k` (int, 1–50)
- `dedup_similarity_threshold` (float, 0.5–1.0)

---

## 22.21 Scope 列表

```
GET /api/ai/memory/scopes
```

获取所有存在记忆数据的 Scope Key 列表及各表统计。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "scope_key": "group:789012",
            "scope_type": "group",
            "scope_id": "789012",
            "episode_count": 50,
            "entity_count": 30,
            "edge_count": 40,
            "category_count": 0,
            "thread_count": 4,
            "preference_count": 0
        },
        {
            "scope_key": "user_global:12345",
            "scope_type": "user_global",
            "scope_id": "12345",
            "episode_count": 20,
            "entity_count": 15,
            "edge_count": 10,
            "category_count": 0,
            "thread_count": 2,
            "preference_count": 3
        }
    ]
}
```

---

## 22.22 清理与批量删除记忆

以下接口会同步清理 SQL 四表 + 偏好、SQLite FTS5 索引以及 Qdrant 四个集合（`memory_episodes` / `memory_entities` / `memory_edges` / `memory_threads`）。

### 22.22.1 删除单个 Scope 记忆

```
DELETE /api/ai/memory/scopes/{scope_key}
```

### 22.22.2 高级批量清空（精确或前缀匹配）

```
POST /api/ai/memory/clear
```

**请求体**:
```json
{
    "scope_key": "group:789012",
    "scope_pattern": null,
    "dry_run": false
}
```

### 22.22.3 清空群记忆

```
DELETE /api/ai/memory/groups/{group_id}/clear?include_user_in_group=true&dry_run=false
```

### 22.22.4 清空用户全局记忆

```
DELETE /api/ai/memory/users/{user_id}/global/clear?dry_run=false
```

**删除响应结构**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "affected_scope_keys": ["group:789012"],
        "deleted_episodes": 50,
        "deleted_entities": 30,
        "deleted_edges": 40,
        "deleted_threads": 4,
        "deleted_categories": 0,
        "deleted_preferences": 0
    }
}
```

---

## 22.23 本地评测与批量回灌端点（`GSUID_LOCAL_TEST_MODE=1`）

以下端点默认返回 404，仅在环境变量 `GSUID_LOCAL_TEST_MODE=1` 开启（且校验 `X-Local-Test-Token`）时可用，专供离线评测基准（LongMemEval / BEAM / CorpusQA / LOFT）调用：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/ai/memory/batch_observe` | 批量回灌历史对话（支持 granular Episode 切块嵌入、窗口化 Pass 1/2 抽取与索引对账） |
| POST | `/api/ai/memory/eval/retrieve` | 评测检索分段（只跑 `dual_route_retrieve`，不调答题 LLM） |
| POST | `/api/ai/memory/eval/episodes` | 列出 user turn Episode（供 oracle 对齐） |
| POST | `/api/ai/memory/eval/episode_vectors` | 按 Episode ID 批量读取 Qdrant dense 向量 |
| POST | `/api/ai/memory/eval/embed_texts` | 用当前记忆 dense 模型批量嵌入文本 |
| POST | `/api/ai/memory/eval/extract_aspects` | 触发睡眠期故事线补抽（`extract_aspects_for_scope`） |

---

## API 总览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/ai/memory/search` | 五模态记忆检索 |
| GET | `/api/ai/memory/episodes` | Episode 列表 |
| GET | `/api/ai/memory/episodes/{episode_id}` | Episode 详情 |
| DELETE | `/api/ai/memory/episodes/{episode_id}` | 删除 Episode |
| GET | `/api/ai/memory/entities` | Entity 列表 |
| GET | `/api/ai/memory/entities/{entity_id}` | Entity 详情 |
| DELETE | `/api/ai/memory/entities/{entity_id}` | 删除 Entity |
| GET | `/api/ai/memory/edges` | Edge 四态事实边列表（支持 `include_history`） |
| GET | `/api/ai/memory/edges/{edge_id}` | Edge 详情 |
| DELETE | `/api/ai/memory/edges/{edge_id}` | 删除 Edge |
| GET | `/api/ai/memory/categories` | 记忆虚拟分类投影（L0 故事线 + L1 四大原语） |
| GET | `/api/ai/memory/categories/{category_id}` | 虚拟分类详情 |
| GET | `/api/ai/memory/threads` | 故事线（Thread）列表 |
| GET | `/api/ai/memory/threads/{thread_id}` | 故事线详情（含里程碑、片段与实体） |
| GET | `/api/ai/memory/preferences` | 偏好规则列表 |
| GET | `/api/ai/memory/preferences/{pref_id}` | 偏好规则详情 |
| PATCH | `/api/ai/memory/preferences/{pref_id}` | 更新偏好规则（人工纠偏） |
| DELETE | `/api/ai/memory/preferences/{pref_id}` | 删除偏好规则 |
| GET | `/api/ai/memory/hiergraph/status` | 虚拟分类投影状态 |
| POST | `/api/ai/memory/hiergraph/rebuild` | 零 LLM 索引对账（FTS5 + Qdrant 向量补偿） |
| GET | `/api/ai/memory/stats` | 记忆统计（含 `thread_count` 与 `edge_state_counts`） |
| GET | `/api/ai/memory/config` | 获取记忆配置 |
| PUT | `/api/ai/memory/config` | 更新运行时记忆配置 |
| GET | `/api/ai/memory/scopes` | Scope 列表 |
| DELETE | `/api/ai/memory/scopes/{scope_key}` | 删除 Scope 记忆 |
| POST | `/api/ai/memory/clear` | 清空记忆（高级批量删除） |
| DELETE | `/api/ai/memory/groups/{group_id}/clear` | 清空群全部记忆 |
| DELETE | `/api/ai/memory/users/{user_id}/global/clear` | 清空用户全局记忆 |
