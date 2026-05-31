# 29 - 表情包管理 API (Meme)

> **模块**: `gsuid_core/webconsole/meme_api.py`
> **前缀**: `/api/meme`
> **认证**: 所有端点均需 `require_auth`

## 概述

表情包管理 API 提供完整的表情包增删改查、手动上传、重新打标、统计概览、批量删除、批量导出/导入等功能。
前端页面通过调用这些 REST API 实现管理界面，无需维护本地状态。

---

## API 清单

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/meme/list` | 列表查询 |
| GET | `/api/meme/{meme_id}` | 获取单条记录详情 |
| GET | `/api/meme/image/{meme_id}` | 获取原始图片文件 |
| PUT | `/api/meme/{meme_id}` | 更新标签/描述/归属 |
| POST | `/api/meme/{meme_id}/move` | 移动表情包到目标文件夹 |
| DELETE | `/api/meme/{meme_id}` | 删除表情包（文件+记录） |
| POST | `/api/meme/upload` | 手动上传表情包 |
| POST | `/api/meme/{meme_id}/retag` | 重新触发 VLM 打标 |
| GET | `/api/meme/stats` | 统计概览 |
| POST | `/api/meme/batch_delete` | 批量删除表情包 |
| POST | `/api/meme/purge_rejected` | 清除所有已拒绝的表情包 |
| POST | `/api/meme/batch_retag_pending` | 批量重新打标（待手动处理状态） |
| POST | `/api/meme/export` | 批量导出为 .meme 格式 |
| POST | `/api/meme/import` | 导入 .meme 格式文件 |

---

## 详细接口

### 1. 列表查询

```
GET /api/meme/list
```

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `folder` | string | 否 | - | 文件夹过滤，如 `common`, `persona_xxx` |
| `status` | string | 否 | - | 状态过滤：`pending`, `tagged`, `manual`, `pending_manual`, `rejected` |
| `sort` | string | 否 | `created_at_desc` | 排序方式：`created_at_desc`, `use_count_desc`, `use_count_asc` |
| `page` | int | 否 | `1` | 页码 |
| `page_size` | int | 否 | `20` | 每页数量 |
| `q` | string | 否 | - | 搜索关键词（语义向量检索） |

**响应**:

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "records": [
            {
                "meme_id": "ab12cd34ef56gh78",
                "file_path": "common/ab12cd34ef56gh78.webp",
                "file_size": 123456,
                "file_mime": "image/webp",
                "width": 300,
                "height": 300,
                "source_group": "",
                "folder": "common",
                "persona_hint": "common",
                "emotion_tags": ["搞笑", "无语"],
                "scene_tags": ["吐槽"],
                "description": "一只猫翻白眼",
                "custom_tags": [],
                "status": "tagged",
                "nsfw_score": 0.0,
                "use_count": 5,
                "last_used_at": "2026-05-03T12:00:00",
                "last_used_group": "123456",
                "created_at": "2026-05-01T08:00:00",
                "tagged_at": "2026-05-01T08:05:00",
                "updated_at": "2026-05-03T12:00:00"
            }
        ],
        "total": 100,
        "page": 1,
        "page_size": 20
    }
}
```

### 2. 获取单条记录详情

```
GET /api/meme/{meme_id}
```

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `meme_id` | string | 表情包 ID（sha256 前 16 位） |

**响应**: 同列表查询中的单条记录格式。

### 3. 获取原始图片文件

```
GET /api/meme/image/{meme_id}
```

**路径参数**: 同上。

**响应**: 返回图片二进制流，`Content-Type` 为图片 MIME 类型。

### 4. 更新标签/描述/归属

```
PUT /api/meme/{meme_id}
```

**请求体** (JSON):

```json
{
    "description": "新描述",
    "emotion_tags": ["开心", "搞笑"],
    "scene_tags": ["日常"],
    "custom_tags": ["猫咪"],
    "persona_hint": "common"
}
```

所有字段均为可选，只更新传入的字段。更新后状态自动设为 `manual`。

**响应**:

```json
{
    "status": 0,
    "msg": "更新成功",
    "data": null
}
```

### 5. 移动表情包到目标文件夹

```
POST /api/meme/{meme_id}/move
```

**表单参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `target_folder` | string | 是 | 目标文件夹名，如 `common`, `persona_xxx` |

**响应**:

```json
{
    "status": 0,
    "msg": "已移动到 common",
    "data": null
}
```

### 6. 删除表情包

```
DELETE /api/meme/{meme_id}
```

删除文件和数据库记录，同时删除 Qdrant 向量索引。

**响应**:

```json
{
    "status": 0,
    "msg": "删除成功",
    "data": null
}
```

### 7. 手动上传表情包

```
POST /api/meme/upload
```

**表单参数** (multipart/form-data):

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `file` | file | 是 | - | 图片文件 |
| `folder` | string | 否 | `common` | 目标文件夹 |
| `auto_tag` | bool | 否 | `true` | 是否自动触发 VLM 打标 |

**响应**:

```json
{
    "status": 0,
    "msg": "上传成功",
    "data": {
        "meme_id": "ab12cd34ef56gh78"
    }
}
```

### 8. 重新触发 VLM 打标

```
POST /api/meme/{meme_id}/retag
```

将记录状态重置为 `pending` 并加入打标队列。

**响应**:

```json
{
    "status": 0,
    "msg": "已加入打标队列",
    "data": null
}
```

### 9. 统计概览

```
GET /api/meme/stats
```

**响应**:

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "total": 500,
        "status_counts": {
            "pending": 10,
            "tagged": 400,
            "manual": 50,
            "pending_manual": 30,
            "rejected": 10
        },
        "folder_counts": {
            "inbox": 10,
            "common": 300,
            "persona_早柚": 100,
            "rejected": 10
        },
        "total_usage": 1234,
        "top_memes": [
            {
                "meme_id": "ab12cd34ef56gh78",
                "description": "一只猫翻白眼",
                "use_count": 50,
                "file_path": "common/ab12cd34ef56gh78.webp"
            }
        ]
    }
}
```

---

### 10. 批量删除表情包

```
POST /api/meme/batch_delete
```

批量删除多个表情包（文件+数据库记录+Qdrant 向量），逐条处理，返回成功/失败详情。

**请求体** (JSON):

```json
{
    "meme_ids": ["ab12cd34ef56gh78", "1234abcd5678efgh"]
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `meme_ids` | string[] | 是 | 要删除的表情包 ID 列表，至少 1 个 |

**响应**:

全部成功时：

```json
{
    "status": 0,
    "msg": "批量删除成功，共删除 2 个",
    "data": {
        "success_count": 2,
        "failed": []
    }
}
```

部分失败时：

```json
{
    "status": 1,
    "msg": "删除完成：成功 1 个，失败 1 个",
    "data": {
        "success_count": 1,
        "failed": [
            {"meme_id": "1234abcd5678efgh", "reason": "不存在"}
        ]
    }
}
```

### 10b. 清除所有已拒绝的表情包

```
POST /api/meme/purge_rejected
```

一键删除所有状态为 `rejected` 的表情包，包括源文件、数据库记录和 Qdrant 向量索引。无需请求体。

**响应**:

无已拒绝表情包时：

```json
{
    "status": 0,
    "msg": "没有已拒绝的表情包",
    "data": {
        "purged_count": 0,
        "failed": []
    }
}
```

全部成功时：

```json
{
    "status": 0,
    "msg": "已清除 15 个已拒绝的表情包",
    "data": {
        "purged_count": 15,
        "failed": []
    }
}
```

部分失败时：

```json
{
    "status": 1,
    "msg": "清除完成：成功 14 个，失败 1 个",
    "data": {
        "purged_count": 14,
        "failed": [
            {"meme_id": "xxx", "reason": "删除失败"}
        ]
    }
}
```

### 10c. 批量重新打标（待手动处理状态）

```
POST /api/meme/batch_retag_pending
```

一键将所有状态为 `pending_manual`（VLM 打标失败，待人工处理）的表情包重新加入 VLM 打标队列。无需请求体。

执行逻辑：将 `pending_manual` 状态的记录重置为 `pending`，并逐条调用打标队列入队。

**响应**:

无待手动处理的表情包时：

```json
{
    "status": 0,
    "msg": "没有待手动处理的表情包",
    "data": {
        "retag_count": 0,
        "failed": []
    }
}
```

全部成功时：

```json
{
    "status": 0,
    "msg": "已将 8 个待手动处理的表情包加入打标队列",
    "data": {
        "retag_count": 8,
        "failed": []
    }
}
```

部分失败时：

```json
{
    "status": 1,
    "msg": "操作完成：成功 7 个，失败 1 个",
    "data": {
        "retag_count": 7,
        "failed": [
            {"meme_id": "xxx", "reason": "入队失败"}
        ]
    }
}
```

### 11. 批量导出表情包（.meme 格式）

```
POST /api/meme/export
```

将选定的表情包打包为 `.meme` 格式文件（实际为 ZIP），包含源文件和元数据。

**请求体** (JSON):

```json
{
    "meme_ids": ["ab12cd34ef56gh78", "1234abcd5678efgh"]
}
```

或按文件夹导出：

```json
{
    "folder": "common"
}
```

或不传参数导出全部：

```json
{}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `meme_ids` | string[] | 否 | 要导出的表情包 ID 列表，为空则导出全部 |
| `folder` | string | 否 | 按文件夹导出（与 `meme_ids` 互斥，优先使用 `meme_ids`） |

**响应**: 返回 `.meme` 文件二进制流，`Content-Type` 为 `application/octet-stream`。

**响应头**:

```
Content-Disposition: attachment; filename="memes_20260530_093000.meme"
```

**错误响应** (无数据可导出时返回 JSON):

```json
{
    "status": 1,
    "msg": "没有可导出的表情包"
}
```

### 12. 导入 .meme 格式文件

```
POST /api/meme/import
```

导入 `.meme` 格式文件，解析其中的源文件和元数据，逐条写入文件系统和数据库。

**表单参数** (multipart/form-data):

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `file` | file | 是 | - | `.meme` 格式文件（ZIP） |
| `skip_existing` | bool | 否 | `true` | 是否跳过已存在的表情包 |
| `auto_tag` | bool | 否 | `false` | 是否对新导入的表情包触发 VLM 打标 |

**响应**:

```json
{
    "status": 0,
    "msg": "导入完成：成功 10 个，跳过 2 个，失败 0 个",
    "data": {
        "imported_count": 10,
        "skipped_count": 2,
        "imported_ids": ["ab12cd34ef56gh78", "..."],
        "skipped_ids": ["1234abcd5678efgh", "..."],
        "failed": []
    }
}
```

部分失败时 `failed` 列表包含失败详情：

```json
{
    "failed": [
        {"meme_id": "xxx", "reason": "包中缺少源文件: files/xxx.webp"}
    ]
}
```

---

## .meme 文件格式规范

`.meme` 文件本质为 ZIP 压缩包，扩展名为 `.meme`，用于表情包的完整导出与导入。

### 目录结构

```
memes_20260530_093000.meme
├── manifest.json      # 版本与导出信息
├── metadata.json      # 表情包元数据列表
└── files/             # 表情包源文件目录
    ├── ab12cd34ef56gh78.webp
    ├── 1234abcd5678efgh.jpg
    └── ...
```

### manifest.json

```json
{
    "version": "1.0",
    "exported_at": "2026-05-30T09:30:00+00:00",
    "total_count": 10
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `version` | string | 格式版本号，当前为 `"1.0"` |
| `exported_at` | string | 导出时间（ISO 8601） |
| `total_count` | int | 包含的表情包数量 |

### metadata.json

数组格式，每项对应一个表情包的元数据：

```json
[
    {
        "meme_id": "ab12cd34ef56gh78",
        "file_path": "common/ab12cd34ef56gh78.webp",
        "file_size": 123456,
        "file_mime": "image/webp",
        "width": 300,
        "height": 300,
        "folder": "common",
        "persona_hint": "common",
        "emotion_tags": ["搞笑", "无语"],
        "scene_tags": ["吐槽"],
        "description": "一只猫翻白眼",
        "custom_tags": [],
        "status": "tagged",
        "nsfw_score": 0.0
    }
]
```

> **注意**: `metadata.json` 中不包含运行时字段（`use_count`、`last_used_at`、`last_used_group`、`created_at`、`tagged_at`、`updated_at`、`qdrant_id`、`source_group`、`source_user`、`source_url`），这些字段在导入时会重置为默认值。

### files/ 目录

存放表情包源文件，文件名格式为 `{meme_id}.{ext}`，与 `metadata.json` 中的 `file_path` 字段对应。

### 版本兼容

- 当前版本：`1.0`
- 导入时校验 `manifest.json` 中的 `version` 字段，仅支持 `1.x` 版本

---

## 数据模型

### AiMemeRecord 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `meme_id` | string | 主键，sha256(图片内容)[:16] |
| `file_path` | string | 相对路径，如 `common/ab12cd34.webp` |
| `file_size` | int | 文件大小（字节） |
| `file_mime` | string | MIME 类型 |
| `width` | int | 图片宽度（px） |
| `height` | int | 图片高度（px） |
| `source_group` | string | 来源群组 ID（不对外暴露） |
| `folder` | string | 文件夹：`inbox`, `common`, `persona_{name}`, `rejected` |
| `persona_hint` | string | Persona 归属提示 |
| `emotion_tags` | string[] | 情绪标签 |
| `scene_tags` | string[] | 场景标签 |
| `description` | string | 图片描述 |
| `custom_tags` | string[] | 自定义标签 |
| `status` | string | 状态：`pending`, `tagged`, `manual`, `pending_manual`, `rejected` |
| `nsfw_score` | float | NSFW 分数（0~1） |
| `use_count` | int | 使用次数 |
| `last_used_at` | datetime | 最后使用时间 |
| `last_used_group` | string | 最后使用的群组 |
| `created_at` | datetime | 创建时间 |
| `tagged_at` | datetime | 打标完成时间 |
| `updated_at` | datetime | 最后更新时间 |

### 状态流转

```
pending → tagged (VLM 打标成功)
pending → pending_manual (VLM 打标失败)
pending_manual → tagged (重新打标成功)
pending_manual → manual (人工编辑)
tagged → manual (人工编辑)
any → rejected (NSFW 或质量不达标)
```

---

## 前端页面设计要点

1. **列表页**: 瀑布流图片网格，支持文件夹/状态过滤、排序（最新/发送次数）。每张卡片显示缩略图、简要描述、情绪标签、使用次数。支持关键字搜索（调用 `?q=xxx`）。支持多选模式，选中后可批量删除或批量导出。
2. **详情面板**: 大图预览，可编辑描述、情绪标签、场景标签、自定义标签、Persona 归属。显示使用统计（次数、最后使用时间、群）。提供移动文件夹、重新打标、删除操作。
3. **上传区**: 拖拽或点击上传，可选择目标文件夹，支持自动打标或手动输入标签。
4. **统计概览**: 展示总图片数、AI 发送总次数、待打标数、各文件夹分布（图表）。Top 10 最常用表情包（图片+次数）。
5. **批量操作**: 列表页多选后出现操作栏，支持批量删除（调用 `POST /api/meme/batch_delete`）和批量导出（调用 `POST /api/meme/export`）。导出时可选按文件夹导出或全量导出。
6. **导入导出**: 提供导入按钮，上传 `.meme` 文件（调用 `POST /api/meme/import`），可选跳过已存在项和自动打标。导出按钮下载 `.meme` 文件，包含源文件和标签元数据，可用于备份迁移。
