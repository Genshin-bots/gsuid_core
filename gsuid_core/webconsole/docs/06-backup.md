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
