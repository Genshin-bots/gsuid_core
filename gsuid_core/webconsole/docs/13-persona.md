# 13. Persona API - /api/persona

## 13.1 获取角色列表

```
GET /api/persona/list
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
            "name": "角色名1",
            "has_avatar": true,
            "has_image": false,
            "has_audio": true
        },
        {
            "name": "角色名2",
            "has_avatar": false,
            "has_image": true,
            "has_audio": false
        }
    ]
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 角色名称 |
| has_avatar | boolean | 是否有头像文件 |
| has_image | boolean | 是否有立绘文件 |
| has_audio | boolean | 是否有音频文件（任何支持的格式） |

---

## 13.2 获取角色详情

```
GET /api/persona/{persona_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| persona_name | string | 角色名称 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "角色名",
        "content": "# 角色名\n\n角色描述内容（Markdown格式）...",
        "metadata": {
            "name": "角色名",
            "has_avatar": true,
            "has_image": false,
            "has_audio": true
        }
    }
}
```

**错误响应**（角色不存在）：
```json
{
    "status": 1,
    "msg": "角色 'xxx' 不存在",
    "data": null
}
```

---

## 13.3 获取角色头像

```
GET /api/persona/{persona_name}/avatar
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| persona_name | string | 角色名称 |

**响应**：
- 成功：返回 PNG 图片文件（Content-Type: image/png）
- 失败：HTTP 404，角色头像不存在

---

## 13.4 获取角色立绘

```
GET /api/persona/{persona_name}/image
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| persona_name | string | 角色名称 |

**响应**：
- 成功：返回 PNG 图片文件（Content-Type: image/png）
- 失败：HTTP 404，角色立绘不存在

---

## 13.5 获取角色音频

```
GET /api/persona/{persona_name}/audio
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| persona_name | string | 角色名称 |

**响应**：
- 成功：返回音频文件（根据实际格式返回对应的 Content-Type）
- 失败：HTTP 404，角色音频不存在

**支持的音频格式**：
- MP3 (audio/mpeg) - 优先级最高
- OGG (audio/ogg)
- WAV (audio/wav)
- M4A (audio/mp4)
- FLAC (audio/flac)

**说明**：如果同一角色存在多个格式的音频文件，优先返回 MP3 格式。

---

## 13.6 上传角色头像

```
POST /api/persona/{persona_name}/avatar
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: application/json
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| persona_name | string | 角色名称 |

**请求体**：
```json
{
    "image": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
}
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "path": "/absolute/path/to/persona/角色名/avatar.png"
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "请提供图片数据",
    "data": null
}
```

---

## 13.7 上传角色立绘

```
POST /api/persona/{persona_name}/image
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: application/json
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| persona_name | string | 角色名称 |

**请求体**：
```json
{
    "image": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
}
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "path": "/absolute/path/to/persona/角色名/image.png"
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "请提供图片数据",
    "data": null
}
```

---

## 13.8 上传角色音频

```
POST /api/persona/{persona_name}/audio
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: application/json
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| persona_name | string | 角色名称 |

**请求体**：
```json
{
    "audio": "data:audio/mpeg;base64,//uQZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWgAAAA0...",
    "format": "mp3"
}
```

**请求字段说明**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| audio | string | 是 | Base64编码的音频数据 |
| format | string | 否 | 音频格式，支持 mp3、ogg、wav、m4a、flac，默认为 mp3 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "path": "/absolute/path/to/persona/角色名/audio.mp3",
        "format": "mp3"
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "请提供音频数据",
    "data": null
}
```

或

```json
{
    "status": 1,
    "msg": "不支持的音频格式: xxx，支持的格式: mp3, ogg, wav, m4a, flac",
    "data": null
}
```

---

## 13.9 创建新角色

```
POST /api/persona/create
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: application/json
```

**请求体**：
```json
{
    "name": "角色名称",
    "query": "角色描述，用于AI生成角色提示词"
}
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "角色名称",
        "content": "# 角色名\n\n生成的角色描述内容（Markdown格式）..."
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "请提供角色名称",
    "data": null
}
```

---

## 13.10 删除角色

```
DELETE /api/persona/{persona_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| persona_name | string | 角色名称 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": null
}
```

**说明**：删除角色会删除该角色的整个文件夹，包括 persona.md、avatar.png、image.png、audio.*（所有音频格式）等所有文件。

---

## 13.11 获取角色配置

```
GET /api/persona/{persona_name}/config
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| persona_name | string | 角色名称 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "ai_mode": ["提及应答", "定时巡检"],
        "scope": "specific",
        "target_groups": ["群聊ID1", "群聊ID2"],
        "inspect_interval": 30
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| ai_mode | array | AI行动模式列表，可选值："提及应答", "定时巡检", "趣向捕捉(暂不可用)", "困境救场(暂不可用)" |
| scope | string | 启用范围，可选值："disabled"(不对任何群聊启用), "global"(对所有群/角色启用), "specific"(仅对指定群聊启用) |
| target_groups | array | 当 scope 为 "specific" 时，指定该人格对哪些群聊/角色启用 |
| inspect_interval | integer | 定时巡检间隔（分钟），当 ai_mode 包含"定时巡检"时有效，可选值：5, 10, 15, 30, 60 |
| keywords | array | 唤醒关键词列表，当 ai_mode 包含"提及应答"时有效，消息包含这些关键词也会触发AI响应 |

**错误响应**（配置不存在）：
```json
{
    "status": 1,
    "msg": "角色 'xxx' 的配置不存在",
    "data": null
}
```

---

## 13.12 更新角色配置

```
PUT /api/persona/{persona_name}/config
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: application/json
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| persona_name | string | 角色名称 |

**请求体**：
```json
{
    "ai_mode": ["提及应答", "定时巡检"],
    "scope": "specific",
    "target_groups": ["群聊ID1", "群聊ID2"],
    "inspect_interval": 30,
    "keywords": ["关键词1", "关键词2"]
}
```

**请求字段说明**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| ai_mode | array | 否 | AI行动模式列表 |
| scope | string | 否 | 启用范围，可选值："disabled", "global", "specific" |
| target_groups | array | 否 | 目标群聊/角色列表，当 scope 为 "specific" 时生效 |
| inspect_interval | integer | 否 | 定时巡检间隔（分钟），当 ai_mode 包含"定时巡检"时生效，可选值：5, 10, 15, 30, 60 |
| keywords | array | 否 | 唤醒关键词列表，当 ai_mode 包含"提及应答"时生效，消息包含这些关键词也会触发AI响应 |

**响应**：
```json
{
    "status": 0,
    "msg": "已更新: scope: specific, target_groups: ['群聊ID1', '群聊ID2']",
    "data": {
        "ai_mode": ["提及应答", "定时巡检"],
        "scope": "specific",
        "target_groups": ["群聊ID1", "群聊ID2"],
        "inspect_interval": 30,
        "keywords": ["关键词1", "关键词2"]
    }
}
```

**错误响应**（角色不存在）：
```json
{
    "status": 1,
    "msg": "角色 'xxx' 不存在",
    "data": null
}
```

**错误响应**（全局启用冲突）：
```json
{
    "status": 1,
    "msg": "无法设置为对所有群/角色启用，因为 '其他角色名' 已配置为全局启用",
    "data": null
}
```

**⚠️ 重要提示**：
> **全部人格中只能有一个配置为 "global"（对所有群/角色启用）**。如果尝试将多个角色同时设置为 "global"，后端会返回错误。
>
> 前端在设置 scope 为 "global" 时，应当：
> 1. 先调用 `GET /api/persona/config/global` 检查是否已有其他角色配置为全局启用
> 2. 如果存在冲突，提示用户先取消其他角色的全局启用设置
> 3. 或者提供切换功能，自动将其他角色的 scope 改为 "disabled" 或 "specific"

---

## 13.13 获取全局启用的角色

```
GET /api/persona/config/global
```

**请求头**：
```
Authorization: Bearer <token>
```

**响应**（存在全局启用的角色）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": "角色名称"
}
```

**响应**（没有全局启用的角色）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": null
}
```

---

## 13.14 获取所有角色配置

```
GET /api/persona/config/all
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
        "角色名1": {
            "ai_mode": ["提及应答", "定时巡检"],
            "scope": "global",
            "target_groups": [],
            "inspect_interval": 30,
            "keywords": ["关键词1", "关键词2"]
        },
        "角色名2": {
            "ai_mode": ["提及应答"],
            "scope": "specific",
            "target_groups": ["群聊ID1"],
            "inspect_interval": 15,
            "keywords": []
        }
    }
}
```
