# 14. AI Tools API - /api/ai/tools

> AI 工具 API 提供对系统中已注册 AI 工具的访问能力。支持按**分类**和**插件**两个维度进行筛选，方便前端构建工具管理界面。

## 工具分类说明

| 分类 | 说明 | 加载方式 |
|------|------|---------|
| `self` | 自我调用工具（如 create_subagent） | 主 Agent 默认加载 |
| `buildin` | 内置工具（如 search_knowledge、send_message） | 主 Agent 默认加载 |
| `default` | 默认分类工具 | 按需通过向量检索加载 |
| `common` | 通用工具 | 按需通过向量检索加载 |

## 14.1 获取 AI 工具列表

```
GET /api/ai/tools/list
```

**请求头**：
```
Authorization: Bearer <token>
```

**Query 参数**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| category | string | 否 | 按分类筛选，如 'self', 'buildin', 'default', 'common' |
| plugin | string | 否 | 按插件名称筛选，如 'core', 'GenshinUID' |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "tools": [
            {
                "name": "search_knowledge",
                "description": "检索知识库相关内容...",
                "plugin": "core",
                "category": "buildin"
            },
            {
                "name": "gen_image_by_text",
                "description": "通过文本生成图片...",
                "plugin": "_RH_ComfyUI",
                "category": "default"
            }
        ],
        "by_category": {
            "self": [...],
            "buildin": [...],
            "default": [...],
            "common": [...]
        },
        "by_plugin": {
            "core": [...],
            "GenshinUID": [...]
        },
        "categories": ["self", "buildin", "default", "common"],
        "plugins": ["core", "GenshinUID", "_RH_ComfyUI"],
        "count": 10,
        "total_count": 15
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功 |
| msg | string | 状态信息 |
| data.tools | array | 工具列表（筛选后），每个工具包含 name, description, plugin, category |
| data.by_category | object | 按分类分组的工具字典 |
| data.by_plugin | object | 按插件分组的工具字典 |
| data.categories | array | 所有可用的分类列表 |
| data.plugins | array | 所有可用的插件列表 |
| data.count | integer | 筛选后的工具数量 |
| data.total_count | integer | 工具总数（筛选前） |

**前端使用建议**：
1. 首次加载时调用 `/api/ai/tools/list` 获取完整的 `by_category` 和 `by_plugin` 结构
2. 使用 `categories` 和 `plugins` 列表构建筛选器
3. 根据用户选择的筛选条件，通过 `category` 或 `plugin` 参数进行筛选
4. 每个工具的 `category` 和 `plugin` 字段可用于同时按两个维度筛选

---

## 14.2 获取工具分类列表

```
GET /api/ai/tools/categories
```

**请求头**：
```
Authorization: Bearer <token>
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {"name": "self", "count": 2},
        {"name": "buildin", "count": 5},
        {"name": "default", "count": 8},
        {"name": "common", "count": 3}
    ]
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data[].name | string | 分类名称 |
| data[].count | integer | 该分类下的工具数量 |

---

## 14.3 获取指定工具详情

```
GET /api/ai/tools/{tool_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| tool_name | string | 工具名称 |

**响应（工具存在）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "search_knowledge",
        "description": "检索知识库相关内容...",
        "plugin": "core",
        "category": "buildin"
    }
}
```

**错误响应（工具不存在）**：
```json
{
    "status": 1,
    "msg": "Tool 'xxx' not found",
    "data": null
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |
| data.name | string | 工具名称 |
| data.description | string | 工具描述（docstring） |
| data.plugin | string | 所属插件名称，core 表示核心模块 |
| data.category | string | 所属分类 |
