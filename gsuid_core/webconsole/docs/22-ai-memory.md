# 22. AI Memory API - /api/ai/memory

提供 AI 记忆系统（Mnemis 双路检索）的完整管理接口，包括记忆检索、Episode/Entity/Edge 浏览、分层语义图查看、配置管理、统计信息等。

## 系统概述

记忆系统基于 **Mnemis 双路检索** 思想，采用 **向量数据库（Qdrant）+ 关系数据库（SQLAlchemy）** 双路存储架构：

- **System-1**：向量相似度检索，快速召回语义相关的 Episode/Entity/Edge
- **System-2**：分层语义图遍历，从顶层 Category 自顶向下导航，找到相关 Entity

两条路径并行执行，合并去重后经 Reranker 重排序输出。

### 核心数据模型

| 模型 | 说明 | 存储层 |
|------|------|--------|
| **Episode** | 原始对话片段（Base Graph 第一层） | DB + Qdrant |
| **Entity** | 提取的实体节点（Base Graph 第二层） | DB + Qdrant |
| **Edge** | 实体间关系/事实（Base Graph 第三层） | DB + Qdrant |
| **Category** | 分层语义图节点（Hierarchical Graph） | DB |
| **CategoryEdge** | Category 间层次关联 | DB |

### Scope Key 隔离体系

所有记忆节点均携带 `scope_key` 字段实现群组间严格隔离：

| Scope 类型 | 格式 | 说明 |
|------------|------|------|
| `group` | `group:789012` | 群组级记忆 |
| `user_global` | `user_global:12345` | 用户跨群全局画像 |
| `user_in_group` | `user_in_group:12345@789012` | 用户在特定群组内的局部档案 |

---

## 22.1 记忆双路检索

```
POST /api/ai/memory/search
```

并行执行 System-1（向量相似度）和 System-2（分层图遍历），合并去重后经 Reranker 重排序，返回最终的 MemoryContext。

**请求体**:
```json
{
    "query": "用户查询文本",
    "group_id": "789012",
    "user_id": "12345",
    "top_k": 10,
    "enable_system2": true,
    "enable_user_global": false
}
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | 是 | - | 查询文本，1-2000 字符 |
| `group_id` | string | 是 | - | 群组 ID |
| `user_id` | string | 否 | null | 用户 ID，用于联合用户全局记忆 |
| `top_k` | int | 否 | 10 | 返回结果数量上限，1-50 |
| `enable_system2` | bool | 否 | true | 是否启用 System-2 分层图遍历 |
| `enable_user_global` | bool | 否 | false | 是否联合查询用户跨群画像 |

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
                "valid_at": "2024-01-15 10:30:00",
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
                "fact": "实体间关系描述",
                "valid_at": "2024-01-15 10:30:00",
                "score": 0.85
            }
        ],
        "retrieval_meta": {
            "s1_episodes": 8,
            "s2_episodes": 5,
            "scope_keys": ["group:789012"]
        },
        "prompt_text": "【已知事实】\n• 实体间关系描述\n\n【历史对话片段】\n[2024-01-15] 对话片段内容..."
    }
}
```

---

## 22.2 Episode 列表

```
GET /api/ai/memory/episodes
```

获取 Episode（对话片段）列表，支持按群组过滤和分页。设置 `all_scopes=true` 可返回所有范围的 Episode。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `group_id` | string | 否 | - | 群组 ID（优先级低于 scope_key） |
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 group_id） |
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
                "valid_at": "2024-01-15 10:30:00",
                "created_at": "2024-01-15 10:30:00",
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
        "valid_at": "2024-01-15 10:30:00",
        "created_at": "2024-01-15 10:30:00",
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

删除单个 Episode，同时删除数据库记录和 Qdrant 中的向量。

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
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 group_id） |
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
                "created_at": "2024-01-15 10:30:00",
                "updated_at": "2024-01-15 12:00:00"
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

获取单个 Entity 详情，包含关联的 Episode 和 Edge 信息。

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
        "created_at": "2024-01-15 10:30:00",
        "updated_at": "2024-01-15 12:00:00",
        "qdrant_id": "uuid-string",
        "episodes": [
            {
                "id": "uuid-string",
                "content": "对话片段内容（前200字）...",
                "valid_at": "2024-01-15 10:30:00"
            }
        ],
        "edges": [
            {
                "id": "uuid-string",
                "fact": "关系描述",
                "target_entity_id": "uuid-string",
                "valid_at": "2024-01-15 10:30:00",
                "direction": "outgoing"
            },
            {
                "id": "uuid-string",
                "fact": "关系描述",
                "source_entity_id": "uuid-string",
                "valid_at": "2024-01-15 10:30:00",
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

删除单个 Entity，同时删除数据库记录和 Qdrant 中的向量。

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

获取 Edge（实体关系）列表，支持按群组、关联 Entity 过滤。设置 `all_scopes=true` 可返回所有范围的 Edge。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `group_id` | string | 否 | - | 群组 ID |
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 group_id） |
| `all_scopes` | bool | 否 | false | 是否返回所有范围的 Edge |
| `entity_id` | string | 否 | - | 关联的 Entity ID（返回该 Entity 的出边和入边） |
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
                "valid_at": "2024-01-15 10:30:00",
                "invalid_at": null,
                "created_at": "2024-01-15 10:30:00"
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

获取单个 Edge 详情，包含源 Entity 和目标 Entity 信息。

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
        "valid_at": "2024-01-15 10:30:00",
        "invalid_at": null,
        "created_at": "2024-01-15 10:30:00",
        "qdrant_id": "uuid-string",
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

删除单个 Edge，同时删除数据库记录和 Qdrant 中的向量。

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

## 22.11 Category 列表

```
GET /api/ai/memory/categories
```

获取 Category（分层语义图节点）列表，支持按群组、层级过滤。设置 `all_scopes=true` 可返回所有范围的 Category。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `group_id` | string | 否 | - | 群组 ID |
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 group_id） |
| `all_scopes` | bool | 否 | false | 是否返回所有范围的 Category |
| `layer` | int | 否 | - | 层级编号（1=最具体，越大越抽象） |
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
                "name": "分类名称",
                "summary": "分类摘要",
                "tag": ["标签1"],
                "layer": 1,
                "parent_id": null,
                "child_categories_count": 3,
                "member_entities_count": 8,
                "created_at": "2024-01-15 10:30:00",
                "updated_at": "2024-01-15 12:00:00"
            }
        ],
        "total": 25,
        "page": 1,
        "page_size": 20
    }
}
```

---

## 22.12 Category 详情

```
GET /api/ai/memory/categories/{category_id}
```

获取单个 Category 详情，包含子 Category 列表和成员 Entity 列表。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `category_id` | string | Category ID |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "uuid-string",
        "scope_key": "group:789012",
        "name": "分类名称",
        "summary": "分类摘要",
        "tag": ["标签1"],
        "layer": 2,
        "parent_id": "parent-uuid-string",
        "created_at": "2024-01-15 10:30:00",
        "updated_at": "2024-01-15 12:00:00",
        "parent_categories": [
            {
                "id": "uuid-string",
                "name": "父分类名称",
                "layer": 3
            }
        ],
        "child_categories": [
            {
                "id": "uuid-string",
                "name": "子分类名称",
                "layer": 1
            }
        ],
        "member_entities": [
            {
                "id": "uuid-string",
                "name": "实体名称",
                "summary": "实体摘要",
                "is_speaker": false
            }
        ]
    }
}
```

---

## 22.13 分层语义图状态

```
GET /api/ai/memory/hiergraph/status
```

获取分层语义图构建状态，包括最大层级、上次重建时间等。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `group_id` | string | 否 | - | 群组 ID |
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 group_id） |

> 至少需要提供 `group_id` 或 `scope_key` 之一。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "scope_key": "group:789012",
        "initialized": true,
        "max_layer": 3,
        "last_rebuild_at": "2024-01-15 10:30:00",
        "entity_count_at_last_rebuild": 50,
        "current_entity_count": 65,
        "group_summary_cache": "该群组主要讨论技术话题...",
        "group_summary_updated_at": "2024-01-15 12:00:00"
    }
}
```

---

## 22.14 记忆统计

```
GET /api/ai/memory/stats
```

获取记忆系统统计数据，返回指定 scope 或全局的各类记忆节点数量统计。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `group_id` | string | 否 | - | 群组 ID |
| `scope_key` | string | 否 | - | 完整的 Scope Key（优先级高于 group_id） |

> 不提供参数时返回全局统计。

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
        "category_count": 30,
        "observation_queue_size": 5,
        "scope_keys": ["group:789012", "group:345678", "user_global:12345"]
    }
}
```

| 字段 | 说明 |
|------|------|
| `episode_count` | Episode 总数 |
| `entity_count` | Entity 总数 |
| `speaker_entity_count` | 说话者 Entity 数量 |
| `edge_count` | Edge 总数（含已失效） |
| `active_edge_count` | 有效 Edge 数量（invalid_at 为空） |
| `category_count` | Category 总数 |
| `observation_queue_size` | 当前观察队列中待处理的消息数量 |
| `scope_keys` | 所有有记忆数据的 scope_key 列表 |

---

## 22.15 获取记忆配置

```
GET /api/ai/memory/config
```

获取记忆系统当前配置。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "observer_enabled": true,
        "observer_blacklist": [],
        "ingestion_enabled": true,
        "batch_interval_seconds": 1800,
        "batch_max_size": 30,
        "llm_semaphore_limit": 2,
        "enable_retrieval": true,
        "enable_system2": true,
        "enable_user_global_memory": false,
        "enable_heartbeat_memory": true,
        "retrieval_top_k": 10,
        "dedup_similarity_threshold": 0.92,
        "edge_conflict_threshold": 0.88,
        "min_children_per_category": 3,
        "max_layers": 5,
        "hiergraph_rebuild_ratio": 1.10,
        "hiergraph_rebuild_interval_seconds": 86400
    }
}
```

### 配置项说明

#### 观察者配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `observer_enabled` | bool | `true` | 是否启用消息观察者 |
| `observer_blacklist` | string[] | `[]` | 黑名单群组 ID 列表 |

#### 摄入配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `ingestion_enabled` | bool | `true` | 是否启用摄入引擎 |
| `batch_interval_seconds` | int | `1800` | 消息聚合窗口（秒） |
| `batch_max_size` | int | `30` | 单次最大聚合条数 |
| `llm_semaphore_limit` | int | `2` | 同时进行的 LLM 调用上限 |

#### 检索配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_retrieval` | bool | `true` | 是否启用记忆检索 |
| `enable_system2` | bool | `true` | 是否启用 System-2（成本较高） |
| `enable_user_global_memory` | bool | `false` | 是否联合查询用户跨群画像 |
| `enable_heartbeat_memory` | bool | `true` | 是否在 Heartbeat 中注入群组摘要 |
| `retrieval_top_k` | int | `10` | 最终返回的 Episode 数量上限 |

#### 去重与冲突阈值

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `dedup_similarity_threshold` | float | `0.92` | Entity 去重余弦相似度阈值 |
| `edge_conflict_threshold` | float | `0.88` | Edge 语义冲突判断阈值 |

#### 分层图配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `min_children_per_category` | int | `3` | 每个 Category 最少子节点数 |
| `max_layers` | int | `5` | 分层图最大层数 |
| `hiergraph_rebuild_ratio` | float | `1.10` | Entity 增长触发增量重建的比例 |
| `hiergraph_rebuild_interval_seconds` | int | `86400` | 距上次重建触发增量重建的秒数 |

---

## 22.16 更新记忆配置

```
PUT /api/ai/memory/config
```

更新记忆系统配置。仅更新请求中提供的字段，未提供的字段保持不变。配置立即生效，但不会持久化（重启后恢复默认值）。

**请求体**:
```json
{
    "observer_enabled": true,
    "enable_system2": false,
    "retrieval_top_k": 15
}
```

> 只需提供需要修改的字段，未提供的字段保持不变。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "observer_enabled": true,
        "observer_blacklist": [],
        "ingestion_enabled": true,
        "batch_interval_seconds": 1800,
        "batch_max_size": 30,
        "llm_semaphore_limit": 2,
        "enable_retrieval": true,
        "enable_system2": false,
        "enable_user_global_memory": false,
        "enable_heartbeat_memory": true,
        "retrieval_top_k": 15,
        "dedup_similarity_threshold": 0.92,
        "edge_conflict_threshold": 0.88,
        "min_children_per_category": 3,
        "max_layers": 5,
        "hiergraph_rebuild_ratio": 1.10,
        "hiergraph_rebuild_interval_seconds": 86400
    }
}
```

---

## 22.17 Scope 列表

```
GET /api/ai/memory/scopes
```

获取所有有记忆数据的 Scope Key 列表，附带各类节点的数量统计。

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
            "category_count": 10
        },
        {
            "scope_key": "user_global:12345",
            "scope_type": "user_global",
            "scope_id": "12345",
            "episode_count": 20,
            "entity_count": 15,
            "edge_count": 10,
            "category_count": 5
        }
    ]
}
```

---

## 22.18 删除 Scope 记忆

```
DELETE /api/ai/memory/scopes/{scope_key}
```

删除指定 Scope 下的所有记忆数据，包括 Episode、Entity、Edge、Category 及其 Qdrant 向量。

> ⚠️ **此操作不可逆，请谨慎使用！**

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `scope_key` | string | 完整的 Scope Key（如 `group:789012`） |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "scope_key": "group:789012",
        "deleted_episodes": 50,
        "deleted_entities": 30,
        "deleted_edges": 40,
        "deleted_categories": 10
    }
}
```

---

## API 总览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/ai/memory/search` | 记忆双路检索 |
| GET | `/api/ai/memory/episodes` | Episode 列表 |
| GET | `/api/ai/memory/episodes/{episode_id}` | Episode 详情 |
| DELETE | `/api/ai/memory/episodes/{episode_id}` | 删除 Episode |
| GET | `/api/ai/memory/entities` | Entity 列表 |
| GET | `/api/ai/memory/entities/{entity_id}` | Entity 详情 |
| DELETE | `/api/ai/memory/entities/{entity_id}` | 删除 Entity |
| GET | `/api/ai/memory/edges` | Edge 列表 |
| GET | `/api/ai/memory/edges/{edge_id}` | Edge 详情 |
| DELETE | `/api/ai/memory/edges/{edge_id}` | 删除 Edge |
| GET | `/api/ai/memory/categories` | Category 列表 |
| GET | `/api/ai/memory/categories/{category_id}` | Category 详情 |
| GET | `/api/ai/memory/hiergraph/status` | 分层语义图状态 |
| GET | `/api/ai/memory/stats` | 记忆统计 |
| GET | `/api/ai/memory/config` | 获取记忆配置 |
| PUT | `/api/ai/memory/config` | 更新记忆配置 |
| GET | `/api/ai/memory/scopes` | Scope 列表 |
| DELETE | `/api/ai/memory/scopes/{scope_key}` | 删除 Scope 记忆 |
