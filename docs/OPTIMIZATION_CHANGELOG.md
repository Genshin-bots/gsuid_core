# GsCore 优化变更日志

> 本文档记录 GsCore 框架的优化变更，涵盖多模态处理、Agent 协作、安全加固、性能优化和 Persona 增强。

---

## 目录

1. [多模态消息处理模块](#1-多模态消息处理模块)
2. [Agent 间通信与协作协议](#2-agent-间通信与协作协议)
3. [MCP 工具权限穿透修复](#3-mcp-工具权限穿透修复)
4. [ResourceManager TTL 机制](#4-resourcemanager-ttl-机制)
5. [History Token 上限精确控制](#5-history-token-上限精确控制)
6. [Persona 情绪状态机](#6-persona-情绪状态机)
7. [Persona 群聊适应性](#7-persona-群聊适应性)
8. [文件变更清单](#8-文件变更清单)

---

## 1. 多模态消息处理模块

**路径**: `gsuid_core/ai_core/multimodal/`

### 1.1 概述

框架对图片的处理路径是"图片 → `understand_image()` 转文字 → 传给 LLM"，但语音、视频、文档等多媒体类型完全缺失。新增 `multimodal/` 模块提供完整的多媒体消息处理闭环。

### 1.2 模块结构

```
multimodal/
├── __init__.py      # 模块导出
├── asr.py           # 语音转文字（ASR）
├── tts.py           # 文字转语音（TTS）
├── video.py         # 视频关键帧提取 + 多帧理解
└── document.py      # 文档内容提取管道（PDF/Word/Excel → Markdown）
```

### 1.3 各模块说明

#### asr.py — 语音转文字

- **接口**: `transcribe_audio(audio_data: bytes, audio_format: str = "ogg", language: str | None = None) -> str`
- **支持格式**: ogg, mp3, wav, m4a
- **实现**: 通过 MCP 工具统一接口，配置项 `asr_mcp_tool_id`
- **配置**: `ai_config` 中新增 `asr_provider`（默认 "MCP"）

#### tts.py — 文字转语音

- **接口**: `synthesize_speech(text: str, voice: str | None = None, speed: float = 1.0, output_format: str = "mp3") -> bytes`
- **返回**: 音频二进制数据，可直接通过 `bot.send()` 发送
- **实现**: 通过 MCP 工具统一接口，配置项 `tts_mcp_tool_id`
- **配置**: `ai_config` 中新增 `tts_provider`（默认 "MCP"）

#### video.py — 视频理解

- **接口**: `understand_video(video_data: bytes, video_format: str = "mp4", prompt: str | None = None, max_frames: int = 5) -> str`
- **两种方案**:
  1. 直接使用 MCP 视频理解工具（如果配置了 `video_understand_mcp_tool_id`）
  2. 提取关键帧 + 图片理解（fallback）
- **配置**: `ai_config` 中新增 `video_understand_provider`（默认 "MCP"）

#### document.py — 文档提取

- **接口**: `extract_document_content(file_data: bytes, filename: str, page_range: str | None = None) -> str`
- **支持格式**: PDF, Word (.doc/.docx), Excel (.xls/.xlsx), PPT (.ppt/.pptx), 纯文本 (.txt/.md/.csv/.json/.xml/.html)
- **纯文本文件**: 直接解码返回，不调用 MCP
- **配置**: `ai_config` 中新增 `document_extract_provider`（默认 "MCP"）

### 1.4 配置项

在 `ai_config.py` 的 `AI_CONFIG` 中新增：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `asr_provider` | str | "MCP" | 语音识别服务提供方 |
| `tts_provider` | str | "MCP" | 语音合成服务提供方 |
| `video_understand_provider` | str | "MCP" | 视频理解服务提供方 |
| `document_extract_provider` | str | "MCP" | 文档提取服务提供方 |

---

## 2. Agent 间通信与协作协议

**路径**: `gsuid_core/ai_core/agent_mesh/`

### 2.1 概述

原有 `create_subagent` 仅支持单向的父子关系，子 Agent 生命周期在单次请求内。新增 `agent_mesh/` 模块提供持久化 Agent、消息总线和多 Agent 任务协调能力。

### 2.2 模块结构

```
agent_mesh/
├── __init__.py          # 模块导出
├── models.py            # AgentTask / AgentMessage 数据模型
├── message_bus.py       # Agent 间异步消息总线
├── persistent_agent.py  # 持久化子 Agent
└── coordinator.py       # 多 Agent 任务协调器（DAG 依赖图执行）
```

### 2.3 与 create_subagent 的区别

| 维度 | `create_subagent` | `create_persistent_agent_tool` |
|------|-------------------|-------------------------------|
| 生命周期 | 单次请求内，执行完即销毁 | 跨请求持久运行，直到手动停止或空闲超时 |
| 使用场景 | "帮我查一下XX资料并总结" | "创建一个持续监控XX的助手" |
| 返回方式 | 同步等待结果返回给主 Agent | 异步执行，通过 `send_agent_task` 获取结果 |
| 内部 Prompt | 内置 Plan-and-Solve 强力 Prompt | 用户自定义或默认通用 Prompt |
| 资源消耗 | 轻量，用完即释放 | 持续占用内存和 Agent 实例 |

### 2.4 AI 工具

已注册为 `@ai_tools`，AI 可直接调用：

| 工具名 | category | 说明 |
|--------|----------|------|
| `create_persistent_agent_tool` | self | 创建持久化子 Agent |
| `send_agent_task_tool` | self | 向持久化 Agent 发送任务 |
| `list_agents_tool` | buildin | 列出所有活跃的持久化 Agent |
| `stop_agent_tool` | self | 停止指定的持久化 Agent |

### 2.5 生命周期管理

- 持久化 Agent 空闲超过 1 小时自动停止
- 框架关闭时通过 `@on_core_shutdown` 钩子自动停止所有 Agent
- Agent 状态通过消息总线可查询

---

## 3. MCP 工具权限穿透修复

**涉及文件**: `mcp/config_manager.py`, `mcp/startup.py`, `ai_core/models.py`, `webconsole/mcp_config_api.py`

### 3.1 问题

`register_as_ai_tools: true` 的 MCP 服务器工具被注册为 AI 工具后，任何触发 AI 的用户都能通过对话诱导 AI 调用敏感工具（如发送邮件、调用付费 API）。

### 3.2 解决方案

在 `MCPConfig` 中新增 `tool_permissions` 字段，直接使用 pm 权限等级（整数）：

```json
{
    "tool_permissions": {
        "send_email": 0,
        "query_data": 6
    }
}
```

权限等级与 `Event.user_pm` 直接对比（pm 值越小权限越高）：

| pm 值 | 含义 | 说明 |
|-------|------|------|
| `0` | master | 仅 master 用户（机器人主人） |
| `1` | superuser | superuser 及以上 |
| `2` | 群主 | 群主及以上 |
| `3` | 群管理员 | 群管理员及以上 |
| `4` | 频道管理员 | 频道管理员及以上 |
| `5` | 当前频道管理员 | 当前频道管理员及以上 |
| `6` | 普通用户 | 所有用户（默认值） |

对比逻辑：`ev.user_pm > required_pm` 时拒绝调用。

### 3.3 实现细节

- `MCPConfig.get_tool_required_pm(tool_name)` — 返回工具的 pm 等级（默认 6）
- `_build_mcp_check_func(config, tool_name)` — 自动生成权限检查函数
- MCP 工具注册时自动为每个工具生成 `check_func`
- 前端 API 已支持 `tool_permissions: Dict[str, int]` 字段的读写

### 3.4 前端集成

详见 [`docs/MCP_TOOL_PERMISSIONS.md`](MCP_TOOL_PERMISSIONS.md)

---

## 4. ResourceManager TTL 机制

**涉及文件**: `utils/resource_manager.py`

### 4.1 问题

`RM.register()` 生成的资源 ID 永久存储在内存中，不会被清理。高频触发器调用会导致内存泄漏。

### 4.2 解决方案

- `_store` 改为 `{resource_id: (data, created_at)}` 结构，记录创建时间
- 新增 `start_cleanup_loop()` / `stop_cleanup_loop()` 定期清理任务
- `_cleanup_expired()` 每 5 分钟检查一次，清理超过 30 分钟未使用的资源
- 通过 `@on_core_start(priority=10)` / `@on_core_shutdown(priority=10)` 钩子自动启停

### 4.3 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `_ttl_seconds` | 1800 (30分钟) | 资源存活时间 |
| `_cleanup_interval` | 300 (5分钟) | 清理检查间隔 |

---

## 5. History Token 上限精确控制

**涉及文件**: `ai_core/history/manager.py`

### 5.1 问题

`deque(maxlen=40)` 仅按消息条数截断，存在"隐形 Token 爆炸"风险。群聊中用户频繁发长文时，截断时机可能过晚。

### 5.2 解决方案

在 `HistoryManager.add_message()` 时，对消息内容做 Token 估算，维护滑动窗口 Token 总量上限。

### 5.3 Token 估算

使用 `_estimate_tokens()` 快速估算函数（不需要加载 tiktoken）：

- 中文字符：约 2 tokens/字符
- 英文单词：约 1.3 tokens/单词
- 其他字符：约 0.5 tokens/字符

### 5.4 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MAX_HISTORY_TOKENS` | 160000 | 每个 session 的 Token 总量上限 |

### 5.5 工作流程

1. 新消息加入时估算 Token 数
2. 更新 session 的 Token 总量
3. 如果超限，从最旧消息开始逐条删除直到回到限制内
4. 保证至少保留 1 条消息

---

## 6. Persona 情绪状态机

**涉及文件**: `ai_core/persona/mood.py`, `ai_core/persona/processor.py`, `ai_core/handle_ai.py`

### 6.1 概述

Persona 从静态的系统提示词升级为带有情绪状态的动态角色。情绪会随用户交互变化，并在构建 Prompt 时注入。

### 6.2 情绪类型

| 情绪 | 中文描述 | 触发事件 |
|------|----------|----------|
| `neutral` | 心情平静 | 默认/重置 |
| `happy` | 开心愉悦 | 被赞美 |
| `excited` | 兴奋激动 | 兴奋的事 |
| `warm` | 温暖亲切 | 友好问候 |
| `cold` | 冷淡疏远 | 被无视 |
| `concerned` | 关切担忧 | 坏消息 |
| `sad` | 难过失落 | 伤心事 |
| `annoyed` | 烦躁不满 | 争执 |

### 6.3 情绪衰减

情绪强度随时间自然衰减，半衰期为 30 分钟。衰减公式：

```
effective_intensity = intensity * 0.5^(elapsed / 1800)
```

当有效强度低于 0.1 时，视为中性。

### 6.4 情绪更新

在 `handle_ai.py` 中，AI 回复后异步调用 `_update_persona_mood()`，通过关键词匹配检测情绪事件：

- 赞美关键词 → `praise` → happy
- 争执关键词 → `argument` → annoyed
- 伤心关键词 → `sad_news` → sad
- 坏消息关键词 → `bad_news` → concerned
- 兴奋关键词 → `exciting` → excited
- 问候关键词 → `greeting` → warm
- 普通消息 → `neutral`（微弱衰减）

### 6.5 Prompt 注入

在 `processor.py` 的 `build_persona_prompt()` 中，如果存在情绪状态且强度足够，会注入：

```
【当前状态】略微心情不错，语气轻快愉悦
```

---

## 7. Persona 群聊适应性

**涉及文件**: `ai_core/persona/group_context.py`, `ai_core/persona/processor.py`, `ai_core/ai_router.py`

### 7.1 概述

同一个 Persona 在不同群聊中应有微妙的行为差异。通过获取群聊上下文信息注入 Persona Prompt 实现。

### 7.2 信息来源

1. **群聊名称**: 从 `CoreGroup.base_select_data()` 获取
2. **群聊画像摘要**: 从记忆系统 `AIMemHierarchicalGraphMeta.group_summary_cache` 获取（由分层图重建时 LLM 自动生成）

### 7.3 缓存机制

- 缓存有效期：10 分钟
- 缓存 key：group_id
- 缓存内容：群聊上下文描述文本

### 7.4 Prompt 注入

在 `processor.py` 的 `build_persona_prompt()` 中，如果存在群聊上下文，会注入：

```
【当前群聊环境】群名: XX技术群；群聊画像: 这是一个讨论 Python 和 AI 的技术群...
```

---

## 8. 文件变更清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `gsuid_core/ai_core/multimodal/__init__.py` | 多模态模块导出 |
| `gsuid_core/ai_core/multimodal/asr.py` | 语音转文字 |
| `gsuid_core/ai_core/multimodal/tts.py` | 文字转语音 |
| `gsuid_core/ai_core/multimodal/video.py` | 视频理解 |
| `gsuid_core/ai_core/multimodal/document.py` | 文档提取 |
| `gsuid_core/ai_core/agent_mesh/__init__.py` | Agent 协作模块导出 |
| `gsuid_core/ai_core/agent_mesh/models.py` | Agent 数据模型 |
| `gsuid_core/ai_core/agent_mesh/message_bus.py` | Agent 消息总线 |
| `gsuid_core/ai_core/agent_mesh/persistent_agent.py` | 持久化 Agent |
| `gsuid_core/ai_core/agent_mesh/coordinator.py` | DAG 任务协调器 |
| `gsuid_core/ai_core/persona/mood.py` | 情绪状态机 |
| `gsuid_core/ai_core/persona/group_context.py` | 群聊适应性 |
| `gsuid_core/ai_core/buildin_tools/agent_mesh_tools.py` | Agent Mesh AI 工具 |
| `docs/MCP_TOOL_PERMISSIONS.md` | MCP 权限前端文档 |
| `docs/OPTIMIZATION_CHANGELOG.md` | 本文档 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `gsuid_core/ai_core/configs/ai_config.py` | 新增 4 个多模态配置项 |
| `gsuid_core/ai_core/mcp/config_manager.py` | `MCPConfig` 新增 `tool_permissions` + `get_tool_required_pm()` |
| `gsuid_core/ai_core/mcp/startup.py` | `_build_mcp_check_func()` 自动生成权限检查 |
| `gsuid_core/ai_core/models.py` | `ToolBase` 新增 `check_func` 属性 |
| `gsuid_core/utils/resource_manager.py` | TTL 清理机制 |
| `gsuid_core/ai_core/history/manager.py` | Token 滑动窗口 |
| `gsuid_core/ai_core/persona/processor.py` | 注入情绪状态 + 群聊上下文 |
| `gsuid_core/ai_core/ai_router.py` | 获取群聊上下文注入 Persona |
| `gsuid_core/ai_core/handle_ai.py` | 情绪状态更新集成 |
| `gsuid_core/ai_core/buildin_tools/__init__.py` | 注册 Agent Mesh 工具 |
| `gsuid_core/ai_core/image_understand/understand.py` | 同步IO改为 aiofiles |
| `gsuid_core/webconsole/mcp_config_api.py` | API 支持 `tool_permissions` |
