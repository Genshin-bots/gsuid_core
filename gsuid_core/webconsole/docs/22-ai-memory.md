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
| **Preference** | 程序性/偏好规则（纠正 Agent 未来行为，如"调 `generate_image` 用竖图"），**硬约束**工具调用 | DB（SQL-only，无向量） |

> **Preference 与上面 4 类的区别**：Episode/Entity/Edge/Category 是**陈述性记忆**（用户是谁、说过什么），并构成向量+图谱双路检索；**Preference 是程序性记忆**（该怎样为这个用户调工具/排版/选参数），SQL-only 扁平规则、不构成图谱、以**置顶强约束**注入。详见实现文档 [`docs/PROCEDURAL_PREFERENCE_AND_RFMEM_IMPLEMENTATION_20260614.md`](../../../docs/PROCEDURAL_PREFERENCE_AND_RFMEM_IMPLEMENTATION_20260614.md)。

### Scope Key 隔离体系

所有记忆节点均携带 `scope_key` 字段实现群组间严格隔离：

| Scope 类型 | 格式 | 说明 |
|------------|------|------|
| `group` | `group:789012` | 群组级记忆 |
| `user_global` | `user_global:12345` | 用户跨群全局画像 |
| `user_in_group` | `user_in_group:12345@789012` | 用户在特定群组内的局部档案 |

### 记忆质量增强机制

记忆系统在提取、写入、检索三个阶段引入了一组质量增强机制：

| 机制 | 阶段 | 说明 |
|------|------|------|
| **信息完整性检查** | 提取 | 提取提示词要求每条事实必须是「带明确主语的完整句子」且「含结论/具体信息」，丢弃缺主语的短语（"建议关注X"）和只有行为无结论的流水账（"询问了X"） |
| **事实主语补全** | 检索 | 格式化注入文本时，用边的 source 实体名称给历史数据中缺主语的事实补全主语（如"建议关注X"→"用户444835641建议关注X"） |
| **别名识别与重定向** | 提取 + 写入 | 识别"X就是Y"等别名声明，把外号（如"大班尼特"="妮可"）的所有关系边重定向到正式实体，避免同一事物的记忆被分散 |
| **群组画像** | 写入 | 自动维护每个群组的语境标签（主要话题）与词汇映射表（群内特有别名），存于通用持久状态存储 |
| **别名展开检索** | 检索 | query 中出现群内别名时，自动附加正式名称以提升召回 |
| **Token 预算格式化** | 检索 | 注入对话的记忆文本按"核心事实 55% / 语义类目 15% / 相关对话 30%"预算配分，保证高信息密度 |

群组画像还会以【当前群聊语境】文本注入对话上下文，让 AI 直接知道某个词在本群指什么。详见 [`docs/AI_AGENT_CAPABILITY_UPGRADE.md`](../../../docs/AI_AGENT_CAPABILITY_UPGRADE.md)。

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
            "s1_episodes": 8,
            "s2_episodes": 5,
            "scope_keys": ["group:789012"]
        },
        "prompt_text": "【用户偏好/纠错 - 须严格遵守】\n• [do] 调用生成图片类工具时默认用竖图\n\n【已知事实】\n• 实体间关系描述\n\n【历史对话片段】\n[2024-01-15] 对话片段内容..."
    }
}
```

> `preferences` 为**本次检索会命中并注入的程序性/偏好硬约束**（纠错规则与 `general` 通用规则永远注入，软偏好按本轮能力域过滤）。控制台据此排查"为什么 Agent 还调错工具"。规则治理见 [22.13–22.16](#2213-偏好规则列表)。该字段在 `enable_preference_memory` 关闭时为空数组。

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

## 22.13 偏好规则列表

```
GET /api/ai/memory/preferences
```

获取程序性/偏好规则列表，支持多维过滤与分页。排序：**纠错优先 → 高频强化 → 最近更新**。

> 偏好规则会以**置顶强约束**注入对话、**硬约束**工具调用。误抽的规则会持续误导 Agent，故人工治理（核对/编辑/停用/删除）比 Episode 更关键。偏好为 **SQL-only（无向量）**，所有治理操作只动数据库。

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `scope_key` | string | 否 | - | 完整 Scope Key（偏好主存 `user_global:{uid}`，跨群随用户） |
| `user_id` | string | 否 | - | 用户 ID；仅传 `user_id` 时自动按 `user_global:{user_id}` 推导 scope_key |
| `target_context` | string | 否 | - | 绑定上下文：能力域/工具名，或 `general`（跨能力通用约定，永远注入） |
| `is_correction` | bool | 否 | - | 是否为纠错规则（纠错类受保护、衰减更慢） |
| `polarity` | string | 否 | - | 极性：`do`（应当） / `dont`（禁止） |
| `is_active` | bool | 否 | - | 是否启用（软停用规则 `is_active=false`，保留审计） |
| `all_scopes` | bool | 否 | false | 是否返回所有范围（忽略 scope_key/user_id 推导） |
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
                "created_at": "2026-06-14 10:30:00",
                "updated_at": "2026-06-14 12:00:00",
                "last_applied_at": "2026-06-14 12:30:00"
            }
        ],
        "total": 7,
        "page": 1,
        "page_size": 20
    }
}
```

| 字段 | 说明 |
|------|------|
| `target_context` | 规则适用范围：具体能力域/工具名，或 `general`（风格/时区/单位/语言等横切约定，对所有相关请求生效） |
| `polarity` | `do` = 应当这样做；`dont` = 禁止这样做 |
| `is_correction` | 是否由用户纠正意图触发（纠错规则注入优先级最高、生命周期受保护） |
| `mention_count` | 同一规则被重复强化的次数（越高越稳定） |
| `source_episode_id` | 溯源：蒸馏出该规则的 Episode ID |
| `last_applied_at` | 最近一次被检索命中并注入的时间（命中后台异步刷新） |

---

## 22.14 偏好规则详情

```
GET /api/ai/memory/preferences/{pref_id}
```

获取单条偏好规则详情（字段同列表项，含溯源 `source_episode_id`、强化计数 `mention_count`、最近应用时间 `last_applied_at`）。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `pref_id` | string | 偏好规则 ID |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "uuid-string",
        "scope_key": "user_global:12345",
        "user_id": "12345",
        "target_context": "generate_image",
        "preference_rule": "调用 generate_image 时默认使用竖图（9:16）",
        "polarity": "do",
        "is_correction": true,
        "is_active": true,
        "mention_count": 1,
        "source_episode_id": "uuid-string",
        "created_at": "2026-06-14 10:30:00",
        "updated_at": "2026-06-14 10:30:00",
        "last_applied_at": null
    }
}
```

---

## 22.15 更新偏好规则（人工纠偏）

```
PATCH /api/ai/memory/preferences/{pref_id}
```

人工纠偏：修改规则正文 / 极性 / 绑定上下文 / 启停。**停用为软停用（`is_active=false`）而非删除，保留审计。** 仅更新请求中提供的字段。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `pref_id` | string | 偏好规则 ID |

**请求体**:
```json
{
    "preference_rule": "调用 generate_image 时默认使用竖图（9:16）",
    "polarity": "do",
    "target_context": "generate_image",
    "is_active": true
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `preference_rule` | string | 否 | 规则正文，1-2000 字符 |
| `polarity` | string | 否 | 极性 `do` / `dont`（非 `dont` 一律归一为 `do`） |
| `target_context` | string | 否 | 绑定上下文（≤128 字符；为空时归为 `general`） |
| `is_active` | bool | 否 | 启用 / 软停用 |

> 至少提供一个可修改字段；任一字段变更都会刷新 `updated_at`。

**响应**: 返回更新后的完整偏好规则对象（结构同 §22.14 详情）。

---

## 22.16 删除偏好规则

```
DELETE /api/ai/memory/preferences/{pref_id}
```

删除单条误抽的偏好规则。偏好为 SQL-only，无向量需清理。

> 提示：误抽规则可优先用 §22.15 软停用（`is_active=false`）保留审计；确认无用再删除。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `pref_id` | string | 偏好规则 ID |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": null
}
```

---

## 22.17 分层语义图状态

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

## 22.18 记忆统计

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
        "preference_count": 7,
        "preference_correction_count": 4,
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
| `preference_count` | 活跃（`is_active=true`）程序性/偏好规则数量 |
| `preference_correction_count` | 其中由纠错触发（`is_correction=true`）的规则数量 |
| `observation_queue_size` | 当前观察队列中待处理的消息数量 |
| `scope_keys` | 所有有记忆数据的 scope_key 列表 |

---

## 22.19 获取记忆配置

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

#### 程序性 / 偏好记忆配置（默认开）

> 自 2026-06-14 起，下列字段经 `MEMORY_CONFIG`（`@property` 转发）管理：在 WebConsole 的
> **"GsCore AI 记忆配置 → 程序性/偏好记忆设置"** 分组调整并**持久化**到 `data/ai_core/memory_config.json`。
> 本 `/api/ai/memory/config` 端点仅**只读反射**其当前值（GET 返回），**不能**经本端点的 PUT 修改。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_preference_memory` | bool | `true` | 程序性/偏好记忆总开关（**默认开**）；关闭后写入/蒸馏/注入/即时 flush 全部停用 |
| `preference_max_inject` | int | `12` | 单次回复注入偏好规则条数上限 |
| `preference_max_per_context` | int | `5` | 单能力域(`target_context`)保留的活跃规则上限（生命周期裁剪用） |
| `preference_inject_budget_ratio` | float | `0.10` | 偏好区块占记忆注入字符预算的比例 |
| `preference_immediate_flush` | bool | `true` | 纠错命中即时 flush（带去抖；受总开关前置） |

#### RF-Mem 双过程检索配置（进阶，默认关）

> 同上，经 **"RF-Mem 双过程检索设置(进阶)"** 分组持久化调整；本端点只读反射。
> 阈值（`familiarity_theta_*` / `tau`）须按嵌入模型标定后再放量，回忆环额外要求 `qdrant_provider=remote`。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_familiarity_routing` | bool | `false` | 探针熟悉度路由总开关（关时不发探针、行为不变） |
| `enable_recollection_path` | bool | `false` | 回忆环开关（仅 `qdrant_provider=remote` + 路由判低熟悉 + System-2 未触发时生效） |
| `familiarity_theta_high` | float | `0.6` | 熟悉度上阈 θ_high（余弦语义，需标定） |
| `familiarity_theta_low` | float | `0.3` | 熟悉度下阈 θ_low（需标定） |
| `familiarity_tau` | float | `0.22` | 列表熵阈 τ（中段由熵裁决） |
| `familiarity_lambda` | float | `20.0` | 温度 softmax 锐度 λ（不敏感） |
| `familiarity_probe_k` | int | `15` | 探针候选数 |

---

## 22.20 更新记忆配置

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

## 22.21 Scope 列表

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

## 22.22 删除 Scope 记忆

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

## 22.23 清空记忆（高级批量删除）

```
POST /api/ai/memory/clear
```

清空指定 Scope 下的所有记忆数据，支持精确匹配或前缀模糊匹配。

### Scope Key 匹配模式

| 模式 | 说明 |
|------|------|
| **精确匹配** | `scope_key = "group:789012"` → 仅匹配 `"group:789012"` |
| **前缀模糊匹配** | `scope_pattern = "group:789012"` → 匹配 `"group:789012"` 以及 `"group:789012@..."` |

> 前缀模糊匹配适用于 `user_in_group` 类型 scope（如 `user_in_group:12345@789012`），它的前半部分与 `group:789012` 无关，因此前缀模式只能匹配 `"group:"` 开头、可能带 `@` 后缀的 scope_key。

**请求体**:
```json
{
    "scope_key": "group:789012",
    "scope_pattern": null,
    "dry_run": false
}
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `scope_key` | string | 否 | null | 精确匹配的 Scope Key |
| `scope_pattern` | string | 否 | null | 前缀匹配的 Scope Key |
| `dry_run` | bool | 否 | false | 为 true 时仅统计数量，不实际删除 |

> `scope_key` 和 `scope_pattern` 至少提供一个，同时提供时以 `scope_key` 为准（精确优先）。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "affected_scope_keys": ["group:789012"],
        "deleted_episodes": 50,
        "deleted_entities": 30,
        "deleted_edges": 40,
        "deleted_categories": 10
    }
}
```

---

## 22.24 清空群记忆

```
DELETE /api/ai/memory/groups/{group_id}/clear?include_user_in_group=true&dry_run=false
```

清空某个群的全部记忆，包括：

1. `group:{group_id}` 下的所有记忆（Episode/Entity/Edge/Category）
2. `user_in_group:*@{group_id}` 下所有用户的群内记忆档案（当 `include_user_in_group=true` 时）

同时删除数据库记录和 Qdrant 中的向量。

> ⚠️ **此操作不可逆，请谨慎使用！**

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `group_id` | string | 群组 ID |

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `include_user_in_group` | bool | 否 | true | 是否同时清空该群内所有用户的 user_in_group 记忆档案 |
| `dry_run` | bool | 否 | false | 为 true 时仅统计数量，不实际删除 |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "group_id": "789012",
        "affected_scope_keys": [
            "group:789012",
            "user_in_group:11111@789012",
            "user_in_group:22222@789012"
        ],
        "deleted_episodes": 50,
        "deleted_entities": 30,
        "deleted_edges": 40,
        "deleted_categories": 10
    }
}
```

---

## 22.25 清空用户全局记忆

```
DELETE /api/ai/memory/users/{user_id}/global/clear?dry_run=false
```

清空某个用户的跨群全局记忆画像（`user_global:{user_id}` 下的所有记忆数据）。

> ⚠️ **此操作不可逆，请谨慎使用！**

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | string | 用户 ID |

**Query 参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `dry_run` | bool | 否 | false | 为 true 时仅统计数量，不实际删除 |

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "affected_scope_keys": ["user_global:12345"],
        "deleted_episodes": 20,
        "deleted_entities": 15,
        "deleted_edges": 10,
        "deleted_categories": 5
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
| GET | `/api/ai/memory/preferences` | 偏好规则列表 |
| GET | `/api/ai/memory/preferences/{pref_id}` | 偏好规则详情 |
| PATCH | `/api/ai/memory/preferences/{pref_id}` | 更新偏好规则（人工纠偏） |
| DELETE | `/api/ai/memory/preferences/{pref_id}` | 删除偏好规则 |
| GET | `/api/ai/memory/hiergraph/status` | 分层语义图状态 |
| POST | `/api/ai/memory/hiergraph/rebuild` | 手动触发分层图重建 |
| GET | `/api/ai/memory/stats` | 记忆统计 |
| GET | `/api/ai/memory/config` | 获取记忆配置 |
| PUT | `/api/ai/memory/config` | 更新记忆配置 |
| GET | `/api/ai/memory/scopes` | Scope 列表 |
| DELETE | `/api/ai/memory/scopes/{scope_key}` | 删除 Scope 记忆 |
| POST | `/api/ai/memory/clear` | 清空记忆（高级批量删除） |
| DELETE | `/api/ai/memory/groups/{group_id}/clear` | 清空群全部记忆 |
| DELETE | `/api/ai/memory/users/{user_id}/global/clear` | 清空用户全局记忆 |
