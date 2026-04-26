# 2. 系统 API - /api/system

## 2.1 获取系统信息
```
GET /api/system/info
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "version": "1.0.0",
        "python_version": "3.x",
        "uptime": "N/A"
    }
}
```

---

## 2.2 健康检查
```
GET /api/system/health
```
**无需认证**

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "status": "healthy"
    }
}
```

---

## 2.3 重启核心服务
```
POST /api/system/restart
```

---

## 2.4 停止核心服务
```
POST /api/system/stop
```

---

## 2.5 恢复核心服务
```
POST /api/system/resume
```
