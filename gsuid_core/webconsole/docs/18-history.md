# 18. History Manager API - /api/history

> History Manager API 用于管理会话的消息历史记录，支持查看、清空 session 历史以及查看 session 使用的 persona。

> **关于 AI 总开关（enable_ai）**
>
> 消息历史模块（`gsuid_core.message_history`）已从 `ai_core` 解耦为通用的 Bot 消息输入/输出历史记录模块。
> 因此本组 API 在 AI 总开关关闭（`enable_ai=False`）时**依然可以正常读取与管理消息历史**，
> 仅 AI 会话相关的增强信息会按下述规则降级：
>
> - **列出 Session**：`has_ai_session` 恒为 `false`、`ai_history_length` 恒为 `0`。
> - **查看历史记录**：完全不受影响，始终可用。
> - **清空 Session**：AI 开启时清空「消息历史 + AI 会话对象」；AI 关闭时仅清空「消息历史」。
> - **查看 Persona**：persona 属于 AI 会话信息，AI 关闭时该接口统一返回「session 不存在」。
> - **统计信息**：`history_manager` 部分始终可用；`ai_router_sessions` 在 AI 关闭时为空统计。

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
            "session_id": "ws-onebot:onebot:bot_001:private:123456",
            "session_key": "ws-onebot:onebot:bot_001:private:123456",
            "type": "private",
            "group_id": null,
            "user_id": "user123",
            "message_count": 15,
            "last_access": 1712345678.0,
            "created_at": 1712345600.0
        },
        {
            "session_id": "ws-onebot:onebot:bot_001:group:789012",
            "session_key": "ws-onebot:onebot:bot_001:group:789012",
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
| data[].session_id | string | Session 标识符，格式为 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}` 或 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}` |
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
| session_id | string | Session 标识符，格式为 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}` 或 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}` |

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
        "session_id": "ws-onebot:onebot:bot_001:private:123456",
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
        "session_id": "ws-onebot:onebot:bot_001:private:123456",
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
        "session_id": "ws-onebot:onebot:bot_001:private:123456",
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
        "session_id": "ws-onebot:onebot:bot_001:private:123456",
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
| session_id | string | Session 标识符，格式为 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}` 或 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}` |

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|--------|------|------|
| delete_session | boolean | 否 | false | 是否完全删除 session（释放内存），false 则仅清空历史 |

> **行为说明**：
> - AI 开启时：清空/删除「消息历史」与对应的「AI 会话对象」（完全删除时会触发 AI 会话日志落盘）。
> - AI 关闭时：仅清空/删除「消息历史」，不存在 AI 会话对象。

**响应（清空历史）**：
```json
{
    "status": 0,
    "msg": "Session user123&&None 的历史记录已清空",
    "data": {
        "session_id": "ws-onebot:onebot:bot_001:private:123456",
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
        "session_id": "ws-onebot:onebot:bot_001:private:123456",
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
| session_id | string | Session 标识符，格式为 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}` 或 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}` |

> **行为说明**：persona 属于 AI 会话信息。当 AI 总开关关闭（`enable_ai=False`）时，
> 系统不存在任何 AI 会话对象，本接口统一返回「session 不存在或尚未创建」（`status: 1`）。

**响应（有 persona）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "session_id": "ws-onebot:onebot:bot_001:private:123456",
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
        "session_id": "ws-onebot:onebot:bot_001:private:123456",
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
                "ws-onebot:onebot:bot_001:private:123456": {
                    "session_id": "ws-onebot:onebot:bot_001:private:123456",
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
| data.ai_router_sessions | object | AI 会话注册表中的 session 信息（AI 关闭时为空统计 `{"count": 0, "sessions": []}`） |
| data.ai_router_sessions.count | integer | AI 会话注册表中的 session 数量 |
| data.ai_router_sessions.sessions | object | 各 session 的详细信息 |

---

## 18.6 向指定 Session 发送消息

```
POST /api/history/{session_id}/send
```

根据 `session_id` 解析出 `WS_BOT_ID` / `bot_id` / `group_id` / `user_id`，定位对应的 Bot 连接后，将文本与图片组装为消息段并调用 `bot.send()` 发送。支持**纯文本、纯图片、多图、图文混排**。

> **说明**：
> - 请求类型为 `multipart/form-data`。**图片由前端直接上传文件，无需自行做 base64 编码**，后端读取二进制后自动转换。
> - 发送的消息会经由 `target_send` **自动记录进该 session 的消息历史**，无需额外调用。
> - 本接口属于通用 Bot 能力，与 AI 总开关无关，`enable_ai=False` 时同样可用。
> - 群聊发往 `group_id`，私聊发往 `user_id`。

**请求头**：
```
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| session_id | string | Session 标识符，格式为 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}` 或 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}` |

**表单字段**（`multipart/form-data`）：
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| message | string | 否 | `""` | 文本内容，可为空 |
| images | file | 否 | - | 图片文件，可重复该字段上传多张 |
| image_urls | string | 否 | - | 图片直链（**仅 http/https**），可重复该字段传多个 |
| at_sender | boolean | 否 | false | 是否 @ 发送对象（仅群聊场景有意义） |

> `message` 文本与 `images` / `image_urls` 图片**至少需提供其一**，否则返回「消息内容不能为空」。

**请求示例（curl）**：
```bash
curl -X POST "http://<host>/api/history/ws-onebot:onebot:bot_001:group:789012/send" \
  -H "Authorization: Bearer <token>" \
  -F "message=你好，这是一条图文消息" \
  -F "images=@/path/to/pic1.png" \
  -F "images=@/path/to/pic2.jpg" \
  -F "image_urls=https://example.com/remote.png" \
  -F "at_sender=false"
```

**响应（发送成功）**：
```json
{
    "status": 0,
    "msg": "消息发送成功",
    "data": {
        "session_id": "ws-onebot:onebot:bot_001:group:789012",
        "target_type": "group",
        "target_id": "789012",
        "text_sent": true,
        "image_count": 3
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.target_type | string | 发送目标类型：`group` 或 `private` |
| data.target_id | string | 实际发送目标 ID（群号或用户 ID） |
| data.text_sent | boolean | 本次是否发送了文本 |
| data.image_count | integer | 本次发送的图片数量（上传文件 + 直链合计） |

**错误响应**：
| 场景 | status | msg |
|------|--------|-----|
| 文本与图片均为空 | 1 | 消息内容不能为空（需提供 message 文本或 images/image_urls 图片） |
| session_id 格式非法 | 1 | 无效的session_id格式，应为 '{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}' 或 '{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}' |
| session_id 缺少发送目标 | 1 | session_id 中缺少有效的发送目标 |
| image_urls 非 http(s) 直链 | 1 | image_urls 仅支持 http/https 直链: {url} |
| 当前无任何已连接 Bot | 1 | 当前没有任何已连接的 Bot |
| 指定的 Bot 未连接 | 1 | Bot '{bot_id}' 当前未连接，无法发送消息 |

---

## Session ID 格式说明

Session ID 用于唯一标识一个会话，格式为 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}` 或 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}`：

| 场景 | session_id 格式 | 示例 | 说明 |
|------|----------------|------|------|
| 私聊 | `{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}` | `ws-onebot:onebot:bot_001:private:123456` | 用户私聊会话 |
| 群聊 | `{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}` | `ws-onebot:onebot:bot_001:group:789012` | 群聊会话 |

> 注意：
> - 新格式使用 `:` 作为分隔符，包含 WS_BOT_ID、bot_id、bot_self_id 和会话目标（group 或 private）
> - 私聊时使用 `private:{user_id}` 格式
> - 群聊时使用 `group:{group_id}` 格式
> - WS_BOT_ID 是 `gss.active_bot` 中的 WS 链接标识符；bot_id 是该 WS 链接对应的平台标识
