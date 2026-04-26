# 24. Provider Config API - /api/provider_config

Provider Config API 用于统一管理 OpenAI 和 Anthropic 格式的 AI 模型配置，支持高级/低级任务配置切换。

> **架构说明**：框架不再使用 "current provider" 的概念。每个任务级别（high/low）可以独立选择配置，provider 类型由配置名自动判断。

---

## 24.1 获取 Provider 列表

```
GET /api/provider_config/providers
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
        "providers": [
            {
                "id": "openai",
                "name": "OpenAI 兼容格式",
                "description": "支持 OpenAI、Azure、第三方兼容 API",
                "config_count": 2,
                "configs": ["openai_config", "azure_config"]
            },
            {
                "id": "anthropic",
                "name": "Anthropic 格式",
                "description": "支持 Claude 系列模型",
                "config_count": 1,
                "configs": ["claude_config"]
            }
        ]
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| providers | array | Provider 列表 |
| providers[].id | string | Provider 标识符 |
| providers[].name | string | Provider 显示名称 |
| providers[].description | string | Provider 描述 |
| providers[].config_count | number | 该 provider 下的配置文件数量 |
| providers[].configs | string[] | 配置文件名列表 |

---

## 24.2 获取任务级别配置

```
GET /api/provider_config/task_config/{task_level}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| task_level | string | 任务级别（high/low） |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "task_level": "high",
        "current_config": "gpt-4o-config",
        "current_provider": "openai",
        "config_detail": {
            "name": "gpt-4o-config",
            "provider": "openai",
            "config": {
                "base_url": {
                    "title": "OpenAI API基础URL",
                    "desc": "指定OpenAI API的基础URL",
                    "data": "https://api.openai.com/v1",
                    "options": ["https://api.openai.com/v1", "..."]
                },
                "api_key": {...},
                "model_name": {...}
            }
        },
        "available_configs": {
            "openai": ["openai_config", "gpt-4o-config"],
            "anthropic": ["claude_config"]
        }
    }
}
```

---

## 24.4 设置任务级别配置

```
POST /api/provider_config/task_config/{task_level}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| task_level | string | 任务级别（high/low） |

**请求体**：
```json
{
    "config_name": "claude-config"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| config_name | string | 配置文件名（不含扩展名） |

> **说明**：provider 类型由配置名自动判断，无需指定。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "task_level": "high",
        "config_name": "claude-config",
        "provider": "anthropic"
    }
}
```

---

## 24.5 获取所有配置摘要

```
GET /api/provider_config/all_configs
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
        "openai_configs": [
            {
                "name": "openai_config",
                "provider": "openai",
                "model_name": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1"
            }
        ],
        "anthropic_configs": [
            {
                "name": "claude_config",
                "provider": "anthropic",
                "model_name": "claude-sonnet-4-20250514",
                "base_url": "https://api.anthropic.com"
            }
        ],
        "high_level_config": "gpt-4o-config",
        "low_level_config": "openai_config"
    }
}
```

---

## 24.6 获取配置详情

```
GET /api/provider_config/config/{provider}/{config_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| provider | string | provider 类型（openai/anthropic） |
| config_name | string | 配置文件名（不含扩展名） |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "openai_config",
        "provider": "openai",
        "config": {
            "base_url": {...},
            "api_key": {...},
            "model_name": {...}
        }
    }
}
```

---

## 24.7 创建或更新配置

```
POST /api/provider_config/config/{provider}/{config_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| provider | string | provider 类型（openai/anthropic） |
| config_name | string | 配置文件名（不含扩展名） |

**请求体**：
```json
{
    "config": {
        "base_url": {
            "data": "https://api.openai.com/v1"
        },
        "api_key": {
            "data": ["sk-xxx"]
        },
        "model_name": {
            "data": "gpt-4o"
        }
    }
}
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "openai_config",
        "provider": "openai"
    }
}
```

---

## 24.8 创建默认配置

```
POST /api/provider_config/config/{provider}/{config_name}/create_default
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| provider | string | provider 类型（openai/anthropic） |
| config_name | string | 配置文件名（不含扩展名） |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "new_config",
        "provider": "openai"
    }
}
```

---

## 24.9 删除配置

```
DELETE /api/provider_config/config/{provider}/{config_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| provider | string | provider 类型（openai/anthropic） |
| config_name | string | 配置文件名（不含扩展名） |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": null
}
```

**错误响应**（配置正在使用中）：
```json
{
    "status": 1,
    "msg": "无法删除当前激活的配置文件 'xxx'，请先切换到其他配置",
    "data": null
}
```

---

## 24.10 获取配置可选项

```
GET /api/provider_config/config/{provider}/options
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| provider | string | provider 类型（openai/anthropic） |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "provider": "openai",
        "options": {
            "base_url": [
                "https://api.openai.com/v1",
                "https://api.bltcy.ai/v1"
            ],
            "model_name": [
                "gpt-4o-mini",
                "gpt-4o",
                "gemini-2.5-flash"
            ],
            "embedding_model": [
                "text-embedding-3-small",
                "text-embedding-3-large"
            ],
            "model_support": ["text", "image", "audio", "video"]
        }
    }
}
```

---

## 设计说明

### Provider 概念

Provider 代表 AI 服务提供方的格式类型：
- **openai**：OpenAI 兼容格式，支持 OpenAI API、Azure、第三方兼容 API
- **anthropic**：Anthropic 格式，支持 Claude 系列模型

### 配置文件目录

- `openai_config/*.json` - OpenAI 兼容格式的配置文件
- `anthropic_config/*.json` - Anthropic 格式的配置文件

### 任务级别

- **高级任务 (high)**：复杂推理、工具调用、多轮对话等需要更强模型能力的任务
- **低级任务 (low)**：简单问答、快速响应等只需基础模型能力的任务

用户可以为高级任务和低级任务配置不同的模型，例如：
- 高级任务使用 GPT-4o 或 Claude Sonnet
- 低级任务使用 GPT-4o-mini 或 Claude Haiku
