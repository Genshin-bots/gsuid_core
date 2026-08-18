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
            "request_method": ["chat_completions", "responses"],
            "forward_end_user_id": ["off", "hashed", "raw"]
        }
    }
}
```

> **`request_method`（仅 openai provider）**：选择 OpenAI 接口风格。`chat_completions`
> 走 `/v1/chat/completions`（通用兼容）；`responses` 走 `/v1/responses`（仅 OpenAI 官方及
> 实现该端点的网关支持）。改动后存活会话下次 run 即热替换，无需 `coreclear`。

> **`forward_end_user_id`（仅 openai provider）**：见下方「终端用户标识透传」。

---

## 终端用户标识透传（`forward_end_user_id`）

OpenAI 协议定义了标准的请求体字段 `user`（end-user ID）：请求带上它，上游网关就能把
用量、日志与滥用监控按调用方聚合，而不必给每个调用方单独签发密钥。框架把这个字段接到
了自身的调用归属链路上，逐 provider 配置开关：

| 取值 | 行为 |
|------|------|
| `off`（默认） | 不携带 `user` 字段，与不存在该特性完全一致 |
| `hashed` | 携带 `HMAC-SHA256(salt, 调用方标识)` 的前 32 位十六进制 |
| `raw` | 携带原始调用方标识 |

配套字段 `end_user_id_salt`（`secret`，WebConsole `/ai-config` 默认隐藏、管理员 GET 下发明文）是 `hashed`
模式的盐值。**留空即无密钥摘要**：标识空间小的场景（纯数字账号 / QQ 号）可被枚举反查，
只起混淆作用；需要抗反查请填一段随机字符串。改动盐值会让此前发出的摘要全部失去对应关系，
上游按调用方的历史聚合会断裂。

**开关放在 provider 配置文件而不是全局配置**，因为「该不该把调用方标识发给这个上游」是
逐上游的判断——发给自建网关和发给第三方官方端点显然不是一回事。对外部端点建议保持
`off` 或至多 `hashed`。

**标识从哪来**：框架既有的调用归属三元组 `(group_id, user_id, bot_id)` 中的 `user_id`。
交互链路由 `Event` 解析；后台自主调用（巡检 / 主动发言 / 记忆摄入等）经
`bind_budget_scope` / `set_budget_scope_context` 绑定；嵌套调用沿 contextvar 自动继承。
真正无归属的调用（如共享素材库打标）不携带该字段，由上游归入匿名桶。

**仅 openai provider 支持**：`user` 是 OpenAI 协议字段，Anthropic / Gemini 没有对等的标准
字段，这两类配置不提供该开关。

### 鉴权身份 vs 归属标识

自建网关常见两层信息，不要混为一谈：

| 层 | 载体 | 典型用途 |
|----|------|----------|
| **鉴权身份** | `Authorization` 请求头（或 profile 里的 `api_key`） | 网关验签、按人限额、SpendLogs 主键 |
| **归属标识** | 请求体 `user` 字段（由 `forward_end_user_id` 控制） | 日志聚合、滥用监控、与鉴权解耦的观测 |

二者可以一致（`raw` + 用户 JWT 鉴权），也可以分离（服务凭据鉴权 + `raw` 携带真实
`user_id`）。网关日志里应同时能看到「谁通过了鉴权」与「这次调用归谁」——若只见
`end_user=1` 而鉴权仍是 `service`，说明归属已透传、逐 run 凭据尚未挂上，需检查解析器
与 HTTP 入口是否在 `create_task` 边界传递了登录态。

### 服务凭据与逐 run 凭据

profile 里的 `api_key` 是**兜底**：解析器未给出 `extra_headers` 时使用（后台 run、无登录态
的自主任务等）。对接按人鉴权的自建网关时，这把 key 应是**长期有效的服务凭据**，而不是
某个人的短期登录 token。

有登录态的交互 run 可由宿主注册的解析器在 `extra_headers` 里附带调用方凭据；`Authorization`
会覆盖该次请求的 `api_key`，逐 run 生效，模型对象不受污染。解析器只在部分 run 上给出凭据时，
其余 run 自动落回 profile 里的服务凭据。

### 宿主扩展点

需要把框架内部标识映射为上游认得的主体、或附带自定义请求头时，可注册
`gsuid_core.ai_core.configs.attribution.register_attribution_resolver`。解析器返回 `None`
即本次不透传；抛异常会被降级为不透传并打 warning，不会打断一次真实的 run。配置开关仍是
主闸——`off` 的上游即使注册了解析器也不会收到 `user` 字段。

只想追加请求头、标识仍按配置语义走时，调 `default_end_user_id(req)` 拿默认结果填回
`CallAttribution.end_user_id`（`raw`/`hashed` 由配置决定，salt 不经过解析器）：

```python
def resolver(req: AttributionRequest) -> Optional[CallAttribution]:
    credential = lookup_credential(req.user_id)  # 宿主自己的映射
    if credential is None:
        return CallAttribution(end_user_id=default_end_user_id(req))  # 弃权但保留归属
    return CallAttribution(
        end_user_id=default_end_user_id(req),
        extra_headers={"Authorization": f"Bearer {credential}"},
    )
```

解析器是同步旁路，别在里面做 I/O。`extra_headers` 只应指向**明确信任的自建网关**；
对第三方官方端点附带内部凭据等于泄露密钥。

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
