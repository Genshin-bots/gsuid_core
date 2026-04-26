# 18. History Manager API - /api/history

> History Manager API 用于管理 AI 会话的历史记录，支持查看、清空 session 历史以及查看 session 使用的 persona。

## 18.1 获取所有 Session 列表

```
GET /api/history/sessions
```

**请求头**：
```
Authorization: Bearer <token>
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "session_id": "bot:0:private:123456",
            "session_key": "bot:0:private:123456",
            "type": "private",
            "group_id": null,
            "user_id": "user123",
            "message_count": 15,
            "last_access": 1712345678.0,
            "created_at": 1712345600.0
        },
        {
            "session_id": "bot:0:group:789012",
            "session_key": "bot:0:group:789012",
            "type": "group",
            "group_id": "group456",
            "user_id": null,
            "message_count": 30,
            "last_access": 1712345678.0,
            "created_at": 1712345600.0
        }
    ]
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data | array | Session 列表 |
| data[].session_id | string | Session 标识符，格式为 `bot:{bot_id}:group:{group_id}` 或 `bot:{bot_id}:private:{user_id}` |
| data[].session_key | string | 内部使用的 session key |
| data[].type | string | Session 类型：`private`(私聊) 或 `group`(群聊) |
| data[].group_id | string/null | 群聊 ID，私聊时为 null |
| data[].user_id | string/null | 用户 ID，群聊时为 null |
| data[].message_count | integer | 该 session 的消息数量 |
| data[].last_access | float/null | 最后访问时间戳 |
| data[].created_at | float/null | 创建时间戳 |

---

## 18.2 获取指定 Session 的历史记录

```
GET /api/history/{session_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| session_id | string | Session 标识符，格式为 `bot:{bot_id}:group:{group_id}` 或 `bot:{bot_id}:private:{user_id}` |

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|--------|------|------|
| format_type | string | 否 | text | 返回格式：`text`(文本格式)、`json`(原始JSON)、`messages`(OpenAI格式) |

**响应（text 格式）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "session_id": "bot:0:private:123456",
        "content": "[用户-用户昵称]: 你好\n[AI]: 你好！有什么可以帮助你的吗？\n[用户-用户昵称]: 今天天气怎么样？",
        "count": 3
    }
}
```

**响应（json 格式）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "session_id": "bot:0:private:123456",
        "messages": [
            {
                "role": "user",
                "content": "你好",
                "user_id": "user123",
                "user_name": "用户昵称",
                "timestamp": 1712345600.0,
                "metadata": {}
            },
            {
                "role": "assistant",
                "content": "你好！有什么可以帮助你的吗？",
                "user_id": "ai",
                "user_name": null,
                "timestamp": 1712345601.0,
                "metadata": {}
            }
        ],
        "count": 2
    }
}
```

**响应（messages 格式）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "session_id": "bot:0:private:123456",
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮助你的吗？"}
        ],
        "count": 2
    }
}
```

**错误响应（session 不存在）**：
```json
{
    "status": 0,
    "msg": "该session没有历史记录",
    "data": {
        "session_id": "bot:0:private:123456",
        "messages": [],
        "count": 0
    }
}
```

---

## 18.3 清空指定 Session 的历史记录

```
DELETE /api/history/{session_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| session_id | string | Session 标识符，格式为 `bot:{bot_id}:group:{group_id}` 或 `bot:{bot_id}:private:{user_id}` |

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|--------|------|------|
| delete_session | boolean | 否 | false | 是否完全删除 session（释放内存），false 则仅清空历史 |

**响应（清空历史）**：
```json
{
    "status": 0,
    "msg": "Session user123&&None 的历史记录已清空",
    "data": {
        "session_id": "bot:0:private:123456",
        "cleared": true
    }
}
```

**响应（完全删除）**：
```json
{
    "status": 0,
    "msg": "Session user123&&None 已完全删除",
    "data": {
        "session_id": "bot:0:private:123456",
        "deleted": true
    }
}
```

**错误响应（session 不存在）**：
```json
{
    "status": 1,
    "msg": "Session user123&&None 不存在",
    "data": null
}
```

---

## 18.4 获取指定 Session 的 Persona 内容

```
GET /api/history/{session_id}/persona
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| session_id | string | Session 标识符，格式为 `bot:{bot_id}:group:{group_id}` 或 `bot:{bot_id}:private:{user_id}` |

**响应（有 persona）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "session_id": "bot:0:private:123456",
        "persona_content": "[角色扮演开始]\n\n### [Character: 智能助手]\n..."
    }
}
```

**响应（无 persona）**：
```json
{
    "status": 0,
    "msg": "该session没有设置persona",
    "data": {
        "session_id": "bot:0:private:123456",
        "persona_content": null
    }
}
```

**错误响应（session 不存在）**：
```json
{
    "status": 1,
    "msg": "Session user123&&None 不存在或尚未创建",
    "data": null
}
```

---

## 18.5 获取历史管理器统计信息

```
GET /api/history/stats
```

**请求头**：
```
Authorization: Bearer <token>
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "history_manager": {
            "total_sessions": 10,
            "total_messages": 150,
            "group_sessions": 5,
            "max_messages_per_session": 30
        },
        "ai_router_sessions": {
            "count": 8,
            "sessions": {
                "bot:0:private:123456": {
                    "session_id": "bot:0:private:123456",
                    "last_access": 1712345678.0,
                    "created_at": 1712345600.0,
                    "history_length": 15
                }
            }
        }
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.history_manager | object | HistoryManager 统计信息 |
| data.history_manager.total_sessions | integer | 总 session 数量 |
| data.history_manager.total_messages | integer | 总消息数量 |
| data.history_manager.group_sessions | integer | 群聊 session 数量 |
| data.history_manager.max_messages_per_session | integer | 每个 session 最大消息数 |
| data.ai_router_sessions | object | AI Router 中的 session 信息 |
| data.ai_router_sessions.count | integer | AI Router 中的 session 数量 |
| data.ai_router_sessions.sessions | object | 各 session 的详细信息 |

---

## Session ID 格式说明

Session ID 用于唯一标识一个会话，格式为 `bot:{bot_id}:group:{group_id}` 或 `bot:{bot_id}:private:{user_id}`：

| 场景 | session_id 格式 | 示例 | 说明 |
|------|----------------|------|------|
| 私聊 | `bot:{bot_id}:private:{user_id}` | `bot:0:private:123456` | 用户私聊会话 |
| 群聊 | `bot:{bot_id}:group:{group_id}` | `bot:0:group:789012` | 群聊会话 |

> 注意：
> - 新格式使用 `:` 作为分隔符，包含 bot_id 和会话目标（group 或 private）
> - 私聊时使用 `private:{user_id}` 格式
> - 群聊时使用 `group:{group_id}` 格式
> - bot_id 通常为 "0" 或具体的机器人实例ID
