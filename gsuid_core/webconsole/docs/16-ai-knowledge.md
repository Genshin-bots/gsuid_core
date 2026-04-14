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
