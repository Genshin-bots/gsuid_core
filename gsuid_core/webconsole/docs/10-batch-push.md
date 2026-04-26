# 10. 消息推送 API - /api/BatchPush

## 10.1 批量推送
```
POST /api/BatchPush
```

**请求体**：
```json
{
    "push_text": "<p>推送内容</p><img src='base64,...'/>",
    "push_tag": "ALLUSER,ALLGROUP,g:123456|bot1,u:654321|bot2",
    "push_bot": "bot1,bot2"
}
```

**推送目标格式**：
- `ALLUSER`: 所有用户
- `ALLGROUP`: 所有群组
- `g:群ID|botID`: 指定群
- `u:用户ID|botID`: 指定用户
