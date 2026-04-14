# 12. 主题配置 API - /api/theme

## 12.1 获取主题配置
```
GET /api/theme/config
```
**无需认证**

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "mode": "dark",
        "style": "glassmorphism",
        "color": "red",
        "language": "zh-CN",
        "icon_color": "colored",
        "background_image": "https://...",
        "blur_intensity": 12,
        "theme_preset": "shadcn"
    }
}
```

---

## 12.2 保存主题配置
```
POST /api/theme/config
```

**请求体**：
```json
{
    "mode": "dark",
    "style": "glassmorphism",
    "color": "red",
    "language": "zh-CN",
    "icon_color": "colored",
    "background_image": "https://...",
    "blur_intensity": 12,
    "theme_preset": "shadcn"
}
```
