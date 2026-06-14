# 16. AI Knowledge Base API - /api/ai/knowledge

> 知识库 API 用于管理手动添加的知识库条目。通过此接口添加的知识不会在框架启动时被插件同步流程检查、修改或删除。

## 16.1 获取知识库列表（分页）

```
GET /api/ai/knowledge/list
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| offset | integer | 否 | 0 | 起始偏移量（会被page参数覆盖） |
| limit | integer | 否 | 20 | 每页数量 |
| source | string | 否 | all | 来源过滤，"all"表示所有知识，"plugin"只查插件添加的，"manual"只查手动添加的 |
| page | integer | 否 | 1 | 页码，从1开始，例如page=2表示第二页（offset=20） |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "list": [
            {
                "id": "manual_001",
                "plugin": "manual",
                "title": "手动添加的知识",
                "content": "这是手动添加的知识内容...",
                "tags": ["手动", "自定义"],
                "source": "manual"
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 20,
        "next_offset": null
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功 |
| msg | string | 状态信息 |
| data.list | array | 知识列表 |
| data.list[].id | string | 知识 ID |
| data.list[].plugin | string | 所属插件/来源 |
| data.list[].title | string | 知识标题 |
| data.list[].content | string | 知识内容 |
| data.list[].tags | array | 知识标签 |
| data.list[].source | string | 来源标识，"manual"表示手动添加 |
| data.total | integer | 知识总数 |
| data.offset | integer | 当前偏移量 |
| data.limit | integer | 每页数量 |
| data.next_offset | integer/null | 下一页偏移量，null表示没有更多 |
| data.page | integer | 当前页码 |
| data.page_size | integer | 每页数量 |

---

## 16.2 获取知识详情

```
GET /api/ai/knowledge/{entity_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| entity_id | string | 知识 ID |

**响应（知识存在）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "manual_001",
        "plugin": "manual",
        "title": "手动添加的知识",
        "content": "这是手动添加的知识内容...",
        "tags": ["手动", "自定义"],
        "source": "manual"
    }
}
```

**错误响应（知识不存在）**：
```json
{
    "status": 1,
    "msg": "Knowledge 'manual_001' not found",
    "data": null
}
```

---

## 16.3 新增知识

```
POST /api/ai/knowledge
```

**请求头**：
```
Authorization: Bearer <token>
```

**请求体**：
```json
{
    "plugin": "manual",
    "title": "手动添加的知识",
    "content": "这是手动添加的知识内容...",
    "tags": ["手动", "自定义"]
}
```

**请求字段说明**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| plugin | string | 否 | 所属插件，默认"manual" |
| title | string | 是 | 知识标题 |
| content | string | 是 | 知识内容 |
| tags | array | 是 | 知识标签列表 |

> 注意：id 由后端自动生成，无需传入。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "manual_001",
        "title": "手动添加的知识"
    }
}
```

**错误响应（添加失败）**：
```json
{
    "status": 1,
    "msg": "Failed to add knowledge to database",
    "data": null
}
```

---

## 16.4 更新知识

```
PUT /api/ai/knowledge/{entity_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| entity_id | string | 知识 ID |

**请求体**：
```json
{
    "title": "更新后的标题",
    "content": "更新后的内容...",
    "tags": ["更新", "标签"]
}
```

> 注意：id 和 source 字段不允许修改，只会更新提供的字段。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "manual_001"
    }
}
```

**错误响应（知识不存在）**：
```json
{
    "status": 1,
    "msg": "Knowledge 'manual_001' not found or update failed",
    "data": null
}
```

---

## 16.5 删除知识

```
DELETE /api/ai/knowledge/{entity_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| entity_id | string | 知识 ID |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "manual_001"
    }
}
```

**错误响应（知识不存在）**：
```json
{
    "status": 1,
    "msg": "Knowledge 'manual_001' not found or delete failed",
    "data": null
}
```

---

## 16.6 搜索知识

```
GET /api/ai/knowledge/search
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 查询文本 |
| limit | integer | 否 | 10 | 返回数量限制 |
| source | string | 否 | all | 来源过滤，"all"表示所有知识，"plugin"只搜插件添加的，"manual"只搜手动添加的 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "results": [
            {
                "id": "manual_001",
                "plugin": "manual",
                "title": "手动添加的知识",
                "content": "这是手动添加的知识内容...",
                "tags": ["手动", "自定义"],
                "source": "manual"
            }
        ],
        "count": 1,
        "query": "关键词"
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.results | array | 匹配的知识列表 |
| data.count | integer | 匹配数量 |
| data.query | string | 查询文本 |

---

## 16.7 批量导入（服务端分片，导入长文/数十万字）

```
POST /api/ai/knowledge/bulk
```

> 用于把长文（如手册、规则、剧本，乃至数十万字）一次导入。服务端会**自动分片**，每片单独
> 向量化——避免整段长文被嵌入模型按上限（本地 `bge-small-zh-v1.5` 仅 512 token）静默截断、
> 导致绝大部分内容不可检索。同一 `doc_id` 重复导入会**先清空旧分片再写**（幂等，不产生重复）。
> 手动知识以 SQL（`AIKnowledgeChunk`）为真值源，向量库丢失/换模型后可自动从 SQL 重嵌恢复。

**请求体**（`full_text` 与 `items` 二选一）：
```json
{
    "title": "运营手册v3",
    "doc_id": "handbook_v3",
    "full_text": "……数十万字……",
    "tags": ["运营"],
    "plugin": "manual",
    "chunk_size": 400,
    "chunk_overlap": 60,
    "replace": true
}
```

**请求字段说明**：
| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| title | string | 是 | - | 文档标题（多分片时每片标题追加"- 第N段"） |
| doc_id | string | 否 | 自动生成 | 文档标识；同一 doc_id 重导即覆盖。建议显式传，便于按文档管理/清理 |
| full_text | string | 否* | - | 整篇长文，服务端按 chunk_size/overlap 分片 |
| items | array | 否* | - | 客户端已分好的分片数组，每项形如 `{"content": "..."}` |
| tags | array | 否 | [] | 统一标签（所有分片共享） |
| plugin | string | 否 | manual | 所属分组 |
| chunk_size | integer | 否 | 400 | 单片最大字符数（50–4000）。本地默认模型建议 ≤400，远程大上下文模型可放宽 |
| chunk_overlap | integer | 否 | 60 | 相邻片重叠字符数 |
| replace | boolean | 否 | true | 先删除该 doc_id 旧分片再写，避免新版分片更少时残留孤儿分片 |

> *：`full_text` 与 `items` 至少提供一个。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": { "doc_id": "handbook_v3", "total_chunks": 312, "written": 312, "skipped": 0 }
}
```

| 字段 | 说明 |
|------|------|
| total_chunks | 分片总数 |
| written | 成功写入向量的分片数 |
| skipped | 被嵌入端限流（413）跳过的分片数（仍保留在 SQL 真值源，下次启动对账时补嵌） |

---

## 16.8 删除整篇文档

```
DELETE /api/ai/knowledge/doc/{doc_id}
```

> 按 `doc_id` 删除该文档的全部分片（SQL 真值源 + Qdrant 向量）。用于"重导前清旧"或文档级清理。

**响应**：
```json
{ "status": 0, "msg": "ok", "data": { "doc_id": "handbook_v3", "deleted_chunks": 312 } }
```

> 按 `doc_id` 浏览某篇文档的分片：`GET /api/ai/knowledge/list?source=manual&doc_id=handbook_v3`。
> `source=manual` 的列表走 SQL 真值源原生分页（offset 为 O(1)，大库不再每页全量 scroll）。

---

## 16.9 导出备份（JSONL）

```
GET /api/ai/knowledge/backup/export
```

> 流式导出**全部手动知识**为 JSONL（每行一条 JSON），作为用户级备份/迁移件。
> 响应为文件下载（`Content-Disposition: attachment; filename=manual_knowledge.jsonl`）。

每行形如：
```json
{"id":"handbook_v3#0","doc_id":"handbook_v3","chunk_index":0,"plugin":"manual","title":"运营手册v3 - 第1段","content":"...","tags":["运营"],"source":"manual"}
```

---

## 16.10 导入恢复（从备份）

```
POST /api/ai/knowledge/backup/import
```

> 从导出件恢复手动知识（写 SQL 真值源 + 重新嵌入入库）。`records` 与 `jsonl` 至少提供一个。

**请求体**（二选一）：
```json
{ "records": [ {"id":"handbook_v3#0","title":"...","content":"...","tags":["运营"]} ] }
```
或
```json
{ "jsonl": "{\"id\":\"a#0\",\"content\":\"...\"}\n{\"id\":\"a#1\",\"content\":\"...\"}" }
```

**响应**：
```json
{ "status": 0, "msg": "ok", "data": { "total": 312, "written": 312, "skipped": 0 } }
```

> 提示：`id` 缺失时自动生成；带原 `id`/`doc_id` 则按主键幂等覆盖，可重复导入不产生重复。

---

## 16.11 深度对账（运维）

```
POST /api/ai/knowledge/reconcile
```

> 逐条比对手动知识的 **SQL 真值源**与 **Qdrant 向量**，修复启动期"按数量对账"覆盖不到的
> "数量相等但内容分叉"盲区。建议在**换嵌入模型 / 迁移 / 疑似数据分叉**时手动触发。

对账动作：
- **Qdrant 有、SQL 无** → 回填 SQL（向量已在，不重嵌）；
- **SQL 有、Qdrant 无** → 从 SQL 重嵌入；
- **两侧都有但 `content_hash` 不一致** → 以 **SQL 为真值源**重嵌入覆盖 Qdrant 点。

> ⚠️ 比启动期数量对账昂贵：需全量 `scroll` Qdrant 手动点 + 全表读 SQL + 必要时批量重嵌，
> 大知识库耗时较长。非启动链路、不自动触发。

**请求体**：无（POST 空体即可）。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "sql_total": 312,
        "qdrant_total": 310,
        "backfilled": 0,
        "reembedded_missing": 2,
        "reembedded_mismatch": 1,
        "reembedded_written": 3,
        "consistent": false
    }
}
```

> `consistent=true` 表示对账前两侧已完全一致（无回填、无重嵌）。`reembedded_written` 为本次实际
> 写回 Qdrant 的分片数（缺失 + 哈希不一致）。RAG 未初始化时 `data` 为 `{"error": "..."}`。
