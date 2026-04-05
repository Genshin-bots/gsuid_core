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
    "data": ["角色名1", "角色名2", "智能助手"]
}
```

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
        "content": "# 角色名\n\n角色描述内容（Markdown格式）..."
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

### 13.3 上传角色头像

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
        "path": "/absolute/path/to/persona/角色名.png"
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

### 13.4 创建新角色

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

### 13.5 删除角色

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
