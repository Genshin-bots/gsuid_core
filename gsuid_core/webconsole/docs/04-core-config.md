# 4. 核心配置 API - /api/core

## 4.1 获取核心配置
```
GET /api/core/config
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "HOST": "localhost",
        "PORT": "8765",
        "ENABLE_HTTP": false,
        "WS_TOKEN": "",
        "masters": [],
        "superusers": [],
        "command_start": [],
        "enable_empty_start": true,
        "log": {...}
    }
}
```

---

## 4.2 保存核心配置
```
POST /api/core/config
```

**请求体**：核心配置键值对
