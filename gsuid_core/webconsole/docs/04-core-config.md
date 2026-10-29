# 4. 核心配置 API - /api/core

## 4.1 获取核心配置
```
GET /api/core/config
```

管理员专用，且只认 `Authorization: Bearer`（不接受 `?token=`）。`WS_TOKEN`、`REGISTER_CODE` 等密钥键对管理员下发明文（空串保持空）。前端默认视觉隐藏。Live Chat 用登录会话连 WS，不读本接口的 `WS_TOKEN`。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "HOST": "localhost",
        "PORT": "8765",
        "ENABLE_HTTP": false,
        "WS_TOKEN": "****",
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

**请求体**：核心配置键值对。

保存写入当前进程内存，并写回 `data/config.json`。`HOST`、`PORT`、`WS_TOKEN` 仍在启动时读一次，改完要重启。`TRUSTED_IPS` 在下一次 HTTP 请求和下一次 WebSocket 连接读取这份内存。响应 `msg` 固定为「配置已保存，重启后生效」。网页控制台的成功提示用前端自己的文案，不读这句 `msg`。
