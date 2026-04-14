# 19. AI Image RAG API - /api/ai/images

> 图片 RAG API 用于管理通过向量检索的图片。图片通过插件注册或前端上传，存储在独立的向量集合中，支持基于语义的图片搜索。

## 19.1 上传图片

```
POST /api/ai/images/upload
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

**请求体**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file | file | 是 | 图片文件 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "filename": "a1b2c3d4.png",
        "path": "C:/.../gsuid_core/data/local_embedding_images/a1b2c3d4.png",
        "relative_path": "data/local_embedding_images/a1b2c3d4.png"
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.filename | string | 生成的唯一文件名 |
| data.path | string | 图片的绝对路径 |
| data.relative_path | string | 相对路径，可用于入库 |

---

## 19.2 创建图片实体（入库）

```
POST /api/ai/images
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: application/x-www-form-urlencoded
```

**请求参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| id | string | 否 | 自动生成 | 图片唯一标识 |
| plugin | string | 否 | manual | 插件名 |
| path | string | 是 | - | 图片路径（上传接口返回的path） |
| tags | string | 是 | - | 标签，多个用逗号分隔，如"胡桃,原神,角色" |
| content | string | 否 | - | 详细描述文本 |

**请求示例**：
```
POST /api/ai/images
Content-Type: application/x-www-form-urlencoded

id=hutao_001&plugin=manual&path=C%3A%2F...%2Fa1b2c3d4.png&tags=%E8%83%A1%E6%A1%83%2C%E5%8E%9F%E7%A5%9E%2C%E8%A7%92%E8%89%B2&content=%E8%83%A1%E6%A1%83%E8%A7%92%E8%89%B2%E7%AB%8B%E7%BB%98
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "hutao_001",
        "path": "C:/.../gsuid_core/data/local_embedding_images/a1b2c3d4.png",
        "tags": ["胡桃", "原神", "角色"]
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "Failed to add image to vector database",
    "data": null
}
```

---

## 19.3 获取图片列表（分页）

```
GET /api/ai/images/list
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
| plugin | string | 否 | - | 按插件名过滤 |
| page | integer | 否 | 1 | 页码，从1开始 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "list": [
            {
                "id": "hutao_character",
                "plugin": "GenshinUID",
                "path": "./resources/characters/hutao.png",
                "tags": ["胡桃", "原神", "角色", "火系"],
                "content": "胡桃角色立绘图片",
                "source": "plugin"
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 20,
        "next_offset": null,
        "page": 1,
        "page_size": 20
    }
}
```

---

## 19.4 搜索图片

```
GET /api/ai/images/search
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 查询文本（描述想要找的图片内容） |
| limit | integer | 否 | 10 | 返回数量限制 |
| plugin | string | 否 | - | 按插件名过滤 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "results": [
            {
                "id": "hutao_character",
                "plugin": "GenshinUID",
                "path": "./resources/characters/hutao.png",
                "tags": ["胡桃", "原神", "角色"],
                "content": "胡桃角色立绘图片",
                "score": 0.95
            }
        ],
        "count": 1,
        "query": "胡桃图片"
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.results | array | 匹配的图片列表 |
| data.results[].id | string | 图片 ID |
| data.results[].plugin | string | 所属插件 |
| data.results[].path | string | 图片文件路径 |
| data.results[].tags | array | 图片标签 |
| data.results[].content | string | 图片描述 |
| data.results[].score | float | 匹配分数（0-1，越高越匹配） |
| data.count | integer | 结果数量 |
| data.query | string | 查询文本 |

---

## 19.5 获取最佳匹配图片路径

```
GET /api/ai/images/path
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 查询文本 |
| plugin | string | 否 | - | 按插件名过滤 |

**响应（找到图片）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "path": "./resources/characters/hutao.png"
    }
}
```

**响应（未找到图片）**：
```json
{
    "status": 1,
    "msg": "No matching image found",
    "data": null
}
```

---

## 19.6 删除图片

```
DELETE /api/ai/images/{entity_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| entity_id | string | 图片 ID |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "hutao_character"
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "Image 'hutao_character' not found or delete failed",
    "data": null
}
```

---

## 19.7 插件注册图片示例

插件开发者可以通过以下方式注册图片：

```python
from gsuid_core.ai_core.models import ImageEntity
from gsuid_core.ai_core.register import ai_image

# 注册图片
ai_image(ImageEntity(
    id="hutao_character",
    plugin="GenshinUID",
    path="./resources/characters/hutao.png",
    tags=["胡桃", "原神", "角色", "火系"],
    content="胡桃角色立绘图片，往生堂第七十七代堂主",
    source="plugin",
    _hash="",
))
```

注册后的图片会在系统启动时自动同步到向量数据库，支持语义搜索。
