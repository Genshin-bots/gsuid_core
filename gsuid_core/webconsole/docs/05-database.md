# 5. 数据库 API - /api/database

## 5.1 获取所有插件数据库
```
GET /api/database/plugins
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "plugin_id": "xxx",
            "plugin_name": "xxx",
            "tables": [...],
            "icon": "base64..."
        }
    ]
}
```

---

## 5.2 获取插件表信息
```
GET /api/database/{plugin_id}/tables
```

---

## 5.3 获取表元数据
```
GET /api/database/table/{table_name}
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "columns": [
            {"name": "id", "type": "INTEGER"},
            {"name": "user_id", "type": "TEXT"}
        ]
    }
}
```

---

## 5.4 获取表数据（分页）
```
GET /api/database/table/{table_name}/data
```

**Query 参数**：
- `page`: 页码，默认1
- `per_page`: 每页数量，默认20
- `search`: 搜索关键字
- `search_columns`: 搜索列（逗号分隔）
- `filter_columns`: 过滤列（逗号分隔）
- `filter_values`: 过滤值（逗号分隔）

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [...],
        "total": 100,
        "page": 1,
        "per_page": 20
    }
}
```

---

## 5.5 创建记录
```
POST /api/database/table/{table_name}/data
```

**请求体**：记录数据

---

## 5.6 更新记录
```
PUT /api/database/table/{table_name}/data/{id}
```

**请求体**：更新后的数据

---

## 5.7 删除记录
```
DELETE /api/database/table/{table_name}/data/{id}
```
