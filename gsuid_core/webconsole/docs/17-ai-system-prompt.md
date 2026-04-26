# 17. AI System Prompt API - /api/ai/system_prompt

> System Prompt API 用于管理系统提示词，支持向量检索。可以让AI根据任务描述自动匹配合适的System Prompt创建子Agent完成任务。

## 17.1 获取System Prompt列表（分页）

```
GET /api/ai/system_prompt/list
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
| page | integer | 否 | 1 | 页码，从1开始 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "list": [
            {
                "id": "sp_001",
                "title": "代码专家",
                "desc": "擅长编写各种编程语言的代码",
                "content": "你是一个专业的程序员，擅长编写高质量的代码...",
                "tags": ["代码", "编程", "专家"]
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 20,
        "page": 1,
        "page_size": 20
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.list | array | System Prompt列表 |
| data.list[].id | string | 唯一标识 |
| data.list[].title | string | 标题 |
| data.list[].desc | string | 描述（用于向量检索） |
| data.list[].content | string | 完整内容（作为系统提示词） |
| data.list[].tags | array | 标签列表 |
| data.total | integer | 总数 |
| data.offset | integer | 当前偏移量 |
| data.limit | integer | 每页数量 |
| data.page | integer | 当前页码 |
| data.page_size | integer | 每页数量 |

---

## 17.2 获取System Prompt详情

```
GET /api/ai/system_prompt/{prompt_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| prompt_id | string | System Prompt ID |

**响应（存在）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "sp_001",
        "title": "代码专家",
        "desc": "擅长编写各种编程语言的代码",
        "content": "你是一个专业的程序员，擅长编写高质量的代码...",
        "tags": ["代码", "编程", "专家"]
    }
}
```

**错误响应（不存在）**：
```json
{
    "status": 1,
    "msg": "System Prompt 'sp_001' not found",
    "data": null
}
```

---

## 17.3 新增System Prompt

```
POST /api/ai/system_prompt
```

**请求头**：
```
Authorization: Bearer <token>
```

**请求体**：
```json
{
    "title": "代码专家",
    "desc": "擅长编写各种编程语言的代码",
    "content": "你是一个专业的程序员，擅长编写高质量的代码...",
    "tags": ["代码", "编程", "专家"]
}
```

**请求字段说明**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| title | string | 是 | 标题 |
| desc | string | 是 | 描述（用于向量检索匹配） |
| content | string | 是 | 完整内容（将作为系统提示词） |
| tags | array | 是 | 标签列表 |

> 注意：id 由后端自动生成（UUID），无需传入。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "title": "代码专家"
    }
}
```

---

## 17.4 更新System Prompt

```
PUT /api/ai/system_prompt/{prompt_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| prompt_id | string | System Prompt ID |

**请求体**：
```json
{
    "title": "更新后的标题",
    "desc": "更新后的描述",
    "content": "更新后的内容...",
    "tags": ["新标签"]
}
```

> 注意：只更新传入的字段，空字符串或空数组的字段会被忽略。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "sp_001"
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "System Prompt 'sp_001' not found or update failed",
    "data": null
}
```

---

## 17.5 删除System Prompt

```
DELETE /api/ai/system_prompt/{prompt_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| prompt_id | string | System Prompt ID |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "sp_001"
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "System Prompt 'sp_001' not found or delete failed",
    "data": null
}
```

---

## 17.6 搜索System Prompt

```
GET /api/ai/system_prompt/search
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 查询文本 |
| tags | string | 否 | - | 逗号分隔的标签列表，如 "代码,编程" |
| limit | integer | 否 | 10 | 返回数量限制 |
| use_vector | boolean | 否 | true | 是否使用向量检索，false则使用简单文本匹配 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "results": [
            {
                "id": "sp_001",
                "title": "代码专家",
                "desc": "擅长编写各种编程语言的代码",
                "content": "你是一个专业的程序员，擅长编写高质量的代码...",
                "tags": ["代码", "编程", "专家"]
            }
        ],
        "count": 1,
        "query": "写一个Python排序函数"
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.results | array | 匹配的System Prompt列表（按相似度排序） |
| data.count | integer | 匹配数量 |
| data.query | string | 查询文本 |
