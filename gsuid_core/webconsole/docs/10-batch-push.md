# 10. 消息推送 API - /api/BatchPush

## 10.1 批量推送
```
POST /api/BatchPush
```

**请求体**：
```json
{
    "push_text": "<p>推送内容</p><img src='base64,...'/>",
    "push_tag": "ALLUSER,ALLGROUP,g:123456|onebot,u:654321|onebot|3399214199",
    "push_bot": "ws-bot-a,ws-bot-b",
    "push_bot_self_id": "3399214199"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `push_text` | string | 是 | HTML 正文（解析 `<p>` / `<img>`） |
| `push_tag` | string | 是 | 逗号分隔的推送目标（见下） |
| `push_bot` | string | 否 | 逗号分隔的 **WS_BOT_ID**（`gss.active_bot` 的 key）；**空 = 全部 active bot** |
| `push_bot_self_id` | string | 否 | 逗号分隔的 **bot_self_id**（机器人账号 ID）。tag 未写第三段时回落到此值；空则传空串给适配器（兼容旧行为） |

**推送目标格式（`push_tag`）**：
- `ALLUSER`: 所有用户
- `ALLGROUP`: 所有群组
- `g:群ID|平台bot_id`: 指定群（平台维度）
- `u:用户ID|平台bot_id`: 指定用户（平台维度）
- `g:群ID|平台bot_id|bot_self_id`: 指定群 + 精确机器人账号
- `u:用户ID|平台bot_id|bot_self_id`: 指定用户 + 精确机器人账号

**精准控制维度**：
| 维度 | 字段 | 含义 |
|------|------|------|
| 连接 / Bot | `push_bot` | 走哪条 WS 连接（active_bot） |
| 平台 | tag 中的 `bot_id` 段 | 如 `onebot` / `telegram` |
| 机器人账号 | `push_bot_self_id` 或 tag 第三段 | 如 QQ 号 `3399214199` |
| 人 / 群 | tag 中的 `g:` / `u:` / `ALL*` | 发送对象 |

同一平台下有多个 `bot_self_id` 时，务必指定 `push_bot_self_id`（或 tag 第三段），否则适配器无法区分从哪个账号发出。

> **注意**：`push_bot_self_id` 为**多个**值且 `push_tag` 含 `ALLUSER` / `ALLGROUP` 时，会对每个 self_id **各发一遍**（N 倍流量）。生产环境请谨慎。

**实现要点**（`message_api.batch_push`）：

- 每条消息独立拷贝 `base_msg`，避免往共享 list 追加 `group` 段污染后续发送。
- 目标聚合维度：`platform_bot_id → bot_self_id → [target_id, ...]`。
- 空 `push_bot` = 遍历全部 `gss.active_bot`。

---

## 10.1.1 拉取可选目标（分页 + 筛选）
```
GET /api/BatchPush/targets
```

**Query**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `bot_id` | string | 按**平台** bot_id 过滤目标（空=全部） |
| `bot_self_id` | string | 过滤返回的 `bot_self_ids` 子集（目标表本身无此字段） |
| `kind` | string | `all` / `group` / `user` |
| `q` | string | 模糊搜索 label / value |
| `limit` | int | 1–1000，默认 200 |
| `offset` | int | 页偏移 |

**响应 `data`**：
```json
{
  "bots": [
    { "bot_id": "ws-xxx", "name": "ws-xxx", "ws_bot_id": "ws-xxx", "connected": true }
  ],
  "bot_self_ids": [
    {
      "id": "3399214199:onebot",
      "bot_id": "onebot",
      "bot_self_id": "3399214199",
      "label": "3399214199 (onebot)"
    }
  ],
  "items": [
    { "kind": "group", "bot_id": "onebot", "bot_self_id": "", "label": "onebot · 123", "value": "g:123|onebot" }
  ],
  "total": 1,
  "limit": 200,
  "offset": 0,
  "has_more": false
}
```

- `bots`：当前 active WS 连接（用于 `push_bot`）
- `bot_self_ids`：已知机器人账号实例（统计库 + 历史 session），用于 `push_bot_self_id` 选择器
- `items[].value` 默认为两段格式；前端在提交时可按所选账号追加 `|{bot_self_id}`

**前端 `/batch-push`（gsuid_hub）**：

| 控件 | 对应字段 |
|------|----------|
| 目标 Bot（WS） | `push_bot`；「全部」= 空串 → 后端发全部 active |
| 机器人账号 | `InputWithDropdown`：列表来自 `bot_self_ids`，**可手填**（纯 self_id / `self:platform` / `self (platform)`）→ `push_bot_self_id` |
| 群/用户多选 | `push_tag`；选中账号时非宏 tag 追加 `\|{bot_self_id}` |

列表为空时仍可手填 self_id；手填仅 self_id 时不按平台筛目标列表。

---

## 10.2 通用图片上传

```
POST /api/uploadImage/{suffix}/{filename}/{UPLOAD_PATH:path}
```

**描述**: 通用图片文件上传接口，允许向服务器指定的物理路径上传并保存图片文件。

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `suffix` | string | 否 | 文件后缀名，如 `jpg`、`png` |
| `filename` | string | 否 | 自定义文件名（不含后缀） |
| `UPLOAD_PATH` | string | 是 | 上传目标路径 |

**请求体**: `multipart/form-data`，包含 `file` 字段（图片文件）

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "上传成功",
    "data": {
        "filename": "20260514203000.jpg"
    }
}
```

---

## 10.3 通用图片读取

```
GET /api/getImage/{suffix}/{filename}/{IMAGE_PATH:path}
```

**描述**: 通用图片文件读取接口，从指定的物理路径读取并返回图片流。

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `suffix` | string | 是 | 文件后缀名 |
| `filename` | string | 是 | 文件名（不含后缀） |
| `IMAGE_PATH` | string | 是 | 图片所在路径 |

**响应**: 返回图片二进制流（Content-Type: image/jpeg）

---

## 10.4 图片资源读取（阅后即焚）

```
GET /api/image/{image_id}
```

**描述**: 从机器人的 `image_res` 缓存目录获取图片返回，内置异步定时删除（阅后即焚）功能。此接口**不需要认证**。

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `image_id` | string | 是 | 图片 ID |

**响应**: 返回图片二进制流（支持 JPEG、GIF 等格式）

**说明**：
- 如果配置了 `EnableCleanPicSrv` 为 `true`，图片在返回后会根据 `ScheduledCleanPicSrv` 配置的时间自动删除
- 支持 `.jpg`、`.gif` 等格式，GIF 会直接返回原始字节，其他格式会转换为 JPEG
