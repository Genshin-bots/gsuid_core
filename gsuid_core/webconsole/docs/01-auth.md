# 1. 认证 API - /api/auth

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
