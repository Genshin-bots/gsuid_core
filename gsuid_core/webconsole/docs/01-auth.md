# 1. 认证 API - /api/auth

> **传输安全**：登录 / 注册 / 改密接口**强制**使用应用层混合加密（X25519 + HKDF + AES-256-GCM），
> 密码不会以明文出现在 HTTP 流量中。详见 [`docs/WEBCONSOLE_AUTH_ENCRYPTION.md`](../../../docs/WEBCONSOLE_AUTH_ENCRYPTION.md)。
> 加密**无开关、不可关闭**：明文报文（或 `enc!=true`）一律被拒，前端必须先取公钥再提交加密报文。
> 解密失败（畸形 / 重放 / 明文）会计入该 IP 的限流，连续异常将被临时封禁。

## 1.0 获取认证加密公钥
```
GET /api/auth/pubkey
```

无需鉴权。前端在发起加密的登录/注册/改密请求前调用，获取服务端 X25519 公钥。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "key_id": "de6ab98a979b5656",
        "alg": "x25519-aes256gcm",
        "pubkey": "<base64 urlsafe 无 padding，32 字节 X25519 公钥>",
        "fingerprint": "ddbca71e6bfa4ad2"
    }
}
```

加密报文格式与算法细节见 [`docs/WEBCONSOLE_AUTH_ENCRYPTION.md`](../../../docs/WEBCONSOLE_AUTH_ENCRYPTION.md)。

---

## 1.1 用户登录
```
POST /api/auth/login
```

**请求体**：
```json
{
    "email": "user@example.com",
    "password": "password123"
}
```

**响应**：
```json
{
    "status": 0,
    "msg": "登录成功",
    "data": {
        "user": {
            "id": "1",
            "email": "user@example.com",
            "name": "用户名",
            "role": "admin",
            "avatar": null
        },
        "token": "email:hash"
    }
}
```

---

## 1.2 用户注册
```
POST /api/auth/register
```

**请求体**：
```json
{
    "name": "新用户",
    "email": "new@example.com",
    "password": "password123",
    "register_code": "注册码"
}
```

**响应**：
```json
{
    "status": 0,
    "msg": "注册成功",
    "data": {
        "user": {...},
        "token": "..."
    }
}
```

---

## 1.3 检查管理员是否存在
```
GET /api/auth/admin/exists
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "is_admin_exist": true
    }
}
```

---

## 1.4 获取当前用户信息
```
GET /api/auth/user_info
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "1",
        "email": "user@example.com",
        "name": "用户名",
        "role": "admin",
        "avatar": "base64..."
    }
}
```

---

## 1.5 更新用户信息
```
PUT /api/auth/user_info
```

**请求体**：
```json
{
    "name": "新昵称",
    "avatar": "base64..."
}
```

---

## 1.6 修改密码
```
PUT /api/auth/password
```

**请求体**：
```json
{
    "old_password": "旧密码",
    "new_password": "新密码"
}
```

---

## 1.7 上传头像
```
POST /api/auth/upload_avatar
Content-Type: multipart/form-data

file: [图片文件]
```

---

## 1.8 退出登录
```
POST /api/auth/logout
```
