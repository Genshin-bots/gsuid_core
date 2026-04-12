# GsCore WebConsole 后端 API 设计文档

## 概述

GsCore WebConsole 提供基于 FastAPI 的 RESTful API，供前端 React 应用调用。所有 API 均以 `/api` 为前缀，采用 JSON 格式交互。

**认证方式**：除特殊说明外，所有 API 需通过 `Authorization: Bearer <token>` Header 携带访问令牌。

**通用响应格式**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {}
}
```
- `status`: 0=成功，1=失败，其他=错误码
- `msg`: 状态描述
- `data`: 响应数据

---

## 目录

1. [认证 API - /api/auth](#1-认证-api---apiauth)
2. [系统 API - /api/system](#2-系统-api---apisystem)
3. [插件 API - /api/plugins](#3-插件-api---apiplugins)
4. [核心配置 API - /api/core](#4-核心配置-api---apicore)
5. [数据库 API - /api/database](#5-数据库-api---apidatabase)
6. [备份 API - /api/backup](#6-备份-api---apibackup)
7. [日志 API - /api/logs](#7-日志-api---apilogs)
8. [调度器 API - /api/scheduler](#8-调度器-api---apischeduler)
9. [仪表盘 API - /api/dashboard](#9-仪表盘-api---apidashboard)
10. [消息推送 API - /api/BatchPush](#10-消息推送-api---apibatchpush)
11. [图片资源 API - /api/assets](#11-图片资源-api---apiassets)
12. [主题配置 API - /api/theme](#12-主题配置-api---apitheme)
13. [Persona API - /api/persona](#13-persona-api---apipersona)
14. [AI Tools API - /api/ai/tools](#14-ai-tools-api---apiai-tools)
15. [AI Skills API - /api/ai/skills](#15-ai-skills-api---apiaiskills)
16. [AI Knowledge Base API - /api/ai/knowledge](#16-ai-knowledge-base-api---apiai-knowledge)
17. [AI System Prompt API - /api/ai/system_prompt](#17-ai-system-prompt-api---apiaisystem_prompt)
18. [History Manager API - /api/history](#18-history-manager-api---apihistory)
19. [AI Image RAG API - /api/ai/images](#19-ai-image-rag-api---apiaiimages)
20. [AI Statistics API - /api/ai/statistics](#20-ai-statistics-api---apiaistatistics)

---

## 1. 认证 API - /api/auth

### 1.1 用户登录
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

### 1.2 用户注册
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

### 1.3 检查管理员是否存在
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

### 1.4 获取当前用户信息
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

### 1.5 更新用户信息
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

### 1.6 修改密码
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

### 1.7 上传头像
```
POST /api/auth/upload_avatar
Content-Type: multipart/form-data

file: [图片文件]
```

---

### 1.8 退出登录
```
POST /api/auth/logout
```

---

## 2. 系统 API - /api/system

### 2.1 获取系统信息
```
GET /api/system/info
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "version": "1.0.0",
        "python_version": "3.x",
        "uptime": "N/A"
    }
}
```

---

### 2.2 健康检查
```
GET /api/system/health
```
**无需认证**

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "status": "healthy"
    }
}
```

---

### 2.3 重启核心服务
```
POST /api/system/restart
```

---

### 2.4 停止核心服务
```
POST /api/system/stop
```

---

### 2.5 恢复核心服务
```
POST /api/system/resume
```

---

## 3. 插件 API - /api/plugins

### 3.1 获取插件列表
```
GET /api/plugins/list
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "id": "plugin_name",
            "name": "插件名称",
            "description": "插件描述",
            "enabled": true,
            "status": "running",
            "icon": "base64..."
        }
    ]
}
```

---

### 3.2 获取插件详情
```
GET /api/plugins/{plugin_name}
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "plugin_name",
        "name": "插件名称",
        "description": "...",
        "enabled": true,
        "icon": "base64...",
        "config": {
            "config_key": {
                "value": {},
                "default": {},
                "type": "string",
                "title": "配置项",
                "desc": "描述"
            }
        },
        "config_groups": [...],
        "service": {
            "enabled": true,
            "pm": 6,
            "priority": 5,
            "area": "ALL",
            "black_list": [],
            "white_list": [],
            "prefix": [],
            "force_prefix": []
        },
        "sv_list": [
            {
                "name": "服务名称",
                "enabled": true,
                "pm": 6,
                "priority": 5,
                "area": "GROUP",
                "black_list": [],
                "white_list": [],
                "commands": [
                    {
                        "type": "command",
                        "keyword": "帮助",
                        "block": false,
                        "to_me": false
                    },
                    {
                        "type": "prefix",
                        "keyword": "我的",
                        "block": false,
                        "to_me": false
                    },
                    {
                        "type": "keyword",
                        "keyword": "原石",
                        "block": false,
                        "to_me": false
                    },
                    {
                        "type": "regex",
                        "keyword": ".*原石.*",
                        "block": false,
                        "to_me": false
                    }
                ]
            }
        ]
    }
}
```

> **前端调用说明**：前端可以通过遍历 `data.sv_list` 获取每个服务（SV），每个服务的 `commands` 数组包含了该服务下所有触发器的信息，可用于渲染命令列表。
>
> **commands 字段说明**：
> - `type`: 触发器类型，可选值: `"command"`(命令), `"prefix"`(前缀匹配), `"suffix"`(后缀匹配), `"keyword"`(关键字匹配), `"fullmatch"`(完全匹配), `"regex"`(正则匹配), `"file"`(文件类型), `"message"`(消息)
> - `keyword`: 触发关键字/正则表达式
> - `block`: 是否阻止后续触发
> - `to_me`: 是否仅响应 @ 机器人

---

### 3.3 获取插件配置
```
GET /api/plugins/{plugin_name}/config
```

---

### 3.4 保存插件配置
```
POST /api/plugins/{plugin_name}/config
```

**请求体**：插件配置键值对

---

### 3.5 更新插件服务配置
```
POST /api/plugins/{plugin_name}/service
```

**请求体**：
```json
{
    "enabled": true,
    "pm": 6,
    "priority": 5,
    "area": "ALL",
    "black_list": [],
    "white_list": [],
    "prefix": [],
    "force_prefix": []
}
```

---

### 3.6 切换插件开关
```
POST /api/plugins/{plugin_name}/toggle
```

---

### 3.7 重新加载插件
```
POST /api/plugins/{plugin_name}/reload
```

---

### 3.8 安装插件
```
POST /api/plugins/install
```

**请求体**：
```json
{
    "plugin_name": "插件名",
    "plugin_version": "1.0.0",
    "repo_url": "https://..."
}
```

---

### 3.9 卸载插件
```
DELETE /api/plugins/{plugin_name}
```

---

### 3.10 获取插件市场
```
GET /api/plugins/market
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [...]
}
```

---

### 3.11 检查插件更新
```
GET /api/plugins/{plugin_name}/update
```

---

## 4. 核心配置 API - /api/core

### 4.1 获取核心配置
```
GET /api/core/config
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "HOST": "localhost",
        "PORT": "8765",
        "ENABLE_HTTP": false,
        "WS_TOKEN": "",
        "masters": [],
        "superusers": [],
        "command_start": [],
        "enable_empty_start": true,
        "log": {...}
    }
}
```

---

### 4.2 保存核心配置
```
POST /api/core/config
```

**请求体**：核心配置键值对

---

## 5. 数据库 API - /api/database

### 5.1 获取所有插件数据库
```
GET /api/database/plugins
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "plugin_id": "xxx",
            "plugin_name": "xxx",
            "tables": [...],
            "icon": "base64..."
        }
    ]
}
```

---

### 5.2 获取插件表信息
```
GET /api/database/{plugin_id}/tables
```

---

### 5.3 获取表元数据
```
GET /api/database/table/{table_name}
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "columns": [
            {"name": "id", "type": "INTEGER"},
            {"name": "user_id", "type": "TEXT"}
        ]
    }
}
```

---

### 5.4 获取表数据（分页）
```
GET /api/database/table/{table_name}/data
```

**Query 参数**：
- `page`: 页码，默认1
- `per_page`: 每页数量，默认20
- `search`: 搜索关键字
- `search_columns`: 搜索列（逗号分隔）
- `filter_columns`: 过滤列（逗号分隔）
- `filter_values`: 过滤值（逗号分隔）

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [...],
        "total": 100,
        "page": 1,
        "per_page": 20
    }
}
```

---

### 5.5 创建记录
```
POST /api/database/table/{table_name}/data
```

**请求体**：记录数据

---

### 5.6 更新记录
```
PUT /api/database/table/{table_name}/data/{id}
```

**请求体**：更新后的数据

---

### 5.7 删除记录
```
DELETE /api/database/table/{table_name}/data/{id}
```

---

## 6. 备份 API - /api/backup

### 6.1 获取备份文件列表
```
GET /api/backup/files
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "fileName": "backup_20240101.zip",
            "downloadUrl": "/api/backup/download?file_id=backup_20240101.zip",
            "deleteUrl": "/api/backup/backup_20240101.zip",
            "size": 1234567,
            "created": "2024-01-01T00:00:00"
        }
    ]
}
```

---

### 6.2 创建备份
```
POST /api/backup/create
```

---

### 6.3 下载备份文件
```
GET /api/backup/download?file_id=xxx
```

---

### 6.4 删除备份文件
```
DELETE /api/backup/{file_id}
```

---

### 6.5 获取备份配置
```
GET /api/backup/config
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "backup_time": "02:00",
        "backup_dir": ["config", "data"],
        "backup_method": ["local"],
        "webdav_url": "",
        "webdav_username": "",
        "webdav_password": ""
    }
}
```

---

### 6.6 保存备份配置
```
POST /api/backup/config
```

**请求体**：
```json
{
    "backup_time": "03:00",
    "backup_dir": ["config", "data"],
    "backup_method": ["local", "webdav"],
    "webdav_url": "https://...",
    "webdav_username": "user",
    "webdav_password": "pass"
}
```

---

## 7. 日志 API - /api/logs

### 7.1 获取日志列表
```
GET /api/logs
```

**Query 参数**：
- `date`: 日期 YYYY-MM-DD，默认今天
- `level`: 级别筛选 (info/warn/error/debug)
- `source`: 来源筛选 (api/auth/database/scheduler/core)
- `page`: 页码，默认1
- `per_page`: 每页数量，默认50

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "count": 100,
        "rows": [
            {
                "id": 1,
                "timestamp": "2024-01-01 12:00:00",
                "level": "info",
                "source": "core",
                "message": "日志内容",
                "details": null
            }
        ],
        "page": 1,
        "per_page": 50
    }
}
```

---

### 7.2 获取可用日期列表
```
GET /api/logs/available-dates
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": ["2024-01-01", "2023-12-31"]
}
```

---

### 7.3 获取日志来源
```
GET /api/logs/sources
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": ["api", "auth", "database", "scheduler", "core"]
}
```

---

### 7.4 获取日志统计
```
GET /api/logs/stats
```

**Query 参数**：同 7.1

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "total": 100,
        "pages": 2,
        "count_by_level": {
            "info": 80,
            "warn": 15,
            "error": 5
        }
    }
}
```

---

## 8. 调度器 API - /api/scheduler

### 8.1 获取任务列表
```
GET /api/scheduler/jobs
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "id": "job_id",
            "name": "任务名称",
            "description": "任务描述",
            "next_run_time": "2024-01-01 12:00:00",
            "trigger": "date",
            "trigger_description": "...",
            "paused": false
        }
    ]
}
```

---

### 8.2 手动触发任务
```
POST /api/scheduler/jobs/{job_id}/run
```

---

### 8.3 删除任务
```
DELETE /api/scheduler/jobs/{job_id}
```

---

### 8.4 暂停任务
```
POST /api/scheduler/jobs/{job_id}/pause
```

---

### 8.5 恢复任务
```
POST /api/scheduler/jobs/{job_id}/resume
```

---

## 9. 仪表盘 API - /api/dashboard

### 9.1 获取关键指标
```
GET /api/dashboard/metrics
```

**Query 参数**：
- `bot_id`: Bot ID 筛选，格式 `bot_self_id:bot_id` 或 `all`

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "dau": 100,
        "dag": 50,
        "mau": 1000,
        "mag": 500,
        "retention": "10%",
        "newUsers": 20,
        "churnedUsers": 5,
        "dauMauRatio": "10",
        "dagMagRatio": "10"
    }
}
```

---

### 9.2 获取命令统计
```
GET /api/dashboard/commands
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "date": "2024-01-01",
            "sentCommands": 500,
            "receivedCommands": 1000,
            "commandCalls": 800,
            "imageGenerated": 100
        }
    ]
}
```

---

### 9.3 获取用户群组数据
```
GET /api/dashboard/users-groups
```

**响应**：30天用户/群组变化趋势数据

---

### 9.4 获取命令排行榜
```
GET /api/dashboard/commands/ranking
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "command": "帮助",
            "count": 1000
        }
    ]
}
```

---

### 9.5 获取活跃时段数据
```
GET /api/dashboard/active-time
```

**响应**：24小时各时段活跃度数据

---

## 10. 消息推送 API - /api/BatchPush

### 10.1 批量推送
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

---

## 11. 图片资源 API - /api/assets

### 11.1 上传图片
```
POST /api/assets/upload
```

**请求体**：
```json
{
    "image": "base64编码数据",
    "filename": "image.jpg",
    "upload_to": "/path/to/save",
    "target_filename": "custom_name.jpg"
}
```

**响应**：
```json
{
    "status": 0,
    "msg": "上传成功",
    "data": {
        "path": "/absolute/path/to/image.jpg",
        "url": "/api/assets/preview?path=base64encoded"
    }
}
```

---

### 11.2 预览图片
```
GET /api/assets/preview?path=base64encoded
```
**可选 token 参数**

---

### 11.3 删除图片
```
DELETE /api/assets/delete
```

**Query 参数**：`path`: URL 编码的文件路径

---

### 11.4 上传图片（文件）
```
POST /api/uploadImage/{suffix}/{filename}/{UPLOAD_PATH:path}
```

**Form Data**：`file`: 图片文件

---

### 11.5 获取图片
```
GET /api/getImage/{suffix}/{filename}/{IMAGE_PATH:path}
```

---

### 11.6 阅后即焚图片
```
GET /api/tempImage/{image_id}
```

---

## 12. 主题配置 API - /api/theme

### 12.1 获取主题配置
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

### 12.2 保存主题配置
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

---

## 附录

### A. 错误码说明

| 错误码 | 说明 |
|--------|------|
| 0 | 成功 |
| 1 | 失败 |
| 400 | 请求参数错误 |
| 401 | 未授权/Token 无效 |
| 403 | 权限不足 |
| 404 | 资源不存在 |
| 500 | 服务器内部错误 |

### B. 用户角色

| 角色 | 权限 |
|------|------|
| admin | 完全访问权限 |
| user | 基本访问权限 |

### C. 权限等级 (user_pm)

| 等级 | 说明 |
|------|------|
| 0 | 主人 (masters) |
| 1 | 超级用户 (superusers) |
| 2 | 普通用户 |
| 3+ | 受限用户 |

---

## 13. Persona API - /api/persona

### 13.1 获取角色列表

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

### 13.2 获取角色详情

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

### 13.3 获取角色头像

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

### 13.4 获取角色立绘

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

### 13.5 获取角色音频

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

### 13.6 上传角色头像

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

### 13.7 上传角色立绘

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

### 13.8 上传角色音频

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

### 13.9 创建新角色

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

### 13.10 删除角色

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

### 13.11 获取角色配置

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

### 13.12 更新角色配置

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

### 13.13 获取全局启用的角色

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

### 13.14 获取所有角色配置

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

---

## 14. AI Tools API - /api/ai/tools

### 14.1 获取 AI 工具列表

```
GET /api/ai/tools/list
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
        "tools": {
            "core": [
                {
                    "name": "search_knowledge",
                    "description": "检索知识库相关内容..."
                }
            ],
            "_RH_ComfyUI": [
                {
                    "name": "gen_image_by_text",
                    "description": "通过文本生成图片..."
                },
                {
                    "name": "gen_music",
                    "description": "生成音乐..."
                }
            ]
        },
        "count": 10
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功 |
| msg | string | 状态信息 |
| data.tools | object | 工具字典，按插件名称分组 |
| data.tools\[插件名\] | array | 该插件下的工具列表 |
| data.tools\[插件名\]\[].name | string | 工具名称 |
| data.tools\[插件名\]\[].description | string | 工具描述（docstring） |
| data.count | integer | 工具总数 |

---

### 14.2 获取指定工具详情

```
GET /api/ai/tools/{tool_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| tool_name | string | 工具名称 |

**响应（工具存在）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "search_knowledge",
        "description": "检索知识库相关内容...",
        "plugin": "core"
    }
}
```

**错误响应（工具不存在）**：
```json
{
    "status": 1,
    "msg": "Tool 'xxx' not found",
    "data": null
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |
| data.name | string | 工具名称 |
| data.description | string | 工具描述（docstring） |
| data.plugin | string | 所属插件名称，core表示核心模块 |

---

## 15. AI Skills API - /api/ai/skills

### 15.1 获取 AI 技能列表

```
GET /api/ai/skills/list
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
        "skills": [
            {
                "name": "summarize",
                "description": "Summarize URLs or files with the summarize CLI...",
                "content": "# Summarize\n\nFast CLI to summarize URLs...",
                "license": null,
                "compatibility": null,
                "uri": "F:\\gsuid_core\\data\\ai_core\\skills\\summarize",
                "metadata": {
                    "homepage": "https://summarize.sh"
                }
            }
        ],
        "count": 1
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功 |
| msg | string | 状态信息 |
| data.skills | array | 技能列表 |
| data.skills[].name | string | 技能名称 |
| data.skills[].description | string | 技能描述 |
| data.skills[].content | string | 技能内容（markdown格式） |
| data.skills[].license | string/null | 许可证信息 |
| data.skills[].compatibility | string/null | 兼容性要求 |
| data.skills[].uri | string | 技能目录路径 |
| data.skills[].metadata | object | 技能元数据 |
| data.count | integer | 技能总数 |

---

### 15.2 获取指定技能详情

```
GET /api/ai/skills/{skill_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| skill_name | string | 技能名称 |

**响应（技能存在）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "summarize",
        "description": "Summarize URLs or files with the summarize CLI...",
        "content": "# Summarize\n\nFast CLI to summarize URLs...",
        "license": null,
        "compatibility": null,
        "uri": "F:\\gsuid_core\\data\\ai_core\\skills\\summarize",
        "metadata": {
            "homepage": "https://summarize.sh"
        },
        "resources": [
            {
                "name": "_meta.json",
                "description": null,
                "uri": "F:\\gsuid_core\\data\\ai_core\\skills\\summarize\\_meta.json"
            }
        ],
        "scripts": []
    }
}
```

**错误响应（技能不存在）**：
```json
{
    "status": 1,
    "msg": "Skill 'xxx' not found",
    "data": null
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |
| data.name | string | 技能名称 |
| data.description | string | 技能描述 |
| data.content | string | 技能内容（markdown格式） |
| data.license | string/null | 许可证信息 |
| data.compatibility | string/null | 兼容性要求 |
| data.uri | string | 技能目录路径 |
| data.metadata | object | 技能元数据 |
| data.resources | array | 技能资源列表 |
| data.resources[].name | string | 资源名称 |
| data.resources[].description | string/null | 资源描述 |
| data.resources[].uri | string | 资源路径 |
| data.scripts | array | 技能脚本列表 |
| data.scripts[].name | string | 脚本名称 |
| data.scripts[].description | string/null | 脚本描述 |
| data.scripts[].uri | string/null | 脚本路径 |

---

### 15.3 删除 AI 技能

```
DELETE /api/ai/skills/{skill_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| skill_name | string | 技能名称 |

**响应（成功）**：
```json
{
    "status": 0,
    "msg": "Skill 'xxx' deleted successfully"
}
```

**错误响应（技能不存在）**：
```json
{
    "status": 1,
    "msg": "Skill 'xxx' not found"
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |

---

### 15.4 从 Git 克隆 AI 技能

```
POST /api/ai/skills/clone
```

**请求头**：
```
Authorization: Bearer <token>
```

**请求体**：
```json
{
    "git_url": "https://github.com/user/skill-repo.git",
    "skill_name": "optional-custom-name"
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| git_url | string | 是 | Git 仓库 URL |
| skill_name | string | 否 | 自定义技能名称，不提供则使用仓库名 |

**响应（成功）**：
```json
{
    "status": 0,
    "msg": "Skill 'skill-repo' cloned successfully",
    "skill_name": "skill-repo"
}
```

**错误响应（技能已存在）**：
```json
{
    "status": 1,
    "msg": "Skill 'xxx' already exists"
}
```

**错误响应（Git 克隆失败）**：
```json
{
    "status": 1,
    "msg": "Git clone failed: error message"
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |
| skill_name | string | 克隆后的技能名称（仅成功时返回） |

---

### 15.5 获取 AI 技能 Markdown 内容

```
GET /api/ai/skills/{skill_name}/markdown
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| skill_name | string | 技能名称 |

**响应（成功）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "skill_name": "summarize",
        "content": "# Summarize\n\nSkill description...",
        "path": "F:\\gsuid_core\\data\\ai_core\\skills\\summarize\\SKILL.md"
    }
}
```

**错误响应（技能不存在）**：
```json
{
    "status": 1,
    "msg": "Skill 'xxx' not found",
    "data": null
}
```

**错误响应（Markdown 文件不存在）**：
```json
{
    "status": 1,
    "msg": "Markdown file not found for skill 'xxx'",
    "data": null
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |
| data.skill_name | string | 技能名称 |
| data.content | string | Markdown 文件内容 |
| data.path | string | Markdown 文件完整路径 |

---

### 15.6 更新 AI 技能 Markdown 内容

```
PUT /api/ai/skills/{skill_name}/markdown
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| skill_name | string | 技能名称 |

**请求体**：
```json
{
    "content": "# Updated Skill Name\n\nUpdated description..."
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| content | string | 是 | 新的 Markdown 内容 |

**响应（成功）**：
```json
{
    "status": 0,
    "msg": "Skill 'xxx' markdown updated successfully"
}
```

**错误响应（技能不存在）**：
```json
{
    "status": 1,
    "msg": "Skill 'xxx' not found"
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |

---

## 16. AI Knowledge Base API - /api/ai/knowledge

> 知识库 API 用于管理手动添加的知识库条目。通过此接口添加的知识不会在框架启动时被插件同步流程检查、修改或删除。

### 16.1 获取知识库列表（分页）

```
GET /api/ai/knowledge/list
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| offset | integer | 否 | 0 | 起始偏移量（会被page参数覆盖） |
| limit | integer | 否 | 20 | 每页数量 |
| source | string | 否 | all | 来源过滤，"all"表示所有知识，"plugin"只查插件添加的，"manual"只查手动添加的 |
| page | integer | 否 | 1 | 页码，从1开始，例如page=2表示第二页（offset=20） |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "list": [
            {
                "id": "manual_001",
                "plugin": "manual",
                "title": "手动添加的知识",
                "content": "这是手动添加的知识内容...",
                "tags": ["手动", "自定义"],
                "source": "manual"
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 20,
        "next_offset": null
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功 |
| msg | string | 状态信息 |
| data.list | array | 知识列表 |
| data.list[].id | string | 知识 ID |
| data.list[].plugin | string | 所属插件/来源 |
| data.list[].title | string | 知识标题 |
| data.list[].content | string | 知识内容 |
| data.list[].tags | array | 知识标签 |
| data.list[].source | string | 来源标识，"manual"表示手动添加 |
| data.total | integer | 知识总数 |
| data.offset | integer | 当前偏移量 |
| data.limit | integer | 每页数量 |
| data.next_offset | integer/null | 下一页偏移量，null表示没有更多 |
| data.page | integer | 当前页码 |
| data.page_size | integer | 每页数量 |

---

### 16.2 获取知识详情

```
GET /api/ai/knowledge/{entity_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| entity_id | string | 知识 ID |

**响应（知识存在）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "manual_001",
        "plugin": "manual",
        "title": "手动添加的知识",
        "content": "这是手动添加的知识内容...",
        "tags": ["手动", "自定义"],
        "source": "manual"
    }
}
```

**错误响应（知识不存在）**：
```json
{
    "status": 1,
    "msg": "Knowledge 'manual_001' not found",
    "data": null
}
```

---

### 16.3 新增知识

```
POST /api/ai/knowledge
```

**请求头**：
```
Authorization: Bearer <token>
```

**请求体**：
```json
{
    "plugin": "manual",
    "title": "手动添加的知识",
    "content": "这是手动添加的知识内容...",
    "tags": ["手动", "自定义"]
}
```

**请求字段说明**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| plugin | string | 否 | 所属插件，默认"manual" |
| title | string | 是 | 知识标题 |
| content | string | 是 | 知识内容 |
| tags | array | 是 | 知识标签列表 |

> 注意：id 由后端自动生成，无需传入。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "manual_001",
        "title": "手动添加的知识"
    }
}
```

**错误响应（添加失败）**：
```json
{
    "status": 1,
    "msg": "Failed to add knowledge to database",
    "data": null
}
```

---

### 16.4 更新知识

```
PUT /api/ai/knowledge/{entity_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| entity_id | string | 知识 ID |

**请求体**：
```json
{
    "title": "更新后的标题",
    "content": "更新后的内容...",
    "tags": ["更新", "标签"]
}
```

> 注意：id 和 source 字段不允许修改，只会更新提供的字段。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "manual_001"
    }
}
```

**错误响应（知识不存在）**：
```json
{
    "status": 1,
    "msg": "Knowledge 'manual_001' not found or update failed",
    "data": null
}
```

---

### 16.5 删除知识

```
DELETE /api/ai/knowledge/{entity_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| entity_id | string | 知识 ID |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "manual_001"
    }
}
```

**错误响应（知识不存在）**：
```json
{
    "status": 1,
    "msg": "Knowledge 'manual_001' not found or delete failed",
    "data": null
}
```

---

### 16.6 搜索知识

```
GET /api/ai/knowledge/search
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 查询文本 |
| limit | integer | 否 | 10 | 返回数量限制 |
| source | string | 否 | all | 来源过滤，"all"表示所有知识，"plugin"只搜插件添加的，"manual"只搜手动添加的 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "results": [
            {
                "id": "manual_001",
                "plugin": "manual",
                "title": "手动添加的知识",
                "content": "这是手动添加的知识内容...",
                "tags": ["手动", "自定义"],
                "source": "manual"
            }
        ],
        "count": 1,
        "query": "关键词"
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.results | array | 匹配的知识列表 |
| data.count | integer | 匹配数量 |
| data.query | string | 查询文本 |

---

## 17. AI System Prompt API - /api/ai/system_prompt

> System Prompt API 用于管理系统提示词，支持向量检索。可以让AI根据任务描述自动匹配合适的System Prompt创建子Agent完成任务。

### 17.1 获取System Prompt列表（分页）

```
GET /api/ai/system_prompt/list
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| offset | integer | 否 | 0 | 起始偏移量（会被page参数覆盖） |
| limit | integer | 否 | 20 | 每页数量 |
| page | integer | 否 | 1 | 页码，从1开始 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "list": [
            {
                "id": "sp_001",
                "title": "代码专家",
                "desc": "擅长编写各种编程语言的代码",
                "content": "你是一个专业的程序员，擅长编写高质量的代码...",
                "tags": ["代码", "编程", "专家"]
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 20,
        "page": 1,
        "page_size": 20
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.list | array | System Prompt列表 |
| data.list[].id | string | 唯一标识 |
| data.list[].title | string | 标题 |
| data.list[].desc | string | 描述（用于向量检索） |
| data.list[].content | string | 完整内容（作为系统提示词） |
| data.list[].tags | array | 标签列表 |
| data.total | integer | 总数 |
| data.offset | integer | 当前偏移量 |
| data.limit | integer | 每页数量 |
| data.page | integer | 当前页码 |
| data.page_size | integer | 每页数量 |

---

### 17.2 获取System Prompt详情

```
GET /api/ai/system_prompt/{prompt_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| prompt_id | string | System Prompt ID |

**响应（存在）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "sp_001",
        "title": "代码专家",
        "desc": "擅长编写各种编程语言的代码",
        "content": "你是一个专业的程序员，擅长编写高质量的代码...",
        "tags": ["代码", "编程", "专家"]
    }
}
```

**错误响应（不存在）**：
```json
{
    "status": 1,
    "msg": "System Prompt 'sp_001' not found",
    "data": null
}
```

---

### 17.3 新增System Prompt

```
POST /api/ai/system_prompt
```

**请求头**：
```
Authorization: Bearer <token>
```

**请求体**：
```json
{
    "title": "代码专家",
    "desc": "擅长编写各种编程语言的代码",
    "content": "你是一个专业的程序员，擅长编写高质量的代码...",
    "tags": ["代码", "编程", "专家"]
}
```

**请求字段说明**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| title | string | 是 | 标题 |
| desc | string | 是 | 描述（用于向量检索匹配） |
| content | string | 是 | 完整内容（将作为系统提示词） |
| tags | array | 是 | 标签列表 |

> 注意：id 由后端自动生成（UUID），无需传入。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "title": "代码专家"
    }
}
```

---

### 17.4 更新System Prompt

```
PUT /api/ai/system_prompt/{prompt_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| prompt_id | string | System Prompt ID |

**请求体**：
```json
{
    "title": "更新后的标题",
    "desc": "更新后的描述",
    "content": "更新后的内容...",
    "tags": ["新标签"]
}
```

> 注意：只更新传入的字段，空字符串或空数组的字段会被忽略。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "sp_001"
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "System Prompt 'sp_001' not found or update failed",
    "data": null
}
```

---

### 17.5 删除System Prompt

```
DELETE /api/ai/system_prompt/{prompt_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| prompt_id | string | System Prompt ID |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "sp_001"
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "System Prompt 'sp_001' not found or delete failed",
    "data": null
}
```

---

### 17.6 搜索System Prompt

```
GET /api/ai/system_prompt/search
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 查询文本 |
| tags | string | 否 | - | 逗号分隔的标签列表，如 "代码,编程" |
| limit | integer | 否 | 10 | 返回数量限制 |
| use_vector | boolean | 否 | true | 是否使用向量检索，false则使用简单文本匹配 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "results": [
            {
                "id": "sp_001",
                "title": "代码专家",
                "desc": "擅长编写各种编程语言的代码",
                "content": "你是一个专业的程序员，擅长编写高质量的代码...",
                "tags": ["代码", "编程", "专家"]
            }
        ],
        "count": 1,
        "query": "写一个Python排序函数"
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.results | array | 匹配的System Prompt列表（按相似度排序） |
| data.count | integer | 匹配数量 |
| data.query | string | 查询文本 |

---

## 18. History Manager API - /api/history

> History Manager API 用于管理 AI 会话的历史记录，支持查看、清空 session 历史以及查看 session 使用的 persona。

### 18.1 获取所有 Session 列表

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

### 18.2 获取指定 Session 的历史记录

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
|------|------|------|--------|------|
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

### 18.3 清空指定 Session 的历史记录

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
|------|------|------|--------|------|
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

### 18.4 获取指定 Session 的 Persona 内容

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

### 18.5 获取历史管理器统计信息

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

### Session ID 格式说明

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

---

## 19. AI Image RAG API - /api/ai/images

> 图片 RAG API 用于管理通过向量检索的图片。图片通过插件注册或前端上传，存储在独立的向量集合中，支持基于语义的图片搜索。

### 19.1 上传图片

```
POST /api/ai/images/upload
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

**请求体**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file | file | 是 | 图片文件 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "filename": "a1b2c3d4.png",
        "path": "C:/.../gsuid_core/data/local_embedding_images/a1b2c3d4.png",
        "relative_path": "data/local_embedding_images/a1b2c3d4.png"
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.filename | string | 生成的唯一文件名 |
| data.path | string | 图片的绝对路径 |
| data.relative_path | string | 相对路径，可用于入库 |

---

### 19.2 创建图片实体（入库）

```
POST /api/ai/images
```

**请求头**：
```
Authorization: Bearer <token>
Content-Type: application/x-www-form-urlencoded
```

**请求参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| id | string | 否 | 自动生成 | 图片唯一标识 |
| plugin | string | 否 | manual | 插件名 |
| path | string | 是 | - | 图片路径（上传接口返回的path） |
| tags | string | 是 | - | 标签，多个用逗号分隔，如"胡桃,原神,角色" |
| content | string | 否 | - | 详细描述文本 |

**请求示例**：
```
POST /api/ai/images
Content-Type: application/x-www-form-urlencoded

id=hutao_001&plugin=manual&path=C%3A%2F...%2Fa1b2c3d4.png&tags=%E8%83%A1%E6%A1%83%2C%E5%8E%9F%E7%A5%9E%2C%E8%A7%92%E8%89%B2&content=%E8%83%A1%E6%A1%83%E8%A7%92%E8%89%B2%E7%AB%8B%E7%BB%98
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "hutao_001",
        "path": "C:/.../gsuid_core/data/local_embedding_images/a1b2c3d4.png",
        "tags": ["胡桃", "原神", "角色"]
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "Failed to add image to vector database",
    "data": null
}
```

---

### 19.3 获取图片列表（分页）

```
GET /api/ai/images/list
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| offset | integer | 否 | 0 | 起始偏移量（会被page参数覆盖） |
| limit | integer | 否 | 20 | 每页数量 |
| plugin | string | 否 | - | 按插件名过滤 |
| page | integer | 否 | 1 | 页码，从1开始 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "list": [
            {
                "id": "hutao_character",
                "plugin": "GenshinUID",
                "path": "./resources/characters/hutao.png",
                "tags": ["胡桃", "原神", "角色", "火系"],
                "content": "胡桃角色立绘图片",
                "source": "plugin"
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 20,
        "next_offset": null,
        "page": 1,
        "page_size": 20
    }
}
```

---

### 19.4 搜索图片

```
GET /api/ai/images/search
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 查询文本（描述想要找的图片内容） |
| limit | integer | 否 | 10 | 返回数量限制 |
| plugin | string | 否 | - | 按插件名过滤 |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "results": [
            {
                "id": "hutao_character",
                "plugin": "GenshinUID",
                "path": "./resources/characters/hutao.png",
                "tags": ["胡桃", "原神", "角色"],
                "content": "胡桃角色立绘图片",
                "score": 0.95
            }
        ],
        "count": 1,
        "query": "胡桃图片"
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| data.results | array | 匹配的图片列表 |
| data.results[].id | string | 图片 ID |
| data.results[].plugin | string | 所属插件 |
| data.results[].path | string | 图片文件路径 |
| data.results[].tags | array | 图片标签 |
| data.results[].content | string | 图片描述 |
| data.results[].score | float | 匹配分数（0-1，越高越匹配） |
| data.count | integer | 结果数量 |
| data.query | string | 查询文本 |

---

### 19.5 获取最佳匹配图片路径

```
GET /api/ai/images/path
```

**请求头**：
```
Authorization: Bearer <token>
```

**查询参数**：
| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 查询文本 |
| plugin | string | 否 | - | 按插件名过滤 |

**响应（找到图片）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "path": "./resources/characters/hutao.png"
    }
}
```

**响应（未找到图片）**：
```json
{
    "status": 1,
    "msg": "No matching image found",
    "data": null
}
```

---

### 19.6 删除图片

```
DELETE /api/ai/images/{entity_id}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| entity_id | string | 图片 ID |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "hutao_character"
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "Image 'hutao_character' not found or delete failed",
    "data": null
}
```

---

### 19.7 插件注册图片示例

插件开发者可以通过以下方式注册图片：

```python
from gsuid_core.ai_core.models import ImageEntity
from gsuid_core.ai_core.register import ai_image

# 注册图片
ai_image(ImageEntity(
    id="hutao_character",
    plugin="GenshinUID",
    path="./resources/characters/hutao.png",
    tags=["胡桃", "原神", "角色", "火系"],
    content="胡桃角色立绘图片，往生堂第七十七代堂主",
    source="plugin",
    _hash="",
))
```

注册后的图片会在系统启动时自动同步到向量数据库，支持语义搜索。

---

## 20. AI Statistics API - /api/ai/statistics

提供 AI 模块的完整统计数据，包括 Token 消耗、费用估算、延迟统计、意图分布、Heartbeat 决策、RAG 效果等。

### 20.1 获取统计数据摘要

```
GET /api/ai/statistics/summary
```

**Query 参数**:
- `date`: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天（获取今日实时数据）。指定日期时从数据库查询历史数据

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "date": "2024-01-15",
        "token_usage": {
            "total_input_tokens": 150000,
            "total_output_tokens": 80000,
            "total_cost_usd": 1.25,
            "total_cost_cny": 9.00,
            "by_model": [
                {
                    "model": "gpt-4",
                    "input_tokens": 100000,
                    "output_tokens": 50000,
                    "cost_usd": 1.0
                }
            ]
        },
        "latency": {
            "avg": 1.5,
            "p95": 3.2
        },
        "intent_distribution": {
            "chat": {"count": 120, "percentage": 40.0},
            "tool": {"count": 80, "percentage": 26.7},
            "qa": {"count": 100, "percentage": 33.3}
        },
        "errors": {
            "timeout": 2,
            "rate_limit": 1,
            "network_error": 0,
            "usage_limit": 0,
            "agent_error": 1,
            "total": 4
        },
        "heartbeat": {
            "should_speak_true": 45,
            "should_speak_false": 30,
            "conversion_rate": 60.0
        },
        "trigger_distribution": {
            "mention": {"count": 150, "percentage": 50.0},
            "keyword": {"count": 100, "percentage": 33.3},
            "heartbeat": {"count": 50, "percentage": 16.7},
            "scheduled": {"count": 20, "percentage": 6.7}
        },
        "rag": {
            "hit_count": 80,
            "miss_count": 20,
            "hit_rate": 80.0
        },
        "active_users": [
            {
                "group_id": "123456",
                "user_id": "user001",
                "ai_interaction": 30,
                "message_count": 100
            }
        ]
    }
}
```

---

### 20.2 获取按模型分组的 Token 消耗

```
GET /api/ai/statistics/token-by-model
```

**Query 参数**:
- `date`: 日期字符串 (YYYY-MM-DD)，默认为今天

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "model": "gpt-4",
            "input_tokens": 100000,
            "output_tokens": 50000,
            "cost_usd": 1.0
        },
        {
            "model": "gpt-3.5-turbo",
            "input_tokens": 50000,
            "output_tokens": 30000,
            "cost_usd": 0.08
        }
    ]
}
```

---

### 20.3 获取活跃用户/群组排行

```
GET /api/ai/statistics/active-users
```

**Query 参数**:
- `limit`: 返回数量限制，默认为 20

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "group_id": "123456",
            "user_id": "user001",
            "ai_interaction": 30,
            "message_count": 100
        }
    ]
}
```

---

### 20.4 获取触发方式占比

```
GET /api/ai/statistics/trigger-distribution
```

**Query 参数**:
- 无

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "mention": {"count": 150, "percentage": 50.0},
        "keyword": {"count": 100, "percentage": 33.3},
        "heartbeat": {"count": 50, "percentage": 16.7}
    }
}
```

---

### 20.5 获取意图分布统计

```
GET /api/ai/statistics/intent-distribution
```

**Query 参数**:
- 无

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "chat": {"count": 120, "percentage": 40.0},
        "tool": {"count": 80, "percentage": 26.7},
        "qa": {"count": 100, "percentage": 33.3}
    }
}
```

---

### 20.6 获取错误统计

```
GET /api/ai/statistics/errors
```

**Query 参数**:
- 无

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "timeout": 2,
        "rate_limit": 1,
        "network_error": 0,
        "usage_limit": 0,
        "agent_error": 1,
        "total": 4
    }
}
```

---

### 20.7 获取 Heartbeat 巡检统计

```
GET /api/ai/statistics/heartbeat
```

**Query 参数**:
- 无

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "should_speak_true": 45,
        "should_speak_false": 30,
        "conversion_rate": 60.0
    }
}
```

---

### 20.8 获取 RAG 知识库效果统计

```
GET /api/ai/statistics/rag
```

**说明**: RAG 统计是全局数据，不区分 bot_id。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "hit_count": 80,
        "miss_count": 20,
        "hit_rate": 80.0
    }
}
```

---

### 20.9 获取 RAG 文档命中统计

```
GET /api/ai/statistics/rag/documents
```

**说明**: RAG 文档命中统计是全局数据，不区分 bot_id。

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "document_name": "游戏攻略",
            "hit_count": 15
        },
        {
            "document_name": "角色介绍",
            "hit_count": 8
        }
    ]
}
```

---

### 20.10 获取历史统计数据

```
GET /api/ai/statistics/history
```

**Query 参数**:
- `days`: 查询天数，默认为 7

**响应**:
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "date": "2024-01-14"
        },
        {
            "date": "2024-01-15"
        }
    ]
}
```
