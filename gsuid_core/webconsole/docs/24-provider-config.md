# 24. Provider Config API - /api/provider_config

Provider Config API 用于统一管理 OpenAI 和 Anthropic 格式的 AI 模型配置，支持高级/低级任务配置切换。

> **架构说明**：框架不再使用 "current provider" 的概念。每个任务级别（high/low）可以独立选择配置，provider 类型由配置名自动判断。

## 配置名称格式

配置名称采用 `"provider++config_name"` 格式（例如 `"openai++MiniMAX"`）：

- **provider**: `"openai"` / `"anthropic"` / `"gemini"`
- **config_name**: 配置文件名称（不含扩展名）
- **分隔符**: `"++"`
- **兼容旧格式**: 不含 `"++"` 的名称默认按 `"openai"` provider 处理

> **注意**：配置文件名称本身不允许包含 `"+"` 字符，因为 `"+"` 是 provider 与配置名称的分隔符。

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
                "configs": ["openai++openai_config", "openai++azure_config"]
            },
            {
                "id": "anthropic",
                "name": "Anthropic 格式",
                "description": "支持 Claude 系列模型",
                "config_count": 1,
                "configs": ["anthropic++claude_config"]
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
        "current_config": "openai++gpt-4o-config",
        "current_provider": "openai",
        "config_detail": {
            "name": "openai++gpt-4o-config",
            "provider": "openai",
            "config_name": "gpt-4o-config",
            "config": {
                "base_url": {
                    "title": "OpenAI API基础URL",
                    "desc": "指定OpenAI API的基础URL",
                    "data": "https://api.openai.com/v1",
                    "options": ["https://api.openai.com/v1", "..."]
                },
                "api_key": {
                    "title": "API密钥",
                    "desc": "指定API密钥",
                    "data": ["sk-xxx"],
                    "options": ["sk-"]
                },
                "model_name": {
                    "title": "模型名称",
                    "desc": "指定模型名称",
                    "data": "gpt-4o",
                    "options": ["gpt-4o", "gpt-4o-mini"]
                }
            }
        }
    }
}
```

---

## 24.3 清除任务级别配置

```
DELETE /api/provider_config/task_config/{task_level}
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
        "config_name": ""
    }
}
```

**使用场景**：当前端需要删除正在激活的配置文件时，应先调用此接口清除对应的任务级别配置（将配置名置空），然后再执行删除操作。具体流程：

1. **有其他可用配置**：先调用 `POST /api/provider_config/task_config/{task_level}` 切换到另一个配置，再删除原配置
2. **没有其他可用配置**：先调用 `DELETE /api/provider_config/task_config/{task_level}` 清除任务配置，再删除配置文件

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
    "config_name": "anthropic++claude-config"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| config_name | string | 配置文件名，支持 `"provider++config_name"` 格式或纯配置名（默认 openai） |

> **说明**：
> - 推荐使用 `"provider++config_name"` 格式，如 `"openai++gpt-4o-config"`、`"anthropic++claude-config"`
> - 兼容旧格式：纯配置名（如 `"gpt-4o-config"`）默认按 `"openai"` provider 处理

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "task_level": "high",
        "config_name": "anthropic++claude-config",
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
        "configs": [
            {
                "name": "openai++openai_config",
                "provider": "openai",
                "config_name": "openai_config",
                "model_name": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1"
            },
            {
                "name": "anthropic++claude_config",
                "provider": "anthropic",
                "config_name": "claude_config",
                "model_name": "claude-sonnet-4-20250514",
                "base_url": "https://api.anthropic.com"
            }
        ],
        "high_level_config": "openai++gpt-4o-config",
        "low_level_config": "openai++openai_config"
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| configs | array | 所有配置摘要列表 |
| configs[].name | string | 完整配置名（`provider++config_name` 格式） |
| configs[].provider | string | provider 类型 |
| configs[].config_name | string | 配置文件名（不含 provider 前缀） |
| configs[].model_name | string | 配置的模型名称 |
| configs[].base_url | string | 配置的 API 基础 URL |
| high_level_config | string | 当前高级任务配置（`provider++name` 格式） |
| low_level_config | string | 当前低级任务配置（`provider++name` 格式） |

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
| provider | string | provider 类型（openai/anthropic/gemini） |
| config_name | string | 配置文件名（不含扩展名） |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "openai++openai_config",
        "provider": "openai",
        "config_name": "openai_config",
        "config": {
            "base_url": {
                "title": "OpenAI API基础URL",
                "desc": "指定OpenAI API的基础URL",
                "data": "https://api.openai.com/v1",
                "options": ["https://api.openai.com/v1", "..."]
            },
            "api_key": {
                "title": "API密钥",
                "desc": "指定API密钥",
                "data": ["sk-xxx"],
                "options": ["sk-"]
            },
            "model_name": {
                "title": "模型名称",
                "desc": "指定模型名称",
                "data": "gpt-4o",
                "options": ["gpt-4o", "gpt-4o-mini"]
            }
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
| provider | string | provider 类型（openai/anthropic/gemini） |
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

> **说明**：请求体中的 `config` 对象只需包含要更新的字段。每个字段的值可以是直接值（如 `"gpt-4o"`）或包含 `"data"` 键的对象（如 `{"data": "gpt-4o"}`）。后端只更新配置模板中存在的字段，不存在的字段会被自动跳过。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "openai++openai_config",
        "provider": "openai",
        "config_name": "openai_config"
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
| provider | string | provider 类型（openai/anthropic/gemini） |
| config_name | string | 配置文件名（不含扩展名） |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "openai++new_config",
        "provider": "openai",
        "config_name": "new_config"
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
| provider | string | provider 类型（openai/anthropic/gemini） |
| config_name | string | 配置文件名（不含扩展名） |

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": null
}
```

> **前端删除激活配置的推荐流程**：
> 1. 检查该配置是否为当前激活的高级/低级任务配置
> 2. **有其他可用配置**：先调用 `POST /api/provider_config/task_config/{task_level}` 切换到另一个配置，再删除原配置
> 3. **没有其他可用配置**：先调用 `DELETE /api/provider_config/task_config/{task_level}` 清除任务配置（置空），再删除配置文件

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
| provider | string | provider 类型（openai/anthropic/gemini） |

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
            "model_support": ["text", "image", "audio", "video"],
            "request_method": ["chat_completions", "responses"]
        }
    }
}
```

> **`request_method`（仅 openai provider）**：选择 OpenAI 接口风格。`chat_completions`
> 走 `/v1/chat/completions`（通用兼容）；`responses` 走 `/v1/responses`（仅 OpenAI 官方及
> 实现该端点的网关支持）。改动后存活会话下次 run 即热替换，无需 `coreclear`。

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

### 删除激活配置的流程

当需要删除当前正在使用的配置文件时，必须遵循以下流程：

1. **有其他可用配置**：先调用 `POST /api/provider_config/task_config/{task_level}` 切换到另一个配置，再删除原配置
2. **没有其他可用配置**：先调用 `DELETE /api/provider_config/task_config/{task_level}` 清除任务配置（将配置名置空），再删除配置文件

如果尝试删除当前激活的配置文件，后端会返回错误：
```json
{
    "status": 1,
    "msg": "无法删除当前激活的配置文件 'openai++gpt-4o-config'，请先切换到其他配置",
    "data": null
}
```
