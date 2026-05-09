# MCP 工具权限配置 — 前端集成文档

## 概述

MCP 工具权限系统允许管理员为每个 MCP 服务器的工具配置访问权限，防止敏感工具（如发送邮件、调用付费 API）被任意用户通过对话诱导 AI 调用。

## 配置格式

在 MCP 服务器配置 JSON 中新增 `tool_permissions` 字段，值为 **pm 权限等级**（整数）：

```json
{
    "name": "MyMCP",
    "command": "uvx",
    "args": ["my-mcp-server"],
    "env": {},
    "enabled": true,
    "register_as_ai_tools": true,
    "tools": [
        {
            "name": "send_email",
            "description": "发送邮件",
            "parameters": {
                "to": {"type": "string", "required": true},
                "subject": {"type": "string", "required": true},
                "body": {"type": "string", "required": true}
            }
        },
        {
            "name": "query_data",
            "description": "查询数据",
            "parameters": {
                "query": {"type": "string", "required": true}
            }
        }
    ],
    "tool_permissions": {
        "send_email": 0,
        "query_data": 6
    }
}
```

## 权限等级（pm）

权限等级与 `Event.user_pm` 直接对比，**用户 pm 值 <= 工具要求的 pm 值才能调用**：

| pm 值 | 含义 | 说明 |
|-------|------|------|
| `0` | master | 仅 master 用户（机器人主人） |
| `1` | superuser | superuser 及以上 |
| `2` | 群主 | 群主及以上 |
| `3` | 群管理员 | 群管理员及以上 |
| `4` | 频道管理员 | 频道管理员及以上 |
| `5` | 当前频道管理员 | 当前频道管理员及以上 |
| `6` | 普通用户 | 所有用户（默认值） |

## 字段说明

### `tool_permissions`

- **类型**: `Dict[str, int]`
- **默认值**: `{}`（空字典，所有工具对所有用户可用，等同于 pm=6）
- **格式**: `{工具名: pm等级}`
- **示例**: `{"send_email": 0, "query_data": 6}`

### 行为规则

1. **未配置的工具**: 如果某个工具名不在 `tool_permissions` 中，默认 pm=6（所有用户可用）
2. **权限检查时机**: AI 调用工具时自动检查，权限不足时返回错误消息给 AI
3. **仅对 `register_as_ai_tools: true` 有效**: 只有注册为 AI 工具的 MCP 工具才会进行权限检查
4. **对比逻辑**: `ev.user_pm > required_pm` 时拒绝（pm 值越小权限越高）

## 前端页面设计建议

### MCP 配置编辑页面

在现有的 MCP 配置编辑页面中，新增"工具权限"区域：

```
┌─────────────────────────────────────────────────┐
│  MCP 服务器配置                                   │
├─────────────────────────────────────────────────┤
│  名称: [MyMCP                          ]        │
│  命令: [uvx                            ]        │
│  参数: [my-mcp-server                  ]        │
│  ☑ 启用                                          │
│  ☑ 注册为 AI 工具                                 │
├─────────────────────────────────────────────────┤
│  工具权限配置                                      │
│  ┌───────────────────────────────────────────┐  │
│  │ 工具名称        │ 最低权限等级 (pm)        │  │
│  ├─────────────────┼─────────────────────────┤  │
│  │ send_email      │ [0 ▾] master            │  │
│  │ query_data      │ [6 ▾] 普通用户          │  │
│  └───────────────────────────────────────────┘  │
│  提示: pm 值越小权限越高。0=master, 1=superuser,  │  │
│        2=群主, 3=群管理员, 6=普通用户             │  │
├─────────────────────────────────────────────────┤
│  [保存]  [取消]                                   │
└─────────────────────────────────────────────────┘
```

### 交互逻辑

1. **工具列表来源**: 从 MCP 服务器连接后获取的工具列表（`tools` 字段）自动填充
2. **权限选择器**: 下拉框，选项为 0/1/2/3/4/5/6，显示对应的中文含义
3. **默认值**: 新添加的工具默认为 6（所有人可用）
4. **保存时**: 将 `tool_permissions` 写入配置 JSON

### API 接口

现有的 MCP 配置 API（`/api/ai/mcp/`）已支持 `tool_permissions` 字段的读写，无需新增接口。

#### 获取配置

```
GET /api/ai/mcp/{config_id}
```

响应中包含 `tool_permissions` 字段：

```json
{
    "config_id": "my_mcp",
    "name": "MyMCP",
    "tool_permissions": {
        "send_email": 0,
        "query_data": 6
    }
}
```

#### 更新配置

```
PUT /api/ai/mcp/{config_id}
```

请求体中包含 `tool_permissions`：

```json
{
    "tool_permissions": {
        "send_email": 0,
        "query_data": 6
    }
}
```

## 权限检查流程

```
用户发送消息 → AI 决定调用 MCP 工具
                    │
                    ▼
            读取 tool_permissions[tool_name]
                    │
                    ▼
            获取 required_pm（默认 6）
                    │
        ┌───────────┴───────────┐
        │                       │
    required_pm < 6         required_pm >= 6
        │                       │
        ▼                       ▼
    检查 ev.user_pm          允许调用（无需检查）
        │
    ┌───┴───┐
    │       │
  <= pm   > pm
    │       │
    ▼       ▼
  执行    返回错误消息给 AI
  工具    "❌ 权限不足：需要 pm<=0，当前 pm=6"
```

## 示例场景

### 场景 1: 邮件发送工具（仅 master）

```json
{
    "tool_permissions": {
        "send_email": 0,
        "read_email": 6
    }
}
```

- 只有 master 用户（pm=0）可以通过 AI 发送邮件
- 所有用户都可以通过 AI 读取邮件

### 场景 2: 付费 API 工具

```json
{
    "tool_permissions": {
        "premium_search": 1,
        "basic_search": 6
    }
}
```

- 付费搜索功能仅 superuser 及以上（pm<=1）可用
- 基础搜索对所有人开放

### 场景 3: 无限制工具

```json
{
    "tool_permissions": {}
}
```

- 空字典表示所有工具对所有用户可用（默认行为）
