# AI 配置向导 API - /api/ai/wizard

## 概述

AI 配置向导 API 用于帮助前端展示 AI 功能配置状态，提供配置缺失提醒和修复建议。该 API 整合了 AI 配置的各个方面信息，帮助用户快速了解 AI 功能状态。

---

## 基础配置状态 API

### GET /api/ai/wizard/status

获取 AI 配置状态向导数据。

**认证**：需要认证

**响应示例**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "ai_enabled": true,
        "ai_enable_range": {
            "mode": "all",
            "mode_desc": "全部用户可用",
            "white_list": [],
            "black_list": [],
            "note": "全部用户可用"
        },
        "high_level_model": {
            "configured": true,
            "provider": "openai",
            "config_name": "GPT4",
            "model_name": "gpt-4o",
            "full_name": "openai++GPT4"
        },
        "low_level_model": {
            "configured": true,
            "provider": "openai",
            "config_name": "GPT35",
            "model_name": "gpt-3.5-turbo",
            "full_name": "openai++GPT35"
        },
        "vision_support": {
            "available": true,
            "high_level_vision": {
                "supported": true,
                "model_name": "gpt-4o",
                "note": "支持图片理解"
            },
            "low_level_vision": {
                "supported": false,
                "model_name": "gpt-3.5-turbo",
                "note": "不支持图片理解"
            },
            "vlm_fallback": {
                "configured": false,
                "provider": "MCP",
                "tools": [],
                "note": "未配置图片理解 MCP 工具"
            }
        },
        "persona": {
            "persona_count": 3,
            "enabled_count": 2,
            "inspect_enabled_count": 1,
            "configured": true,
            "personas": [
                {
                    "name": "早柚",
                    "ai_mode": ["提及应答", "定时巡检"],
                    "inspect_interval": 30,
                    "has_inspect": true,
                    "scope": "global",
                    "target_groups": [],
                    "is_enabled": true,
                    "scope_desc": "全部群聊"
                },
                {
                    "name": "神里绫人",
                    "ai_mode": ["提及应答"],
                    "inspect_interval": null,
                    "has_inspect": false,
                    "scope": "specific",
                    "target_groups": ["123456789", "987654321"],
                    "is_enabled": true,
                    "scope_desc": "限定 2 个群聊 (specific)"
                },
                {
                    "name": "禁用的人格",
                    "ai_mode": [],
                    "inspect_interval": null,
                    "has_inspect": false,
                    "scope": "disabled",
                    "target_groups": [],
                    "is_enabled": false,
                    "scope_desc": "已禁用"
                }
            ],
            "note": "共 3 个人格，2 个已启用，1 个启用了定时巡检"
        },
        "memory": {
            "enabled": true,
            "memory_mode": ["被动感知", "主动会话"],
            "memory_session": "按人格配置"
        },
        "embedding": {
            "provider": "local",
            "configured": true,
            "issues": [],
            "model_name": "BAAI/bge-small-zh-v1.5",
            "note": "使用本地嵌入模型: BAAI/bge-small-zh-v1.5"
        },
        "web_search": {
            "provider": "Tavily",
            "configured": true,
            "issues": [],
            "note": "已配置 1 个 Tavily API Key"
        },
        "missing_configs": [
            {
                "category": "vision",
                "item": "图片理解能力",
                "severity": "warning",
                "message": "当前模型不支持图片理解，且未配置 VLM 备用方案",
                "recommendation": "配置支持视觉的模型或添加 MCP 图片理解工具"
            }
        ],
        "summary": {
            "total_issues": 1,
            "critical_count": 0,
            "warning_count": 1,
            "info_count": 0,
            "ai_usable": true,
            "note": "AI 可用，但存在警告问题"
        }
    }
}
```

**响应字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ai_enabled` | bool | AI 总开关是否启用 |
| `ai_enable_range` | object | **全局** AI 用户级别启用范围（白名单优先于黑名单） |
| `ai_enable_range.mode` | string | 启用模式：`all`=全部用户, `white_list`=白名单, `black_list`=黑名单 |
| `ai_enable_range.white_list` | array | **白名单用户 ID 列表**（仅当 mode=white_list 时有效，启用后仅有白名单用户可用） |
| `ai_enable_range.black_list` | array | **黑名单用户 ID 列表**（仅当 mode=black_list 时有效，黑名单用户不可用） |
| `high_level_model` | object | 高级任务模型配置（用于工具调用） |
| `low_level_model` | object | 低级任务模型配置（用于简单问答） |
| `vision_support` | object | 图片理解能力配置 |
| `vision_support.available` | bool | 是否有图片理解能力 |
| `vision_support.high_level_vision` | object | 高级模型的视觉支持情况 |
| `vision_support.low_level_vision` | object | 低级模型的视觉支持情况 |
| `vision_support.vlm_fallback` | object | VLM 备用方案（MCP 图片理解工具） |
| `persona` | object | **人格配置信息（合并了启用范围）** |
| `persona.persona_count` | int | 人格总数 |
| `persona.enabled_count` | int | 已启用的人格数量 |
| `persona.personas` | array | **人格详细列表，包含每个人格的启用范围** |
| `persona.personas[].name` | string | 人格名称 |
| `persona.personas[].ai_mode` | array | AI 行动模式列表 |
| `persona.personas[].inspect_interval` | int/null | 定时巡检间隔（分钟） |
| `persona.personas[].has_inspect` | bool | 是否启用了定时巡检 |
| `persona.personas[].scope` | string | 启用范围：`disabled`/`global`/`specific` |
| `persona.personas[].target_groups` | array | 限定的群聊 ID 列表（仅 scope=specific 时有效） |
| `persona.personas[].is_enabled` | bool | 该人格是否启用 |
| `persona.personas[].scope_desc` | string | 范围描述文本 |
| `memory` | object | 记忆系统配置 |
| `memory.enabled` | bool | 记忆功能是否启用 |
| `memory.memory_mode` | array | 记忆路径列表（被动感知/主动会话） |
| `memory.memory_session` | string | 被动感知范围（按人格配置/全部群聊） |
| `embedding` | object | 嵌入模型配置 |
| `web_search` | object | Web Search 配置 |
| `missing_configs` | array | 缺失配置项列表 |
| `missing_configs[].category` | string | 问题分类 |
| `missing_configs[].item` | string | 问题项名称 |
| `missing_configs[].severity` | string | 严重程度：`critical`/`warning`/`info` |
| `missing_configs[].message` | string | 问题描述 |
| `missing_configs[].recommendation` | string | 修复建议 |
| `summary` | object | 汇总信息 |
| `summary.ai_usable` | bool | AI 是否可用 |

---

## 简化检查清单 API

### GET /api/ai/wizard/checklist

获取 AI 配置检查清单（扁平化格式），用于快速展示配置状态。

**认证**：需要认证

**响应示例**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "items": [
            {
                "id": "ai_enable",
                "category": "基础",
                "name": "AI 服务",
                "status": "ok",
                "value": true,
                "message": "已启用"
            },
            {
                "id": "high_level_model",
                "category": "模型",
                "name": "高级任务模型",
                "status": "ok",
                "value": "openai++GPT4",
                "message": "openai++GPT4"
            },
            {
                "id": "low_level_model",
                "category": "模型",
                "name": "低级任务模型",
                "status": "ok",
                "value": "openai++GPT35",
                "message": "openai++GPT35"
            },
            {
                "id": "vision",
                "category": "功能",
                "name": "图片理解",
                "status": "warning",
                "value": false,
                "message": "不支持"
            },
            {
                "id": "persona",
                "category": "人格",
                "name": "人格配置",
                "status": "ok",
                "value": 3,
                "message": "共 3 个人格，2 个已启用，1 个启用了定时巡检"
            },
            {
                "id": "memory",
                "category": "记忆",
                "name": "记忆功能",
                "status": "warning",
                "value": {
                    "enabled": true,
                    "memory_mode": ["被动感知", "主动会话"],
                    "memory_session": "全部群聊",
                    "is_all_groups_warning": true
                },
                "message": "被动记忆 + 主动记忆 | 全部群聊 ⚠️"
            },
            {
                "id": "embedding",
                "category": "RAG",
                "name": "嵌入模型",
                "status": "ok",
                "value": "BAAI/bge-small-zh-v1.5",
                "message": "使用本地嵌入模型: BAAI/bge-small-zh-v1.5"
            },
            {
                "id": "websearch",
                "category": "工具",
                "name": "网络搜索",
                "status": "ok",
                "value": "Tavily",
                "message": "已配置 1 个 Tavily API Key"
            },
            {
                "id": "ai_range",
                "category": "基础",
                "name": "AI 用户范围",
                "status": "ok",
                "value": "all",
                "message": "全部用户可用"
            }
        ],
        "overall_status": "overall_warning",
        "usable": true,
        "summary": {
            "total": 9,
            "ok": 8,
            "warning": 1,
            "error": 0
        }
    }
}
```

**响应字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `items` | array | 检查项列表 |
| `items[].id` | string | 检查项 ID |
| `items[].category` | string | 分类（基础/模型/功能/人格/记忆/RAG/工具） |
| `items[].name` | string | 检查项名称 |
| `items[].status` | string | 状态：`ok`/`warning`/`error` |
| `items[].value` | any | 当前值 |
| `items[].message` | string | 状态消息 |
| `overall_status` | string | 整体状态：`overall_ok`/`overall_warning`/`overall_error` |
| `usable` | bool | AI 是否可用 |
| `summary` | object | 统计汇总 |
| `summary.total` | int | 检查项总数 |
| `summary.ok` | int | 正常项数量 |
| `summary.warning` | int | 警告项数量 |
| `summary.error` | int | 错误项数量 |

---

## 记忆功能说明

记忆功能配置包含两个维度：

### 记忆路径（memory_mode）

| 值 | 说明 |
|----|------|
| `被动感知` | 被动记忆 - 自动感知群内所有成员的发言（无需触发命令或 AI），逐条经规则门控后写入记忆 |
| `主动会话` | 主动记忆 - 仅在 AI 实际参与交互时记录本轮对话，**同时包含「触发者的发言」和「Bot 自身的回复」** |

> **两种路径可同时开启，互不冲突：**
> - 仅开 `被动感知`：记录所有群友发言，但不单独记录 Bot 自己的回复。
> - 仅开 `主动会话`：只记录触发了 AI 的那一轮对话（触发者原话 + Bot 回复），其余未触发的闲聊不记。
> - 同时开启：被动路径已在消息入口记录过触发者发言，主动路径会**自动跳过**对触发者发言的重复记录（`"被动感知" not in memory_mode` 守卫），仅补记 Bot 自身回复，避免同一条消息被二次写入。
>
> 写入去向：触发者发言与普通群友发言一样进入**群组事实图谱**（可被实体抽取）；Bot 自身回复写入独立的 `SELF` scope（仅作情景记忆，不污染群组事实）。

### 记忆范围（memory_session）

| 值 | 说明 |
|----|------|
| `按人格配置` | 只处理人格范围内的群聊记忆 |
| `全部群聊` | 处理所有群聊的记忆（⚠️ 警告：可能占用较多 token） |

### ⚠️ 全部群聊警告

当 `memory_session` 为 `全部群聊` 时，状态为 `warning`，并在 message 中显示 `⚠️` 标记。

### checklist 中的记忆项 value 结构

```json
{
    "enabled": true,
    "memory_mode": ["被动感知", "主动会话"],
    "memory_session": "按人格配置",
    "is_all_groups_warning": false
}
```

**message 示例**：
- `"被动记忆 + 主动记忆 | 按人格配置"` - 同时启用了被动和主动记忆，范围按人格配置
- `"被动记忆 | 全部群聊 ⚠️"` - 只有被动记忆，处理全部群聊（带警告）
- `"未启用"` - 记忆功能未启用

---

## 人格启用范围说明

人格配置中每个人格都有独立的启用范围设置：

| scope 值 | 说明 | target_groups |
|---------|------|---------------|
| `disabled` | 该人格已禁用，不对任何群聊启用 | [] |
| `global` | 对所有群聊/角色启用 | [] |
| `specific` | 仅对指定群聊/角色启用 | [群ID列表] |

**注意**：
- `ai_enable_range`（用户级范围）是全局的，决定哪些用户可以使用 AI
- 每个人格的 `scope` 是独立的，决定该人格在哪些群聊生效
- AI 要在某个群聊生效，需要同时满足：用户级范围允许 + 该群聊有人格启用

---

## 用户级 AI 启用范围说明

`ai_enable_range` 控制全局的用户级 AI 访问权限：

| mode 值 | 说明 | white_list | black_list |
|---------|------|------------|------------|
| `all` | 全部用户可用 | [] | [] |
| `white_list` | **仅白名单用户可用**（白名单优先） | [用户ID列表] | [] |
| `black_list` | 除黑名以外全部可用 | [] | [用户ID列表] |

**优先级**：白名单 > 黑名单

**示例响应**（mode=white_list）：
```json
{
    "mode": "white_list",
    "mode_desc": "白名单模式 (2 个用户)",
    "white_list": ["user_123", "user_456"],
    "black_list": [],
    "note": "白名单模式 (2 个用户)"
}
```

**示例响应**（mode=black_list）：
```json
{
    "mode": "black_list",
    "mode_desc": "黑名单模式 (1 个用户)",
    "white_list": [],
    "black_list": ["user_789"],
    "note": "黑名单模式 (1 个用户)"
}
```

前端在展示 AI 用户范围时，应该：
- 显示当前模式（all/white_list/black_list）
- 如果是 white_list，展开显示所有白名单用户 ID
- 如果是 black_list，展开显示所有黑名单用户 ID

---

## 前端使用建议

### 配置向导页面布局

建议前端按照以下分类展示配置状态：

1. **基础配置**（红色警告级别）
   - AI 总开关
   - AI 用户级范围（白名单/黑名单/全部用户）

2. **模型配置**（红色警告级别）
   - 高级任务模型（用于工具调用）
   - 低级任务模型（用于简单问答）

3. **功能支持**（黄色警告级别）
   - 图片理解能力（模型视觉支持 + VLM 备用方案）

4. **人格配置**（黄色警告级别）
   - 已配置人格数量和启用数量
   - **每个人格的详细范围**：
     - `global` 人格：显示 "全部群聊"
     - `specific` 人格：显示 "限定 N 个群聊"
     - `disabled` 人格：显示 "已禁用"

5. **记忆系统**（蓝色信息级别）
   - 记忆功能开关
   - 记忆模式（被动感知/主动会话）

6. **RAG 配置**（红色/黄色警告级别）
   - 嵌入模型配置状态
   - 嵌入模型类型和名称

7. **工具配置**（黄色警告级别）
   - 网络搜索配置状态
   - 搜索服务提供方

### 状态颜色映射

| severity/status | 建议颜色 | 说明 |
|-----------------|----------|------|
| `critical`/`error` | 红色 `#EF4444` | 必须修复，否则 AI 不可用 |
| `warning` | 黄色 `#F59E0B` | 建议修复，影响部分功能 |
| `info`/`ok` | 绿色 `#10B981` | 配置正常 |

### 人格范围展示示例

前端可以这样展示人格列表：

```
人格配置（共 3 个，2 个已启用）

✓ 早柚 [global]
  └─ 全部群聊 | 定时巡检(30分钟) | 提及应答

✓ 神里绫人 [specific]
  └─ 限定 2 个群聊 | 提及应答
     ├─ 群聊: 123456789
     └─ 群聊: 987654321

✗ 禁用的人格 [disabled]
  └─ 已禁用
```

### 缺失配置处理流程

1. 调用 `/api/ai/wizard/status` 获取完整状态
2. 检查 `summary.critical_count`
   - 如果 > 0，阻止用户使用 AI 功能，引导修复
3. 检查 `summary.warning_count`
   - 如果 > 0，在界面上显示警告提示
4. 根据 `missing_configs` 列表，逐项提供修复建议入口
