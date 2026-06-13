# 27. 嵌入模型配置 API - /api/embedding_config

嵌入模型配置 API 用于管理嵌入模型提供方（local/openai/插件注册的第三方）及其配置。支持在本地 fastembed 模型和 OpenAI 兼容格式的远程 API 之间自由切换。

> **架构说明**：嵌入模型提供方通过 `ai_config` 中的 `embedding_provider` 配置项控制。切换提供方后需要重启生效。本地和远程配置相互独立，互不影响。
>
> **插件扩展**：插件可通过 `gsuid_core.ai_core.rag.embedding_registry.register_embedding_provider`
> 注册第三方 provider（如 `sentence_transformers`），注册后自动出现在 `available_providers`
> 与 provider 下拉选项中；其配置项通过 27.7 的 `extra_providers` 字段返回，
> 修改则走插件管理页（`/api/plugins` 通用配置渲染）。

---

## 27.1 获取当前嵌入模型提供方

```
GET /api/embedding_config/provider
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
        "provider": "local",
        "available_providers": ["local", "openai"]
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| provider | string | 当前嵌入模型提供方，`"local"`、`"openai"` 或插件注册的 provider 名 |
| available_providers | string[] | 可用的提供方列表（内置 + 插件注册，注册表驱动） |

---

## 27.2 设置嵌入模型提供方

```
POST /api/embedding_config/provider
```

**请求头**：
```
Authorization: Bearer <token>
```

**请求体**：
```json
{
    "provider": "openai"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| provider | string | 是 | 嵌入模型提供方，须在 `available_providers` 列表内 |

**响应**：
```json
{
    "status": 0,
    "msg": "嵌入模型提供方已切换为 'openai'，重启后生效",
    "data": {
        "provider": "openai"
    }
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "不支持的嵌入模型提供方: 'xxx'，可用: ['local', 'openai', 'sentence_transformers']",
    "data": null
}
```

---

## 27.3 获取本地嵌入模型配置

```
GET /api/embedding_config/local
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
        "embedding_model_name": {
            "title": "指定嵌入模型名称",
            "desc": "指定启用的嵌入模型名称",
            "data": "BAAI/bge-small-zh-v1.5",
            "options": ["BAAI/bge-small-zh-v1.5"]
        }
    }
}
```

---

## 27.4 保存本地嵌入模型配置

```
POST /api/embedding_config/local
```

**请求头**：
```
Authorization: Bearer <token>
```

**请求体**：
```json
{
    "embedding_model_name": "BAAI/bge-small-zh-v1.5"
}
```

**响应**：
```json
{
    "status": 0,
    "msg": "本地嵌入模型配置已保存，重启后生效",
    "data": null
}
```

---

## 27.5 获取 OpenAI 嵌入模型配置

```
GET /api/embedding_config/openai
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
        "base_url": {
            "title": "嵌入模型API基础URL",
            "desc": "指定OpenAI兼容格式的嵌入模型API基础URL, 注意一般是以 /v1 结尾",
            "data": "https://api.openai.com/v1",
            "options": [
                "https://api.openai.com/v1",
                "https://api.siliconflow.cn/v1",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "https://api.deepseek.com",
                "http://localhost:3000",
                "http://127.0.0.1:3000"
            ]
        },
        "api_key": {
            "title": "嵌入模型API密钥",
            "desc": "指定OpenAI兼容格式的嵌入模型API密钥, 支持添加多个",
            "data": ["sk-"],
            "options": ["sk-"]
        },
        "embedding_model": {
            "title": "嵌入模型名称",
            "desc": "指定嵌入模型名称, 该模型将会用于处理文本嵌入",
            "data": "text-embedding-3-small",
            "options": [
                "text-embedding-3-small",
                "text-embedding-3-large",
                "text-embedding-ada-002",
                "BAAI/bge-m3",
                "BAAI/bge-large-zh-v1.5",
                "Pro/BAAI/bge-m3"
            ]
        }
    }
}
```

---

## 27.6 保存 OpenAI 嵌入模型配置

```
POST /api/embedding_config/openai
```

**请求头**：
```
Authorization: Bearer <token>
```

**请求体**：
```json
{
    "base_url": "https://api.siliconflow.cn/v1",
    "api_key": ["sk-xxx"],
    "embedding_model": "BAAI/bge-m3"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| base_url | string | 否 | OpenAI 兼容格式的 API 基础 URL |
| api_key | string[] | 否 | API 密钥列表（支持多个） |
| embedding_model | string | 否 | 嵌入模型名称 |

**响应**：
```json
{
    "status": 0,
    "msg": "OpenAI 嵌入模型配置已保存，重启后生效",
    "data": null
}
```

---

## 27.7 获取嵌入模型配置摘要

```
GET /api/embedding_config/summary
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
        "provider": "local",
        "available_providers": ["local", "openai"],
        "local_config": {
            "embedding_model_name": {
                "title": "指定嵌入模型名称",
                "desc": "指定启用的嵌入模型名称",
                "data": "BAAI/bge-small-zh-v1.5",
                "options": ["BAAI/bge-small-zh-v1.5"]
            }
        },
        "openai_config": {
            "base_url": {
                "title": "嵌入模型API基础URL",
                "desc": "...",
                "data": "https://api.openai.com/v1",
                "options": ["..."]
            },
            "api_key": {
                "title": "嵌入模型API密钥",
                "desc": "...",
                "data": ["sk-"],
                "options": ["sk-"]
            },
            "embedding_model": {
                "title": "嵌入模型名称",
                "desc": "...",
                "data": "text-embedding-3-small",
                "options": ["..."]
            }
        },
        "extra_providers": {
            "sentence_transformers": {
                "display_name": "SentenceTransformers (本地)",
                "plugin": "STEmbedding",
                "kind": "local",
                "config": {
                    "st_model_name": {
                        "title": "模型名称",
                        "desc": "...",
                        "data": "BAAI/bge-m3",
                        "options": []
                    }
                }
            }
        }
    }
}
```

**`extra_providers` 字段说明**（插件注册的 provider，前端未跟进时可静默忽略）：
| 字段 | 类型 | 说明 |
|------|------|------|
| display_name | string | 展示名 |
| plugin | string | 来源插件名 |
| kind | string | `"local"`（本地推理）或 `"remote"`（远程 API） |
| config | object | 该 provider 的配置项（与 `local_config`/`openai_config` 同构，只读展示；修改走插件管理页） |

> **前端建议**：使用此接口一次性获取所有嵌入模型配置信息，减少请求次数。根据 `provider` 字段决定显示哪一组配置表单。
