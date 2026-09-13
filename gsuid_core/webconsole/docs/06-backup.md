# 6. 备份 API - /api/backup

## 6.1 获取备份文件列表
```
GET /api/backup/files
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "fileName": "backup_20240101.zip",
            "downloadUrl": "/api/backup/download?file_id=backup_20240101.zip",
            "deleteUrl": "/api/backup/backup_20240101.zip",
            "size": 1234567,
            "created": "2024-01-01T00:00:00"
        }
    ]
}
```

---

## 6.2 创建备份
```
POST /api/backup/create
```

---

## 6.3 下载备份文件
```
GET /api/backup/download?file_id=xxx
```

---

## 6.4 删除备份文件
```
DELETE /api/backup/{file_id}
```

---

## 6.5 获取备份配置
```
GET /api/backup/config
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "backup_time": "02:00",
        "backup_dir": ["config", "data"],
        "backup_method": ["local"],
        "webdav_url": "",
        "webdav_username": "",
        "webdav_password": ""
    }
}
```

---

## 6.6 保存备份配置
```
POST /api/backup/config
```

**请求体**：
```json
{
    "backup_time": "03:00",
    "backup_dir": ["config", "data"],
    "backup_method": ["local", "webdav"],
    "webdav_url": "https://...",
    "webdav_username": "user",
    "webdav_password": "pass"
}
```

---

## 6.7 获取备份文件树（分页）
```
GET /api/backup/file-tree
```

列出 `data/` 下**某一目录的直接子项**，供备份勾选。不再一次性返回整棵树。

**Query 参数**：
- `path`: 相对 `data/` 的目录，空 = 根
- `sort`: `size`（默认，按体积降序）或 `count`（按文件数降序）
- `offset`: 默认 0
- `limit`: 默认 100，**上限 100**（继续拉下一页请加大 `offset`，不要一次要全量）

缓存 / 临时目录不会出现：`IMAGE_TEMP`、`DATA_CACHE_PATH`、`data_cache`、`GsCore_BACKUP_PATH`、`dist`、`__pycache__`、`node_modules`、`.` 开头。

扫盘在线程池，不堵 Core 主循环。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "path": "plugin-res",
        "name": "plugin-res",
        "type": "directory",
        "size_bytes": 123456,
        "file_count": 120,
        "child_total": 120,
        "offset": 0,
        "limit": 100,
        "truncated": true,
        "omitted_count": 20,
        "sort": "size",
        "children": [
            {
                "id": "plugin-res/cache-000.bin",
                "name": "cache-000.bin",
                "type": "file",
                "path": "plugin-res/cache-000.bin",
                "size_bytes": 122880,
                "file_count": 1,
                "has_children": false
            }
        ]
    }
}
```

非法路径 `{status: 1}`。前端「还有 N 项」用 `offset = 已加载条数` 再请求同一 `path`。
